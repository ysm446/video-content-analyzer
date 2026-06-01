"""ユーザーによるシステムプロンプトのプリセット管理・解決。

- デフォルト（各モジュールの定数）は一切改変しない。
- キーごとに複数のユーザープリセットを作成・切替・削除でき、選択中(active)のものを実行時に使う。
- 保存先は data/prompts.json。形式:
    { key: { "active": "<preset-id>" | "default", "presets": [ {"id","name","text"}, ... ] } }
- 上書きを許可するのは system プロンプト系のみ（EDITABLE_KEYS）。出力フォーマット等は対象外。
- 旧形式 { key: "<text>" }（単一上書き）は読み込み時に1プリセットへ自動移行する。
"""
import json
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_FILE = DATA_DIR / "prompts.json"

# 上書きを許可するキー（system プロンプトのみ）
EDITABLE_KEYS = {"translate", "lookup", "analyze", "qa"}

_cache: dict | None = None
_cache_mtime: int | None = None


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _load_raw() -> dict:
    """data/prompts.json の生データを読む（mtime キャッシュ付き）。"""
    global _cache, _cache_mtime
    try:
        mtime = STORE_FILE.stat().st_mtime_ns
    except (FileNotFoundError, OSError):
        _cache, _cache_mtime = {}, None
        return {}
    if _cache is None or _cache_mtime != mtime:
        try:
            data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
        _cache_mtime = mtime
    return _cache


def _normalize_entry(entry) -> dict:
    """1キー分のエントリを {"active","presets"} 形式へ正規化（旧形式も移行）。"""
    if isinstance(entry, str) and entry.strip():
        pid = _gen_id()
        return {"active": pid, "presets": [{"id": pid, "name": "カスタム", "text": entry}]}
    if isinstance(entry, dict):
        presets = [
            {"id": str(p["id"]), "name": str(p.get("name") or "無題"), "text": str(p.get("text") or "")}
            for p in entry.get("presets", [])
            if isinstance(p, dict) and p.get("id") and isinstance(p.get("text"), str)
        ]
        active = entry.get("active") or "default"
        if active != "default" and not any(p["id"] == active for p in presets):
            active = "default"
        return {"active": active, "presets": presets}
    return {"active": "default", "presets": []}


def load_store() -> dict:
    """全 EDITABLE_KEYS について正規化済みエントリを返す。"""
    raw = _load_raw()
    return {key: _normalize_entry(raw.get(key)) for key in EDITABLE_KEYS}


def _save_store(store: dict) -> None:
    global _cache_mtime
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # プリセットが無く active=default のキーは保存しない（ファイルを汚さない）
    clean = {
        key: entry
        for key, entry in store.items()
        if key in EDITABLE_KEYS and (entry.get("presets") or entry.get("active") != "default")
    }
    STORE_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache_mtime = None


def _require_editable(key: str) -> None:
    if key not in EDITABLE_KEYS:
        raise ValueError(f"このプロンプトは編集できません: {key}")


def list_for(key: str) -> dict:
    """設定画面用に {active, presets:[{id,name,text}]} を返す。"""
    return load_store().get(key, {"active": "default", "presets": []})


def resolve(key: str, default: str) -> str:
    """選択中(active)のプリセットがあればそのテキスト、無ければ default を返す。"""
    if key not in EDITABLE_KEYS:
        return default
    entry = load_store().get(key) or {}
    active = entry.get("active", "default")
    if active != "default":
        for p in entry.get("presets", []):
            if p["id"] == active:
                return p["text"]
    return default


def create_preset(key: str, name: str, text: str) -> dict:
    _require_editable(key)
    store = load_store()
    preset = {"id": _gen_id(), "name": (name or "無題").strip() or "無題", "text": text or ""}
    store[key]["presets"].append(preset)
    store[key]["active"] = preset["id"]  # 作成したら選択状態にする
    _save_store(store)
    return preset


def update_preset(key: str, preset_id: str, name: str | None, text: str | None) -> None:
    _require_editable(key)
    store = load_store()
    for p in store[key]["presets"]:
        if p["id"] == preset_id:
            if name is not None and name.strip():
                p["name"] = name.strip()
            if text is not None:
                p["text"] = text
            _save_store(store)
            return
    raise ValueError(f"プリセットが見つかりません: {preset_id}")


def delete_preset(key: str, preset_id: str) -> None:
    _require_editable(key)
    store = load_store()
    before = len(store[key]["presets"])
    store[key]["presets"] = [p for p in store[key]["presets"] if p["id"] != preset_id]
    if len(store[key]["presets"]) == before:
        raise ValueError(f"プリセットが見つかりません: {preset_id}")
    if store[key]["active"] == preset_id:
        store[key]["active"] = "default"
    _save_store(store)


def set_active(key: str, active: str) -> None:
    _require_editable(key)
    store = load_store()
    if active != "default" and not any(p["id"] == active for p in store[key]["presets"]):
        raise ValueError(f"プリセットが見つかりません: {active}")
    store[key]["active"] = active
    _save_store(store)
