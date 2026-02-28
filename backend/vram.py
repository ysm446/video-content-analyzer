"""VRAM 使用量制限のユーティリティ。

【推論時 VRAM が爆発する原因】
  max_memory はモデル重みの GPU 配置計画にしか効かない。
  推論中の KV キャッシュ・アテンション行列・中間アクティベーションには無効。

【本ファイルで行う対策】
  1. set_process_memory_fraction() → CUDA アロケータへのハードキャップ（run_backend.py で呼ぶ）
  2. max_memory_map()              → モデル重みの配置制御（from_pretrained に渡す）
  3. MAX_PIXELS_PER_FRAME          → 視覚トークン数の制限（画像 1 枚あたり最大 N トークン）
"""
import torch

# ------------------------------------------------------------------ #
#  定数（必要に応じて調整）
# ------------------------------------------------------------------ #

# モデル重みのロード時 VRAM 使用上限（0.0〜1.0）
VRAM_FRACTION = 0.9

# CPU RAM へのスピルオーバー上限
CPU_FALLBACK = "32GiB"

# フレーム 1 枚あたりの最大ピクセル数 → 視覚トークン数に比例する
#   計算式: max_tokens_per_frame = MAX_PIXELS_PER_FRAME / (28 * 28)
#   256 * 28 * 28 = 200,704 px → 最大 256 トークン/枚
#   938 * 28 * 28 = 735,664 px → 最大 938 トークン/枚（変更前のデフォルト）
#
#   15 枚 × 256 トークン = 3,840 トークン（変更後）
#   15 枚 × 938 トークン = 14,074 トークン（変更前 → VRAM 枯渇の原因）
MAX_PIXELS_PER_FRAME = 256 * 28 * 28  # ← ここを増やすと画質↑・VRAM↑


# ------------------------------------------------------------------ #
#  関数
# ------------------------------------------------------------------ #

def set_process_memory_fraction(fraction: float = VRAM_FRACTION) -> None:
    """
    CUDA アロケータへのハードキャップを設定する。

    モデル重み・KV キャッシュ・アクティベーション全てに適用される。
    uvicorn 起動前（run_backend.py）で呼ぶこと。
    """
    if not torch.cuda.is_available():
        return
    for i in range(torch.cuda.device_count()):
        torch.cuda.set_per_process_memory_fraction(fraction, i)
    total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    print(f"[VRAM] ハードキャップ: {fraction*100:.0f}% of {total:.1f} GiB "
          f"= {total * fraction:.1f} GiB")


def max_memory_map() -> dict | None:
    """
    from_pretrained の max_memory 引数に渡す辞書を返す（重みの配置制御用）。
    CUDA 未使用の場合は None を返す。
    """
    if not torch.cuda.is_available():
        return None
    total      = torch.cuda.get_device_properties(0).total_memory
    limit_bytes = int(total * VRAM_FRACTION)
    return {0: limit_bytes, "cpu": CPU_FALLBACK}
