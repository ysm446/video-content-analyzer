"""llama.cpp(llama-server) をサブプロセスで起動・管理する共通マネージャ。

翻訳用（テキスト GGUF）と動画レビュー用（VL GGUF, mmproj 付き）で共有する。
- `meta_resolver(model_id)` がモデルのメタ辞書（model_path / mmproj_path など）を返す
- メタに有効な `mmproj_path` があれば `--mmproj` 付きで起動する（VL モデル）
- `acquire_model` / `release_client` による参照カウントで、複数クライアント
  （video_reviewer と translator）が同一サーバーを共有できる
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib import error, request

import torch

from . import cancel

def resolve_llama_dir() -> Path:
    """llama-server の実行フォルダを解決する。

    優先順: LLAMA_CPP_DIR 環境変数 → runtime/llama-server/ 配下の自動検出
    （設定画面からのインストールで新バージョンが増えても再設定不要）。
    """
    env = os.environ.get("LLAMA_CPP_DIR")
    if env:
        return Path(env)
    from .runtime_manager import find_llama_server_dir
    found = find_llama_server_dir()
    if found is not None:
        return found
    # 未インストール時のフォールバック（エラーメッセージ用の位置を返す）
    return Path(__file__).parent.parent / "runtime" / "llama-server"


LLAMA_CPP_DIR = resolve_llama_dir()
LLAMA_CPP_HOST = os.environ.get("LLAMA_CPP_HOST", "127.0.0.1")
LLAMA_CPP_CTX = int(os.environ.get("LLAMA_CPP_CTX", "16384"))


class LlamaServerManager:
    def __init__(
        self,
        port: int,
        meta_resolver: Callable[[str], Optional[dict]],
        label: str,
        startup_timeout: float = 180.0,
    ):
        self.port = port
        self._meta_resolver = meta_resolver
        self._label = label
        self._startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._current_model_id: str | None = None
        self._current_model_path: Path | None = None
        self._current_mmproj_path: Path | None = None
        self._clients: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    #  HTTP ヘルパー
    # ------------------------------------------------------------------ #
    def _base_url(self) -> str:
        return f"http://{LLAMA_CPP_HOST}:{self.port}"

    def _find_executable(self) -> Path:
        # 呼び出しごとに解決する（設定画面からインストールした直後も再起動なしで使える）
        base = resolve_llama_dir()
        for candidate in (base / "llama-server.exe", base / "bin" / "llama-server.exe"):
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"llama-server.exe が見つかりません: {base}（設定 → ランタイム からインストールできます）"
        )

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

    # ------------------------------------------------------------------ #
    #  プロセス管理
    # ------------------------------------------------------------------ #
    def ensure_model(self, model_id: str) -> None:
        meta = self._meta_resolver(model_id)
        if meta is None:
            raise ValueError(f"未対応のモデルです（{self._label}）: {model_id}")
        model_path = Path(meta["model_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"GGUF モデルが見つかりません: {model_path}")
        mmproj_raw = meta.get("mmproj_path")
        mmproj_path = Path(mmproj_raw) if mmproj_raw else None
        if mmproj_path and not mmproj_path.exists():
            raise FileNotFoundError(f"mmproj モデルが見つかりません: {mmproj_path}")

        with self._lock:
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
            cmd = [str(exe), "-m", str(model_path)]
            if mmproj_path:
                cmd += ["--mmproj", str(mmproj_path)]
            cmd += [
                "--host", LLAMA_CPP_HOST,
                "--port", str(self.port),
                "-c", str(LLAMA_CPP_CTX),
                "--jinja",
                "--reasoning", "off",
                "--reasoning-budget", "0",
            ]
            if torch.cuda.is_available():
                cmd += ["-ngl", "999"]

            self._process = subprocess.Popen(cmd, cwd=str(exe.parent))
            self._current_model_id = model_id
            self._current_model_path = model_path
            self._current_mmproj_path = mmproj_path

            deadline = time.time() + self._startup_timeout
            while time.time() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError(f"{self._label} llama-cpp サーバーが起動直後に終了しました")
                if self._is_ready():
                    print(f"[LlamaServer:{self._label}] ready: {model_id}")
                    return
                time.sleep(1.0)

            self.stop()
            raise TimeoutError(f"{self._label} llama-cpp サーバーの起動がタイムアウトしました")

    def stop(self) -> None:
        with self._lock:
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

    # ------------------------------------------------------------------ #
    #  参照カウント（複数クライアントでの共有）
    # ------------------------------------------------------------------ #
    def acquire_model(self, model_id: str, client_name: str) -> None:
        with self._lock:
            current = self._clients.get(client_name)
            if current == model_id and self.loaded_for(model_id):
                return
            if current is not None and current != model_id:
                self.release_client(client_name)
            self.ensure_model(model_id)
            self._clients[client_name] = model_id

    def release_client(self, client_name: str) -> None:
        with self._lock:
            self._clients.pop(client_name, None)
            if not self._clients:
                self.stop()

    # ------------------------------------------------------------------ #
    #  推論
    # ------------------------------------------------------------------ #
    def _open_stream(self, messages: list[dict], max_tokens: int, response_format: dict | None = None):
        """/v1/chat/completions をストリーミングで開き、HTTPResponse を返す。"""
        payload = {
            "messages": messages,
            "think": False,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "stream": True,
        }
        if response_format is not None:
            # llama-server は json_schema を GBNF 文法に変換して出力を構文的に制約する
            payload["response_format"] = response_format
        req = request.Request(
            self._base_url() + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return request.urlopen(req, timeout=300.0)

    def chat(self, model_id: str, messages: list[dict], max_tokens: int, response_format: dict | None = None) -> str:
        text, _meta = self.chat_with_meta(model_id, messages, max_tokens, response_format)
        return text

    def chat_with_meta(self, model_id: str, messages: list[dict], max_tokens: int, response_format: dict | None = None) -> tuple[str, dict]:
        """非ストリーミング相当の推論。(テキスト, {"usage", "finish_reason"}) を返す。

        中断に即応するため内部はストリーミングで受信し、
        トークン行ごとに cancel.is_canceled() を確認する。中断時は接続を閉じて
        llama-server の生成を止め、CanceledError を送出する。
        finish_reason が "length" のときは max_tokens で打ち切られている。
        """
        self.ensure_model(model_id)
        cancel.raise_if_canceled()
        parts: list[str] = []
        usage: dict | None = None
        finish_reason: str | None = None
        try:
            with self._open_stream(messages, max_tokens, response_format) as resp:
                for raw_line in resp:
                    if cancel.is_canceled():
                        resp.close()
                        raise cancel.CanceledError()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            parts.append(content)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"llama-cpp API error: {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"{self._label} llama-cpp サーバーに接続できません: {exc}") from exc
        return "".join(parts).strip(), {"usage": usage or {}, "finish_reason": finish_reason or ""}

    def stream_chat_with_meta(self, model_id: str, messages: list[dict], max_tokens: int, response_format: dict | None = None):
        self.ensure_model(model_id)
        cancel.raise_if_canceled()
        usage: dict | None = None
        finish_reason: str | None = None
        try:
            with self._open_stream(messages, max_tokens, response_format) as resp:
                for raw_line in resp:
                    if cancel.is_canceled():
                        resp.close()
                        raise cancel.CanceledError()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield content
                return {
                    "usage": usage or {},
                    "finish_reason": finish_reason or "",
                }
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"llama-cpp API error: {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"{self._label} llama-cpp サーバーに接続できません: {exc}") from exc
