import os
import re
import tempfile
from importlib import metadata

import ffmpeg
import soundfile as sf
import torch

from .vram import max_memory_map

MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
FORCED_ALIGNER_ID = "Qwen/Qwen3-ForcedAligner-0.6B"

# チャンクサイズ（秒）
# ForcedAligner の上限は 170s だが、ASR の出力トークン上限による末尾切り捨てを
# 防ぐため、実際の入力は 90s に抑える。
MAX_ALIGN_SEC = 90

# Qwen3-ASR は言語コードではなく言語名を要求する
LANGUAGE_MAP = {
    "en": "English",
    "zh": "Chinese",
    "yue": "Cantonese",
    "ar": "Arabic",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "th": "Thai",
    "vi": "Vietnamese",
    "tr": "Turkish",
    "hi": "Hindi",
    "ms": "Malay",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "tl": "Filipino",
    "fa": "Persian",
    "el": "Greek",
    "ro": "Romanian",
    "hu": "Hungarian",
    "mk": "Macedonian",
}


class ASRProcessor:
    def __init__(self):
        self.model = None

    def load(self):
        try:
            from qwen_asr import Qwen3ASRModel
        except Exception as exc:
            try:
                tf_ver = metadata.version("transformers")
            except metadata.PackageNotFoundError:
                tf_ver = "not installed"
            raise RuntimeError(
                "Failed to import qwen_asr. "
                f"Installed transformers={tf_ver}. "
                "Please install compatible versions, e.g. "
                "`pip install -U \"transformers==4.57.6\" qwen-asr`."
            ) from exc

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        mm = max_memory_map()
        self.model = Qwen3ASRModel.from_pretrained(
            MODEL_ID,
            dtype=dtype,
            device_map="auto",
            **({"max_memory": mm} if mm else {}),
            forced_aligner=FORCED_ALIGNER_ID,
            forced_aligner_kwargs={
                "torch_dtype": dtype,
                "device_map": "auto",
                **({"max_memory": mm} if mm else {}),
            },
        )
        print(f"[ASR] Loaded {MODEL_ID} + {FORCED_ALIGNER_ID}")

    def unload(self):
        if self.model is not None:
            del self.model
            self.model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[ASR] モデルをアンロードしました")

    def extract_audio(self, video_path: str) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        (
            ffmpeg.input(video_path)
            .output(tmp.name, ar=16000, ac=1, format="wav")
            .overwrite_output()
            .run(quiet=True)
        )
        return tmp.name

    def _align_to_segments(
        self,
        align_result,
        offset_sec: float,
        max_words: int = 12,
    ) -> list[dict]:
        segments: list[dict] = []
        current_words: list[tuple[str, float, float]] = []
        seg_start: float | None = None

        def flush():
            nonlocal seg_start
            if not current_words:
                return
            text = " ".join(word[0] for word in current_words)
            seg_end = current_words[-1][2]
            segments.append({"text": text, "timestamp": (seg_start, seg_end)})
            current_words.clear()
            seg_start = None

        for item in align_result:
            word = item.text.strip()
            if not word:
                continue

            w_start = item.start_time + offset_sec
            w_end = item.end_time + offset_sec

            if seg_start is None:
                seg_start = w_start

            current_words.append((word, w_start, w_end))

            ends_sentence = bool(re.search(r"[.!?]$", word))
            if ends_sentence or len(current_words) >= max_words:
                flush()

        flush()
        return segments

    def transcribe(
        self,
        video_path: str,
        language: str = None,
    ) -> list[dict]:
        lang_name = LANGUAGE_MAP.get(language) if language else None

        audio_path = self.extract_audio(video_path)
        try:
            data, sr = sf.read(audio_path, dtype="float32")
        finally:
            os.unlink(audio_path)

        if data.ndim > 1:
            data = data.mean(axis=1)

        chunk_samples = int(MAX_ALIGN_SEC * sr)
        total_chunks = max(1, -(-len(data) // chunk_samples))
        segments: list[dict] = []

        for chunk_idx, start_sample in enumerate(range(0, len(data), chunk_samples)):
            chunk = data[start_sample : start_sample + chunk_samples]

            if len(chunk) < sr * 0.5:
                continue

            start_sec = start_sample / sr
            print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks} @ {start_sec:.1f}s 処理中...")

            results = self.model.transcribe(
                (chunk, sr),
                language=lang_name,
                return_time_stamps=True,
            )

            if not results:
                print(
                    f"[ASR] チャンク {chunk_idx + 1}/{total_chunks} @ "
                    f"{start_sec:.1f}s: 結果なし（スキップ）"
                )
                continue

            result = results[0]
            aligned_items = list(result.time_stamps) if result.time_stamps is not None else []
            total_words = len(result.text.split()) if result.text else 0
            align_ratio = len(aligned_items) / total_words if total_words > 0 else 0.0

            print(
                f"[ASR] チャンク {chunk_idx + 1}/{total_chunks}: "
                f"text={total_words}単語, aligned={len(aligned_items)}単語, ratio={align_ratio:.0%}"
            )

            if aligned_items:
                segments.extend(self._align_to_segments(aligned_items, offset_sec=start_sec))
            else:
                text = result.text.strip()
                if text:
                    end_sec = min(start_sample + chunk_samples, len(data)) / sr
                    segments.append({"text": text, "timestamp": (start_sec, end_sec)})

        return segments
