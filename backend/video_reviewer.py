import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import error, request

import ffmpeg
import torch
from PIL import Image

from .model_catalog import (
    available_review_models as catalog_review_models,
    default_review_model_id,
    get_review_model_meta,
)
from .vram import MAX_PIXELS_PER_FRAME

LLAMA_CPP_DIR = Path(os.environ.get("LLAMA_CPP_DIR", str(Path(__file__).parent.parent / "bin" / "llama-server" / "llama-b8466-bin-win-cuda-13.1-x64")))
LLAMA_CPP_HOST = os.environ.get("LLAMA_CPP_HOST", "127.0.0.1")
LLAMA_CPP_VISION_PORT = int(os.environ.get("LLAMA_CPP_VISION_PORT", "8767"))
LLAMA_CPP_CTX = int(os.environ.get("LLAMA_CPP_CTX", "8192"))

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
)

_ANALYZE_INSTR_AUDIO = (
    "この動画からサンプリングされたフレームと、以下の音声書き起こしを総合して分析し、"
    "以下のJSON形式のみで回答してください。"
    "コードブロック（```）は付けず、純粋なJSONだけを出力してください。\n"
    "scenes は映像や音声の内容・雰囲気が変わるたびに新しい場面として追加してください（目安: 6〜8場面）。"
    "label には場面の内容を表す短いタイトルを付けてください。"
    "description は各場面につき1文だけ、短く書いてください。"
)

QA_SYSTEM = (
    "/no_think\n"
    "あなたは動画分析の専門家です。"
    "提供されたフレーム画像と、必要に応じて音声書き起こしに基づいて質問に日本語で答えてください。"
    "具体的に回答してください。"
)

_TRANSCRIPT_MAX_CHARS = 3000
QA_MAX_NEW_TOKENS = 2048


def available_review_models() -> list[dict]:
    return catalog_review_models()


class LlamaCppVisionServerManager:
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._current_model_id: str | None = None
        self._current_model_path: Path | None = None
        self._current_mmproj_path: Path | None = None
        self._clients: dict[str, str] = {}

    def _base_url(self) -> str:
        return f"http://{LLAMA_CPP_HOST}:{LLAMA_CPP_VISION_PORT}"

    def _find_executable(self) -> Path:
        candidates = [
            LLAMA_CPP_DIR / "llama-server.exe",
            LLAMA_CPP_DIR / "bin" / "llama-server.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"llama-server.exe が見つかりません: {LLAMA_CPP_DIR}")

    def _request_json(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self._base_url() + path, data=data, method=method, headers=headers)
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _is_ready(self) -> bool:
        try:
            self._request_json("GET", "/v1/models", timeout=2.0)
            return True
        except Exception:
            return False

    def ensure_model(self, model_id: str) -> None:
        meta = get_review_model_meta(model_id)
        if meta is None:
            raise ValueError(f"未対応の動画レビュー用モデルです: {model_id}")
        model_path = Path(meta["model_path"])
        mmproj_path = Path(meta["mmproj_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"GGUF モデルが見つかりません: {model_path}")
        if not mmproj_path.exists():
            raise FileNotFoundError(f"mmproj モデルが見つかりません: {mmproj_path}")

        same_model = (
            self._process is not None
            and self._process.poll() is None
            and self._current_model_id == model_id
            and self._current_model_path == model_path
            and self._current_mmproj_path == mmproj_path
            and self._is_ready()
        )
        if same_model:
            return

        self.stop()
        exe = self._find_executable()
        cmd = [
            str(exe),
            "-m",
            str(model_path),
            "--mmproj",
            str(mmproj_path),
            "--host",
            LLAMA_CPP_HOST,
            "--port",
            str(LLAMA_CPP_VISION_PORT),
            "-c",
            str(LLAMA_CPP_CTX),
            "--jinja",
            "--reasoning",
            "off",
            "--reasoning-budget",
            "0",
        ]
        if torch.cuda.is_available():
            cmd.extend(["-ngl", "999"])

        self._process = subprocess.Popen(cmd, cwd=str(exe.parent))
        self._current_model_id = model_id
        self._current_model_path = model_path
        self._current_mmproj_path = mmproj_path

        deadline = time.time() + 180.0
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("動画レビュー用 llama-cpp サーバーが起動直後に終了しました")
            if self._is_ready():
                print(f"[VideoReviewer] llama.cpp vision server ready: {model_id}")
                return
            time.sleep(1.0)

        self.stop()
        raise TimeoutError("動画レビュー用 llama-cpp サーバーの起動がタイムアウトしました")

    def stop(self) -> None:
        proc = self._process
        self._process = None
        self._current_model_id = None
        self._current_model_path = None
        self._current_mmproj_path = None
        self._clients = {}
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)

    def loaded_for(self, model_id: str) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._current_model_id == model_id
            and self._is_ready()
        )

    def acquire_model(self, model_id: str, client_name: str) -> None:
        current = self._clients.get(client_name)
        if current == model_id and self.loaded_for(model_id):
            return
        if current is not None and current != model_id:
            self.release_client(client_name)
        self.ensure_model(model_id)
        self._clients[client_name] = model_id

    def release_client(self, client_name: str) -> None:
        if client_name in self._clients:
            del self._clients[client_name]
        if not self._clients:
            self.stop()

    def chat(self, model_id: str, messages: list[dict], max_tokens: int) -> str:
        self.ensure_model(model_id)
        payload = {
            "messages": messages,
            "think": False,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "stream": False,
        }
        try:
            response = self._request_json("POST", "/v1/chat/completions", payload=payload, timeout=300.0)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"llama-cpp API error: {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"動画レビュー用 llama-cpp サーバーに接続できません: {exc}") from exc
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("動画レビュー API の応答に choices がありません")
        message = choices[0].get("message") or {}
        return str(message.get("content", "")).strip()

    def stream_chat(self, model_id: str, messages: list[dict], max_tokens: int):
        self.ensure_model(model_id)
        payload = {
            "messages": messages,
            "think": False,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "stream": True,
        }
        req = request.Request(
            self._base_url() + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=300.0) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield content
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"llama-cpp API error: {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"動画レビュー用 llama-cpp サーバーに接続できません: {exc}") from exc


_vision_server = LlamaCppVisionServerManager()


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
        genre = ""
        tags: list[str] = []

        if m := re.search(r'"summary"\s*:\s*"((?:\\.|[^"\\])*)"', raw, flags=re.S):
            summary = cls._decode_json_string(m.group(1)).strip()
        if m := re.search(r'"genre"\s*:\s*"((?:\\.|[^"\\])*)"', raw, flags=re.S):
            genre = cls._decode_json_string(m.group(1)).strip()
        if m := re.search(r'"tags"\s*:\s*\[(.*?)\]', raw, flags=re.S):
            tags = [
                cls._decode_json_string(x)
                for x in re.findall(r'"((?:\\.|[^"\\])*)"', m.group(1))
            ]

        return {
            "summary": summary or "分析結果のJSONを最後まで生成できませんでした。",
            "scenes": [],
            "tags": tags,
            "genre": genre or "不明",
        }

    def _frame_to_data_url(self, frame: Image.Image) -> str:
        max_side = int((MAX_PIXELS_PER_FRAME or 200704) ** 0.5)
        image = frame.copy().convert("RGB")
        image.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=88)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _build_messages(self, frames: list[Image.Image], system: str, prompt: str, timestamps: list[float] | None = None) -> list[dict]:
        content: list[dict] = []
        if timestamps and len(timestamps) == len(frames):
            for frame, ts in zip(frames, timestamps):
                content.append({"type": "image_url", "image_url": {"url": self._frame_to_data_url(frame)}})
                content.append({"type": "text", "text": f"[{self._fmt_ts(ts)}]"})
        else:
            for frame in frames:
                content.append({"type": "image_url", "image_url": {"url": self._frame_to_data_url(frame)}})
        content.append({"type": "text", "text": prompt})
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    def _infer(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int, timestamps: list[float] | None = None) -> str:
        messages = self._build_messages(frames, system, prompt, timestamps)
        print(f"[VideoReviewer] 推論開始: フレーム={len(frames)}枚")
        t0 = time.time()
        result = _vision_server.chat(self.model_id, messages, max_new_tokens)
        elapsed = time.time() - t0
        print(f"[VideoReviewer] 推論完了: {elapsed:.1f}秒")
        return self._clean_generated_text(result)

    def _infer_stream(self, frames: list[Image.Image], system: str, prompt: str, max_new_tokens: int, timestamps: list[float] | None = None):
        messages = self._build_messages(frames, system, prompt, timestamps)
        print(f"[VideoReviewer] ストリーミング推論開始: フレーム={len(frames)}枚")
        t0 = time.time()
        prefix_buffer = ""
        content_started = False
        try:
            for delta in _vision_server.stream_chat(self.model_id, messages, max_new_tokens):
                if delta:
                    if not content_started:
                        prefix_buffer += delta
                        cleaned = self._clean_stream_prefix(prefix_buffer)
                        if not cleaned:
                            continue
                        content_started = True
                        yield cleaned
                        prefix_buffer = ""
                    else:
                        yield delta.replace("<think>", "").replace("</think>", "")
        finally:
            elapsed = time.time() - t0
            print(f"[VideoReviewer] ストリーミング推論完了: {elapsed:.1f}秒")

    def _get_duration(self, video_path: str) -> float:
        probe = ffmpeg.probe(video_path)
        return float(probe["format"]["duration"])

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

    def extract_frames(self, video_path: str, max_frames: int, min_interval: float) -> tuple[list[Image.Image], dict]:
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
                    frames.append(Image.open(str(Path(tmpdir) / fname)).copy())

        timestamps = [round(i * interval, 1) for i in range(len(frames))]
        meta = {"count": len(frames), "interval": interval, "duration": duration, "timestamps": timestamps, "mode": "uniform"}
        m, s = divmod(int(duration), 60)
        print(f"[VideoReviewer] フレーム抽出完了: {len(frames)}枚 (動画 {m}分{s}秒 / 間隔 {interval:.1f}秒 / 最大 {max_frames}枚指定)")
        return frames, meta

    def extract_frames_between(self, video_path: str, start_sec: float, end_sec: float, max_frames: int, min_interval: float) -> tuple[list[Image.Image], dict]:
        start = max(0.0, float(start_sec))
        end = max(start + 0.1, float(end_sec))
        duration = end - start
        interval = max(duration / max(max_frames, 1), min_interval)

        frames: list[Image.Image] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            outpattern = str(Path(tmpdir) / "frame_%04d.jpg")
            (
                ffmpeg.input(video_path, ss=start, to=end)
                .filter("fps", fps=f"1/{interval:.4f}")
                .output(outpattern, format="image2", vcodec="mjpeg", q=2)
                .overwrite_output()
                .run(quiet=True)
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
    def _fmt_ts(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _parse_ts(ts: str) -> float:
        try:
            parts = ts.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except Exception:
            pass
        return 0.0

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
    def _build_analyze_prompt(transcript: str, timestamps: list[float] = [], output_lang: str = "ja") -> str:
        lang_instr = (
            "summary / scenes[].label / scenes[].description / tags / genre は日本語で記述してください。"
            if output_lang == "ja"
            else "summary / scenes[].label / scenes[].description / tags / genre は英語で記述してください。"
        )
        ts_hint = VideoReviewer._ts_hint(timestamps)
        if not transcript:
            return _ANALYZE_INSTR_VISUAL + ts_hint + "\n" + lang_instr + "\n\n" + _ANALYZE_JSON_FORMAT
        truncated = VideoReviewer._truncate_transcript(transcript)
        return f"[音声書き起こし]\n{truncated}\n\n{_ANALYZE_INSTR_AUDIO}{ts_hint}\n{lang_instr}\n\n{_ANALYZE_JSON_FORMAT}"

    @staticmethod
    def _build_qa_prompt(question: str, transcript: str, timestamps: list[float] = []) -> str:
        parts = []
        if timestamps:
            parts.append("各フレーム画像の直後に [m:ss] 形式でタイムスタンプが付いています。時刻を参考にしてください。")
        if transcript:
            parts.append(f"[字幕テキスト]\n{VideoReviewer._truncate_transcript(transcript)}")
        parts.append(f"質問: {question}")
        parts.append("映像フレームと字幕テキストを参照して、日本語で具体的に回答してください。")
        return "\n\n".join(parts)

    def analyze_frames(self, frames: list[Image.Image], transcript: str = "", timestamps: list[float] = [], output_lang: str = "ja") -> dict:
        self._ensure_loaded()
        prompt = self._build_analyze_prompt(transcript, timestamps, output_lang)
        raw = self._infer(frames, ANALYZE_SYSTEM, prompt, max_new_tokens=3072, timestamps=timestamps or None)
        clean = self._strip_code_fences(raw)
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
                return self._salvage_analysis_fields(clean)

        if isinstance(result.get("scenes"), list):
            result["scenes"] = self._dedup_scenes(result["scenes"])
        return result

    def qa_frames(self, frames: list[Image.Image], question: str, transcript: str = "", timestamps: list[float] = []) -> str:
        self._ensure_loaded()
        prompt = self._build_qa_prompt(question, transcript, timestamps)
        return self._infer(frames, QA_SYSTEM, prompt, max_new_tokens=QA_MAX_NEW_TOKENS, timestamps=timestamps or None)

    def qa_frames_stream(self, frames: list[Image.Image], question: str, transcript: str = "", timestamps: list[float] = []):
        self._ensure_loaded()
        prompt = self._build_qa_prompt(question, transcript, timestamps)
        yield from self._infer_stream(frames, QA_SYSTEM, prompt, max_new_tokens=QA_MAX_NEW_TOKENS, timestamps=timestamps or None)
