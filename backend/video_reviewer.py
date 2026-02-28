import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import ffmpeg
import torch
from PIL import Image
from qwen_vl_utils import process_vision_info
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

from .vram import MAX_PIXELS_PER_FRAME, max_memory_map

DEFAULT_MODEL_ID = "huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated"

# シーン検出の閾値（0.0〜1.0、低いほど敏感）
SCENE_THRESHOLD = 0.35

ANALYZE_SYSTEM = (
    "/no_think\n"
    "あなたは動画分析の専門家です。"
    "提供されたフレーム画像と、"
    "必要に応じて音声の書き起こしテキストを総合して動画の内容を分析します。"
)

# JSON 出力フォーマット（映像のみ・音声付き共通）
_ANALYZE_JSON_FORMAT = (
    "{\n"
    '  "summary": "動画全体の概要（2〜4文）",\n'
    '  "scenes": [\n'
    '    {"timestamp": "0:00", "label": "場面のタイトル", "description": "この場面で起きていることの説明"},\n'
    '    {"timestamp": "1:30", "label": "場面のタイトル", "description": "この場面で起きていることの説明"}\n'
    '    ... （場面転換ごとに繰り返す）\n'
    "  ],\n"
    '  "tags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5"],\n'
    '  "genre": "ジャンル（例：アクション映画、料理動画、講義、スポーツなど）"\n'
    "}"
)

# 指示文のみ（JSON フォーマットはプロンプト構築時に追加）
_ANALYZE_INSTR_VISUAL = (
    "この動画からサンプリングされたフレームを分析し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は映像の内容・雰囲気・場所・被写体が変化するたびに新しい場面として追加してください。"
    "提供されたフレーム数を参考に細かく場面分けしてください（目安: 8〜15場面）。"
    "label には場面の内容を表す短いタイトルを付けてください。"
)

_ANALYZE_INSTR_AUDIO = (
    "この動画からサンプリングされたフレームと、以下の音声書き起こしを総合して分析し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は映像や音声の内容・雰囲気が変わるたびに新しい場面として追加してください（目安: 8〜15場面）。"
    "label には場面の内容を表す短いタイトルを付けてください。"
)

QA_SYSTEM = (
    "/no_think\n"
    "あなたは動画分析の専門家です。"
    "提供されたフレーム画像と、必要に応じて音声書き起こしに基づいて質問に日本語で答えてください。"
    "具体的に回答してください。"
)

# 書き起こしテキストをプロンプトに含める際の最大文字数（トークン超過を防ぐ）
_TRANSCRIPT_MAX_CHARS = 3000


class VideoReviewer:
    def __init__(self):
        self.model_id = DEFAULT_MODEL_ID
        self.model = None
        self.processor = None
        # フレームキャッシュ: 同じ動画・同じ設定なら再抽出しない
        # _frame_cache = ((video_path, frame_mode, max_frames), frames, meta)
        self._frame_cache: tuple | None = None

    def set_model_id(self, model_id: str):
        if self.model_id != model_id:
            self.unload()
            self.model_id = model_id
            print(f"[VideoReviewer] モデルを {model_id} に変更（次回使用時にロード）")

    def load(self):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        mm = max_memory_map()
        self.model = Qwen3VLForConditionalGeneration.from_pretrained(
            self.model_id,
            torch_dtype=dtype,
            device_map="auto",
            attn_implementation="sdpa",   # メモリ効率のよい Attention（O(N) instead of O(N²)）
            **({"max_memory": mm} if mm else {}),
        )
        # max_pixels でフレームあたりの視覚トークン数を制限
        # 256 * 28 * 28 = 200,704 px → 最大 256 トークン/枚（vram.py で調整可能）
        self.processor = AutoProcessor.from_pretrained(
            self.model_id,
            min_pixels=64  * 28 * 28,
            max_pixels=MAX_PIXELS_PER_FRAME,
        )
        print(f"[VideoReviewer] Loaded {self.model_id}")

    def unload(self):
        if self.model is not None:
            del self.model
            del self.processor
            self.model = None
            self.processor = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[VideoReviewer] モデルをアンロードしました")
        self._frame_cache = None

    def cache_frames(self, video_path: str, frame_mode: str, max_frames: int,
                     frames: list, meta: dict):
        """フレームをキャッシュして次のQ&Aで再利用できるようにする"""
        self._frame_cache = ((video_path, frame_mode, max_frames), frames, meta)
        print(f"[VideoReviewer] フレームキャッシュ保存: {len(frames)}枚 ({frame_mode})")

    def get_cached_frames(self, video_path: str, frame_mode: str,
                          max_frames: int) -> tuple[list, dict] | None:
        """キャッシュがヒットすれば (frames, meta) を返す。なければ None"""
        if self._frame_cache is None:
            return None
        key, frames, meta = self._frame_cache
        if key == (video_path, frame_mode, max_frames):
            print(f"[VideoReviewer] フレームキャッシュヒット: {len(frames)}枚 ({frame_mode})")
            return frames, meta
        return None

    def _ensure_loaded(self):
        if self.model is None:
            self.load()

    # ---------- フレーム抽出 ----------

    def _get_duration(self, video_path: str) -> float:
        probe = ffmpeg.probe(video_path)
        return float(probe["format"]["duration"])

    def extract_frames_scene(
        self,
        video_path: str,
        max_frames: int,
        threshold: float = SCENE_THRESHOLD,
    ) -> tuple[list[Image.Image], dict]:
        """
        ffmpeg のシーン変化検出でキーフレームを抽出して PIL.Image のリストを返す。
        先頭フレームを常に含む。検出数が max_frames を超える場合は間引く。
        フレームが 1 枚以下の場合は均等サンプリングにフォールバック。

        Returns:
            (frames, meta)
            meta = {"count": int, "interval": None, "duration": float,
                    "timestamps": list[float], "mode": "scene"}
        """
        duration = self._get_duration(video_path)
        frames: list[Image.Image] = []
        timestamps: list[float] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            outpattern = str(Path(tmpdir) / "frame_%04d.jpg")
            # 先頭フレーム（eq(n\,0)）＋シーン変化フレームを抽出
            # showinfo フィルターで各フレームの pts_time を stderr に出力させる
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", f"select=eq(n\\,0)+gt(scene\\,{threshold}),showinfo",
                "-vsync", "vfr",
                "-vcodec", "mjpeg", "-q:v", "2",
                outpattern, "-y",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

            # stderr から pts_time を抽出（showinfo の出力形式: "pts_time:1.234"）
            ts_pattern = re.compile(r"\bpts_time:(\d+\.?\d*)")
            raw_ts = [
                float(m.group(1))
                for line in result.stderr.splitlines()
                if (m := ts_pattern.search(line))
            ]

            frame_files = sorted(
                f for f in os.listdir(tmpdir)
                if f.startswith("frame_") and f.endswith(".jpg")
            )

            # タイムスタンプとファイル数を合わせる（ずれた場合は末尾を補完）
            if len(raw_ts) > len(frame_files):
                raw_ts = raw_ts[:len(frame_files)]
            elif len(raw_ts) < len(frame_files):
                step = duration / max(len(frame_files) - 1, 1)
                raw_ts += [step * i for i in range(len(raw_ts), len(frame_files))]

            # max_frames を超えたら間引く（先頭は必ず保持）
            if len(frame_files) > max_frames:
                step = (len(frame_files) - 1) / (max_frames - 1)
                indices = sorted(set(
                    [0] + [min(round(i * step), len(frame_files) - 1)
                           for i in range(1, max_frames)]
                ))
                frame_files = [frame_files[i] for i in indices]
                raw_ts = [raw_ts[i] for i in indices]

            for fname in frame_files:
                frames.append(Image.open(str(Path(tmpdir) / fname)).copy())
            timestamps = raw_ts[:len(frames)]

        # フレームが取れなかった場合は均等サンプリングにフォールバック
        if len(frames) <= 1:
            print("[VideoReviewer] シーン検出フレームなし → 均等サンプリングにフォールバック")
            return self.extract_frames(video_path, max_frames, 5.0)

        m_d, s_d = divmod(int(duration), 60)
        print(
            f"[VideoReviewer] シーン検出フレーム抽出完了: {len(frames)}枚"
            f" (動画 {m_d}分{s_d}秒 / 閾値 {threshold} / 最大 {max_frames}枚指定)"
        )
        return frames, {
            "count": len(frames),
            "interval": None,
            "duration": duration,
            "timestamps": timestamps,
            "mode": "scene",
        }

    def extract_frames(
        self,
        video_path: str,
        max_frames: int,
        min_interval: float,
    ) -> tuple[list[Image.Image], dict]:
        """
        動画から均等にフレームをサンプリングして PIL.Image のリストを返す。

        間隔 = max(動画長 / max_frames, min_interval)
        → 短い動画では min_interval が効き、長い動画では max_frames に収まる。

        Returns:
            (frames, meta)
            meta = {"count": int, "interval": float, "duration": float}
        """
        duration = self._get_duration(video_path)
        interval = max(duration / max(max_frames, 1), min_interval)

        frames: list[Image.Image] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            outpattern = str(Path(tmpdir) / "frame_%04d.jpg")
            (
                ffmpeg.input(video_path)
                .filter("fps", fps=f"1/{interval:.4f}")
                .output(outpattern, format="image2", vcodec="mjpeg", q=2)
                .overwrite_output()
                .run(quiet=True)
            )
            for fname in sorted(os.listdir(tmpdir)):
                if fname.startswith("frame_") and fname.endswith(".jpg"):
                    img = Image.open(str(Path(tmpdir) / fname)).copy()
                    frames.append(img)

        timestamps = [round(i * interval, 1) for i in range(len(frames))]
        meta = {"count": len(frames), "interval": interval, "duration": duration,
                "timestamps": timestamps, "mode": "uniform"}
        m, s = divmod(int(duration), 60)
        print(
            f"[VideoReviewer] フレーム抽出完了: {len(frames)}枚"
            f" (動画 {m}分{s}秒 / 間隔 {interval:.1f}秒 / 最大 {max_frames}枚指定)"
        )
        return frames, meta

    # ---------- 推論 ----------

    def _infer(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int) -> str:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [{"type": "image", "image": f} for f in frames]
                + [{"type": "text", "text": prompt}],
            },
        ]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self.processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        ).to(self.model.device)

        n_input_tokens = inputs.input_ids.shape[1]
        tokens_per_frame = (n_input_tokens // len(frames)) if frames else 0
        print(f"[VideoReviewer] 推論開始: フレーム={len(frames)}枚, "
              f"入力トークン={n_input_tokens} (~{tokens_per_frame}トークン/枚)")
        t0 = time.time()

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )

        elapsed = time.time() - t0
        generated_ids = output_ids[0][inputs.input_ids.shape[1] :]
        n_output = len(generated_ids)
        print(f"[VideoReviewer] 推論完了: {elapsed:.1f}秒, 出力トークン={n_output}")

        result = self.processor.batch_decode(
            [generated_ids], skip_special_tokens=True
        )[0].strip()

        # thinking トークンが残っている場合は除去
        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()

        return result

    # ---------- プロンプト構築ヘルパー ----------

    @staticmethod
    def _truncate_transcript(transcript: str) -> str:
        """長すぎる書き起こしを切り詰め、末尾に省略記号を付ける"""
        if len(transcript) <= _TRANSCRIPT_MAX_CHARS:
            return transcript
        return transcript[:_TRANSCRIPT_MAX_CHARS] + "…（以下省略）"

    @staticmethod
    def _fmt_ts(secs: float) -> str:
        """秒数を m:ss 形式にフォーマット"""
        m, s = divmod(int(secs), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _ts_hint(timestamps: list[float]) -> str:
        """フレームタイムスタンプをプロンプトに挿入するヒント文を生成"""
        if not timestamps:
            return ""
        parts = [
            f"フレーム{i + 1}={VideoReviewer._fmt_ts(t)}"
            for i, t in enumerate(timestamps)
        ]
        return (
            f"\n\n{len(parts)} 枚のフレームが提供されています。"
            "フレームのタイムスタンプ（フレーム番号=動画内の時刻）: "
            + ", ".join(parts)
            + "\nscenes の timestamp にはその場面が始まる最も近いフレームの時刻（m:ss 形式）を記入してください。"
            f"\n場面数は {max(len(parts) // 2, 5)} 以上を目安に、変化を見逃さず細かく記録してください。"
        )

    @staticmethod
    def _build_analyze_prompt(transcript: str, timestamps: list[float] = []) -> str:
        ts_hint = VideoReviewer._ts_hint(timestamps)
        if not transcript:
            return _ANALYZE_INSTR_VISUAL + ts_hint + "\n\n" + _ANALYZE_JSON_FORMAT
        truncated = VideoReviewer._truncate_transcript(transcript)
        return (
            f"[音声書き起こし]\n{truncated}\n\n"
            + _ANALYZE_INSTR_AUDIO
            + ts_hint
            + "\n\n"
            + _ANALYZE_JSON_FORMAT
        )

    @staticmethod
    def _build_qa_prompt(question: str, transcript: str) -> str:
        if not transcript:
            return f"質問: {question}\n\nフレームに基づいて日本語で具体的に回答してください。"
        truncated = VideoReviewer._truncate_transcript(transcript)
        return (
            f"[音声書き起こし]\n{truncated}\n\n"
            f"質問: {question}\n\n"
            "映像フレームと音声書き起こしを総合して、日本語で具体的に回答してください。"
        )

    # ---------- 公開 API ----------

    def analyze_frames(
        self,
        frames: list[Image.Image],
        transcript: str = "",
        timestamps: list[float] = [],
    ) -> dict:
        """フレームリストから動画を分析してサマリー・シーン・タグを返す"""
        self._ensure_loaded()
        prompt = self._build_analyze_prompt(transcript, timestamps)
        raw = self._infer(frames, ANALYZE_SYSTEM, prompt, max_new_tokens=3000)

        # JSON パース（コードブロック除去 → パース → 失敗時はフォールバック）
        try:
            clean = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
            clean = re.sub(r"\n?```$", "", clean.strip(), flags=re.MULTILINE)
            return json.loads(clean.strip())
        except Exception:
            return {"summary": raw, "scenes": [], "tags": [], "genre": "不明"}

    def qa_frames(self, frames: list[Image.Image], question: str, transcript: str = "") -> str:
        """フレームリストと質問から回答を生成する"""
        self._ensure_loaded()
        prompt = self._build_qa_prompt(question, transcript)
        return self._infer(frames, QA_SYSTEM, prompt, max_new_tokens=512)
