import os
import json
import asyncio
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

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
    print("Language Caption Player API が起動しました（モデルはオンデマンドでロード）")
    yield
    # シャットダウン時に VRAM を解放
    asr.unload()
    video_reviewer.unload()


app = FastAPI(title="Language Caption Player API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(data: dict) -> str:
    """Server-Sent Events 形式にシリアライズ"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


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
    video_path:    str
    max_frames:    int   = 30
    min_interval:  float = 5.0
    include_audio: bool  = False


class QARequest(BaseModel):
    video_path:   str
    question:     str
    max_frames:   int   = 20
    min_interval: float = 5.0
    transcript:   str   = ""


class SetVLModelRequest(BaseModel):
    model_id: str


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
      {"status": "loading_asr"}                                      ← include_audio=true かつ未ロード時
      {"status": "transcribing"}                                     ← include_audio=true 時
      {"status": "asr_done", "chars": int}                          ← include_audio=true 時
      {"status": "loading_model"}
      {"status": "extracting_frames"}
      {"status": "analyzing", "count": int, "interval": float, "duration": float}
      {"status": "done", "result": dict, "meta": dict, "transcript": str}
      {"status": "error", "message": str}
    """
    video_path = Path(req.video_path)
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path}")

    async def stream():
        loop = asyncio.get_event_loop()
        transcript = ""

        # --- オプション: 音声書き起こし（include_audio=true 時）---
        if req.include_audio:
            if asr.model is None:
                yield sse({"status": "loading_asr"})
                try:
                    await loop.run_in_executor(None, asr.load)
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return

            yield sse({"status": "transcribing"})
            try:
                segments = await loop.run_in_executor(
                    None, asr.transcribe, str(video_path), None
                )
                transcript = "\n".join(s["text"] for s in segments if s.get("text"))
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return
            finally:
                await loop.run_in_executor(None, asr.unload)

            yield sse({"status": "asr_done", "chars": len(transcript)})

        # --- VL モデルロード & 分析（終了後は必ずアンロード）---
        try:
            if video_reviewer.model is None:
                yield sse({"status": "loading_model"})
                try:
                    await loop.run_in_executor(None, video_reviewer.load)
                except Exception as e:
                    yield sse({"status": "error", "message": str(e)})
                    return

            yield sse({"status": "extracting_frames"})
            try:
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

            yield sse({"status": "analyzing",
                       **{k: v for k, v in meta.items() if k != "timestamps"}})
            try:
                result = await loop.run_in_executor(
                    None, video_reviewer.analyze_frames,
                    frames, transcript, meta.get("timestamps", [])
                )
            except Exception as e:
                yield sse({"status": "error", "message": str(e)})
                return

            yield sse({"status": "done", "result": result, "meta": meta, "transcript": transcript})
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

        yield sse({"status": "extracting_frames"})
        try:
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
        try:
            answer = await loop.run_in_executor(
                None, video_reviewer.qa_frames, frames, req.question, req.transcript
            )
        except Exception as e:
            yield sse({"status": "error", "message": str(e)})
            return

        yield sse({"status": "done", "answer": answer})

    return StreamingResponse(stream(), media_type="text/event-stream")
