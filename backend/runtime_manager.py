"""外部ランタイム（llama-cpp / Whisper モデル / ffmpeg）の状態確認とインストール。

- llama-cpp: GitHub の ggml-org/llama.cpp 最新リリースから Windows ビルドを取得して
  runtime/llama-server/<バージョン名>/ に展開する。CUDA が使える環境では CUDA ビルド
  （＋cudart 同梱 zip）、なければ CPU ビルドを選ぶ。
- Whisper: faster-whisper のモデル重みを models/（HF キャッシュ形式）へ事前ダウンロード。
- ffmpeg: PATH に無ければ BtbN/FFmpeg-Builds の固定名アセットを runtime/ffmpeg/ に展開し、
  add_ffmpeg_to_path() で PATH に追加する（バックエンド起動時にも呼ぶ）。

ダウンロードは cancel フラグ（POST /cancel）を確認しながら進み、
progress_cb(dict) で SSE 用の進捗イベントを呼び出し側に渡す。
"""
import os
import re
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib import error, request

import torch

from . import cancel
from .align import ALIGN_MODELS, ENGINE_FASTER_WHISPER
from .asr import MODEL_ID as WHISPER_MODEL_ID

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / "runtime"
LLAMA_SERVER_DIR = RUNTIME_DIR / "llama-server"
FFMPEG_DIR = RUNTIME_DIR / "ffmpeg"
MODELS_DIR = ROOT / "models"
SETTINGS_PATH = ROOT / "settings.json"

# 設定画面で選べる Whisper モデル（faster-whisper のモデル名）
WHISPER_MODELS = [
    {"id": "tiny", "size": "約 75 MB"},
    {"id": "base", "size": "約 145 MB"},
    {"id": "small", "size": "約 480 MB"},
    {"id": "medium", "size": "約 1.5 GB"},
    {"id": "large-v3", "size": "約 3.1 GB"},
    {"id": "large-v3-turbo", "size": "約 1.6 GB"},
]
WHISPER_MODEL_IDS = {m["id"] for m in WHISPER_MODELS}

# 設定画面で選べる文字起こしエンジン
ASR_ENGINE_CHOICES = [
    {"id": "faster-whisper", "label": "faster-whisper（標準）"},
    {"id": "whisperx", "label": "faster-whisper + WhisperX 整列（タイミング精密化）"},
]

_UA_HEADERS = {"User-Agent": "video-content-analyzer"}
LLAMA_LATEST_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
# BtbN ビルドはアセット名が固定なので latest/download の別名 URL がそのまま使える
FFMPEG_DOWNLOAD_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"

COMPONENTS = ("llama-cpp", "whisper", "ffmpeg")

_DOWNLOAD_CHUNK = 1024 * 1024  # 1MiB
_PROGRESS_EVERY_BYTES = 4 * 1024 * 1024  # 進捗イベントの間引き


# ------------------------------------------------------------------ #
#  状態検出
# ------------------------------------------------------------------ #

def _load_settings() -> dict:
    """settings.json を読む（読み取り専用。書き込みは server.py の save_settings が行う）。"""
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _has_llama_exe(child: Path) -> bool:
    return (child / "llama-server.exe").exists() or (child / "bin" / "llama-server.exe").exists()


def installed_llama_versions() -> list[str]:
    """runtime/llama-server/ 配下の llama-server.exe を含むフォルダ名一覧。"""
    if not LLAMA_SERVER_DIR.is_dir():
        return []
    return sorted(
        child.name for child in LLAMA_SERVER_DIR.iterdir()
        if child.is_dir() and _has_llama_exe(child)
    )


def find_llama_server_dir() -> Path | None:
    """runtime/llama-server/ 配下で llama-server.exe を含むフォルダを返す（複数なら最新）。"""
    candidates: list[tuple[float, Path]] = []
    for name in installed_llama_versions():
        child = LLAMA_SERVER_DIR / name
        candidates.append((child.stat().st_mtime, child))
    if not candidates:
        return None
    return max(candidates)[1]


def resolve_active_llama_dir() -> Path | None:
    """使用する llama-server フォルダを解決する。

    優先順: LLAMA_CPP_DIR 環境変数 → settings.json の llama_version（設定画面で選択）
    → 自動検出（最新）。
    """
    env = os.environ.get("LLAMA_CPP_DIR")
    if env:
        return Path(env)
    selected = _load_settings().get("llama_version")
    if selected:
        child = LLAMA_SERVER_DIR / str(selected)
        if child.is_dir() and _has_llama_exe(child):
            return child
    return find_llama_server_dir()


def ffmpeg_runtime_bin() -> Path | None:
    """runtime/ffmpeg/ 配下の bin フォルダ（ffmpeg.exe を含む）を返す。"""
    if not FFMPEG_DIR.is_dir():
        return None
    hits = sorted(FFMPEG_DIR.glob("**/bin/ffmpeg.exe"))
    return hits[0].parent if hits else None


def add_ffmpeg_to_path() -> None:
    """runtime/ffmpeg が存在し、PATH に ffmpeg が無ければ PATH の先頭に追加する。"""
    bindir = ffmpeg_runtime_bin()
    if bindir is None:
        return
    current = os.environ.get("PATH", "")
    if str(bindir) not in current.split(os.pathsep):
        os.environ["PATH"] = str(bindir) + os.pathsep + current
        print(f"[Runtime] ffmpeg を PATH に追加: {bindir}")


def _ffmpeg_version_line() -> str:
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        first = (out.stdout or "").splitlines()
        return first[0].strip() if first else ""
    except Exception:
        return ""


def find_whisper_model_dir(model_id: str) -> Path | None:
    """models/ の HF キャッシュから Whisper モデル（model.bin を含む snapshot）を探す。

    faster-whisper のモデル名→リポジトリの対応は組織名が変わりうるので
    `models--*faster-whisper-{model_id}` をワイルドカードで照合する。
    """
    if not MODELS_DIR.is_dir():
        return None
    pattern = f"models--*faster-whisper-{model_id}"
    for repo_dir in sorted(MODELS_DIR.glob(pattern)):
        for snap in sorted((repo_dir / "snapshots").glob("*")):
            if (snap / "model.bin").exists():
                return snap
    return None


def installed_whisper_models() -> list[str]:
    """インストール済み（model.bin あり）の Whisper モデル名一覧。"""
    return [m["id"] for m in WHISPER_MODELS if find_whisper_model_dir(m["id"]) is not None]


def _align_model_installed(repo: str) -> bool:
    """wav2vec2 アライメントモデルが HF キャッシュ（models/hub/）に存在するか。"""
    repo_dir = MODELS_DIR / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots"
    if not repo_dir.is_dir():
        return False
    return any(snap.is_dir() and any(snap.iterdir()) for snap in repo_dir.glob("*"))


def get_status(whisper_active: str | None = None, asr_engine: str | None = None) -> dict:
    """3コンポーネントのインストール状態を返す。

    whisper_active: 現在使用中の Whisper モデル ID（server が asr.model_id を渡す）。
    asr_engine: 現在の文字起こしエンジン（server が渡す。省略時は faster-whisper）。
    """
    active_llama = resolve_active_llama_dir()
    env_dir = os.environ.get("LLAMA_CPP_DIR")

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_ok = shutil.which("ffprobe") is not None

    whisper_model = whisper_active or WHISPER_MODEL_ID
    whisper_dir = find_whisper_model_dir(whisper_model)

    return {
        "llama_cpp": {
            "installed": active_llama is not None,
            "version": active_llama.name if active_llama else "",
            "path": env_dir or (str(active_llama) if active_llama else ""),
            "env_override": bool(env_dir),
            "versions": installed_llama_versions(),
            "cuda": torch.cuda.is_available(),
        },
        "whisper": {
            "installed": whisper_dir is not None,
            "model": whisper_model,
            "path": str(whisper_dir) if whisper_dir else "",
            "installed_models": installed_whisper_models(),
            "available_models": WHISPER_MODELS,
            "engine": asr_engine or ENGINE_FASTER_WHISPER,
            "engines": ASR_ENGINE_CHOICES,
            "align_models": [
                {"language": lang, "model": repo, "installed": _align_model_installed(repo)}
                for lang, repo in (("ja", ALIGN_MODELS["ja"]), ("en", ALIGN_MODELS["en"]))
            ],
        },
        "ffmpeg": {
            "installed": ffmpeg_path is not None and ffprobe_ok,
            "version": _ffmpeg_version_line() if ffmpeg_path else "",
            "path": ffmpeg_path or "",
        },
    }


# ------------------------------------------------------------------ #
#  ダウンロード・展開の共通処理
# ------------------------------------------------------------------ #

def _fetch_json(url: str) -> dict:
    req = request.Request(url, headers=_UA_HEADERS)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url: str, dest: Path, label: str, progress_cb) -> None:
    """URL を dest にダウンロードする。cancel フラグを確認しつつ進捗を通知する。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = request.Request(url, headers=_UA_HEADERS)
    try:
        with request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            received = 0
            since_last = 0
            while True:
                if cancel.is_canceled():
                    raise cancel.CanceledError()
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                since_last += len(chunk)
                if since_last >= _PROGRESS_EVERY_BYTES:
                    since_last = 0
                    progress_cb({
                        "status": "downloading",
                        "asset": label,
                        "received": received,
                        "total": total,
                        "percent": round(received / total * 100, 1) if total else None,
                    })
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    progress_cb({"status": "downloading", "asset": label, "received": dest.stat().st_size,
                 "total": dest.stat().st_size, "percent": 100.0})


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """zip を dest_dir に展開する（zip-slip 対策付き）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (dest_dir / member).resolve()
            if not target.is_relative_to(dest_resolved):
                raise RuntimeError(f"不正な zip エントリを検出: {member}")
        zf.extractall(dest_dir)


# ------------------------------------------------------------------ #
#  llama-cpp
# ------------------------------------------------------------------ #

_LLAMA_WIN_ASSET_RE = re.compile(r"^llama-b\d+-bin-win-([a-z0-9.-]+)-x64\.zip$")
_CUDA_VARIANT_RE = re.compile(r"^cuda-([\d.]+)$")


def _driver_cuda_version() -> tuple | None:
    """NVIDIA ドライバが対応する CUDA バージョンを nvidia-smi から取得する（例: (13, 0)）。"""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
        m = re.search(r"CUDA Version:\s*([\d.]+)", out.stdout or "")
        if m:
            return tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
    except Exception:
        pass
    return None


def _ver_tuple(s: str) -> tuple:
    return tuple(int(x) for x in s.split(".") if x.isdigit())


def list_llama_builds() -> dict:
    """llama.cpp 最新リリースの Windows x64 ビルド一覧を返す。

    各ビルドに variant（cuda-13.1 / cpu / vulkan 等）と、この環境への推奨フラグを付ける。
    推奨は「NVIDIA ドライバの対応 CUDA バージョン以下で最大の CUDA ビルド」
    （NVIDIA GPU が無ければ CPU ビルド）。
    """
    release = _fetch_json(LLAMA_LATEST_RELEASE_API)
    assets = release.get("assets") or []
    builds = []
    for a in assets:
        m = _LLAMA_WIN_ASSET_RE.match(a.get("name", ""))
        if not m:
            continue
        builds.append({
            "asset": a["name"],
            "variant": m.group(1),
            "size": int(a.get("size") or 0),
            "recommended": False,
        })

    driver_cuda = _driver_cuda_version() if torch.cuda.is_available() else None
    recommended = None
    if driver_cuda:
        cuda_builds = []
        for b in builds:
            cm = _CUDA_VARIANT_RE.match(b["variant"])
            if cm and _ver_tuple(cm.group(1))[:1] <= driver_cuda[:1]:
                cuda_builds.append((_ver_tuple(cm.group(1)), b))
        if cuda_builds:
            recommended = max(cuda_builds)[1]
    if recommended is None:
        recommended = next((b for b in builds if b["variant"] == "cpu"), None)
    if recommended:
        recommended["recommended"] = True

    return {
        "tag": release.get("tag_name") or "",
        "driver_cuda": ".".join(str(x) for x in driver_cuda) if driver_cuda else "",
        "builds": builds,
    }


def install_llama_cpp(progress_cb, asset_name: str | None = None) -> dict:
    """llama.cpp の最新リリースからビルドをダウンロードして runtime/llama-server/ に展開する。

    asset_name: インストールするアセット名（設定画面で選択）。省略時は推奨ビルド。
    CUDA ビルドの場合は対応する cudart 同梱 zip も一緒に展開する。
    """
    progress_cb({"status": "resolving", "message": "リリース情報を確認中..."})
    release = _fetch_json(LLAMA_LATEST_RELEASE_API)
    assets = release.get("assets") or []

    if asset_name:
        main_asset = next((a for a in assets if a.get("name") == asset_name), None)
        if main_asset is None or not _LLAMA_WIN_ASSET_RE.match(asset_name):
            raise ValueError(f"リリースに存在しないビルドです: {asset_name}")
    else:
        info = list_llama_builds()
        rec = next((b for b in info["builds"] if b["recommended"]), None)
        if rec is None:
            raise RuntimeError("対応する Windows ビルドがリリースに見つかりませんでした")
        main_asset = next(a for a in assets if a.get("name") == rec["asset"])

    # CUDA ビルドなら cudart 同梱 zip も取得（ランタイム DLL が本体 zip に含まれないため）
    cudart_asset = None
    vm = _LLAMA_WIN_ASSET_RE.match(main_asset["name"])
    cm = _CUDA_VARIANT_RE.match(vm.group(1)) if vm else None
    if cm:
        cudart_asset = next(
            (a for a in assets
             if a.get("name", "") == f"cudart-llama-bin-win-cuda-{cm.group(1)}-x64.zip"),
            None,
        )

    name = main_asset["name"]
    dest_dir = LLAMA_SERVER_DIR / name[: -len(".zip")]
    zips: list[tuple[dict, Path]] = [(main_asset, RUNTIME_DIR / name)]
    if cudart_asset:
        zips.append((cudart_asset, RUNTIME_DIR / cudart_asset["name"]))

    try:
        for asset, zip_path in zips:
            _download_file(asset["browser_download_url"], zip_path, asset["name"], progress_cb)
        progress_cb({"status": "extracting", "message": f"{dest_dir.name} に展開中..."})
        for _asset, zip_path in zips:
            _safe_extract_zip(zip_path, dest_dir)
    finally:
        for _asset, zip_path in zips:
            zip_path.unlink(missing_ok=True)

    if not _has_llama_exe(dest_dir):
        raise RuntimeError(f"展開後に llama-server.exe が見つかりません: {dest_dir}")
    print(f"[Runtime] llama-cpp をインストール: {dest_dir.name}")
    return get_status()


# ------------------------------------------------------------------ #
#  ffmpeg
# ------------------------------------------------------------------ #

def install_ffmpeg(progress_cb) -> dict:
    """BtbN ビルドの ffmpeg をダウンロードして runtime/ffmpeg/ に展開し PATH に追加する。"""
    zip_path = RUNTIME_DIR / "ffmpeg-master-latest-win64-gpl.zip"
    try:
        _download_file(FFMPEG_DOWNLOAD_URL, zip_path, zip_path.name, progress_cb)
        progress_cb({"status": "extracting", "message": "runtime/ffmpeg に展開中..."})
        _safe_extract_zip(zip_path, FFMPEG_DIR)
    finally:
        zip_path.unlink(missing_ok=True)

    if ffmpeg_runtime_bin() is None:
        raise RuntimeError("展開後に ffmpeg.exe が見つかりません")
    add_ffmpeg_to_path()
    print("[Runtime] ffmpeg をインストールしました")
    return get_status()


# ------------------------------------------------------------------ #
#  Whisper モデル
# ------------------------------------------------------------------ #

def install_whisper(progress_cb, model: str | None = None) -> dict:
    """faster-whisper のモデル重みを models/ へ事前ダウンロードする。

    model: ダウンロードするモデル名（WHISPER_MODELS のいずれか）。省略時はデフォルト。
    Hugging Face Hub のダウンロードはバイト単位の進捗を取りにくいため
    不定長（indeterminate）の進捗イベントのみ通知する。中断（cancel）は非対応。
    """
    model_id = model or WHISPER_MODEL_ID
    if model_id not in WHISPER_MODEL_IDS:
        raise ValueError(f"未対応の Whisper モデルです: {model_id}")
    progress_cb({
        "status": "downloading",
        "asset": f"faster-whisper {model_id}",
        "received": 0,
        "total": 0,
        "percent": None,
    })
    from faster_whisper.utils import download_model  # 重い import を遅延

    path = download_model(model_id, cache_dir=str(MODELS_DIR))
    print(f"[Runtime] Whisper モデルをダウンロード: {path}")
    return get_status()


def install(component: str, progress_cb, asset: str | None = None, model: str | None = None) -> dict:
    if component == "llama-cpp":
        return install_llama_cpp(progress_cb, asset_name=asset)
    if component == "ffmpeg":
        return install_ffmpeg(progress_cb)
    if component == "whisper":
        return install_whisper(progress_cb, model=model)
    raise ValueError(f"未対応のコンポーネント: {component}")
