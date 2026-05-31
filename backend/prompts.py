"""ユーザーによるシステムプロンプト上書きの保存・解決。

- デフォルト（各モジュールの定数）は一切改変しない。
- 上書きは data/prompts.json に保存し、実行時に「上書き優先・無ければデフォルト」で解決する。
- 上書きを許可するのは system プロンプト（ペルソナ）系のみ。出力フォーマット指定など
  解析と密結合するプロンプトは EDITABLE_KEYS に含めず、上書き不可（閲覧専用）とする。
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OVERRIDES_FILE = DATA_DIR / "prompts.json"

# 上書きを許可するキー（system プロンプトのみ）
EDITABLE_KEYS = {"translate", "lookup", "analyze", "qa"}

_cache: dict | None = None
_cache_mtime: int | None = None


def load_overrides() -> dict:
    """data/prompts.json を読み込む（mtime キャッシュ付き）。無ければ空 dict。"""
    global _cache, _cache_mtime
    try:
        mtime = OVERRIDES_FILE.stat().st_mtime_ns
    except (FileNotFoundError, OSError):
        _cache, _cache_mtime = {}, None
        return {}
    if _cache is None or _cache_mtime != mtime:
        try:
            data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
        _cache_mtime = mtime
    return _cache


def get_override(key: str) -> str | None:
    ov = load_overrides().get(key)
    return ov if isinstance(ov, str) and ov.strip() else None


def resolve(key: str, default: str) -> str:
    """key の上書きがあればそれを、無ければ default を返す。"""
    if key in EDITABLE_KEYS:
        ov = get_override(key)
        if ov is not None:
            return ov
    return default


def set_override(key: str, text: str | None) -> None:
    """上書きを保存/解除する。text が空/None なら解除（＝デフォルトに戻す）。"""
    if key not in EDITABLE_KEYS:
        raise ValueError(f"このプロンプトは上書きできません: {key}")
    overrides = dict(load_overrides())
    if text and text.strip():
        overrides[key] = text
    else:
        overrides.pop(key, None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 念のため編集可能キー以外は保存しない
    clean = {k: v for k, v in overrides.items() if k in EDITABLE_KEYS and isinstance(v, str) and v.strip()}
    OVERRIDES_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    # キャッシュを次回 load で更新させる
    global _cache_mtime
    _cache_mtime = None
