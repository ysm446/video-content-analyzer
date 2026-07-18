import os
import json
import asyncio
import queue
import threading
import re
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime

import base64
import psutil

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field

# モデルロード前に HF_HOME を設定
os.environ["HF_HOME"] = str(Path(__file__).parent.parent / "models")

from .align import Aligner, ASR_ENGINES, ENGINE_FASTER_WHISPER, ENGINE_WHISPERX, load_audio as load_align_audio
from .asr import ASRProcessor
from .model_catalog import available_review_models as scan_review_models
from .model_catalog import available_translator_models as scan_translator_models
from .translator import Translator, available_translator_models, get_prompts as _translator_prompts
from .subtitle import segments_to_srt, srt_file_to_segments, save_srt, make_output_path, split_long_segments
from .video_reviewer import VideoReviewer, available_review_models, get_prompts as _review_prompts, parse_timestamp_seconds
from . import prompts as _prompts
from . import cancel
from . import runtime_manager

SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


asr = ASRProcessor()
aligner = Aligner()
asr_engine = ENGINE_FASTER_WHISPER  # "faster-whisper" | "whisperx"（settings.json の asr_engine）
translator     = Translator()
video_reviewer = VideoReviewer()
_review_model_ids = {m["id"] for m in scan_review_models() if m.get("exists")}

# 前回選択したモデルを復元
_s = load_settings()
if _m := _s.get("vl_model"):
    if _m in _review_model_ids:
        video_reviewer.set_model_id(_m)
        translator.set_model_id(_m)
elif _m := _s.get("translator_model"):
    translator.set_model_id(_m)
if _w := _s.get("whisper_model"):
    if _w in runtime_manager.WHISPER_MODEL_IDS:
        asr.set_model_id(_w)
if (_e := _s.get("asr_engine")) in ASR_ENGINES:
    asr_engine = _e


@asynccontextmanager
async def lifespan(app: FastAPI):
    # runtime/ffmpeg がインストール済みなら PATH に追加（システムに ffmpeg が無い環境向け）
    runtime_manager.add_ffmpeg_to_path()
    print("Video Content Analyzer API が起動しました（モデルはオンデマンドでロード）")
    yield
    # シャットダウン時に VRAM を解放
    asr.unload()
    aligner.unload()
    translator.unload()
    video_reviewer.unload()


app = FastAPI(title="Video Content Analyzer API", lifespan=lifespan)

# Electron レンダラー（file:// ページ）からの fetch は Origin: "null" になる。
# ブラウザで開いた外部サイトからの呼び出し（Origin: https://... ）は拒否し、
# DNS リバインディング対策として Host もローカルのみ許可する。
_ALLOWED_ORIGINS = {"null", "file://"}
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def _local_only_guard(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host and host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(data: dict) -> str:
    """Server-Sent Events 形式にシリアライズ"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_canceled() -> str:
    """中断完了イベントを返す。

    フラグを残したままにすると、次の SSE 処理が始まるまで /lookup などの
    非 SSE 推論が CanceledError で失敗し続けるため、ここでクリアする。
    """
    cancel.clear_cancel()
    return sse({"status": "canceled"})


def _snap_scene_timestamps(result: dict, frame_timestamps: list[float], interval: float | None) -> None:
    """scenes[].timestamp を、実際にモデルへ送ったフレーム時刻の最近傍に吸着させる。

    VL モデルは提示フレーム間の時刻を捏造することがあるが、モデルが実際に
    見た時刻はフレームのタイムスタンプだけなので、許容誤差内なら最近傍に丸める。
    許容誤差は uniform のサンプリング間隔（scene 検出時は 10 秒）。
    """
    scenes = result.get("scenes") if isinstance(result, dict) else None
    if not isinstance(scenes, list) or not frame_timestamps:
        return
    tolerance = float(interval) if interval else 10.0
    for s in scenes:
        if not isinstance(s, dict):
            continue
        sec = parse_timestamp_seconds(s.get("timestamp"))
        if sec is None:
            continue
        nearest = min(frame_timestamps, key=lambda t: abs(t - sec))
        if abs(nearest - sec) <= tolerance:
            m, r = divmod(int(nearest), 60)
            s["timestamp"] = f"{m}:{r:02d}"


def _analysis_warnings(gen_meta: dict, pass_name: str) -> list[dict]:
    """analyze_frames の生成メタから、ユーザーに通知すべき warning イベントを組み立てる。"""
    warnings: list[dict] = []
    if not isinstance(gen_meta, dict):
        return warnings
    if gen_meta.get("finish_reason") == "length":
        warnings.append({
            "status": "analyze_warning",
            "pass": pass_name,
            "message": "生成がトークン上限で打ち切られました。チャプターや説明が欠けている可能性があります",
        })
    used = gen_meta.get("frames_used")
    requested = gen_meta.get("frames_requested")
    reason = gen_meta.get("reduced_reason")
    if isinstance(used, int) and isinstance(requested, int) and used < requested:
        if reason == "budget":
            message = f"コンテキスト上限に収めるためフレームを {requested}枚 → {used}枚 に間引きました"
        else:
            message = f"画像処理エラーのためフレームを {requested}枚 → {used}枚 に縮小して分析しました"
        warnings.append({"status": "analyze_warning", "pass": pass_name, "message": message})
    elif reason == "budget":
        tpf = gen_meta.get("tokens_per_frame")
        warnings.append({
            "status": "analyze_warning",
            "pass": pass_name,
            "message": f"コンテキスト上限に収めるためフレーム解像度を下げました（{tpf}トークン/枚）",
        })
    return warnings


def _parse_timestamp_seconds(value: str | None) -> float | None:
    # video_reviewer 側の dedup と同じ解釈になるよう実装を一本化（h:mm:ss / m:ss / 分3桁対応）
    return parse_timestamp_seconds(value)


def _build_toc_entries(result: dict, duration: float) -> list[dict]:
    scenes = result.get("scenes") if isinstance(result, dict) else None
    scenes = scenes if isinstance(scenes, list) else []

    rows: list[dict] = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            continue
        start = _parse_timestamp_seconds(s.get("timestamp"))
        if start is None:
            continue
        start = max(0.0, min(float(duration), float(start)))
        rows.append(
            {
                "start_sec": start,
                "title": (s.get("label") or f"チャプター{i+1}").strip(),
                "summary": (s.get("description") or "").strip(),
                "timestamp": s.get("timestamp") or "",
            }
        )

    if not rows:
        summary = (result.get("summary") if isinstance(result, dict) else "") or "動画全体"
        return [
            {
                "id": "ch001",
                "start_sec": 0.0,
                "end_sec": float(duration),
                "title": "全体",
                "summary": str(summary).strip(),
                "timestamp": "0:00",
                "confidence": 0.5,
            }
        ]

    rows.sort(key=lambda x: x["start_sec"])
    dedup: list[dict] = []
    last_start: float | None = None
    for r in rows:
        if last_start is None or abs(r["start_sec"] - last_start) > 1e-3:
            dedup.append(r)
            last_start = r["start_sec"]

    entries: list[dict] = []
    for i, r in enumerate(dedup):
        end_sec = dedup[i + 1]["start_sec"] if i + 1 < len(dedup) else float(duration)
        entries.append(
            {
                "id": f"ch{i+1:03d}",
                "start_sec": round(float(r["start_sec"]), 3),
                "end_sec": round(max(float(r["start_sec"]), float(end_sec)), 3),
                "title": r["title"] or f"チャプター{i+1}",
                "summary": r["summary"],
                "timestamp": r["timestamp"] or "0:00",
                "confidence": 0.8,
            }
        )
    return entries


def _validate_analysis_mode(mode: str) -> str:
    if mode not in {"speed", "balanced", "quality"}:
        raise HTTPException(400, f"無効な analysis_mode: {mode}")
    return mode


def _validate_output_lang(lang: str) -> str:
    if lang not in {"ja", "en"}:
        raise HTTPException(400, f"無効な output_lang: {lang}")
    return lang


def _analysis_plan(mode: str, max_frames: int) -> dict:
    if mode == "speed":
        return {
            "coarse_frames": max_frames,
            "refine_limit": 0,
            "refine_min_span": 999999.0,
            "refine_frames": 0,
            "max_chapter_span": 999999.0,
        }
    if mode == "balanced":
        return {
            "coarse_frames": max(8, min(18, max_frames // 2 or 8)),
            "refine_limit": 4,
            "refine_min_span": 180.0,
            "refine_frames": max(8, min(18, max_frames // 2 or 8)),
            "max_chapter_span": 0.0,
        }
    return {
        "coarse_frames": max(10, min(22, max_frames // 2 or 10)),
        "refine_limit": 8,
        "refine_min_span": 120.0,
        "refine_frames": max(10, min(24, (max_frames * 2) // 3 or 10)),
        "max_chapter_span": 0.0,
    }


def _auto_max_chapter_span(duration: float) -> float:
    return max(90.0, min(480.0, float(duration) * 0.12))


def _select_refine_targets(entries: list[dict], duration: float, plan: dict) -> list[dict]:
    if not entries or int(plan.get("refine_limit", 0)) <= 0:
        return []

    refine_min_span = float(plan.get("refine_min_span", 0.0))
    auto_max_span = _auto_max_chapter_span(duration)
    configured_max_span = float(plan.get("max_chapter_span") or 0.0)
    max_chapter_span = configured_max_span if configured_max_span > 0 else auto_max_span
    refine_limit = int(plan.get("refine_limit", 0))

    annotated = []
    for idx, entry in enumerate(entries):
        start = float(entry.get("start_sec", 0.0))
        end = float(entry.get("end_sec", start))
        span = max(0.0, end - start)
        if span < 10.0:
            continue
        annotated.append((idx, span, entry))

    forced = [row for row in annotated if row[1] >= max_chapter_span]
    optional = [row for row in annotated if row[1] >= refine_min_span and row[1] < max_chapter_span]

    forced.sort(key=lambda row: (-row[1], row[0]))
    optional.sort(key=lambda row: (-row[1], row[0]))

    selected: list[tuple[int, float, dict]] = []
    seen: set[int] = set()
    for row in forced:
        if row[0] in seen:
            continue
        selected.append(row)
        seen.add(row[0])

    for row in optional:
        if len(selected) >= max(refine_limit, len(forced)):
            break
        if row[0] in seen:
            continue
        selected.append(row)
        seen.add(row[0])

    selected.sort(key=lambda row: row[0])
    return [row[2] for row in selected]


def _slice_transcript(transcript: str, start_sec: float, end_sec: float) -> str:
    if not transcript:
        return ""
    rows = []
    for line in transcript.splitlines():
        m = re.match(r"^\[(\d+):(\d{2})\]\s*(.*)$", line.strip())
        if not m:
            continue
        sec = int(m.group(1)) * 60 + int(m.group(2))
        if start_sec <= sec < end_sec:
            rows.append(line)
    return "\n".join(rows)


def _merge_toc_entries(entries: list[dict], duration: float) -> list[dict]:
    if not entries:
        return []
    rows = sorted(entries, key=lambda x: float(x.get("start_sec", 0.0)))
    merged: list[dict] = []
    base_merge_gap_sec = 3.0
    min_chapter_gap_sec = max(4.0, min(15.0, float(duration) * 0.02))
    for r in rows:
        if not merged:
            merged.append(r.copy())
            continue
        prev = merged[-1]
        gap = float(r.get("start_sec", 0.0)) - float(prev.get("start_sec", 0.0))
        same_title = (r.get("title") or "").strip() == (prev.get("title") or "").strip()
        prev_summary = (prev.get("summary") or "").strip()
        curr_summary = (r.get("summary") or "").strip()
        # 動画長に応じた最小チャプター間隔を維持しつつ、同タイトルは積極的に統合する。
        if (
            same_title
            or gap < min_chapter_gap_sec
            or (gap < base_merge_gap_sec and (not prev_summary or not curr_summary))
        ):
            # 後ろの説明を採用する場合は時刻も後ろに寄せて「早すぎる見出し」を防ぐ。
            use_curr = len(curr_summary) > len(prev_summary)
            if use_curr:
                prev["start_sec"] = r.get("start_sec", prev.get("start_sec", 0.0))
                prev["timestamp"] = r.get("timestamp", prev.get("timestamp", "0:00"))
                prev["title"] = r.get("title", prev.get("title", ""))
                prev["summary"] = curr_summary
            continue
        merged.append(r.copy())
    for i, r in enumerate(merged):
        next_start = float(merged[i + 1]["start_sec"]) if i + 1 < len(merged) else float(duration)
        r["id"] = f"ch{i+1:03d}"
        r["end_sec"] = round(max(float(r["start_sec"]), next_start), 3)
        if not r.get("timestamp"):
            s = int(float(r["start_sec"]))
            r["timestamp"] = f"{s//60}:{s%60:02d}"
    return merged


def _entries_from_refine_result(
    result: dict,
    start: float,
    end: float,
    total_duration: float,
) -> list[dict]:
    """
    再解析区間の結果を絶対時刻に正規化する。
    - モデルが絶対時刻を返す場合: そのまま区間内だけ採用
    - モデルが相対時刻(0始まり)を返す場合: 区間開始時刻を加算
    """
    abs_entries = _build_toc_entries(result, total_duration)
    in_range = [
        e for e in abs_entries
        if (start - 2.0) <= float(e.get("start_sec", 0.0)) <= (end + 2.0)
    ]
    if in_range:
        return in_range

    rel_entries = _build_toc_entries(result, end - start)
    for e in rel_entries:
        e["start_sec"] = round(start + float(e.get("start_sec", 0.0)), 3)
        e["end_sec"] = round(start + float(e.get("end_sec", 0.0)), 3)
    return rel_entries


# ---------- リクエストモデル ----------

class TranscribeRequest(BaseModel):
    video_path: str
    language: Optional[str] = None  # "en" / "zh" / "ko" など。None で自動検出


class TranslateRequest(BaseModel):
    srt_path: str  # 翻訳対象の SRT ファイルパス（通常は .original.srt）
    mode: str = "quality"  # "quality"（用語集・文脈重視） | "fast"（軽量・高速）


class LookupRequest(BaseModel):
    word: str


class SetModelRequest(BaseModel):
    translator: Optional[str] = None


class ReviewRequest(BaseModel):
    video_path:   str
    max_frames:   int   = Field(30, ge=1, le=120)
    min_interval: float = Field(5.0, ge=0.1, le=600.0)
    transcript:   str   = ""        # SRT由来のトランスクリプト（フロントから送信）
    frame_mode:   str   = "uniform"  # "uniform" | "scene"
    analysis_mode: str  = "speed"    # "speed" | "balanced" | "quality"
    output_lang: str = "ja"          # "ja" | "en"


class QuestionsRequest(BaseModel):
    video_path: str
    transcript: str = ""
    history: Optional[list[dict]] = None  # 直近の [{question, answer}, ...]
    bookmarks: Optional[list[dict]] = None  # ユーザーのしおり [{time_sec, title, comment}, ...]


class QARequest(BaseModel):
    video_path:   str
    question:     str
    max_frames:   int   = Field(20, ge=1, le=120)
    min_interval: float = Field(5.0, ge=0.1, le=600.0)
    transcript:   str   = ""
    frame_mode:   str   = "uniform"  # "uniform" | "scene"
    history:      Optional[list[dict]] = None  # 直近の [{question, answer}, ...]（マルチターン用）
    bookmarks:    Optional[list[dict]] = None  # ユーザーのしおり [{time_sec, title, comment}, ...]


class SetVLModelRequest(BaseModel):
    model_id: str


class RuntimeInstallRequest(BaseModel):
    component: str  # "llama-cpp" | "whisper" | "ffmpeg"
    asset: Optional[str] = None   # llama-cpp: インストールするビルドのアセット名（省略時は推奨）
    model: Optional[str] = None   # whisper: インストールするモデル名（省略時はデフォルト）


class LlamaSelectRequest(BaseModel):
    version: str  # runtime/llama-server/ 配下のフォルダ名


class WhisperSelectRequest(BaseModel):
    model: str  # WHISPER_MODELS のいずれか


class AsrEngineRequest(BaseModel):
    engine: str  # "faster-whisper" | "whisperx"


class UISettingsRequest(BaseModel):
    frame_mode: Optional[str] = None  # "uniform" | "scene"
    max_frames: Optional[int] = None
    analysis_mode: Optional[str] = None  # "speed" | "balanced" | "quality"
    volume: Optional[float] = None
    playback_rate: Optional[float] = None
    output_lang: Optional[str] = None  # "ja" | "en"
    subtitle_display: Optional[str] = None  # "below" | "overlay"
    subtitle_font: Optional[str] = None  # "noto" | "biz" | "yugothic" | "meiryo"
    translation_mode: Optional[str] = None  # "quality" | "fast"
    analysis_actions_expanded: Optional[bool] = None
    analysis_summary_expanded: Optional[bool] = None
    analysis_detail_expanded: Optional[bool] = None
    analysis_tags_expanded: Optional[bool] = None
    analysis_scenes_expanded: Optional[bool] = None
    show_analysis_panel: Optional[bool] = None
    show_qa_panel: Optional[bool] = None
    screenshot_format: Optional[str] = None  # "png" | "jpg"
    root_folder: Optional[str] = None  # ファイル一覧のルートフォルダ（"" でクリア）
    root_folder_history: Optional[list] = None  # 最近開いたルートフォルダ（新しい順・最大10件）
    show_file_panel: Optional[bool] = None
    file_panel_width: Optional[int] = None  # px（180〜600 にクランプ）
    seek_markers: Optional[bool] = None  # シークバーのチャプター・ブックマークマーカー表示


class TOCLoadRequest(BaseModel):
    video_path: str


class VideoInfoRequest(BaseModel):
    video_path: str


class CacheSaveRequest(BaseModel):
    video_path: str
    data: dict


class CacheLoadRequest(BaseModel):
    video_path: str


class CacheThumbnailRequest(BaseModel):
    video_path: str
    filename: str
    image_base64: str


class ThumbnailsGenerateRequest(BaseModel):
    video_path: str
    scenes: list[dict]  # [{start_sec: float, ...}, ...]（index 順で thumbnails/scene_N.jpg に保存）


class BookmarkThumbnailRequest(BaseModel):
    video_path: str
    time_sec: float
    name: str  # thumbnails/ 直下のファイル名（bookmark_*.jpg のみ許可）


class ThumbnailDeleteRequest(BaseModel):
    video_path: str
    filename: str  # thumbnails/ 直下のファイル名（bookmark_*.jpg のみ許可）


class ScreenshotRequest(BaseModel):
    video_path: str
    time_sec: float
    format: str = "png"  # "png" | "jpg"


class FolderListRequest(BaseModel):
    path: str


class FolderSearchRequest(BaseModel):
    root: str
    query: str


class FileRenameRequest(BaseModel):
    path: str
    new_name: str  # 動画は拡張子込み/なしどちらでも可（なしなら元の拡張子を維持）


def _cache_dir(video_path: str) -> Path:
    p = Path(video_path)
    return p.parent / (p.stem + ".cache")


# ---------- エンドポイント ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cancel")
def cancel_processing():
    """実行中の処理（文字起こし・補正・字幕生成・分析・Q&A）の中断を要求する。

    推論ループ側が中断フラグをポーリングし、安全に停止してから
    （モデルを使い終えてから）unload する。
    """
    cancel.request_cancel()
    return {"status": "ok"}


@app.get("/prompts")
def list_prompts():
    """システムプロンプト一覧を返す。editable 項目には presets / active を含む。"""
    items = _translator_prompts() + _review_prompts()
    for it in items:
        key = it["key"]
        editable = key in _prompts.EDITABLE_KEYS
        it["editable"] = editable
        if editable:
            entry = _prompts.list_for(key)
            it["presets"] = entry["presets"]
            it["active"] = entry["active"]
        else:
            it["presets"] = []
            it["active"] = "default"
    return {"prompts": items}


class PromptPresetRequest(BaseModel):
    key: str
    id: Optional[str] = None      # 省略=新規作成 / 指定=更新
    name: Optional[str] = None
    text: Optional[str] = None


class PromptActiveRequest(BaseModel):
    key: str
    active: str                   # preset id または "default"


class PromptDeleteRequest(BaseModel):
    key: str
    id: str


@app.post("/prompts/preset")
def save_prompt_preset(req: PromptPresetRequest):
    """プリセットを新規作成（id 省略）または更新（id 指定）する。"""
    try:
        if req.id:
            _prompts.update_preset(req.key, req.id, req.name, req.text)
            return {"status": "ok", "id": req.id}
        preset = _prompts.create_preset(req.key, req.name or "無題", req.text or "")
        return {"status": "ok", "id": preset["id"]}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/prompts/active")
def set_prompt_active(req: PromptActiveRequest):
    """選択中プリセットを切り替える（"default" でデフォルトに戻す）。"""
    try:
        _prompts.set_active(req.key, req.active)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@app.post("/prompts/delete")
def delete_prompt_preset(req: PromptDeleteRequest):
    """プリセットを削除する。削除対象が選択中ならデフォルトに戻る。"""
    try:
        _prompts.delete_preset(req.key, req.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@app.get("/system-stats")
def system_stats():
    cpu = psutil.cpu_percent(interval=None)
    vm  = psutil.virtual_memory()
    ram_used  = vm.used  / (1024 ** 3)
    ram_total = vm.total / (1024 ** 3)

    gpu_used  = None
    gpu_total = None
    vram_used  = None
    vram_total = None

    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem  = pynvml.nvmlDeviceGetMemoryInfo(handle)
        gpu_used   = util.gpu
        vram_used  = mem.used  / (1024 ** 3)
        vram_total = mem.total / (1024 ** 3)
    except Exception:
        pass

    return {
        "cpu":        round(cpu, 1),
        "ram_used":   round(ram_used, 2),
        "ram_total":  round(ram_total, 2),
        "gpu":        gpu_used,
        "vram_used":  round(vram_used, 2)  if vram_used  is not None else None,
        "vram_total": round(vram_total, 2) if vram_total is not None else None,
    }


@app.get("/ui-settings")
def get_ui_settings():
    s = load_settings()
    return {
        "frame_mode": s.get("frame_mode", "uniform"),
        "max_frames": s.get("max_frames", 30),
        "analysis_mode": s.get("analysis_mode", "speed"),
        "volume": s.get("volume", 1.0),
        "playback_rate": s.get("playback_rate", 1.0),
        "output_lang": s.get("output_lang", "ja"),
        "subtitle_display": s.get("subtitle_display", "below"),
        "subtitle_font": s.get("subtitle_font", "noto"),
        "translation_mode": s.get("translation_mode", "quality"),
        "analysis_actions_expanded": s.get("analysis_actions_expanded", True),
        "analysis_summary_expanded": s.get("analysis_summary_expanded", True),
        "analysis_detail_expanded": s.get("analysis_detail_expanded", False),
        "analysis_tags_expanded": s.get("analysis_tags_expanded", True),
        "analysis_scenes_expanded": s.get("analysis_scenes_expanded", True),
        "show_analysis_panel": s.get("show_analysis_panel", True),
        "show_qa_panel": s.get("show_qa_panel", True),
        "screenshot_format": s.get("screenshot_format", "png"),
        "root_folder": s.get("root_folder", ""),
        "root_folder_history": s.get("root_folder_history", []),
        "show_file_panel": s.get("show_file_panel", True),
        "file_panel_width": s.get("file_panel_width", 272),
        "seek_markers": s.get("seek_markers", True),
    }


@app.post("/ui-settings")
def post_ui_settings(req: UISettingsRequest):
    to_save = {}
    if req.frame_mode is not None: to_save["frame_mode"] = req.frame_mode
    if req.max_frames is not None: to_save["max_frames"] = req.max_frames
    if req.analysis_mode is not None: to_save["analysis_mode"] = req.analysis_mode
    if req.volume is not None: to_save["volume"] = max(0.0, min(1.0, float(req.volume)))
    if req.playback_rate is not None:
        allowed = {0.5, 0.75, 1.0, 1.25, 1.5}
        val = float(req.playback_rate)
        to_save["playback_rate"] = val if val in allowed else 1.0
    if req.output_lang is not None:
        to_save["output_lang"] = req.output_lang if req.output_lang in {"ja", "en"} else "ja"
    if req.subtitle_display is not None:
        to_save["subtitle_display"] = req.subtitle_display if req.subtitle_display in {"below", "overlay"} else "below"
    if req.subtitle_font is not None:
        to_save["subtitle_font"] = req.subtitle_font if req.subtitle_font in {"noto", "biz", "yugothic", "meiryo"} else "noto"
    if req.translation_mode is not None:
        to_save["translation_mode"] = req.translation_mode if req.translation_mode in {"quality", "fast"} else "quality"
    if req.analysis_actions_expanded is not None:
        to_save["analysis_actions_expanded"] = bool(req.analysis_actions_expanded)
    if req.analysis_summary_expanded is not None:
        to_save["analysis_summary_expanded"] = bool(req.analysis_summary_expanded)
    if req.analysis_detail_expanded is not None:
        to_save["analysis_detail_expanded"] = bool(req.analysis_detail_expanded)
    if req.analysis_tags_expanded is not None:
        to_save["analysis_tags_expanded"] = bool(req.analysis_tags_expanded)
    if req.analysis_scenes_expanded is not None:
        to_save["analysis_scenes_expanded"] = bool(req.analysis_scenes_expanded)
    if req.show_analysis_panel is not None:
        to_save["show_analysis_panel"] = bool(req.show_analysis_panel)
    if req.show_qa_panel is not None:
        to_save["show_qa_panel"] = bool(req.show_qa_panel)
    if req.screenshot_format is not None:
        to_save["screenshot_format"] = req.screenshot_format if req.screenshot_format in {"png", "jpg"} else "png"
    if req.root_folder is not None:
        to_save["root_folder"] = str(req.root_folder)
    if req.root_folder_history is not None:
        to_save["root_folder_history"] = [str(p) for p in req.root_folder_history if str(p).strip()][:10]
    if req.show_file_panel is not None:
        to_save["show_file_panel"] = bool(req.show_file_panel)
    if req.file_panel_width is not None:
        to_save["file_panel_width"] = max(180, min(600, int(req.file_panel_width)))
    if req.seek_markers is not None:
        to_save["seek_markers"] = bool(req.seek_markers)
    if to_save:
        save_settings(to_save)
    return {"status": "ok"}


@app.get("/runtime/status")
async def runtime_status():
    """外部ランタイム（llama-cpp / Whisper モデル / ffmpeg）のインストール状態を返す"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, runtime_manager.get_status, asr.model_id, asr_engine)


@app.get("/runtime/llama/builds")
async def runtime_llama_builds():
    """llama.cpp 最新リリースの Windows ビルド一覧（推奨フラグ付き）を返す"""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, runtime_manager.list_llama_builds)
    except Exception as e:
        raise HTTPException(502, f"リリース情報の取得に失敗しました: {e}")


@app.post("/runtime/llama/select")
async def runtime_llama_select(req: LlamaSelectRequest):
    """使用する llama-server のバージョン（フォルダ）を切り替える"""
    if req.version not in runtime_manager.installed_llama_versions():
        raise HTTPException(400, f"インストールされていないバージョンです: {req.version}")
    save_settings({"llama_version": req.version})
    return {"status": "ok", "version": req.version}


@app.post("/runtime/whisper/select")
async def runtime_whisper_select(req: WhisperSelectRequest):
    """使用する Whisper モデルを切り替える（次回の文字起こしから反映）"""
    if req.model not in runtime_manager.WHISPER_MODEL_IDS:
        raise HTTPException(400, f"未対応の Whisper モデルです: {req.model}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, asr.set_model_id, req.model)
    save_settings({"whisper_model": req.model})
    return {"status": "ok", "model": req.model}


@app.post("/runtime/asr/engine")
async def runtime_asr_engine(req: AsrEngineRequest):
    """文字起こしエンジンを切り替える（次回の文字起こしから反映）"""
    global asr_engine
    if req.engine not in ASR_ENGINES:
        raise HTTPException(400, f"未対応のエンジンです: {req.engine}")
    asr_engine = req.engine
    save_settings({"asr_engine": req.engine})
    return {"status": "ok", "engine": req.engine}


@app.post("/runtime/install")
async def runtime_install(req: RuntimeInstallRequest):
    """
    ランタイムをダウンロード・インストールする（SSE）。

    Events:
      {"status": "resolving", "message": str}          ← リリース情報の取得中（llama-cpp）
      {"status": "downloading", "asset": str, "received": int, "total": int, "percent": float|null}
      {"status": "extracting", "message": str}
      {"status": "canceled"}                            ← POST /cancel で中断（whisper は非対応）
      {"status": "done", "runtime": {...}}              ← 完了後の /runtime/status と同形式
      {"status": "error", "message": str}
    """
    if req.component not in runtime_manager.COMPONENTS:
        raise HTTPException(400, f"未対応のコンポーネント: {req.component}")

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()
        q: queue.Queue[tuple[str, str]] = queue.Queue()

        def run_install():
            try:
                status = runtime_manager.install(
                    req.component,
                    lambda ev: q.put(("progress", json.dumps(ev, ensure_ascii=False))),
                    asset=req.asset,
                    model=req.model,
                )
                q.put(("done", json.dumps(status, ensure_ascii=False)))
            except cancel.CanceledError:
                q.put(("canceled", ""))
            except Exception as e:
                q.put(("error", str(e)))

        th = threading.Thread(target=run_install, daemon=True)
        th.start()

        while True:
            kind, payload = await loop.run_in_executor(None, q.get)
            if kind == "progress":
                yield sse(json.loads(payload))
            elif kind == "done":
                yield sse({"status": "done", "runtime": json.loads(payload)})
                break
            elif kind == "canceled":
                yield sse_canceled()
                break
            else:
                yield sse({"status": "error", "message": payload})
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/models")
def get_models():
    """利用可能なモデルの一覧と現在の選択・ロード状態を返す"""
    translator_models = available_translator_models()
    return {
        "translator": {
            "current":   translator.model_id,
            "loaded":    translator.loaded,
            "available": translator_models,
        },
    }


@app.post("/models")
def set_models(req: SetModelRequest):
    """翻訳・辞書モデルを切り替える（ロード済みの場合は即アンロード・次回使用時に再ロード）"""
    valid_ids = {m["id"] for m in available_translator_models() if m.get("exists")}
    to_save = {}
    if req.translator is not None:
        if req.translator not in valid_ids:
            raise HTTPException(400, f"無効なモデルID: {req.translator}")
        translator.set_model_id(req.translator)
        to_save["translator_model"] = translator.model_id
    if to_save:
        save_settings(to_save)
    return {"status": "ok", "translator": translator.model_id}


@app.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    """
    動画を文字起こしして original SRT を生成する。
    進捗は SSE でストリーミング。

    Events:
      {"status": "loading_model"}          ← ASR モデルが未ロードの場合のみ
      {"status": "extracting_audio"}
      {"status": "loading_align_model", "language": str, "model": str}
                                           ← エンジンが whisperx のときのみ（初回は DL 込み）
      {"status": "aligning", "current": int, "total": int}
                                           ← エンジンが whisperx のときのみ
      {"status": "align_warning", "message": str}
                                           ← 未対応言語・整列失敗（元の時刻で続行）
      {"status": "saving_srt", "segments": int}
      {"status": "done", "srt_path": str, "segments": int}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()

        # ASR モデルが未ロードならロード（使用後は解放するため毎回必要になることがある）
        if asr.model is None:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, asr.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        yield sse({"status": "extracting_audio"})
        try:
            segments, detected_lang = await loop.run_in_executor(
                None, asr.transcribe, str(video_path), req.language
            )
        except cancel.CanceledError:
            yield sse_canceled()
            return
        except Exception as e:
            yield sse({"status": "error", "message": str(e)})
            return
        finally:
            # 文字起こし完了（成功・失敗・中断問わず）後に VRAM を解放
            await loop.run_in_executor(None, asr.unload)

        # WhisperX 方式: wav2vec2 強制アライメントで時刻を精密化（backend/align.py）。
        # 失敗しても文字起こし全体は失敗させず、元の時刻のまま続行する。
        if asr_engine == ENGINE_WHISPERX and segments:
            lang = req.language or detected_lang
            align_model = Aligner.model_for_language(lang)
            if align_model is None:
                yield sse({
                    "status": "align_warning",
                    "message": f"言語 '{lang}' はアライメント未対応のため元のタイムスタンプを使用します",
                })
            else:
                yield sse({"status": "loading_align_model", "language": lang, "model": align_model})
                try:
                    # whisper は unload 済み → wav2vec2 を逐次ロード（VRAM 共有のため）
                    await loop.run_in_executor(None, aligner.load, lang)
                    audio = await loop.run_in_executor(None, load_align_audio, str(video_path))
                    total = len(segments)
                    yield sse({"status": "aligning", "current": 0, "total": total})
                    aligned, warned = [], 0
                    for i, seg in enumerate(segments):
                        cancel.raise_if_canceled()
                        new_seg, warn = await loop.run_in_executor(
                            None, aligner.align_segment, seg, audio
                        )
                        aligned.append(new_seg)
                        if warn:
                            warned += 1
                        yield sse({"status": "aligning", "current": i + 1, "total": total})
                    segments = aligned
                    if warned:
                        yield sse({
                            "status": "align_warning",
                            "message": f"{warned}/{total} セグメントは整列できず元のタイムスタンプを使用しました",
                        })
                except cancel.CanceledError:
                    yield sse_canceled()
                    return
                except Exception as e:
                    yield sse({
                        "status": "align_warning",
                        "message": f"アライメントに失敗したため元のタイムスタンプを使用します: {e}",
                    })
                finally:
                    await loop.run_in_executor(None, aligner.unload)

        segments = split_long_segments(segments)
        yield sse({"status": "saving_srt", "segments": len(segments)})

        srt_content = segments_to_srt(segments)
        out_path = make_output_path(str(video_path), "original")
        save_srt(srt_content, out_path)

        yield sse({"status": "done", "srt_path": out_path, "segments": len(segments)})

    return StreamingResponse(stream(), media_type="text/event-stream")


def _video_context_for_srt(srt_path: Path) -> str:
    """SRT に対応する分析キャッシュ（data.json）の meta から翻訳用の文脈文字列を作る。

    SRT は通常 `{動画名}.cache/` 内にあるので同フォルダの data.json を探す。
    旧配置（動画の横）の SRT は `{動画名}.cache/data.json` にフォールバック。
    キャッシュが無い・壊れている場合は空文字列（翻訳は文脈なしで続行）。
    """
    candidates = [srt_path.parent / "data.json"]
    for suffix in (".original.srt", ".corrected.srt", ".japanese.srt"):
        if srt_path.name.endswith(suffix):
            base = srt_path.name[: -len(suffix)]
            candidates.append(srt_path.parent / f"{base}.cache" / "data.json")
            break
    for c in candidates:
        try:
            if not c.exists():
                continue
            data = json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            continue
        parts = []
        if meta.get("genre"):
            parts.append(f"Genre: {meta['genre']}")
        if meta.get("summary"):
            parts.append(f"Summary: {meta['summary']}")
        tags = meta.get("tags")
        if isinstance(tags, list) and tags:
            parts.append("Topics: " + ", ".join(str(t) for t in tags[:10]))
        return "\n".join(parts)
    return ""


def _sample_lines_for_glossary(segments: list[dict], max_chars: int = 4000) -> str:
    """用語集抽出用に、字幕全編から等間隔サンプリングしたテキストを作る。"""
    texts = [str(s.get("text") or "") for s in segments if str(s.get("text") or "").strip()]
    if not texts:
        return ""
    total = sum(len(t) + 1 for t in texts)
    if total <= max_chars:
        return "\n".join(texts)
    approx = total / len(texts)
    budget = max(1, int(max_chars / approx))
    step = len(texts) / budget
    picked = [texts[int(k * step)] for k in range(budget)]
    return "\n".join(picked)[:max_chars]


@app.post("/translate")
async def translate(req: TranslateRequest):
    """
    原文 SRT を日本語に翻訳して japanese SRT を生成する。
    バッチ単位で翻訳し、進捗を SSE でストリーミング。

    mode:
      quality … 用語集生成＋動画文脈＋文脈5ペア＋先読み2行（既定）
      fast    … 用語集・動画文脈を省略し、文脈2ペア＋先読み1行・バッチ12行の軽量動作

    Events:
      {"status": "loading_model"}          ← Translator モデルが未ロードの場合のみ
      {"status": "building_glossary"}      ← 用語集を生成中（quality のみ）
      {"status": "glossary_done", "terms": int}
      {"status": "translating", "current": int, "total": int}
      {"status": "translate_warning", "message": str}  ← 打ち切り・バッチ検証失敗等
      {"status": "canceled"}
      {"status": "done", "srt_path": str, "total": int}
      {"status": "error", "message": str}
    """
    srt_path = Path(req.srt_path)
    if not srt_path.exists():
        raise HTTPException(404, f"SRT ファイルが見つかりません: {srt_path}")

    if req.mode not in {"quality", "fast"}:
        raise HTTPException(400, f"無効な mode: {req.mode}")

    segments = srt_file_to_segments(str(srt_path))
    total = len(segments)
    if total == 0:
        raise HTTPException(400, "字幕が0件のため翻訳できません。先に文字起こしを実行してください。")

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()
        translated = []

        # Translator が未ロードならロード（翻訳終了後に必ずアンロード）
        if not translator.loaded:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, translator.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        # 動画の文脈（分析キャッシュの meta）と用語集を system prompt 追記として組み立てる。
        # fast モードでは両方を省略して呼び出しを1回減らし、プロンプトも小さくする。
        extra_system = ""
        if req.mode == "quality":
            video_context = _video_context_for_srt(srt_path)
            glossary: list[dict] = []
            yield sse({"status": "building_glossary"})
            try:
                glossary = await loop.run_in_executor(
                    None, translator.build_glossary, _sample_lines_for_glossary(segments)
                )
            except cancel.CanceledError:
                await loop.run_in_executor(None, translator.unload)
                yield sse_canceled()
                return
            except Exception as e:
                yield sse({"status": "translate_warning", "message": f"用語集の生成に失敗したためスキップします: {e}"})
            if glossary:
                yield sse({"status": "glossary_done", "terms": len(glossary)})
            extra_system = Translator.compose_translation_system_extra(video_context, glossary)

        yield sse({"status": "translating", "current": 0, "total": total})

        # 直前 CONTEXT_WINDOW 件の (原文, 翻訳) ペアと、次 LOOKAHEAD 行の原文を参考文脈に使う。
        # BATCH_SIZE 行ずつ json_schema バッチ翻訳し、検証失敗時はその区間だけ行単位に落とす。
        if req.mode == "fast":
            CONTEXT_WINDOW, LOOKAHEAD, BATCH_SIZE = 2, 1, 12
        else:
            CONTEXT_WINDOW, LOOKAHEAD, BATCH_SIZE = 5, 2, 8
        context_history: list[tuple[str, str]] = []

        try:
            i = 0
            while i < total:
                batch = segments[i:i + BATCH_SIZE]
                lines = [s["text"] for s in batch]
                ctx = context_history[-CONTEXT_WINDOW:] or None
                batch_lookahead = [
                    s["text"] for s in segments[i + len(batch): i + len(batch) + LOOKAHEAD]
                ] or None

                batch_results = None
                gen_meta: dict = {}
                if len(batch) > 1:
                    try:
                        batch_results, gen_meta = await loop.run_in_executor(
                            None, translator.translate_batch_ex, lines, ctx, batch_lookahead, extra_system
                        )
                    except cancel.CanceledError:
                        yield sse_canceled()
                        return
                    except Exception as e:
                        print(f"[translate] バッチ翻訳エラー → 行単位にフォールバック: {e}")
                        batch_results = None

                if batch_results is not None:
                    if gen_meta.get("finish_reason") == "length":
                        yield sse({
                            "status": "translate_warning",
                            "message": f"{i + 1}〜{i + len(batch)}行目の訳がトークン上限で打ち切られた可能性があります",
                        })
                    for seg, jp_text in zip(batch, batch_results):
                        if not str(jp_text).strip():
                            jp_text = seg["text"]
                        context_history.append((seg["text"], jp_text))
                        translated.append({**seg, "text": jp_text})
                    i += len(batch)
                    yield sse({"status": "translating", "current": i, "total": total})
                    continue

                # 行単位フォールバック（バッチ検証失敗・バッチ非対応経路）
                if len(batch) > 1:
                    yield sse({
                        "status": "translate_warning",
                        "message": f"{i + 1}〜{i + len(batch)}行目はバッチ翻訳の検証に失敗したため行単位で翻訳します",
                    })
                for j, seg in enumerate(batch):
                    ctx = context_history[-CONTEXT_WINDOW:] or None
                    lookahead = [
                        s["text"] for s in segments[i + j + 1: i + j + 1 + LOOKAHEAD]
                    ] or None
                    try:
                        jp_text, gen_meta = await loop.run_in_executor(
                            None, translator.translate_ex, seg["text"], ctx, lookahead, extra_system
                        )
                    except cancel.CanceledError:
                        yield sse_canceled()
                        return
                    except Exception as e:
                        yield sse({"status": "error", "message": str(e)})
                        return

                    if gen_meta.get("finish_reason") == "length":
                        yield sse({
                            "status": "translate_warning",
                            "message": f"{i + j + 1}行目の訳がトークン上限で打ち切られた可能性があります",
                        })

                    if not str(jp_text).strip():
                        jp_text = seg["text"]

                    context_history.append((seg["text"], jp_text))
                    translated.append({**seg, "text": jp_text})
                    yield sse({"status": "translating", "current": i + j + 1, "total": total})
                i += len(batch)
        finally:
            # 翻訳完了（成功・失敗・中断問わず）後に VRAM を解放
            await loop.run_in_executor(None, translator.unload)

        # japanese.srt を保存
        # .original.srt → .japanese.srt、それ以外は .japanese.srt を付加
        stem = srt_path.name
        if stem.endswith(".original.srt"):
            out_name = stem.replace(".original.srt", ".japanese.srt")
        else:
            out_name = srt_path.stem + ".japanese.srt"
        out_path = str(srt_path.parent / out_name)

        save_srt(segments_to_srt(translated), out_path)
        yield sse({"status": "done", "srt_path": out_path, "total": total})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/refine")
async def refine(req: TranslateRequest):
    """
    原文 SRT を翻訳モデルで保守的に補正し corrected SRT を生成する。
    タイムスタンプはそのまま保持し、各セグメントのテキストのみ補正する。

    Events:
      {"status": "loading_model"}          ← Translator モデルが未ロードの場合のみ
      {"status": "refining", "current": int, "total": int}
      {"status": "done", "srt_path": str, "total": int}
      {"status": "error", "message": str}
    """
    srt_path = Path(req.srt_path)
    if not srt_path.exists():
        raise HTTPException(404, f"SRT ファイルが見つかりません: {srt_path}")

    segments = srt_file_to_segments(str(srt_path))
    total = len(segments)
    if total == 0:
        raise HTTPException(400, "字幕が0件のため補正できません。先に文字起こしを実行してください。")

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()
        refined = []

        # Translator が未ロードならロード（補正終了後に必ずアンロード）
        if not translator.loaded:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, translator.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        yield sse({"status": "refining", "current": 0, "total": total})

        # 直前 CONTEXT_WINDOW 件の (原文, 補正) ペアを文脈として保持（語の表記揺れ抑制）
        CONTEXT_WINDOW = 5
        context_history: list[tuple[str, str]] = []

        try:
            for i, seg in enumerate(segments):
                ctx = context_history[-CONTEXT_WINDOW:] or None
                try:
                    fixed = await loop.run_in_executor(
                        None, translator.refine, seg["text"], ctx
                    )
                except cancel.CanceledError:
                    yield sse_canceled()
                    return
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return

                if not str(fixed).strip():
                    fixed = seg["text"]

                context_history.append((seg["text"], fixed))
                refined.append({**seg, "text": fixed})
                yield sse({"status": "refining", "current": i + 1, "total": total})
        finally:
            # 補正完了（成功・失敗・中断問わず）後に VRAM を解放
            await loop.run_in_executor(None, translator.unload)

        # corrected.srt を保存
        # .original.srt → .corrected.srt、それ以外は .corrected.srt を付加
        stem = srt_path.name
        if stem.endswith(".original.srt"):
            out_name = stem.replace(".original.srt", ".corrected.srt")
        else:
            out_name = srt_path.stem + ".corrected.srt"
        out_path = str(srt_path.parent / out_name)

        save_srt(segments_to_srt(refined), out_path)
        yield sse({"status": "done", "srt_path": out_path, "total": total})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/lookup")
async def lookup(req: LookupRequest):
    """
    英単語の日本語定義を返す。
    Translator は translate エンドポイントでロード済みであれば即応答、
    未ロードの場合は _ensure_loaded() が自動的にロードする。

    Response:
      {"word": str, "definition": str}
    """
    word = req.word.strip()
    if not word:
        raise HTTPException(400, "word が空です")

    loop = asyncio.get_event_loop()
    definition = await loop.run_in_executor(None, translator.lookup, word)
    return {"word": word, "definition": definition}


# ================================================================
# 動画レビュー（Qwen3.5 GGUF / llama.cpp）
# ================================================================

@app.get("/review/models")
def get_vl_models():
    """利用可能な動画レビュー用モデルの一覧と現在の選択・ロード状態を返す"""
    review_models = available_review_models()
    return {
        "current":   video_reviewer.model_id,
        "loaded":    video_reviewer.loaded,
        "translator_model_id": translator.model_id,
        "available": review_models,
    }


@app.post("/review/models")
def set_vl_model(req: SetVLModelRequest):
    """動画レビュー用モデルを切り替える"""
    valid_ids = {m["id"] for m in available_review_models() if m.get("exists")}
    if req.model_id not in valid_ids:
        raise HTTPException(400, f"無効なモデルID: {req.model_id}")
    video_reviewer.set_model_id(req.model_id)
    translator.set_model_id(req.model_id)
    save_settings({"vl_model": video_reviewer.model_id, "translator_model": translator.model_id})
    return {"status": "ok", "model_id": video_reviewer.model_id, "translator_model_id": translator.model_id}


@app.post("/review/load")
async def load_vl_model():
    """選択中のVLモデルを明示的にVRAMへロードする。"""
    if not video_reviewer.model_id:
        raise HTTPException(400, "モデルが選択されていません")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, video_reviewer.load)
    return {"status": "ok", "model_id": video_reviewer.model_id, "loaded": video_reviewer.loaded}


@app.post("/review/unload")
async def unload_vl_model():
    """動画レビュー用モデルを手動でアンロードして VRAM を解放する"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, video_reviewer.unload)
    await loop.run_in_executor(None, translator.unload)
    return {"status": "ok"}


@app.post("/review/analyze")
async def review_analyze(req: ReviewRequest):
    """
    動画を分析してサマリー・シーン・タグを返す（SSE）。

    Events:
      {"status": "loading_model"}
      {"status": "extracting_frames"}
      {"status": "analyzing", "count": int, "interval": float, "duration": float}
      {"status": "done", "result": dict, "meta": dict}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if req.frame_mode not in {"uniform", "scene"}:
        raise HTTPException(400, f"無効な frame_mode: {req.frame_mode}")
    _validate_analysis_mode(req.analysis_mode)
    _validate_output_lang(req.output_lang)

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()
        transcript = req.transcript
        plan = _analysis_plan(req.analysis_mode, req.max_frames)

        try:
            if not video_reviewer.loaded:
                yield sse({"status": "loading_model"})
                try:
                    await loop.run_in_executor(None, video_reviewer.load)
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return

            yield sse({"status": "extracting_frames", "pass": "coarse"})
            try:
                if req.frame_mode == "scene":
                    frames, meta = await loop.run_in_executor(
                        None, video_reviewer.extract_frames_scene, str(video_path), int(plan["coarse_frames"])
                    )
                else:
                    frames, meta = await loop.run_in_executor(
                        None, video_reviewer.extract_frames, str(video_path), int(plan["coarse_frames"]), req.min_interval
                    )
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            yield sse({
                "status": "analyzing",
                **{k: v for k, v in meta.items() if k != "timestamps"},
                "pass": "coarse",
                "analysis_mode": req.analysis_mode,
            })
            try:
                coarse_result = await loop.run_in_executor(
                    None, video_reviewer.analyze_frames, frames, transcript, meta.get("timestamps", []), req.output_lang
                )
            except cancel.CanceledError:
                yield sse_canceled()
                return
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            coarse_gen_meta = coarse_result.pop("_analysis_meta", {}) if isinstance(coarse_result, dict) else {}
            for w in _analysis_warnings(coarse_gen_meta, "coarse"):
                yield sse(w)

            duration = float(meta.get("duration") or 0.0)
            _snap_scene_timestamps(coarse_result, meta.get("timestamps") or [], meta.get("interval"))
            entries = _build_toc_entries(coarse_result, duration)

            if int(plan["refine_limit"]) > 0 and entries:
                targets = _select_refine_targets(entries, duration, plan)
                refined_entries: list[dict] = []
                for idx, ch in enumerate(targets, start=1):
                    start = float(ch.get("start_sec", 0.0))
                    end = float(ch.get("end_sec", start))
                    if end - start < 10.0:
                        continue
                    yield sse({
                        "status": "extracting_frames",
                        "pass": "refine",
                        "current": idx,
                        "total": len(targets),
                        "range": {"start_sec": start, "end_sec": end},
                    })
                    try:
                        r_frames, r_meta = await loop.run_in_executor(
                            None,
                            video_reviewer.extract_frames_between,
                            str(video_path),
                            start,
                            end,
                            int(plan["refine_frames"]),
                            req.min_interval,
                        )
                        r_transcript = _slice_transcript(transcript, start, end)
                        r_result = await loop.run_in_executor(
                            None, video_reviewer.analyze_frames, r_frames, r_transcript, r_meta.get("timestamps", []), req.output_lang, True
                        )
                        r_gen_meta = r_result.pop("_analysis_meta", {}) if isinstance(r_result, dict) else {}
                        for w in _analysis_warnings(r_gen_meta, "refine"):
                            yield sse({**w, "current": idx, "total": len(targets)})
                        _snap_scene_timestamps(r_result, r_meta.get("timestamps") or [], r_meta.get("interval"))
                        rel_entries = _entries_from_refine_result(
                            r_result, start, end, duration
                        )
                        refined_entries.extend(rel_entries)
                    except cancel.CanceledError:
                        raise
                    except Exception as e:
                        yield sse({
                            "status": "refine_warning",
                            "message": f"refine失敗のため coarse 結果で継続します: {e}",
                            "current": idx,
                            "total": len(targets),
                            "range": {"start_sec": start, "end_sec": end},
                        })
                        continue
                entries = _merge_toc_entries(entries + refined_entries, duration)
            result = {
                "summary": coarse_result.get("summary", ""),
                "detail": coarse_result.get("detail", ""),
                "genre": coarse_result.get("genre", "不明"),
                "tags": coarse_result.get("tags", []),
                "scenes": [
                    {
                        "timestamp": e.get("timestamp", "0:00"),
                        "label": e.get("title", ""),
                        "description": e.get("summary", ""),
                        "start_sec": e.get("start_sec", 0.0),
                    }
                    for e in entries
                ],
            }

            # キャッシュキーは実際の抽出パラメータで作る（req.max_frames ではなく
            # coarse_frames。以前は「30枚」と称して間引き済みフレームを保存していた）
            video_reviewer.cache_frames(str(video_path), req.frame_mode, int(plan["coarse_frames"]), req.min_interval, frames, meta)
            yield sse({"status": "done", "result": result, "meta": meta})
        except cancel.CanceledError:
            yield sse_canceled()
            return
        except Exception as e:
            yield sse({"status": "error", "message": str(e)})
            return
    return StreamingResponse(stream(), media_type="text/event-stream")


def _video_meta_text(video_path: str) -> str:
    """分析キャッシュの meta からおすすめ質問生成用の文脈テキストを作る。"""
    try:
        data = json.loads((_cache_dir(video_path) / "data.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict):
        return ""
    parts = []
    if meta.get("genre"):
        parts.append(f"ジャンル: {meta['genre']}")
    if meta.get("summary"):
        parts.append(f"概要: {meta['summary']}")
    if meta.get("detail"):
        parts.append(f"詳細: {str(meta['detail'])[:600]}")
    tags = meta.get("tags")
    if isinstance(tags, list) and tags:
        parts.append("タグ: " + ", ".join(str(t) for t in tags[:10]))
    return "\n".join(parts)


@app.post("/review/questions")
async def review_questions(req: QuestionsRequest):
    """チャット用のおすすめ質問を生成する（非SSE・軽量）。

    モデル未ロード時はロードを誘発せず空リストを返す（フロントは
    キャッシュ済みの質問・固定テンプレートにフォールバックする）。
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if not video_reviewer.loaded:
        return {"questions": []}
    loop = asyncio.get_event_loop()
    try:
        questions = await loop.run_in_executor(
            None,
            lambda: video_reviewer.suggest_questions(
                _video_meta_text(str(video_path)),
                req.transcript,
                req.history,
                bookmarks=req.bookmarks,
            ),
        )
    except Exception as e:
        print(f"[QA] おすすめ質問の生成に失敗: {e}")
        return {"questions": []}
    return {"questions": questions}


@app.post("/review/qa")
async def review_qa(req: QARequest):
    """
    動画に対する質問に回答する（SSE）。

    Events:
      {"status": "loading_model"}
      {"status": "extracting_frames"}
      {"status": "answering", "count": int}
      {"status": "answer_delta", "delta": str}
      {"status": "done", "answer": str, "meta": dict}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if not req.question.strip():
        raise HTTPException(400, "question が空です")

    async def stream():
        loop = asyncio.get_event_loop()
        cancel.clear_cancel()

        if not video_reviewer.loaded:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, video_reviewer.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        # フレームの取得順: ①メモリキャッシュ → ②分析キャッシュのサムネール（ffmpeg 不要・即開始）
        # → ③ffmpeg 抽出（並列シーク）。②③の結果はメモリキャッシュに載せて次の質問から即応答。
        cached = video_reviewer.get_cached_frames(str(video_path), req.frame_mode, req.max_frames, req.min_interval)
        if cached is not None:
            frames, meta = cached
        else:
            frames = meta = None
            try:
                restored = await loop.run_in_executor(
                    None, video_reviewer.load_frames_from_analysis_cache, str(video_path)
                )
                if restored is not None:
                    frames, meta = restored
            except Exception as e:
                print(f"[QA] 分析キャッシュの再利用に失敗（抽出にフォールバック）: {e}")
            if frames is None:
                yield sse({"status": "extracting_frames"})
                try:
                    if req.frame_mode == "scene":
                        frames, meta = await loop.run_in_executor(
                            None,
                            video_reviewer.extract_frames_scene,
                            str(video_path),
                            req.max_frames,
                        )
                    else:
                        frames, meta = await loop.run_in_executor(
                            None,
                            video_reviewer.extract_frames,
                            str(video_path),
                            req.max_frames,
                            req.min_interval,
                        )
                except cancel.CanceledError:
                    yield sse_canceled()
                    return
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return
            video_reviewer.cache_frames(str(video_path), req.frame_mode, req.max_frames, req.min_interval, frames, meta)

        yield sse({"status": "answering", "count": meta["count"]})
        q: queue.Queue[tuple[str, str]] = queue.Queue()

        def run_stream():
            parts: list[str] = []
            try:
                answer_meta = video_reviewer.qa_frames_stream_with_meta(
                    frames,
                    req.question,
                    req.transcript,
                    meta.get("timestamps", []),
                    on_delta=lambda delta: (parts.append(delta), q.put(("answer_delta", delta))),
                    history=req.history,
                    bookmarks=req.bookmarks,
                )
                answer = video_reviewer._clean_generated_text("".join(parts))
                q.put(("done", json.dumps({"answer": answer, "meta": answer_meta}, ensure_ascii=False)))
            except cancel.CanceledError:
                q.put(("canceled", ""))
            except Exception as e:
                q.put(("error", str(e)))

        th = threading.Thread(target=run_stream, daemon=True)
        th.start()

        while True:
            status, payload = await loop.run_in_executor(None, q.get)
            if status == "answer_delta":
                yield sse({"status": "answer_delta", "delta": payload})
                await asyncio.sleep(0)
            elif status == "done":
                done_payload = json.loads(payload)
                done_meta = done_payload.get("meta") or {}
                if done_meta.get("finish_reason") == "length":
                    yield sse({"status": "qa_warning", "message": "回答がトークン上限で打ち切られました"})
                used, requested = done_meta.get("frames_used"), done_meta.get("frames_requested")
                if isinstance(used, int) and isinstance(requested, int) and used < requested:
                    yield sse({"status": "qa_warning", "message": f"フレームを {requested}枚 → {used}枚 に縮小して回答しました"})
                yield sse({"status": "done", "answer": done_payload.get("answer", ""), "meta": done_meta})
                break
            elif status == "canceled":
                yield sse_canceled()
                break
            else:
                yield sse({"status": "error", "message": payload})
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/review/toc/load")
def review_load_toc(req: TOCLoadRequest):
    """旧形式（動画の横の .toc.json）を読み込む。後方互換の読み取り専用。"""
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    toc_path = video_path.with_suffix(".toc.json")
    if not toc_path.exists():
        raise HTTPException(404, f"目次ファイルが見つかりません: {toc_path}")
    try:
        data = json.loads(toc_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"目次ファイルの読み込みに失敗: {e}")
    return {"status": "ok", "toc_path": str(toc_path), "data": data}


# ---------- キャッシュ API ----------

@app.post("/cache/save")
def cache_save(req: CacheSaveRequest):
    """動画キャッシュフォルダに data.json を保存する。"""
    cache = _cache_dir(req.video_path)
    cache.mkdir(exist_ok=True)
    data = dict(req.data)
    data["video"] = Path(req.video_path).name
    data["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        (cache / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"キャッシュの保存に失敗: {e}")
    return {"status": "ok", "cache_dir": str(cache)}


@app.post("/cache/load")
def cache_load(req: CacheLoadRequest):
    """動画キャッシュフォルダから data.json を読み込む。"""
    data_file = _cache_dir(req.video_path) / "data.json"
    if not data_file.exists():
        raise HTTPException(404, "キャッシュが見つかりません")
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"キャッシュの読み込みに失敗: {e}")
    return {"status": "ok", "data": data}


@app.post("/cache/thumbnails/generate")
async def cache_thumbnails_generate(req: ThumbnailsGenerateRequest):
    """シーン開始時刻のサムネールをサーバー側で生成して thumbnails/ に保存する。

    フロントの <video> キャプチャ（seeked と描画のレースで前チャプターの絵や
    重複が発生していた）の置き換え。ffmpeg の入力シークで正確なフレームを取る。
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if len(req.scenes) > 100:
        raise HTTPException(400, "scenes が多すぎます（最大100）")
    loop = asyncio.get_event_loop()
    try:
        thumbnails = await loop.run_in_executor(
            None, video_reviewer.generate_scene_thumbnails, str(video_path), req.scenes
        )
    except Exception as e:
        raise HTTPException(500, f"サムネール生成に失敗: {e}")
    return {"status": "ok", "thumbnails": thumbnails}


@app.post("/video/info")
async def video_info(req: VideoInfoRequest):
    """動画のスペック（解像度・コーデック・ビットレート等）を ffprobe で返す。"""
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, video_reviewer.probe_video_info, str(video_path))
    except Exception as e:
        raise HTTPException(500, f"動画情報の取得に失敗: {e}")
    return {"status": "ok", "info": info}


@app.post("/screenshot")
async def screenshot(req: ScreenshotRequest):
    """再生位置のフレームを {動画名}_screenshot/ フォルダにフル解像度で保存する（F12）。"""
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    fmt = req.format if req.format in {"png", "jpg"} else "png"
    loop = asyncio.get_event_loop()
    try:
        saved = await loop.run_in_executor(
            None, video_reviewer.save_screenshot, str(video_path), req.time_sec, fmt
        )
    except Exception as e:
        raise HTTPException(500, f"スクリーンショットの保存に失敗: {e}")
    return {"status": "ok", "path": saved}


@app.post("/cache/thumbnail")
def cache_thumbnail(req: CacheThumbnailRequest):
    """base64 画像をキャッシュフォルダの thumbnails/ に保存する。"""
    # パストラバーサル対策: filename はファイル名そのもの（パス区切りなし）のみ許可
    if req.filename != Path(req.filename).name or req.filename in {"", ".", ".."}:
        raise HTTPException(400, f"不正なファイル名: {req.filename}")
    thumb_dir = _cache_dir(req.video_path) / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    b64 = req.image_base64
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        (thumb_dir / req.filename).write_bytes(base64.b64decode(b64))
    except Exception as e:
        raise HTTPException(500, f"サムネールの保存に失敗: {e}")
    return {"status": "ok"}


def _validate_bookmark_thumb_name(name: str) -> None:
    """ブックマークサムネールのファイル名を検証する（パストラバーサル・シーン画像の破壊を防ぐ）。"""
    if name != Path(name).name or not name.startswith("bookmark_") or not name.endswith(".jpg"):
        raise HTTPException(400, f"不正なファイル名: {name}")


@app.post("/cache/bookmarks/thumbnail")
async def cache_bookmark_thumbnail(req: BookmarkThumbnailRequest):
    """ブックマーク時刻のサムネールをサーバー側で生成して thumbnails/ に保存する。

    シーンサムネールと同じ ffmpeg 入力シーク方式（video_reviewer.generate_bookmark_thumbnail）。
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    _validate_bookmark_thumb_name(req.name)
    loop = asyncio.get_event_loop()
    try:
        thumbnail = await loop.run_in_executor(
            None, video_reviewer.generate_bookmark_thumbnail, str(video_path), req.time_sec, req.name
        )
    except Exception as e:
        raise HTTPException(500, f"サムネール生成に失敗: {e}")
    return {"status": "ok", "thumbnail": thumbnail}


@app.post("/cache/thumbnail/delete")
def cache_thumbnail_delete(req: ThumbnailDeleteRequest):
    """thumbnails/ 内のブックマーク画像を削除する（ブックマーク削除時の後始末）。

    シーンサムネール（scene_N.jpg）は対象外。存在しないファイルの削除は成功扱い。
    """
    _validate_bookmark_thumb_name(req.filename)
    target = _cache_dir(req.video_path) / "thumbnails" / req.filename
    try:
        target.unlink(missing_ok=True)
    except Exception as e:
        raise HTTPException(500, f"サムネールの削除に失敗: {e}")
    return {"status": "ok"}


@app.post("/cache/patch")
def cache_patch(req: CacheSaveRequest):
    """既存の data.json に部分的なデータをマージして保存する。"""
    cache = _cache_dir(req.video_path)
    cache.mkdir(exist_ok=True)
    data_file = cache / "data.json"
    data: dict = {}
    if data_file.exists():
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.update(req.data)
    data["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"キャッシュのパッチに失敗: {e}")
    return {"status": "ok"}


@app.get("/cache/image")
def cache_image(video_path: str, name: str):
    """キャッシュフォルダの画像ファイルを返す。"""
    cache = _cache_dir(video_path).resolve()
    img_path = (cache / name).resolve()
    # 文字列 prefix 比較だと「video.cache_evil」のような兄弟フォルダを許してしまう
    if not img_path.is_relative_to(cache):
        raise HTTPException(403, "不正なパス")
    if not img_path.exists():
        raise HTTPException(404, "画像が見つかりません")
    # 再分析でファイル名そのまま上書きされるため、ブラウザキャッシュに残さない
    return FileResponse(img_path, headers={"Cache-Control": "no-store"})


# ---------- ファイルマネージャー ----------

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv"}

# ファイル一覧に出さない管理用フォルダ（分析キャッシュ・スクリーンショット・隠しフォルダ）
def _is_managed_dir(name: str) -> bool:
    return name.startswith(".") or name.endswith(".cache") or name.endswith("_screenshot")


def _is_analyzed(cache: Path) -> bool:
    """分析済みかを data.json の内容で判定する。

    ブックマークだけを付けた動画も /cache/patch で data.json ができるため、
    存在チェックだけだと未分析なのに「分析済み」バッジが付いてしまう。
    meta / scenes / toc のいずれかが入っていれば分析済みとみなす。
    """
    data_file = cache / "data.json"
    if not data_file.exists():
        return False
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
        return bool(data.get("meta") or data.get("scenes") or data.get("toc"))
    except Exception:
        # 壊れた JSON 等は従来どおり存在ベースで分析済み扱い
        return True


def _video_entry(p: Path) -> dict:
    """動画1件の一覧表示用エントリ（字幕の有無は存在チェックのみで軽量に判定）。"""
    cache = p.parent / (p.stem + ".cache")

    def _has_srt(suffix: str) -> bool:
        # cache 内を優先し、旧・動画の横（後方互換）もチェック
        return (cache / f"{p.stem}.{suffix}.srt").exists() or (p.parent / f"{p.stem}.{suffix}.srt").exists()

    try:
        st = p.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    thumb = cache / "thumbnails" / "scene_0.jpg"
    return {
        "name": p.name,
        "path": str(p),
        "size": size,
        "mtime": mtime,
        "analyzed": _is_analyzed(cache),
        "has_original_srt": _has_srt("original") or _has_srt("corrected"),
        "has_japanese_srt": _has_srt("japanese"),
        "thumbnail": "thumbnails/scene_0.jpg" if thumb.exists() else None,
    }


@app.post("/folder/list")
def folder_list(req: FolderListRequest):
    """フォルダ直下のサブフォルダと動画ファイルを返す（再帰しない・ツリーの遅延読み込み用）。"""
    p = Path(req.path)
    if not p.is_absolute() or not p.is_dir():
        raise HTTPException(404, "フォルダが見つかりません")
    folders: list[dict] = []
    videos: list[dict] = []
    try:
        entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    except OSError as e:
        raise HTTPException(500, f"フォルダを読み取れません: {e}")
    for entry in entries:
        try:
            if entry.is_dir():
                if not _is_managed_dir(entry.name):
                    folders.append({"name": entry.name, "path": str(entry)})
            elif entry.suffix.lower() in VIDEO_EXTS:
                videos.append(_video_entry(entry))
        except OSError:
            continue
    return {"path": str(p), "folders": folders, "videos": videos}


@app.post("/folder/search")
def folder_search(req: FolderSearchRequest):
    """ルート配下を再帰走査してファイル名の部分一致で動画を検索する（フラット表示用）。"""
    root = Path(req.root)
    if not root.is_absolute() or not root.is_dir():
        raise HTTPException(404, "フォルダが見つかりません")
    q = req.query.strip().lower()
    if not q:
        return {"videos": [], "truncated": False}
    limit = 300
    results: list[dict] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_managed_dir(d)]
        dirnames.sort(key=str.lower)
        for fn in sorted(filenames, key=str.lower):
            fp = Path(dirpath) / fn
            if fp.suffix.lower() not in VIDEO_EXTS or q not in fn.lower():
                continue
            entry = _video_entry(fp)
            rel = os.path.relpath(dirpath, root)
            entry["rel_dir"] = "" if rel == "." else rel
            results.append(entry)
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
    return {"videos": results, "truncated": truncated}


_INVALID_NAME_CHARS = set('\\/:*?"<>|')


@app.post("/file/rename")
def file_rename(req: FileRenameRequest):
    """動画/フォルダのリネーム。動画はサイドカー（.cache/・SRT・_screenshot/）も一緒にリネームする。"""
    p = Path(req.path)
    if not p.is_absolute() or not p.exists():
        raise HTTPException(404, "ファイルが見つかりません")
    new_name = req.new_name.strip()
    if not new_name or new_name in {".", ".."} or any(c in _INVALID_NAME_CHARS for c in new_name):
        raise HTTPException(400, "使用できない名前です")

    if p.is_dir():
        target = p.parent / new_name
        if target.exists():
            raise HTTPException(409, "同名のフォルダが既に存在します")
        try:
            p.rename(target)
        except OSError as e:
            raise HTTPException(500, f"リネームに失敗しました: {e}")
        return {"path": str(target)}

    # 動画ファイル: new_name は stem として扱う（元の拡張子付きで来た場合は剥がす）
    new_stem = new_name
    if p.suffix and new_stem.lower().endswith(p.suffix.lower()):
        new_stem = new_stem[: -len(p.suffix)]
    if not new_stem:
        raise HTTPException(400, "使用できない名前です")
    old_stem = p.stem
    target = p.parent / (new_stem + p.suffix)
    if target.exists():
        raise HTTPException(409, "同名のファイルが既に存在します")

    # 動画本体を最初にリネーム（再生中ロック等で失敗するならここで止まり、サイドカーは無傷）
    try:
        p.rename(target)
    except OSError as e:
        raise HTTPException(500, f"リネームに失敗しました: {e}")

    warnings: list[str] = []

    def _try_rename(src: Path, dst: Path) -> None:
        if not src.exists():
            return
        try:
            src.rename(dst)
        except OSError as e:
            warnings.append(f"{src.name}: {e}")

    # 分析キャッシュフォルダと中の SRT
    cache = p.parent / (old_stem + ".cache")
    new_cache = p.parent / (new_stem + ".cache")
    _try_rename(cache, new_cache)
    srt_dirs = [new_cache if new_cache.is_dir() else None, p.parent]  # cache 内＋旧・横置き（後方互換）
    for d in srt_dirs:
        if d is None:
            continue
        for suffix in ("original", "corrected", "japanese"):
            _try_rename(d / f"{old_stem}.{suffix}.srt", d / f"{new_stem}.{suffix}.srt")
    # スクリーンショットフォルダ
    _try_rename(p.parent / (old_stem + "_screenshot"), p.parent / (new_stem + "_screenshot"))

    # data.json 内の動画ファイル名も更新
    data_file = new_cache / "data.json"
    if data_file.exists():
        try:
            d = json.loads(data_file.read_text(encoding="utf-8"))
            d["video"] = target.name
            data_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            warnings.append(f"data.json: {e}")

    return {"path": str(target), "warnings": warnings}
