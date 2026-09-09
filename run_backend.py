import os
from pathlib import Path

# モデルの保存先を models/ フォルダに強制設定（既存の環境変数を上書き）
os.environ["HF_HOME"] = str(Path(__file__).parent / "models")

# CUDA アロケータへのハードキャップ（全モデル・推論時 KV キャッシュ含む）
# uvicorn / torch モデルより先に呼ぶ必要があるためここで実行
import torch
if torch.cuda.is_available():
    from backend.vram import set_process_memory_fraction
    set_process_memory_fraction()  # デフォルト 90%

import socket

import uvicorn

DEFAULT_PORT = 8765


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def resolve_port(host: str) -> int:
    """BACKEND_PORT 環境変数があればそれを使う（Electron が空きポートを決めて渡してくる）。
    無ければ 8765 から順に空きポートを探す（run_backend.py を直接起動したとき用）。"""
    env = os.environ.get("BACKEND_PORT")
    if env:
        return int(env)
    # llama-server が使う予定のポートは避ける（単体起動時は 8766 / 8767 が既定）
    reserved = {int(os.environ.get("LLAMA_CPP_PORT", "8766")), int(os.environ.get("LLAMA_CPP_VISION_PORT", "8767"))}
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 200):
        if port in reserved:
            continue
        if _port_free(host, port):
            if port != DEFAULT_PORT:
                print(f"[run_backend] ポート {DEFAULT_PORT} は使用中のため {port} で起動します")
            return port
    raise RuntimeError("空きポートが見つかりません")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = resolve_port(host)
    print(f"[run_backend] http://{host}:{port}")
    uvicorn.run(
        "backend.server:app",
        host=host,
        port=port,
        reload=False,
    )
