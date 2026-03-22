import re
from pathlib import Path


MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_SKIP_DIR_NAMES = {"hub", "xet", "__pycache__"}


def _is_model_dir(path: Path) -> bool:
    return path.is_dir() and path.name not in _SKIP_DIR_NAMES and not path.name.startswith(".")


def _model_label(model_path: Path) -> str:
    parent = model_path.parent.name
    stem = model_path.stem
    if stem.lower().startswith(parent.lower()):
        return parent
    return f"{parent} / {stem}"


def _model_id(model_path: Path) -> str:
    return f"gguf:{model_path.relative_to(MODELS_DIR).as_posix()}"


def _estimate_vram_gb(model_path: Path, mmproj_path: Path | None = None) -> float:
    total = model_path.stat().st_size + (mmproj_path.stat().st_size if mmproj_path and mmproj_path.exists() else 0)
    return round(total / (1024 ** 3), 1)


def _parse_param_size(label: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]\b", label)
    return float(m.group(1)) if m else 9999.0


def _find_mmproj_for(model_path: Path) -> Path | None:
    files = sorted(model_path.parent.glob("*.gguf"))
    mmproj_files = [p for p in files if "mmproj" in p.name.lower()]
    if not mmproj_files:
        return None

    stem_norm = re.sub(r"[^a-z0-9]+", "", model_path.stem.lower())
    ranked: list[tuple[int, str, Path]] = []
    for candidate in mmproj_files:
        cand_norm = re.sub(r"[^a-z0-9]+", "", candidate.stem.lower().replace("mmproj", ""))
        score = 0 if cand_norm and cand_norm in stem_norm else 1
        ranked.append((score, candidate.name.lower(), candidate))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return ranked[0][2]


def _scan_models() -> list[dict]:
    rows: list[dict] = []
    if not MODELS_DIR.exists():
        return rows

    for folder in sorted((p for p in MODELS_DIR.iterdir() if _is_model_dir(p)), key=lambda p: p.name.lower()):
        ggufs = sorted(folder.rglob("*.gguf"))
        for model_path in ggufs:
            if "mmproj" in model_path.name.lower():
                continue
            mmproj_path = _find_mmproj_for(model_path)
            label = _model_label(model_path)
            rows.append(
                {
                    "id": _model_id(model_path),
                    "label": label,
                    "model_path": model_path,
                    "path": str(model_path),
                    "mmproj_path": str(mmproj_path) if mmproj_path else None,
                    "has_mmproj": mmproj_path is not None,
                    "vram_gb": _estimate_vram_gb(model_path, mmproj_path),
                    "backend": "llama.cpp",
                    "note": "llama.cpp" + ("・vision対応" if mmproj_path else ""),
                    "exists": True,
                }
            )

    rows.sort(key=lambda row: (_parse_param_size(row["label"]), row["label"].lower(), row["id"]))
    return rows


def available_translator_models() -> list[dict]:
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "vram_gb": row["vram_gb"],
            "note": row["note"],
            "backend": row["backend"],
            "path": row["path"],
            "exists": row["exists"],
        }
        for row in _scan_models()
    ]


def available_review_models() -> list[dict]:
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "vram_gb": row["vram_gb"],
            "note": row["note"],
            "backend": row["backend"],
            "path": row["path"],
            "mmproj_path": row["mmproj_path"],
            "exists": row["exists"],
        }
        for row in _scan_models()
        if row["has_mmproj"]
    ]


def get_text_model_meta(model_id: str) -> dict | None:
    for row in _scan_models():
        if row["id"] == model_id:
            return row
    return None


def get_review_model_meta(model_id: str) -> dict | None:
    meta = get_text_model_meta(model_id)
    if meta and meta["has_mmproj"]:
        return meta
    return None


def default_translator_model_id() -> str | None:
    rows = available_translator_models()
    return rows[0]["id"] if rows else None


def default_review_model_id() -> str | None:
    rows = available_review_models()
    return rows[0]["id"] if rows else None
