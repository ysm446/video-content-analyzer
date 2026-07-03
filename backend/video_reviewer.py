import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ffmpeg
from PIL import Image

from . import cancel

from .model_catalog import (
    available_review_models as catalog_review_models,
    default_review_model_id,
    get_review_model_meta,
)
from .llama_server import LlamaServerManager, LLAMA_CPP_CTX
from .vram import MAX_PIXELS_PER_FRAME
from . import prompts

LLAMA_CPP_VISION_PORT = int(os.environ.get("LLAMA_CPP_VISION_PORT", "8767"))

# シーン検出の閾値（0.0〜1.0、低いほど敏感）
SCENE_THRESHOLD = 0.35

ANALYZE_SYSTEM = (
    "/no_think\n"
    "あなたは動画分析の専門家です。"
    "提供されたフレーム画像と、"
    "必要に応じて音声の書き起こしテキストを総合して動画の内容を分析します。"
)

_ANALYZE_JSON_FORMAT = (
    "{\n"
    '  "summary": "動画全体の概要（1〜2文）",\n'
    '  "detail": "動画の内容のまとめ（何が起きるか・要点を複数文または箇条書きで詳しく）",\n'
    '  "scenes": [\n'
    '    {"timestamp": "0:00", "label": "場面のタイトル", "description": "1文で短く説明"},\n'
    '    {"timestamp": "1:30", "label": "場面のタイトル", "description": "1文で短く説明"}\n'
    "    ... （場面転換ごとに繰り返す）\n"
    "  ],\n"
    '  "tags": ["タグ1", "タグ2", "タグ3"],\n'
    '  "genre": "ジャンル（例：アクション映画、料理動画、講義、スポーツなど）"\n'
    "}"
)

_ANALYZE_INSTR_VISUAL = (
    "この動画からサンプリングされたフレームを分析し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は映像の内容・雰囲気・場所・被写体が変化するたびに新しい場面として追加してください。"
    "提供されたフレーム数を参考に場面分けしてください（目安: 6〜8場面）。"
    "label には場面の内容を表す短いタイトルを付けてください。"
    "description は各場面につき1文だけ、短く書いてください。"
    "summary は1〜2文の概要、detail は動画全体の内容のまとめを概要より詳しく"
    "（要点ごとに複数文または箇条書きで）書いてください。"
)

_ANALYZE_INSTR_AUDIO = (
    "この動画からサンプリングされたフレームと、以下の音声書き起こしを総合して分析し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は映像や音声の内容・雰囲気が変わるたびに新しい場面として追加してください（目安: 6〜8場面）。"
    "label には場面の内容を表す短いタイトルを付けてください。"
    "description は各場面につき1文だけ、短く書いてください。"
    "summary は1〜2文の概要、detail は映像と音声を総合した内容のまとめを概要より詳しく"
    "（要点ごとに複数文または箇条書きで）書いてください。"
)

# 構造化出力（llama-server の response_format → GBNF 制約）用スキーマ。
# プロンプトの JSON フォーマット例と同じキー順にすること（生成順が揃い品質が安定する）。
_SCENE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "timestamp": {"type": "string"},
        "label": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["timestamp", "label", "description"],
}

_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "detail": {"type": "string"},
        "scenes": {"type": "array", "items": _SCENE_ITEM_SCHEMA},
        "tags": {"type": "array", "items": {"type": "string"}},
        "genre": {"type": "string"},
    },
    "required": ["summary", "detail", "scenes", "tags", "genre"],
}

_ANALYZE_SCHEMA_SCENES = {
    "type": "object",
    "properties": {
        "scenes": {"type": "array", "items": _SCENE_ITEM_SCHEMA},
    },
    "required": ["scenes"],
}


def _analysis_response_format(scenes_only: bool) -> dict:
    schema = _ANALYZE_SCHEMA_SCENES if scenes_only else _ANALYZE_SCHEMA
    return {
        "type": "json_schema",
        "json_schema": {"name": "video_analysis", "schema": schema},
    }


# refine パス用: summary/tags/genre は不要で scenes のみ欲しい場合に使う
_ANALYZE_JSON_FORMAT_SCENES = (
    "{\n"
    '  "scenes": [\n'
    '    {"timestamp": "0:00", "label": "場面のタイトル", "description": "1文で短く説明"}\n'
    "    ... （場面転換ごとに繰り返す）\n"
    "  ]\n"
    "}"
)

_ANALYZE_INSTR_SCENES = (
    "指定区間のフレーム（と、あれば音声書き起こし）から場面転換を抽出し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は内容・雰囲気・場所・被写体が変化するたびに新しい場面として追加してください。"
    "label には短いタイトルを、description は各場面1文で書いてください。"
)

QA_SYSTEM = (
    "/no_think\n"
    "あなたは動画分析の専門家です。"
    "提供されたフレーム画像と、必要に応じて音声書き起こしに基づいて質問に日本語で答えてください。"
    "具体的に回答してください。"
)

_TRANSCRIPT_MAX_CHARS = 3000
QA_MAX_NEW_TOKENS = 2048

# コンテキスト予算の見積もり用定数
_CTX_MARGIN_TOKENS = 512     # チャットテンプレート・画像マーカー等の余裕分
_MIN_TOKENS_PER_FRAME = 64   # これ未満に解像度を落とすくらいなら枚数を間引く


def parse_timestamp_seconds(value: str | None) -> float | None:
    """"h:mm:ss(.f)" / "m:ss(.f)" 形式のタイムスタンプを秒に変換する。解釈できなければ None。

    モデル出力は "75:30"（分が2桁超）と "1:15:30"（時刻正規化）の両方がありうるため、
    どちらも受け付ける。server.py 側の TOC 構築とここでの dedup が同じ解釈をするよう、
    この関数に一本化している。
    """
    if not value:
        return None
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{2})(?:\.(\d+))?\s*$", str(value))
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    frac = float(f"0.{m.group(4)}") if m.group(4) else 0.0
    return h * 3600 + mm * 60 + ss + frac


def available_review_models() -> list[dict]:
    return catalog_review_models()


def get_prompts() -> list[dict]:
    """設定画面での閲覧用にこのモジュールのプロンプトを返す。
    system 系（ペルソナ）と、分析の指示・出力フォーマット系を区別して返す。"""
    return [
        {"key": "analyze", "label": "動画分析（system）", "category": "動画分析", "default": ANALYZE_SYSTEM},
        {"key": "qa", "label": "Q&A（system）", "category": "動画分析", "default": QA_SYSTEM},
        {"key": "analyze_instr_visual", "label": "分析指示（映像のみ）", "category": "動画分析（出力指示）", "default": _ANALYZE_INSTR_VISUAL},
        {"key": "analyze_instr_audio", "label": "分析指示（映像＋音声）", "category": "動画分析（出力指示）", "default": _ANALYZE_INSTR_AUDIO},
        {"key": "analyze_instr_scenes", "label": "分析指示（refine: scenesのみ）", "category": "動画分析（出力指示）", "default": _ANALYZE_INSTR_SCENES},
        {"key": "analyze_json_format", "label": "分析JSONフォーマット", "category": "動画分析（出力指示）", "default": _ANALYZE_JSON_FORMAT},
        {"key": "analyze_json_format_scenes", "label": "分析JSONフォーマット（scenesのみ）", "category": "動画分析（出力指示）", "default": _ANALYZE_JSON_FORMAT_SCENES},
    ]


_vision_server = LlamaServerManager(
    port=LLAMA_CPP_VISION_PORT,
    meta_resolver=get_review_model_meta,
    label="動画レビュー用",
)


class VideoReviewer:
    def __init__(self):
        self.model_id = default_review_model_id() or ""
        self.model = None
        self.processor = None
        self._frame_cache: tuple | None = None

    @property
    def loaded(self) -> bool:
        return _vision_server.loaded_for(self.model_id)

    def set_model_id(self, model_id: str):
        if self.model_id != model_id:
            self.unload()
            self.model_id = model_id
            print(f"[VideoReviewer] モデルを {model_id} に変更（次回使用時にロード）")

    def load(self):
        _vision_server.acquire_model(self.model_id, "video_reviewer")
        self.model = {"backend": "llama.cpp", "model_id": self.model_id}
        self.processor = {"backend": "llama.cpp"}

    def unload(self):
        _vision_server.release_client("video_reviewer")
        self.model = None
        self.processor = None
        print("[VideoReviewer] モデルをアンロードしました")

    def _make_cache_key(self, video_path: str, frame_mode: str, max_frames: int, min_interval: float) -> tuple:
        p = Path(video_path)
        stat = p.stat()
        return (
            str(p.resolve()),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            frame_mode,
            int(max_frames),
            round(float(min_interval), 3),
        )

    def cache_frames(self, video_path: str, frame_mode: str, max_frames: int, min_interval: float, frames: list, meta: dict):
        key = self._make_cache_key(video_path, frame_mode, max_frames, min_interval)
        self._frame_cache = (key, frames, meta)
        print(f"[VideoReviewer] フレームキャッシュ保存: {len(frames)}枚 ({frame_mode})")

    def get_cached_frames(self, video_path: str, frame_mode: str, max_frames: int, min_interval: float) -> tuple[list, dict] | None:
        key = self._make_cache_key(video_path, frame_mode, max_frames, min_interval)
        if self._frame_cache is not None:
            mem_key, frames, meta = self._frame_cache
            if mem_key == key:
                print(f"[VideoReviewer] フレームキャッシュヒット: {len(frames)}枚 ({frame_mode})")
                return frames, meta
        return None

    def _ensure_loaded(self):
        if not self.loaded:
            self.load()

    @staticmethod
    def _clean_generated_text(text: str) -> str:
        cleaned = text.strip()
        if "</think>" in cleaned:
            cleaned = cleaned.split("</think>", 1)[-1].strip()
        return cleaned

    @staticmethod
    def _clean_stream_prefix(text: str) -> str:
        cleaned = text
        if "</think>" in cleaned:
            cleaned = cleaned.split("</think>", 1)[-1]
        cleaned = re.sub(r"^\s*</?think>\s*", "", cleaned)
        return cleaned

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"\n?```$", "", cleaned.strip(), flags=re.MULTILINE)
        return cleaned.strip()

    @staticmethod
    def _extract_balanced_json(text: str) -> str | None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:idx + 1]
        return None

    @staticmethod
    def _decode_json_string(value: str) -> str:
        try:
            return json.loads(f'"{value}"')
        except Exception:
            return value

    @classmethod
    def _salvage_analysis_fields(cls, raw: str) -> dict:
        summary = ""
        detail = ""
        genre = ""
        tags: list[str] = []

        if m := re.search(r'"summary"\s*:\s*"((?:\\.|[^"\\])*)"', raw, flags=re.S):
            summary = cls._decode_json_string(m.group(1)).strip()
        if m := re.search(r'"detail"\s*:\s*"((?:\\.|[^"\\])*)"', raw, flags=re.S):
            detail = cls._decode_json_string(m.group(1)).strip()
        if m := re.search(r'"genre"\s*:\s*"((?:\\.|[^"\\])*)"', raw, flags=re.S):
            genre = cls._decode_json_string(m.group(1)).strip()
        if m := re.search(r'"tags"\s*:\s*\[(.*?)\]', raw, flags=re.S):
            tags = [
                cls._decode_json_string(x)
                for x in re.findall(r'"((?:\\.|[^"\\])*)"', m.group(1))
            ]

        return {
            "summary": summary or "分析結果のJSONを最後まで生成できませんでした。",
            "detail": detail,
            "scenes": [],
            "tags": tags,
            "genre": genre or "不明",
        }

    def _frame_to_data_url(self, frame: Image.Image, max_pixels: int | None = None) -> str:
        pixel_limit = int(max_pixels or MAX_PIXELS_PER_FRAME or 200704)
        max_side = max(32, int(pixel_limit ** 0.5))
        image = frame.copy().convert("RGB")
        image.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=88)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _even_indices(total: int, n: int) -> list[int]:
        """0..total-1 から n 個をなるべく等間隔に選ぶ（先頭・末尾を含む）。"""
        if n >= total:
            return list(range(total))
        if n <= 1:
            return [0]
        step = (total - 1) / (n - 1)
        return sorted({min(round(i * step), total - 1) for i in range(n)})

    @staticmethod
    def _fit_frame_budget(n_frames: int, text_chars: int, max_new_tokens: int) -> tuple[int, int]:
        """コンテキスト(-c)に収まるよう (使用フレーム数, 1枚あたり視覚トークン数) を決める。

        テキストは日本語を想定して 1文字≒1トークンの保守的見積もり。
        解像度は VRAM 制限（MAX_PIXELS_PER_FRAME）を上限とし、超過時は
        まず解像度を _MIN_TOKENS_PER_FRAME まで下げ、それでも足りなければ枚数を間引く。
        """
        default_tpf = max(1, int(MAX_PIXELS_PER_FRAME or 200704) // (28 * 28))
        if n_frames <= 0:
            return 0, default_tpf
        avail = LLAMA_CPP_CTX - max_new_tokens - text_chars - _CTX_MARGIN_TOKENS
        if avail < _MIN_TOKENS_PER_FRAME:
            return 1, _MIN_TOKENS_PER_FRAME
        if n_frames * default_tpf <= avail:
            return n_frames, default_tpf
        tpf = max(_MIN_TOKENS_PER_FRAME, avail // n_frames)
        n_use = min(n_frames, max(1, avail // tpf))
        return n_use, tpf

    def _prepare_budgeted_messages(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int, timestamps: list[float] | None) -> tuple[list[dict], list[Image.Image], list[float] | None, dict]:
        """ctx 予算に収まるようフレームを間引き・縮小して messages を構築する。

        戻り値: (messages, 使用フレーム, 使用タイムスタンプ, 予算情報)
        """
        default_tpf = max(1, int(MAX_PIXELS_PER_FRAME or 200704) // (28 * 28))
        n_use, tpf = self._fit_frame_budget(len(frames), len(system) + len(prompt), max_new_tokens)
        use_frames = frames
        use_ts = timestamps
        if n_use < len(frames):
            idx = self._even_indices(len(frames), n_use)
            use_frames = [frames[i] for i in idx]
            if timestamps and len(timestamps) == len(frames):
                use_ts = [timestamps[i] for i in idx]
            print(f"[VideoReviewer] コンテキスト予算によりフレームを間引き: {len(frames)}→{len(use_frames)}枚 ({tpf}トークン/枚)")
        elif tpf < default_tpf:
            print(f"[VideoReviewer] コンテキスト予算により解像度を削減: {tpf}トークン/枚")
        messages = self._build_messages(use_frames, system, prompt, use_ts, max_pixels=tpf * 28 * 28)
        info = {
            "frames_used": len(use_frames),
            "tokens_per_frame": tpf,
            "reduced_reason": "budget" if (len(use_frames) < len(frames) or tpf < default_tpf) else None,
        }
        return messages, use_frames, use_ts, info

    def _build_messages(self, frames: list[Image.Image], system: str, prompt: str, timestamps: list[float] | None = None, max_pixels: int | None = None) -> list[dict]:
        content: list[dict] = []
        if timestamps and len(timestamps) == len(frames):
            for frame, ts in zip(frames, timestamps):
                content.append({"type": "image_url", "image_url": {"url": self._frame_to_data_url(frame, max_pixels=max_pixels)}})
                content.append({"type": "text", "text": f"[{self._fmt_ts(ts)}]"})
        else:
            for frame in frames:
                content.append({"type": "image_url", "image_url": {"url": self._frame_to_data_url(frame, max_pixels=max_pixels)}})
        content.append({"type": "text", "text": prompt})
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    def _infer(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int, timestamps: list[float] | None = None, response_format: dict | None = None) -> tuple[str, dict]:
        """推論して (テキスト, 生成メタ) を返す。

        メタは {"usage", "finish_reason", "elapsed_seconds", "frames_used"}。
        finish_reason=="length" は max_tokens 打ち切りを意味する。
        """
        print(f"[VideoReviewer] 推論開始: フレーム={len(frames)}枚")
        t0 = time.time()
        messages, use_frames, use_ts, budget_info = self._prepare_budgeted_messages(
            frames, system, prompt, max_new_tokens, timestamps
        )
        try:
            result, gen_meta = _vision_server.chat_with_meta(self.model_id, messages, max_new_tokens, response_format)
        except RuntimeError as exc:
            msg = str(exc)
            if "failed to process image" not in msg:
                raise
            retry_pixels = min(int(MAX_PIXELS_PER_FRAME or 200704), 128 * 28 * 28)
            retry_frames = use_frames[: min(len(use_frames), 12)]
            retry_timestamps = use_ts[:len(retry_frames)] if use_ts else None
            print(
                f"[VideoReviewer] 画像処理エラーのため縮小再試行: "
                f"frames={len(retry_frames)}/{len(use_frames)}, max_pixels={retry_pixels}"
            )
            messages = self._build_messages(
                retry_frames,
                system,
                prompt,
                retry_timestamps,
                max_pixels=retry_pixels,
            )
            result, gen_meta = _vision_server.chat_with_meta(self.model_id, messages, max_new_tokens, response_format)
            budget_info = {
                "frames_used": len(retry_frames),
                "tokens_per_frame": retry_pixels // (28 * 28),
                "reduced_reason": "image_error",
            }
        elapsed = time.time() - t0
        print(f"[VideoReviewer] 推論完了: {elapsed:.1f}秒")
        gen_meta = dict(gen_meta or {})
        gen_meta.update(budget_info)
        gen_meta["elapsed_seconds"] = round(elapsed, 2)
        gen_meta["frames_requested"] = len(frames)
        return self._clean_generated_text(result), gen_meta

    def _infer_stream_with_meta(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int, on_delta, timestamps: list[float] | None = None) -> dict:
        print(f"[VideoReviewer] ストリーミング推論開始: フレーム={len(frames)}枚")
        t0 = time.time()
        prefix_buffer = ""
        content_started = False
        meta: dict = {}

        def forward_stream(stream_iter) -> None:
            nonlocal prefix_buffer, content_started, meta
            while True:
                try:
                    delta = next(stream_iter)
                except StopIteration as stop:
                    meta = stop.value or {}
                    return
                if not delta:
                    continue
                if not content_started:
                    prefix_buffer += delta
                    cleaned = self._clean_stream_prefix(prefix_buffer)
                    if not cleaned:
                        continue
                    content_started = True
                    on_delta(cleaned)
                    prefix_buffer = ""
                else:
                    on_delta(delta.replace("<think>", "").replace("</think>", ""))

        messages, use_frames, use_ts, budget_info = self._prepare_budgeted_messages(
            frames, system, prompt, max_new_tokens, timestamps
        )
        try:
            try:
                stream_iter = _vision_server.stream_chat_with_meta(self.model_id, messages, max_new_tokens)
                forward_stream(stream_iter)
            except RuntimeError as exc:
                msg = str(exc)
                if "failed to process image" not in msg:
                    raise
                retry_pixels = min(int(MAX_PIXELS_PER_FRAME or 200704), 128 * 28 * 28)
                retry_frames = use_frames[: min(len(use_frames), 12)]
                retry_timestamps = use_ts[:len(retry_frames)] if use_ts else None
                print(
                    f"[VideoReviewer] ストリーミング画像処理エラーのため縮小再試行: "
                    f"frames={len(retry_frames)}/{len(use_frames)}, max_pixels={retry_pixels}"
                )
                prefix_buffer = ""
                content_started = False
                messages = self._build_messages(
                    retry_frames,
                    system,
                    prompt,
                    retry_timestamps,
                    max_pixels=retry_pixels,
                )
                stream_iter = _vision_server.stream_chat_with_meta(self.model_id, messages, max_new_tokens)
                forward_stream(stream_iter)
                budget_info = {
                    "frames_used": len(retry_frames),
                    "tokens_per_frame": retry_pixels // (28 * 28),
                    "reduced_reason": "image_error",
                }
        finally:
            elapsed = time.time() - t0
            print(f"[VideoReviewer] ストリーミング推論完了: {elapsed:.1f}秒")
        meta.update(budget_info)
        meta["frames_requested"] = len(frames)
        meta["elapsed_seconds"] = elapsed
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
        try:
            completion_tokens_num = float(completion_tokens)
        except (TypeError, ValueError):
            completion_tokens_num = 0.0
        if completion_tokens_num > 0 and elapsed > 0:
            meta["tokens_per_sec"] = completion_tokens_num / elapsed
        return meta

    @staticmethod
    def _run_ffmpeg_extract(stream) -> None:
        """フレーム抽出の ffmpeg を実行する。失敗時は stderr 末尾を含めて報告する。"""
        try:
            stream.run(quiet=True)
        except ffmpeg.Error as e:
            stderr = (e.stderr or b"").decode("utf-8", errors="replace")
            tail = " / ".join(stderr.strip().splitlines()[-3:])
            raise RuntimeError(f"ffmpeg フレーム抽出に失敗: {tail}") from e

    def _get_duration(self, video_path: str) -> float:
        probe = ffmpeg.probe(video_path)
        fmt = probe.get("format") or {}
        if fmt.get("duration") is not None:
            return float(fmt["duration"])
        # format に duration が無いコンテナ向けフォールバック（最長ストリーム長）
        durations = [
            float(s["duration"])
            for s in (probe.get("streams") or [])
            if s.get("duration") is not None
        ]
        return max(durations) if durations else 0.0

    def extract_frames_scene(self, video_path: str, max_frames: int, threshold: float = SCENE_THRESHOLD) -> tuple[list[Image.Image], dict]:
        duration = self._get_duration(video_path)
        frames: list[Image.Image] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            outpattern = str(Path(tmpdir) / "frame_%04d.jpg")
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", f"select=eq(n\\,0)+gt(scene\\,{threshold}),showinfo",
                "-vsync", "vfr",
                "-vcodec", "mjpeg", "-q:v", "2",
                outpattern, "-y",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                tail = "\n".join(result.stderr.strip().splitlines()[-3:])
                print(f"[VideoReviewer] シーン検出 ffmpeg が異常終了 (code={result.returncode}) → 均等サンプリングにフォールバック\n{tail}")
                return self.extract_frames(video_path, max_frames, 5.0)
            ts_pattern = re.compile(r"\bpts_time:(\d+\.?\d*)")
            raw_ts = [
                float(m.group(1))
                for line in result.stderr.splitlines()
                if (m := ts_pattern.search(line))
            ]
            frame_files = sorted(f for f in os.listdir(tmpdir) if f.startswith("frame_") and f.endswith(".jpg"))
            if len(raw_ts) > len(frame_files):
                raw_ts = raw_ts[:len(frame_files)]
            elif len(raw_ts) < len(frame_files):
                step = duration / max(len(frame_files) - 1, 1)
                raw_ts += [step * i for i in range(len(raw_ts), len(frame_files))]
            if len(frame_files) > max_frames:
                if max_frames <= 1:
                    indices = [0]
                else:
                    step = (len(frame_files) - 1) / (max_frames - 1)
                    indices = sorted(set([0] + [min(round(i * step), len(frame_files) - 1) for i in range(1, max_frames)]))
                frame_files = [frame_files[i] for i in indices]
                raw_ts = [raw_ts[i] for i in indices]
            for fname in frame_files:
                frames.append(Image.open(str(Path(tmpdir) / fname)).copy())
            timestamps = raw_ts[:len(frames)]

        if len(frames) <= 1:
            print("[VideoReviewer] シーン検出フレームなし → 均等サンプリングにフォールバック")
            return self.extract_frames(video_path, max_frames, 5.0)

        m_d, s_d = divmod(int(duration), 60)
        print(f"[VideoReviewer] シーン検出フレーム抽出完了: {len(frames)}枚 (動画 {m_d}分{s_d}秒 / 閾値 {threshold} / 最大 {max_frames}枚指定)")
        return frames, {
            "count": len(frames),
            "interval": None,
            "duration": duration,
            "timestamps": timestamps,
            "mode": "scene",
        }

    @staticmethod
    def _grab_frame_at(video_path: str, ts: float) -> Image.Image | None:
        """指定時刻へ入力シークして1フレームだけ取り出す（全編デコードしない）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = str(Path(tmpdir) / "f.jpg")
            cmd = [
                "ffmpeg", "-ss", f"{max(0.0, ts):.3f}", "-i", video_path,
                "-frames:v", "1", "-q:v", "2", out, "-y",
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0 or not Path(out).exists():
                return None
            return Image.open(out).copy()

    def _extract_frames_at(self, video_path: str, timestamps: list[float]) -> tuple[list[Image.Image], list[float]]:
        """複数時刻のフレームを並列シークで取り出す。取得できた (frames, timestamps) を返す。"""
        def grab(ts: float) -> Image.Image | None:
            if cancel.is_canceled():
                return None
            return self._grab_frame_at(video_path, ts)

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(grab, timestamps))
        cancel.raise_if_canceled()
        frames: list[Image.Image] = []
        ok_ts: list[float] = []
        for ts, frame in zip(timestamps, results):
            if frame is not None:
                frames.append(frame)
                ok_ts.append(ts)
        return frames, ok_ts

    def extract_frames(self, video_path: str, max_frames: int, min_interval: float) -> tuple[list[Image.Image], dict]:
        """均等間隔サンプリング。

        以前は fps フィルタで全編をデコードしていたが、長い動画では抽出だけで
        数十秒かかるため、各時刻への入力シーク（-ss）×並列実行に変更した。
        """
        duration = self._get_duration(video_path)
        interval = max(duration / max(max_frames, 1), min_interval)
        count = max(1, min(int(max_frames), int(duration / interval) + 1))
        want_ts: list[float] = []
        for i in range(count):
            ts = round(min(i * interval, max(duration - 0.1, 0.0)), 1)
            if not want_ts or ts > want_ts[-1]:
                want_ts.append(ts)

        frames, timestamps = self._extract_frames_at(video_path, want_ts)
        if not frames:
            raise RuntimeError("ffmpeg フレーム抽出に失敗しました（フレームを1枚も取得できません）")

        meta = {"count": len(frames), "interval": interval, "duration": duration, "timestamps": timestamps, "mode": "uniform"}
        m, s = divmod(int(duration), 60)
        print(f"[VideoReviewer] フレーム抽出完了: {len(frames)}枚 (動画 {m}分{s}秒 / 間隔 {interval:.1f}秒 / 最大 {max_frames}枚指定 / 並列シーク)")
        return frames, meta

    def load_frames_from_analysis_cache(self, video_path: str) -> tuple[list[Image.Image], dict] | None:
        """分析キャッシュ（data.json のシーン＋サムネール）から QA 用フレームを復元する。

        分析済みの動画なら ffmpeg での再抽出なしに即チャットを開始できる。
        シーンサムネールは場面転換ごとの代表フレームなので、均等サンプリングより
        むしろ内容の網羅性が高い。シーンが少なすぎる（4枚未満）場合は None。
        """
        p = Path(video_path)
        cache_dir = p.parent / (p.stem + ".cache")
        data_file = cache_dir / "data.json"
        if not data_file.exists():
            return None
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        scenes = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(scenes, list):
            return None
        frames: list[Image.Image] = []
        timestamps: list[float] = []
        cache_resolved = cache_dir.resolve()
        for s in sorted(scenes, key=lambda x: float(x.get("start_sec") or 0.0) if isinstance(x, dict) else 0.0):
            if not isinstance(s, dict):
                continue
            thumb = s.get("thumbnail")
            start = s.get("start_sec")
            if not thumb or not isinstance(start, (int, float)):
                continue
            tp = cache_dir / str(thumb)
            try:
                if not tp.resolve().is_relative_to(cache_resolved):
                    continue  # キャッシュフォルダ外への参照は無視
                if not tp.exists():
                    continue
                frames.append(Image.open(tp).convert("RGB").copy())
            except Exception:
                continue
            timestamps.append(float(start))
        if len(frames) < 4:
            return None
        meta = {
            "count": len(frames),
            "interval": None,
            "duration": float(data.get("duration") or 0.0),
            "timestamps": timestamps,
            "mode": "analysis-cache",
        }
        print(f"[VideoReviewer] 分析キャッシュのサムネール {len(frames)}枚 を QA フレームに再利用")
        return frames, meta

    def extract_frames_between(self, video_path: str, start_sec: float, end_sec: float, max_frames: int, min_interval: float) -> tuple[list[Image.Image], dict]:
        start = max(0.0, float(start_sec))
        end = max(start + 0.1, float(end_sec))
        duration = end - start
        interval = max(duration / max(max_frames, 1), min_interval)

        frames: list[Image.Image] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            outpattern = str(Path(tmpdir) / "frame_%04d.jpg")
            self._run_ffmpeg_extract(
                ffmpeg.input(video_path, ss=start, to=end)
                .filter("fps", fps=f"1/{interval:.4f}")
                .output(outpattern, format="image2", vcodec="mjpeg", q=2)
                .overwrite_output()
            )
            for fname in sorted(os.listdir(tmpdir)):
                if fname.startswith("frame_") and fname.endswith(".jpg"):
                    frames.append(Image.open(str(Path(tmpdir) / fname)).copy())

        timestamps = [round(start + i * interval, 1) for i in range(len(frames))]
        meta = {
            "count": len(frames),
            "interval": interval,
            "duration": duration,
            "timestamps": timestamps,
            "mode": "uniform-range",
            "start_sec": start,
            "end_sec": end,
        }
        print(f"[VideoReviewer] 区間フレーム抽出: {len(frames)}枚 ({self._fmt_ts(start)}-{self._fmt_ts(end)} / 間隔 {interval:.1f}秒)")
        return frames, meta

    @staticmethod
    def _truncate_transcript(transcript: str) -> str:
        if len(transcript) <= _TRANSCRIPT_MAX_CHARS:
            return transcript
        return transcript[:_TRANSCRIPT_MAX_CHARS] + "…（以下省略）"

    @staticmethod
    def _pick_lines(lines: list[str], indices: list[int], max_chars: int) -> str:
        """行 index を時系列（=元の順序）で並べ、予算内で連結する。"""
        ordered = sorted(set(indices))
        picked: list[str] = []
        total = 0
        for i in ordered:
            add = len(lines[i]) + 1
            if total + add > max_chars:
                break
            picked.append(lines[i])
            total += add
        return "\n".join(picked)

    @staticmethod
    def _timestamped_rows(lines: list[str]) -> list[tuple[int, str]]:
        """`[m:ss] テキスト` 形式の行を (行index, 行テキスト) で返す。"""
        return [
            (idx, line)
            for idx, line in enumerate(lines)
            if re.match(r"^\[(\d+):(\d{2})\]\s*", line.strip())
        ]

    @staticmethod
    def _sample_transcript_uniform(transcript: str, max_chars: int = _TRANSCRIPT_MAX_CHARS) -> str:
        """transcript を全編から時間等間隔に予算内でサンプリングする（分析用）。

        先頭切り捨てだと長編動画で冒頭数分の音声しか分析に反映されないため、
        `[m:ss]` 行を等間隔に拾って動画全体をカバーする。
        タイムスタンプ行が無い形式は従来どおり先頭切り出しにフォールバック。
        """
        if not transcript or len(transcript) <= max_chars:
            return transcript
        lines = transcript.splitlines()
        rows = VideoReviewer._timestamped_rows(lines)
        if not rows:
            return VideoReviewer._truncate_transcript(transcript)
        n = len(rows)
        approx_line = sum(len(line) for _i, line in rows) / max(1, n)
        budget_lines = max(1, int(max_chars / max(1.0, approx_line + 1)))
        if budget_lines >= n:
            return VideoReviewer._pick_lines(lines, [i for i, _l in rows], max_chars)
        step = n / budget_lines
        sampled = [rows[int(k * step)][0] for k in range(budget_lines)]
        return VideoReviewer._pick_lines(lines, sampled, max_chars)

    @staticmethod
    def _tokenize_query(question: str) -> list[str]:
        """質問を検索語に分解する（形態素解析なし）。

        - 英数字は単語単位（小文字化）
        - CJK は連続文字列から 2-gram（1文字のみの場合はその文字）を生成
        """
        terms: list[str] = []
        terms += [w for w in re.findall(r"[A-Za-z0-9]+", question.lower()) if len(w) >= 2]
        for run in re.findall(r"[぀-ヿ㐀-䶿一-鿿]+", question):
            if len(run) == 1:
                terms.append(run)
            else:
                terms += [run[i:i + 2] for i in range(len(run) - 1)]
        # 重複除去（順序保持）
        seen: set[str] = set()
        uniq: list[str] = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    @staticmethod
    def _select_relevant_transcript(transcript: str, question: str, max_chars: int = _TRANSCRIPT_MAX_CHARS) -> str:
        """質問に関連する字幕行を予算内で抽出する（embedding なしのキーワード検索）。

        - 全文が予算内ならそのまま返す
        - `[m:ss] テキスト` 形式の行をキーワード一致でスコア付けし、高スコア行＋前後1行を
          時系列順に予算内で収集
        - 一致がゼロなら全編から等間隔サンプリング（先頭偏重を避ける）
        """
        if not transcript or len(transcript) <= max_chars:
            return transcript

        lines = transcript.splitlines()
        rows = VideoReviewer._timestamped_rows(lines)
        # タイムスタンプ行が無い形式は従来の先頭切り出しにフォールバック
        if not rows:
            return VideoReviewer._truncate_transcript(transcript)

        terms = VideoReviewer._tokenize_query(question)

        def _pick(indices: list[int]) -> str:
            return VideoReviewer._pick_lines(lines, indices, max_chars)

        if terms:
            scored: list[tuple[int, int]] = []  # (score, 行index)
            for idx, line in rows:
                low = line.lower()
                score = sum(low.count(t) for t in terms)
                if score > 0:
                    scored.append((score, idx))
            if scored:
                # 高スコア順に、前後1行の文脈を含めて予算まで集める
                scored.sort(key=lambda x: (-x[0], x[1]))
                chosen: set[int] = set()
                total = 0
                for _score, idx in scored:
                    window = [j for j in (idx - 1, idx, idx + 1) if 0 <= j < len(lines)]
                    add = sum(len(lines[j]) + 1 for j in window if j not in chosen)
                    if total + add > max_chars and chosen:
                        break
                    for j in window:
                        if j not in chosen:
                            chosen.add(j)
                            total += len(lines[j]) + 1
                return _pick(list(chosen))

        # 一致なし: 全編から等間隔サンプリング
        return VideoReviewer._sample_transcript_uniform(transcript, max_chars)

    @staticmethod
    def _fmt_ts(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _parse_ts(ts: str) -> float:
        sec = parse_timestamp_seconds(ts)
        return sec if sec is not None else 0.0

    @staticmethod
    def _dedup_scenes(scenes: list) -> list:
        if not scenes:
            return scenes
        parsed = [(VideoReviewer._parse_ts(s.get("timestamp", "")), s) for s in scenes]
        parsed.sort(key=lambda x: x[0])
        result: list = []
        seen: set[int] = set()
        for ts_sec, s in parsed:
            key = int(ts_sec)
            if key not in seen:
                seen.add(key)
                result.append(s)
        return result

    @staticmethod
    def _ts_hint(timestamps: list[float]) -> str:
        if not timestamps:
            return ""
        return (
            "\n\n各フレーム画像の直後に [m:ss] 形式でタイムスタンプが付いています。"
            "scenes の timestamp にはその場面が始まるフレームのタイムスタンプを使用してください。"
            "\n実際に映像が変化した場面のみを記録してください。内容が変わらない場合は場面を増やさないでください。"
        )

    @staticmethod
    def _build_analyze_prompt(transcript: str, timestamps: list[float] = [], output_lang: str = "ja", scenes_only: bool = False) -> str:
        fields = (
            "scenes[].label / scenes[].description"
            if scenes_only
            else "summary / scenes[].label / scenes[].description / tags / genre"
        )
        lang_word = "日本語" if output_lang == "ja" else "英語"
        lang_instr = f"{fields} は{lang_word}で記述してください。"
        ts_hint = VideoReviewer._ts_hint(timestamps)
        json_format = _ANALYZE_JSON_FORMAT_SCENES if scenes_only else _ANALYZE_JSON_FORMAT
        instr_visual = _ANALYZE_INSTR_SCENES if scenes_only else _ANALYZE_INSTR_VISUAL
        instr_audio = _ANALYZE_INSTR_SCENES if scenes_only else _ANALYZE_INSTR_AUDIO
        if not transcript:
            return instr_visual + ts_hint + "\n" + lang_instr + "\n\n" + json_format
        # 先頭切り捨てではなく全編から時間等間隔にサンプリングする（長編対策）
        truncated = VideoReviewer._sample_transcript_uniform(transcript)
        return f"[音声書き起こし]\n{truncated}\n\n{instr_audio}{ts_hint}\n{lang_instr}\n\n{json_format}"

    @staticmethod
    def _build_qa_prompt(question: str, transcript: str, timestamps: list[float] = [], history: list[dict] | None = None) -> str:
        parts = []
        if timestamps:
            parts.append("各フレーム画像の直後に [m:ss] 形式でタイムスタンプが付いています。時刻を参考にしてください。")
        if history:
            turns: list[str] = []
            for h in history[-4:]:
                if not isinstance(h, dict):
                    continue
                hq = str(h.get("question") or "").strip()
                ha = str(h.get("answer") or "").strip()
                if not hq or not ha:
                    continue
                if len(ha) > 400:
                    ha = ha[:400] + "…"
                turns.append(f"Q: {hq}\nA: {ha}")
            if turns:
                parts.append("[これまでのやり取り（参考）]\n" + "\n\n".join(turns))
        if transcript:
            selected = VideoReviewer._select_relevant_transcript(transcript, question)
            parts.append(f"[字幕テキスト]\n{selected}")
        parts.append(f"質問: {question}")
        parts.append("映像フレームと字幕テキストを参照して、日本語で具体的に回答してください。")
        return "\n\n".join(parts)

    def analyze_frames(self, frames: list[Image.Image], transcript: str = "", timestamps: list[float] = [], output_lang: str = "ja", scenes_only: bool = False) -> dict:
        self._ensure_loaded()
        prompt = self._build_analyze_prompt(transcript, timestamps, output_lang, scenes_only)
        # scenes_only（refine用）は summary/tags/genre を生成しないので出力上限を抑える
        max_tokens = 1536 if scenes_only else 3072
        raw, gen_meta = self._infer(
            frames,
            prompts.resolve("analyze", ANALYZE_SYSTEM),
            prompt,
            max_new_tokens=max_tokens,
            timestamps=timestamps or None,
            response_format=_analysis_response_format(scenes_only),
        )
        clean = self._strip_code_fences(raw)
        # json_schema 制約により通常はそのままパースできる。フォールバックは
        # 打ち切り（finish_reason=="length"）や旧 llama-server 向けの保険として残す。
        try:
            result = json.loads(clean.strip())
        except Exception:
            try:
                maybe_json = self._extract_balanced_json(clean)
                if maybe_json:
                    result = json.loads(maybe_json)
                else:
                    raise ValueError("balanced JSON not found")
            except Exception:
                result = self._salvage_analysis_fields(clean)

        if isinstance(result.get("scenes"), list):
            result["scenes"] = self._dedup_scenes(result["scenes"])
        result["_analysis_meta"] = gen_meta
        return result

    def qa_frames_stream_with_meta(self, frames: list[Image.Image], question: str, transcript: str = "", timestamps: list[float] = [], on_delta=None, history: list[dict] | None = None) -> dict:
        self._ensure_loaded()
        prompt = self._build_qa_prompt(question, transcript, timestamps, history)
        callback = on_delta or (lambda _delta: None)
        return self._infer_stream_with_meta(
            frames,
            prompts.resolve("qa", QA_SYSTEM),
            prompt,
            max_new_tokens=QA_MAX_NEW_TOKENS,
            on_delta=callback,
            timestamps=timestamps or None,
        )
