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

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )
