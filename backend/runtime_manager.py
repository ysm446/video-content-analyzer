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
from .asr import MODEL_ID as WHISPER_MODEL_ID

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / "runtime"
LLAMA_SERVER_DIR = RUNTIME_DIR / "llama-server"
FFMPEG_DIR = RUNTIME_DIR / "ffmpeg"
MODELS_DIR = ROOT / "models"

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

def find_llama_server_dir() -> Path | None:
    """runtime/llama-server/ 配下で llama-server.exe を含むフォルダを返す（複数なら最新）。"""
    if not LLAMA_SERVER_DIR.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for child in sorted(LLAMA_SERVER_DIR.iterdir()):
        if not child.is_dir():
            continue
        for exe in (child / "llama-server.exe", child / "bin" / "llama-server.exe"):
            if exe.exists():
                candidates.append((child.stat().st_mtime, child))
                break
    if not candidates:
        return None
    return max(candidates)[1]


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


def find_whisper_model_dir() -> Path | None:
    """models/ の HF キャッシュから Whisper モデル（model.bin を含む snapshot）を探す。

    faster-whisper のモデル名→リポジトリの対応は組織名が変わりうるので
    `models--*faster-whisper-{MODEL_ID}` をワイルドカードで照合する。
    """
    if not MODELS_DIR.is_dir():
        return None
    pattern = f"models--*faster-whisper-{WHISPER_MODEL_ID}"
    for repo_dir in sorted(MODELS_DIR.glob(pattern)):
        for snap in sorted((repo_dir / "snapshots").glob("*")):
            if (snap / "model.bin").exists():
                return snap
    return None


def get_status() -> dict:
    """3コンポーネントのインストール状態を返す。"""
    llama_dir = find_llama_server_dir()
    env_dir = os.environ.get("LLAMA_CPP_DIR")

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_ok = shutil.which("ffprobe") is not None

    whisper_dir = find_whisper_model_dir()

    return {
        "llama_cpp": {
            "installed": llama_dir is not None or bool(env_dir),
            "version": llama_dir.name if llama_dir else "",
            "path": env_dir or (str(llama_dir) if llama_dir else ""),
            "env_override": bool(env_dir),
            "cuda": torch.cuda.is_available(),
        },
        "whisper": {
            "installed": whisper_dir is not None,
            "model": WHISPER_MODEL_ID,
            "path": str(whisper_dir) if whisper_dir else "",
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

def _pick_llama_assets(assets: list[dict]) -> tuple[dict, dict | None]:
    """リリースアセットから (本体 zip, cudart zip または None) を選ぶ。

    CUDA が使える環境では win-cuda x64 ビルド（複数あれば CUDA バージョン最大）を、
    無ければ win-cpu x64 ビルドを選ぶ。
    """
    cuda_re = re.compile(r"^llama-b\d+-bin-win-cuda-([\d.]+)-x64\.zip$")
    cpu_re = re.compile(r"^llama-b\d+-bin-win-cpu-x64\.zip$")

    def ver_tuple(s: str) -> tuple:
        return tuple(int(x) for x in s.split(".") if x.isdigit())

    if torch.cuda.is_available():
        cuda_hits = []
        for a in assets:
            m = cuda_re.match(a.get("name", ""))
            if m:
                cuda_hits.append((ver_tuple(m.group(1)), m.group(1), a))
        if cuda_hits:
            _ver, ver_str, asset = max(cuda_hits)
            cudart = next(
                (a for a in assets
                 if a.get("name", "") == f"cudart-llama-bin-win-cuda-{ver_str}-x64.zip"),
                None,
            )
            return asset, cudart

    cpu_hit = next((a for a in assets if cpu_re.match(a.get("name", ""))), None)
    if cpu_hit:
        return cpu_hit, None
    raise RuntimeError("対応する Windows ビルドがリリースに見つかりませんでした")


def install_llama_cpp(progress_cb) -> dict:
    """llama.cpp の最新リリースをダウンロードして runtime/llama-server/ に展開する。"""
    progress_cb({"status": "resolving", "message": "最新リリースを確認中..."})
    release = _fetch_json(LLAMA_LATEST_RELEASE_API)
    assets = release.get("assets") or []
    main_asset, cudart_asset = _pick_llama_assets(assets)

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

    exe = dest_dir / "llama-server.exe"
    if not exe.exists() and not (dest_dir / "bin" / "llama-server.exe").exists():
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

def install_whisper(progress_cb) -> dict:
    """faster-whisper のモデル重みを models/ へ事前ダウンロードする。

    Hugging Face Hub のダウンロードはバイト単位の進捗を取りにくいため
    不定長（indeterminate）の進捗イベントのみ通知する。中断（cancel）は非対応。
    """
    progress_cb({
        "status": "downloading",
        "asset": f"faster-whisper {WHISPER_MODEL_ID}",
        "received": 0,
        "total": 0,
        "percent": None,
    })
    from faster_whisper.utils import download_model  # 重い import を遅延

    path = download_model(WHISPER_MODEL_ID, cache_dir=str(MODELS_DIR))
    print(f"[Runtime] Whisper モデルをダウンロード: {path}")
    return get_status()


def install(component: str, progress_cb) -> dict:
    if component == "llama-cpp":
        return install_llama_cpp(progress_cb)
    if component == "ffmpeg":
        return install_ffmpeg(progress_cb)
    if component == "whisper":
        return install_whisper(progress_cb)
    raise ValueError(f"未対応のコンポーネント: {component}")
