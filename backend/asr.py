import base64
import io
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import error, request

import ffmpeg
import soundfile as sf
import torch

from .model_catalog import get_review_model_meta, default_review_model_id

LLAMA_CPP_DIR = Path(os.environ.get("LLAMA_CPP_DIR", str(Path(__file__).parent.parent / "bin" / "llama-server" / "llama-b8763-bin-win-cuda-13.1-x64")))
LLAMA_CPP_HOST = os.environ.get("LLAMA_CPP_HOST", "127.0.0.1")
LLAMA_CPP_ASR_PORT = int(os.environ.get("LLAMA_CPP_ASR_PORT", "8768"))
LLAMA_CPP_CTX = int(os.environ.get("LLAMA_CPP_CTX", "32768"))
LLAMA_CPP_ASR_HF_REPO = os.environ.get("LLAMA_CPP_ASR_HF_REPO", "").strip()
LLAMA_CPP_ASR_STARTUP_TIMEOUT = float(os.environ.get("LLAMA_CPP_ASR_STARTUP_TIMEOUT", "1800"))

# 音声チャンクサイズ（秒）
CHUNK_SEC = 30

LANGUAGE_MAP = {
    "en":  "English",
    "zh":  "Chinese",
    "yue": "Cantonese",
    "ar":  "Arabic",
    "de":  "German",
    "fr":  "French",
    "es":  "Spanish",
    "pt":  "Portuguese",
    "id":  "Indonesian",
    "it":  "Italian",
    "ja":  "Japanese",
    "ko":  "Korean",
    "ru":  "Russian",
    "th":  "Thai",
    "vi":  "Vietnamese",
    "tr":  "Turkish",
    "hi":  "Hindi",
    "ms":  "Malay",
    "nl":  "Dutch",
    "sv":  "Swedish",
    "da":  "Danish",
    "fi":  "Finnish",
    "pl":  "Polish",
    "cs":  "Czech",
    "tl":  "Filipino",
    "fa":  "Persian",
    "el":  "Greek",
    "ro":  "Romanian",
    "hu":  "Hungarian",
    "mk":  "Macedonian",
}

_TRANSCRIBE_PROMPT = (
    "Transcribe the following speech segment in {language} into {language} text.\n\n"
    "Follow these specific instructions for formatting the answer:\n"
    "* Only output the transcription, with no newlines.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not one point seven, "
    "and write 3 instead of three."
)


def _numpy_to_wav_bytes(data, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read()


class LlamaCppASRServerManager:
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._current_model_id: str | None = None

    def _base_url(self) -> str:
        return f"http://{LLAMA_CPP_HOST}:{LLAMA_CPP_ASR_PORT}"

    def _find_executable(self) -> Path:
        candidates = [
            LLAMA_CPP_DIR / "llama-server.exe",
            LLAMA_CPP_DIR / "bin" / "llama-server.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"llama-server.exe が見つかりません: {LLAMA_CPP_DIR}")

    def _hf_repo_for_meta(self, meta: dict) -> str:
        if LLAMA_CPP_ASR_HF_REPO:
            return LLAMA_CPP_ASR_HF_REPO

        model_path = Path(meta["model_path"])
        folder_name = model_path.parent.name.strip()
        if not folder_name:
            raise ValueError(f"ASR 用 Hugging Face repo を推定できません: {model_path}")
        if "/" in folder_name:
            return folder_name
        return f"ggml-org/{folder_name}"

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
            raise ValueError(f"ASR に対応したモデルが見つかりません: {model_id}")
        model_path = Path(meta["model_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"GGUF モデルが見つかりません: {model_path}")
        hf_repo = self._hf_repo_for_meta(meta)

        same_model = (
            self._process is not None
            and self._process.poll() is None
            and self._current_model_id == model_id
            and self._is_ready()
        )
        if same_model:
            return

        self.stop()
        exe = self._find_executable()
        cmd = [
            str(exe),
            "-hf", hf_repo,
            "--host", LLAMA_CPP_HOST,
            "--port", str(LLAMA_CPP_ASR_PORT),
            "-c", str(LLAMA_CPP_CTX),
            "--jinja",
            "--reasoning", "off",
            "--reasoning-budget", "0",
        ]
        if torch.cuda.is_available():
            cmd.extend(["-ngl", "999"])

        env = os.environ.copy()
        env.setdefault("HF_HOME", str(Path(__file__).resolve().parent.parent / "models"))
        self._process = subprocess.Popen(cmd, cwd=str(exe.parent), env=env)
        self._current_model_id = model_id

        deadline = time.time() + LLAMA_CPP_ASR_STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("ASR 用 llama-cpp サーバーが起動直後に終了しました")
            if self._is_ready():
                print(f"[ASR] llama.cpp server ready: {model_id}")
                return
            time.sleep(1.0)

        self.stop()
        raise TimeoutError("ASR 用 llama-cpp サーバーの起動がタイムアウトしました")

    def stop(self) -> None:
        proc = self._process
        self._process = None
        self._current_model_id = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
        print("[ASR] llama.cpp server を停止しました")

    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._is_ready()
        )

    def transcribe_chunk(self, wav_bytes: bytes, language: str) -> str:
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
        prompt = _TRANSCRIBE_PROMPT.format(language=language)
        audio_data_url = f"data:audio/wav;base64,{audio_b64}"
        payloads = [
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "format": "wav",
                                    "data": audio_b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 1024,
                "stream": False,
            },
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "audio_url",
                                "audio_url": {"url": audio_data_url},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 1024,
                "stream": False,
            },
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "audio",
                                "audio_url": {"url": audio_data_url},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 1024,
                "stream": False,
            },
        ]

        last_error_detail = None
        response = None
        for idx, payload in enumerate(payloads):
            try:
                response = self._request_json("POST", "/v1/chat/completions", payload=payload, timeout=120.0)
                break
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error_detail = f"{exc.code} {detail}"
                # Some llama.cpp multimodal builds reject one audio content type but accept the other.
                if exc.code == 400 and "unsupported content[].type" in detail and idx + 1 < len(payloads):
                    continue
                raise RuntimeError(f"ASR API error: {last_error_detail}") from exc
            except error.URLError as exc:
                raise RuntimeError(f"ASR llama-cpp サーバーに接続できません: {exc}") from exc
        if response is None:
            raise RuntimeError(f"ASR API error: {last_error_detail or 'empty response'}")
        choices = response.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content", "")).strip()


_asr_server = LlamaCppASRServerManager()


class ASRProcessor:
    def __init__(self):
        self.model_id = default_review_model_id() or ""
        self.model = None  # None = 未ロード、truthy = ロード済み

    def set_model_id(self, model_id: str) -> None:
        if self.model_id != model_id:
            _asr_server.stop()
            self.model_id = model_id
            self.model = None

    @property
    def loaded(self) -> bool:
        return _asr_server.is_running()

    def load(self) -> None:
        _asr_server.ensure_model(self.model_id)
        self.model = {"backend": "llama.cpp", "model_id": self.model_id}

    def unload(self) -> None:
        _asr_server.stop()
        self.model = None

    def extract_audio(self, video_path: str) -> str:
        """動画ファイルから 16kHz モノラル WAV を一時ファイルに抽出する"""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        (
            ffmpeg.input(video_path)
            .output(tmp.name, ar=16000, ac=1, format="wav")
            .overwrite_output()
            .run(quiet=True)
        )
        return tmp.name

    def transcribe(
        self,
        video_path: str,
        language: str = None,
    ) -> list[dict]:
        """
        動画を文字起こしし、チャンク単位のタイムスタンプ付きセグメントを返す。

        音声を CHUNK_SEC 秒ごとに分割して Gemma 4 に投げ、
        各チャンクの境界時刻をタイムスタンプとして付与する。

        Args:
            video_path: 動画ファイルのパス
            language:   ISO 言語コード（"en"/"zh"/"ja" など）または None（英語扱い）

        Returns:
            [{"text": str, "timestamp": (start_sec, end_sec)}, ...]
        """
        lang_name = LANGUAGE_MAP.get(language, "English") if language else "English"

        audio_path = self.extract_audio(video_path)
        try:
            data, sr = sf.read(audio_path, dtype="float32")
        finally:
            os.unlink(audio_path)

        if data.ndim > 1:
            data = data.mean(axis=1)

        chunk_samples = int(CHUNK_SEC * sr)
        total_chunks = max(1, -(-len(data) // chunk_samples))
        segments: list[dict] = []

        for chunk_idx, start_sample in enumerate(range(0, len(data), chunk_samples)):
            chunk = data[start_sample : start_sample + chunk_samples]

            if len(chunk) < sr * 0.5:
                continue

            start_sec = start_sample / sr
            end_sec = min(start_sample + chunk_samples, len(data)) / sr
            print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks} @ {start_sec:.1f}s 処理中...")

            wav_bytes = _numpy_to_wav_bytes(chunk, sr)
            text = _asr_server.transcribe_chunk(wav_bytes, lang_name)

            if text:
                segments.append({"text": text, "timestamp": (start_sec, end_sec)})
                print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks}: {len(text)}文字")
            else:
                print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks}: 結果なし（スキップ）")

        return segments
