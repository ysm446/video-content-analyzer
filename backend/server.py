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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# モデルロード前に HF_HOME を設定
os.environ["HF_HOME"] = str(Path(__file__).parent.parent / "models")

from .asr import ASRProcessor
from .translator import Translator
from .subtitle import segments_to_srt, srt_file_to_segments, save_srt, make_output_path, split_long_segments
from .video_reviewer import VideoReviewer

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
translator        = Translator()  # バッチ翻訳用（翻訳完了後にアンロード）
translator_lookup = Translator()  # 辞書検索用（常駐）
video_reviewer    = VideoReviewer()

# 前回選択したモデルを復元
_s = load_settings()
if _m := _s.get("translator_model"):
    translator.set_model_id(_m)
if _m := _s.get("lookup_model"):
    translator_lookup.set_model_id(_m)
if _m := _s.get("vl_model"):
    video_reviewer.set_model_id(_m)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Movie Review API が起動しました（モデルはオンデマンドでロード）")
    yield
    # シャットダウン時に VRAM を解放
    asr.unload()
    video_reviewer.unload()


app = FastAPI(title="Movie Review API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(data: dict) -> str:
    """Server-Sent Events 形式にシリアライズ"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_timestamp_seconds(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?\s*$", str(value))
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    frac = float(f"0.{m.group(4)}") if m.group(4) else 0.0
    return h * 3600 + mm * 60 + ss + frac


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


def _analysis_plan(mode: str, max_frames: int) -> dict:
    if mode == "speed":
        return {
            "coarse_frames": max_frames,
            "refine_limit": 0,
            "refine_min_span": 999999.0,
            "refine_frames": 0,
        }
    if mode == "balanced":
        return {
            "coarse_frames": max(8, min(18, max_frames // 2 or 8)),
            "refine_limit": 3,
            "refine_min_span": 180.0,
            "refine_frames": max(8, min(18, max_frames // 2 or 8)),
        }
    return {
        "coarse_frames": max(10, min(22, max_frames // 2 or 10)),
        "refine_limit": 6,
        "refine_min_span": 120.0,
        "refine_frames": max(10, min(24, (max_frames * 2) // 3 or 10)),
    }


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


def _transcript_lines_only(transcript_chunk: str) -> list[str]:
    lines: list[str] = []
    for line in transcript_chunk.splitlines():
        m = re.match(r"^\[\d+:\d{2}\]\s*(.*)$", line.strip())
        text = (m.group(1) if m else line).strip()
        if text:
            lines.append(text)
    return lines


def _ground_entries_with_transcript(entries: list[dict], transcript: str, duration: float) -> list[dict]:
    """
    各チャプターの [start, next_start) 区間で実際に発話された字幕を使い、
    summary を区間内事実に寄せる。
    """
    if not transcript or not entries:
        return entries
    rows = sorted(entries, key=lambda x: float(x.get("start_sec", 0.0)))
    for i, e in enumerate(rows):
        start = float(e.get("start_sec", 0.0))
        end = float(rows[i + 1].get("start_sec", duration)) if i + 1 < len(rows) else float(duration)
        if end <= start:
            continue
        chunk = _slice_transcript(transcript, start, end)
        texts = _transcript_lines_only(chunk)
        if not texts:
            continue
        snippet = " / ".join(texts[:2])
        if len(snippet) > 220:
            snippet = snippet[:220].rstrip() + "…"
        e["summary"] = snippet
    return rows


def _merge_toc_entries(entries: list[dict], duration: float) -> list[dict]:
    if not entries:
        return []
    rows = sorted(entries, key=lambda x: float(x.get("start_sec", 0.0)))
    merged: list[dict] = []
    MERGE_GAP_SEC = 3.0
    for r in rows:
        if not merged:
            merged.append(r.copy())
            continue
        prev = merged[-1]
        gap = float(r.get("start_sec", 0.0)) - float(prev.get("start_sec", 0.0))
        same_title = (r.get("title") or "").strip() == (prev.get("title") or "").strip()
        prev_summary = (prev.get("summary") or "").strip()
        curr_summary = (r.get("summary") or "").strip()
        # 近接マージは厳しめに。タイトル一致時のみ積極的に統合する。
        if same_title or (gap < MERGE_GAP_SEC and (not prev_summary or not curr_summary)):
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


class LookupRequest(BaseModel):
    word: str


class SetModelRequest(BaseModel):
    translator: Optional[str] = None  # バッチ翻訳モデル
    lookup:     Optional[str] = None  # 辞書検索モデル


class ReviewRequest(BaseModel):
    video_path:   str
    max_frames:   int   = 30
    min_interval: float = 5.0
    transcript:   str   = ""        # SRT由来のトランスクリプト（フロントから送信）
    frame_mode:   str   = "uniform"  # "uniform" | "scene"
    analysis_mode: str  = "speed"    # "speed" | "balanced" | "quality"


class QARequest(BaseModel):
    video_path:   str
    question:     str
    max_frames:   int   = 20
    min_interval: float = 5.0
    transcript:   str   = ""
    frame_mode:   str   = "uniform"  # "uniform" | "scene"


class SetVLModelRequest(BaseModel):
    model_id: str


class UISettingsRequest(BaseModel):
    frame_mode: Optional[str] = None  # "uniform" | "scene"
    max_frames: Optional[int] = None
    analysis_mode: Optional[str] = None  # "speed" | "balanced" | "quality"


class TOCBuildRequest(BaseModel):
    video_path: str
    max_frames: int = 30
    min_interval: float = 5.0
    transcript: str = ""
    frame_mode: str = "scene"  # "uniform" | "scene"
    analysis_mode: str = "speed"  # "speed" | "balanced" | "quality"


class TOCLoadRequest(BaseModel):
    video_path: str


class TOCSaveRequest(BaseModel):
    video_path: str
    data: dict


# ---------- 利用可能なモデル ----------

VL_MODELS = [
    {
        "id":      "huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated",
        "label":   "Qwen3-VL 4B",
        "vram_gb": 10,
        "note":    "速い・省メモリ",
    },
    {
        "id":      "huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated",
        "label":   "Qwen3-VL 8B",
        "vram_gb": 18,
        "note":    "高品質",
    },
]

TRANSLATOR_MODELS = [
    {"id": "Qwen/Qwen3-1.7B",  "label": "Qwen3-1.7B",  "vram_gb": 3.5,  "note": "速い・省メモリ"},
    {"id": "Qwen/Qwen3-4B",    "label": "Qwen3-4B",    "vram_gb": 8.0,  "note": "高品質"},
    {"id": "Qwen/Qwen3-8B",    "label": "Qwen3-8B",    "vram_gb": 16.0, "note": "高品質・大容量"},
    {"id": "huihui-ai/Huihui-Qwen3-14B-abliterated-v2", "label": "Huihui Qwen3-14B v2", "vram_gb": 28.0, "note": "最高品質"},
]


# ---------- エンドポイント ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ui-settings")
def get_ui_settings():
    s = load_settings()
    return {
        "frame_mode": s.get("frame_mode", "uniform"),
        "max_frames": s.get("max_frames", 30),
        "analysis_mode": s.get("analysis_mode", "speed"),
    }


@app.post("/ui-settings")
def post_ui_settings(req: UISettingsRequest):
    to_save = {}
    if req.frame_mode is not None: to_save["frame_mode"] = req.frame_mode
    if req.max_frames is not None: to_save["max_frames"] = req.max_frames
    if req.analysis_mode is not None: to_save["analysis_mode"] = req.analysis_mode
    if to_save:
        save_settings(to_save)
    return {"status": "ok"}


@app.get("/models")
def get_models():
    """利用可能なモデルの一覧と現在の選択・ロード状態を返す"""
    return {
        "translator": {
            "current":   translator.model_id,
            "loaded":    translator.model is not None,
            "available": TRANSLATOR_MODELS,
        },
        "lookup": {
            "current":   translator_lookup.model_id,
            "loaded":    translator_lookup.model is not None,
            "available": TRANSLATOR_MODELS,
        },
    }


@app.post("/models")
def set_models(req: SetModelRequest):
    """翻訳・辞書モデルを切り替える（ロード済みの場合は即アンロード・次回使用時に再ロード）"""
    valid_ids = {m["id"] for m in TRANSLATOR_MODELS}
    to_save = {}
    if req.translator is not None:
        if req.translator not in valid_ids:
            raise HTTPException(400, f"無効なモデルID: {req.translator}")
        translator.set_model_id(req.translator)
        to_save["translator_model"] = translator.model_id
    if req.lookup is not None:
        if req.lookup not in valid_ids:
            raise HTTPException(400, f"無効なモデルID: {req.lookup}")
        translator_lookup.set_model_id(req.lookup)
        to_save["lookup_model"] = translator_lookup.model_id
    if to_save:
        save_settings(to_save)
    return {"status": "ok", "translator": translator.model_id, "lookup": translator_lookup.model_id}


@app.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    """
    動画を文字起こしして original SRT を生成する。
    進捗は SSE でストリーミング。

    Events:
      {"status": "loading_model"}          ← ASR モデルが未ロードの場合のみ
      {"status": "extracting_audio"}
      {"status": "saving_srt", "segments": int}
      {"status": "done", "srt_path": str, "segments": int}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")

    async def stream():
        loop = asyncio.get_event_loop()

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
            segments = await loop.run_in_executor(
                None, asr.transcribe, str(video_path), req.language
            )
        except Exception as e:
            yield sse({"status": "error", "message": str(e)})
            return
        finally:
            # 文字起こし完了（成功・失敗問わず）後に VRAM を解放
            await loop.run_in_executor(None, asr.unload)

        segments = split_long_segments(segments)
        yield sse({"status": "saving_srt", "segments": len(segments)})

        srt_content = segments_to_srt(segments)
        out_path = make_output_path(str(video_path), "original")
        save_srt(srt_content, out_path)

        yield sse({"status": "done", "srt_path": out_path, "segments": len(segments)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/translate")
async def translate(req: TranslateRequest):
    """
    原文 SRT を日本語に翻訳して japanese SRT を生成する。
    セグメントごとに進捗を SSE でストリーミング。

    Events:
      {"status": "loading_model"}          ← Translator モデルが未ロードの場合のみ
      {"status": "translating", "current": int, "total": int}
      {"status": "done", "srt_path": str, "total": int}
      {"status": "error", "message": str}
    """
    srt_path = Path(req.srt_path)
    if not srt_path.exists():
        raise HTTPException(404, f"SRT ファイルが見つかりません: {srt_path}")

    segments = srt_file_to_segments(str(srt_path))
    total = len(segments)

    async def stream():
        loop = asyncio.get_event_loop()
        translated = []

        # Translator が未ロードならロード（翻訳終了後に必ずアンロード）
        if translator.model is None:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, translator.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        yield sse({"status": "translating", "current": 0, "total": total})

        # 直前 CONTEXT_WINDOW 件の (原文, 翻訳) ペアをスライディング窓として保持
        CONTEXT_WINDOW = 5
        context_history: list[tuple[str, str]] = []

        try:
            for i, seg in enumerate(segments):
                ctx = context_history[-CONTEXT_WINDOW:] or None
                try:
                    jp_text = await loop.run_in_executor(
                        None, translator.translate, seg["text"], ctx
                    )
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return

                context_history.append((seg["text"], jp_text))
                translated.append({**seg, "text": jp_text})
                yield sse({"status": "translating", "current": i + 1, "total": total})
        finally:
            # 翻訳完了（成功・失敗問わず）後に VRAM を解放
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
    definition = await loop.run_in_executor(None, translator_lookup.lookup, word)
    return {"word": word, "definition": definition}


# ================================================================
# 動画レビュー（Qwen3-VL）
# ================================================================

@app.get("/review/models")
def get_vl_models():
    """利用可能な VL モデルの一覧と現在の選択・ロード状態を返す"""
    return {
        "current":   video_reviewer.model_id,
        "loaded":    video_reviewer.model is not None,
        "available": VL_MODELS,
    }


@app.post("/review/models")
def set_vl_model(req: SetVLModelRequest):
    """VL モデルを切り替える"""
    valid_ids = {m["id"] for m in VL_MODELS}
    if req.model_id not in valid_ids:
        raise HTTPException(400, f"無効なモデルID: {req.model_id}")
    video_reviewer.set_model_id(req.model_id)
    save_settings({"vl_model": video_reviewer.model_id})
    return {"status": "ok", "model_id": video_reviewer.model_id}


@app.post("/review/unload")
async def unload_vl_model():
    """VL モデルを手動でアンロードして VRAM を解放する"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, video_reviewer.unload)
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

    async def stream():
        loop = asyncio.get_event_loop()
        transcript = req.transcript
        plan = _analysis_plan(req.analysis_mode, req.max_frames)

        try:
            if video_reviewer.model is None:
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
                    None, video_reviewer.analyze_frames, frames, transcript, meta.get("timestamps", [])
                )
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            duration = float(meta.get("duration") or 0.0)
            entries = _build_toc_entries(coarse_result, duration)

            if int(plan["refine_limit"]) > 0 and entries:
                targets = [
                    e for e in entries
                    if float(e.get("end_sec", 0.0)) - float(e.get("start_sec", 0.0)) >= float(plan["refine_min_span"])
                ][: int(plan["refine_limit"])]
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
                            None, video_reviewer.analyze_frames, r_frames, r_transcript, r_meta.get("timestamps", [])
                        )
                        rel_entries = _entries_from_refine_result(
                            r_result, start, end, duration
                        )
                        refined_entries.extend(rel_entries)
                    except Exception as e:
                        yield sse({"status": "error", "message": f"refine失敗: {e}"})
                        return
                entries = _merge_toc_entries(entries + refined_entries, duration)
            entries = _ground_entries_with_transcript(entries, transcript, duration)

            result = {
                "summary": coarse_result.get("summary", ""),
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

            video_reviewer.cache_frames(str(video_path), req.frame_mode, req.max_frames, frames, meta)
            yield sse({"status": "done", "result": result, "meta": meta})
        finally:
            await loop.run_in_executor(None, video_reviewer.unload)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/review/qa")
async def review_qa(req: QARequest):
    """
    動画に対する質問に回答する（SSE）。

    Events:
      {"status": "loading_model"}
      {"status": "extracting_frames"}
      {"status": "answering", "count": int}
      {"status": "answer_delta", "delta": str}
      {"status": "done", "answer": str}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if not req.question.strip():
        raise HTTPException(400, "question が空です")

    async def stream():
        loop = asyncio.get_event_loop()

        if video_reviewer.model is None:
            yield sse({"status": "loading_model"})
            try:
                await loop.run_in_executor(None, video_reviewer.load)
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        cached = video_reviewer.get_cached_frames(str(video_path), req.frame_mode, req.max_frames)
        if cached is not None:
            frames, meta = cached
        else:
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
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

        yield sse({"status": "answering", "count": meta["count"]})
        q: queue.Queue[tuple[str, str]] = queue.Queue()

        def run_stream():
            parts: list[str] = []
            try:
                for delta in video_reviewer.qa_frames_stream(
                    frames,
                    req.question,
                    req.transcript,
                    meta.get("timestamps", []),
                ):
                    parts.append(delta)
                    q.put(("answer_delta", delta))
                answer = video_reviewer._clean_generated_text("".join(parts))
                q.put(("done", answer))
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
                yield sse({"status": "done", "answer": payload})
                break
            else:
                yield sse({"status": "error", "message": payload})
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/review/toc/build")
async def review_build_toc(req: TOCBuildRequest):
    """
    視覚モデル＋字幕テキストから目次データを生成し、
    動画と同じフォルダに .toc.json として保存する（SSE）。
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")
    if req.frame_mode not in {"uniform", "scene"}:
        raise HTTPException(400, f"無効な frame_mode: {req.frame_mode}")
    _validate_analysis_mode(req.analysis_mode)

    async def stream():
        loop = asyncio.get_event_loop()
        plan = _analysis_plan(req.analysis_mode, req.max_frames)
        try:
            if video_reviewer.model is None:
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
                        None,
                        video_reviewer.extract_frames_scene,
                        str(video_path),
                        int(plan["coarse_frames"]),
                    )
                else:
                    frames, meta = await loop.run_in_executor(
                        None,
                        video_reviewer.extract_frames,
                        str(video_path),
                        int(plan["coarse_frames"]),
                        req.min_interval,
                    )
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            yield sse({
                "status": "analyzing",
                "count": meta.get("count", 0),
                "mode": meta.get("mode", req.frame_mode),
                "pass": "coarse",
                "analysis_mode": req.analysis_mode,
            })
            try:
                coarse_analysis = await loop.run_in_executor(
                    None, video_reviewer.analyze_frames,
                    frames, req.transcript, meta.get("timestamps", [])
                )
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            duration = float(meta.get("duration") or 0.0)
            entries = _build_toc_entries(coarse_analysis, duration)
            if int(plan["refine_limit"]) > 0 and entries:
                targets = [
                    e for e in entries
                    if float(e.get("end_sec", 0.0)) - float(e.get("start_sec", 0.0)) >= float(plan["refine_min_span"])
                ][: int(plan["refine_limit"])]
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
                        r_transcript = _slice_transcript(req.transcript, start, end)
                        r_result = await loop.run_in_executor(
                            None, video_reviewer.analyze_frames,
                            r_frames, r_transcript, r_meta.get("timestamps", [])
                        )
                        rel_entries = _entries_from_refine_result(
                            r_result, start, end, duration
                        )
                        refined_entries.extend(rel_entries)
                    except Exception as e:
                        yield sse({"status": "error", "message": f"refine失敗: {e}"})
                        return
                entries = _merge_toc_entries(entries + refined_entries, duration)
            entries = _ground_entries_with_transcript(entries, req.transcript, duration)
            toc_doc = {
                "version": 1,
                "video_path": str(video_path),
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "model": {
                    "vl_model": video_reviewer.model_id,
                    "source": "vision+subtitle",
                },
                "meta": {
                    "genre": coarse_analysis.get("genre", "不明"),
                    "summary": coarse_analysis.get("summary", ""),
                    "tags": coarse_analysis.get("tags", []),
                    "frame_mode": req.frame_mode,
                    "analysis_mode": req.analysis_mode,
                    "max_frames": req.max_frames,
                    "min_interval": req.min_interval,
                    "duration_sec": duration,
                },
                "toc": entries,
                "bookmarks": [],
            }

            yield sse({"status": "saving"})
            toc_path = video_path.with_suffix(".toc.json")
            toc_path.write_text(json.dumps(toc_doc, ensure_ascii=False, indent=2), encoding="utf-8")
            yield sse({"status": "done", "toc_path": str(toc_path), "data": toc_doc})
        finally:
            await loop.run_in_executor(None, video_reviewer.unload)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/review/toc/load")
def review_load_toc(req: TOCLoadRequest):
    """動画と同じフォルダの .toc.json を読み込む。"""
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


@app.post("/review/toc/save")
def review_save_toc(req: TOCSaveRequest):
    """目次データを動画と同じフォルダの .toc.json に保存する。"""
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")

    toc_path = video_path.with_suffix(".toc.json")
    data = req.data if isinstance(req.data, dict) else {}
    data["video_path"] = str(video_path)
    data["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if "version" not in data:
        data["version"] = 1
    if "toc" not in data or not isinstance(data["toc"], list):
        data["toc"] = []
    if "bookmarks" not in data or not isinstance(data["bookmarks"], list):
        data["bookmarks"] = []

    try:
        toc_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"目次ファイルの保存に失敗: {e}")
    return {"status": "ok", "toc_path": str(toc_path)}
