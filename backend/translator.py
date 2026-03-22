import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib import error, request

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

from .vram import max_memory_map
from .video_reviewer import _vision_server

MODEL_ID = "gguf:qwen3.5-9b"
LLAMA_CPP_DIR = Path(os.environ.get("LLAMA_CPP_DIR", r"D:\GitHub\llama-b8466-bin-win-cuda-13.1-x64"))
LLAMA_CPP_HOST = os.environ.get("LLAMA_CPP_HOST", "127.0.0.1")
LLAMA_CPP_PORT = int(os.environ.get("LLAMA_CPP_PORT", "8766"))
LLAMA_CPP_CTX = int(os.environ.get("LLAMA_CPP_CTX", "8192"))
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

GGUF_MODELS = {
    "gguf:qwen3.5-2b": {
        "label": "Qwen3.5 2B GGUF",
        "path": MODELS_DIR / "Qwen3.5-2B-GGUF" / "Qwen3.5-2B-Q4_K_M.gguf",
        "vram_gb": 3.0,
        "note": "llama.cpp・辞書向け",
        "backend": "llama.cpp",
    },
    "gguf:qwen3.5-9b": {
        "label": "Qwen3.5 9B GGUF",
        "path": MODELS_DIR / "Huihui-Qwen3.5-9B-abliterated-GGUF" / "Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf",
        "vram_gb": 8.0,
        "note": "llama.cpp・速い",
        "backend": "llama.cpp",
    },
    "gguf:qwen3.5-35b": {
        "label": "Qwen3.5 35B GGUF",
        "path": MODELS_DIR / "Huihui-Qwen3.5-35B-A3B-abliterated-GGUF" / "Huihui-Qwen3.5-35B-A3B-abliterated.Q4_K_M.gguf",
        "vram_gb": 24.0,
        "note": "llama.cpp・高品質",
        "backend": "llama.cpp",
    },
}

# /no_think でthinkingモードをOFF → 字幕バッチ翻訳に最適化
SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a subtitle translator. Translate the given text to natural Japanese "
    "suitable for subtitle display. Output only the Japanese translation, nothing else."
)

LOOKUP_SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a bilingual dictionary assistant. When given an English word, respond in Japanese "
    "with this exact format (no extra text):\n"
    "【品詞】名詞／動詞／形容詞 など\n"
    "【意味】日本語の意味（簡潔に）\n"
    "【例文】An example sentence. ／ 日本語訳\n"
    "If the word has multiple common meanings, list up to 2."
)


def available_translator_models() -> list[dict]:
    rows: list[dict] = []
    for model_id, meta in GGUF_MODELS.items():
        path = Path(meta["path"])
        rows.append(
            {
                "id": model_id,
                "label": meta["label"],
                "vram_gb": meta["vram_gb"],
                "note": meta["note"] if path.exists() else f'{meta["note"]}・未配置',
                "backend": meta["backend"],
                "path": str(path),
                "exists": path.exists(),
            }
        )
    return rows


class LlamaCppServerManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._current_model_id: str | None = None
        self._current_model_path: Path | None = None

    def _base_url(self) -> str:
        return f"http://{LLAMA_CPP_HOST}:{LLAMA_CPP_PORT}"

    def _find_executable(self) -> Path:
        candidates = [
            LLAMA_CPP_DIR / "llama-server.exe",
            LLAMA_CPP_DIR / "bin" / "llama-server.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"llama-server.exe が見つかりません: {LLAMA_CPP_DIR}")

    def _request_json(self, method: str, path: str, payload: Optional[dict] = None, timeout: float = 30.0) -> dict:
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
            self._request_json("GET", "/health", timeout=2.0)
            return True
        except Exception:
            try:
                self._request_json("GET", "/v1/models", timeout=2.0)
                return True
            except Exception:
                return False

    def ensure_model(self, model_id: str) -> None:
        meta = GGUF_MODELS.get(model_id)
        if meta is None:
            raise ValueError(f"未対応の翻訳モデルです: {model_id}")
        model_path = Path(meta["path"])
        if not model_path.exists():
            raise FileNotFoundError(f"GGUF モデルが見つかりません: {model_path}")

        with self._lock:
            same_model = (
                self._process is not None
                and self._process.poll() is None
                and self._current_model_id == model_id
                and self._current_model_path == model_path
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
                "--host",
                LLAMA_CPP_HOST,
                "--port",
                str(LLAMA_CPP_PORT),
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

            self._process = subprocess.Popen(
                cmd,
                cwd=str(exe.parent),
            )
            self._current_model_id = model_id
            self._current_model_path = model_path

            deadline = time.time() + 120.0
            while time.time() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError("llama-cpp サーバーが起動直後に終了しました")
                if self._is_ready():
                    print(f"[Translator] llama.cpp server ready: {model_id}")
                    return
                time.sleep(1.0)

            self.stop()
            raise TimeoutError("llama-cpp サーバーの起動がタイムアウトしました")

    def stop(self) -> None:
        with self._lock:
            proc = self._process
            self._process = None
            self._current_model_id = None
            self._current_model_path = None
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
        with self._lock:
            return (
                self._process is not None
                and self._process.poll() is None
                and self._current_model_id == model_id
                and self._is_ready()
            )

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
            raise RuntimeError(f"llama-cpp サーバーに接続できません: {exc}") from exc

        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("llama-cpp API の応答に choices がありません")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content).strip()


_llama_cpp = LlamaCppServerManager()


class Translator:
    def __init__(self):
        self.model_id = MODEL_ID
        self.model = None
        self.tokenizer = None
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        if self._is_gguf_model():
            if self._uses_shared_vision_server():
                return _vision_server.loaded_for(self.model_id)
            return _llama_cpp.loaded_for(self.model_id)
        return self.model is not None and self.tokenizer is not None

    def _is_gguf_model(self) -> bool:
        return self.model_id in GGUF_MODELS

    def _uses_shared_vision_server(self) -> bool:
        return self.model_id in {"gguf:qwen3.5-9b", "gguf:qwen3.5-35b"}

    def load(self):
        with self._lock:
            if self._is_gguf_model():
                if self._uses_shared_vision_server():
                    _vision_server.acquire_model(self.model_id, "translator")
                    self.model = {"backend": "llama.cpp-vision-shared", "model_id": self.model_id}
                else:
                    _llama_cpp.ensure_model(self.model_id)
                    self.model = {"backend": "llama.cpp", "model_id": self.model_id}
                self.tokenizer = None
                return

            if self.model is not None and self.tokenizer is not None:
                return
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
                mm = max_memory_map()
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.model_id,
                    torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                    device_map="auto",
                    **({"max_memory": mm} if mm else {}),
                )
                print(f"[Translator] Loaded {self.model_id}")
            except Exception:
                self.model = None
                self.tokenizer = None
                raise

    def unload(self):
        with self._lock:
            self._unload()

    def _unload(self):
        if self._is_gguf_model():
            if self._uses_shared_vision_server():
                _vision_server.release_client("translator")
            else:
                _llama_cpp.stop()
            self.model = None
            self.tokenizer = None
            print("[Translator] llama.cpp モデルをアンロードしました")
            return

        if self.model is not None:
            del self.model
            del self.tokenizer
            self.model = None
            self.tokenizer = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[Translator] モデルをアンロードしました")

    def set_model_id(self, model_id: str):
        with self._lock:
            if self.model_id != model_id:
                self._unload()
                self.model_id = model_id
                print(f"[Translator] モデルを {model_id} に変更（次回使用時にロード）")

    def _ensure_loaded(self):
        with self._lock:
            if not self.loaded:
                self.load()

    def _chat_llama_cpp(self, messages: list[dict], max_tokens: int) -> str:
        if self._uses_shared_vision_server():
            result = _vision_server.chat(self.model_id, messages, max_tokens)
        else:
            result = _llama_cpp.chat(self.model_id, messages, max_tokens)
        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()
        return result

    def translate(self, text: str, context: list[tuple[str, str]] | None = None) -> str:
        with self._lock:
            self._ensure_loaded()
            messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

            if context:
                for orig, jp in context:
                    messages.append({"role": "user", "content": f"Translate to Japanese:\n{orig}"})
                    messages.append({"role": "assistant", "content": jp})

            messages.append({"role": "user", "content": f"Translate to Japanese:\n{text}"})

            if self._is_gguf_model():
                return self._chat_llama_cpp(messages, max_tokens=256)

            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            gen_config = GenerationConfig(do_sample=False, max_new_tokens=256)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()

        return result

    def lookup(self, word: str) -> str:
        with self._lock:
            self._ensure_loaded()
            messages = [
                {"role": "system", "content": LOOKUP_SYSTEM_PROMPT},
                {"role": "user", "content": word.strip()},
            ]

            if self._is_gguf_model():
                return self._chat_llama_cpp(messages, max_tokens=128)

            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            gen_config = GenerationConfig(do_sample=False, max_new_tokens=128)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

            if "</think>" in result:
                result = result.split("</think>", 1)[-1].strip()

            return result
