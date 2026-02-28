import os
import re
import tempfile

import torch

from .vram import max_memory_map
import soundfile as sf
import ffmpeg
from qwen_asr import Qwen3ASRModel

MODEL_ID           = "Qwen/Qwen3-ASR-1.7B"
FORCED_ALIGNER_ID  = "Qwen/Qwen3-ForcedAligner-0.6B"

# チャンクサイズ（秒）
# ForcedAligner の上限は 170s だが、ASR の出力トークン上限による末尾切り捨てを
# 防ぐため、実際の入力は 90s に抑える。
MAX_ALIGN_SEC = 90

# Qwen3-ASR は言語コードではなく言語名を要求する
LANGUAGE_MAP = {
    "en":  "English",
    "zh":  "Chinese",
    "yue": "Cantonese",
    "ar":  "Arabic",
    "de":  "German",
    "fr":  "French",
    "es":  "Spanish",
    "pt":  "Portuguese",
    "id":  "Indonesian",
    "it":  "Italian",
    "ja":  "Japanese",
    "ko":  "Korean",
    "ru":  "Russian",
    "th":  "Thai",
    "vi":  "Vietnamese",
    "tr":  "Turkish",
    "hi":  "Hindi",
    "ms":  "Malay",
    "nl":  "Dutch",
    "sv":  "Swedish",
    "da":  "Danish",
    "fi":  "Finnish",
    "pl":  "Polish",
    "cs":  "Czech",
    "tl":  "Filipino",
    "fa":  "Persian",
    "el":  "Greek",
    "ro":  "Romanian",
    "hu":  "Hungarian",
    "mk":  "Macedonian",
}


class ASRProcessor:
    def __init__(self):
        self.model = None

    def load(self):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype  = torch.float16 if device == "cuda" else torch.float32
        mm = max_memory_map()
        self.model = Qwen3ASRModel.from_pretrained(
            MODEL_ID,
            dtype=dtype,
            device_map="auto",
            **({"max_memory": mm} if mm else {}),
            forced_aligner=FORCED_ALIGNER_ID,
            forced_aligner_kwargs={"torch_dtype": dtype, "device_map": "auto",
                                   **({"max_memory": mm} if mm else {})},
        )
        print(f"[ASR] Loaded {MODEL_ID} + {FORCED_ALIGNER_ID}")

    def unload(self):
        """モデルを破棄して VRAM を解放する"""
        if self.model is not None:
            del self.model
            self.model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[ASR] モデルをアンロードしました")

    def extract_audio(self, video_path: str) -> str:
        """動画ファイルから 16kHz モノラル WAV を一時ファイルに抽出する"""
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
        """
        ForcedAlignResult の単語アイテムを字幕セグメントにグループ化する。

        グループ区切りの優先順位:
          1. 文末句読点（. ! ?）の直後
          2. max_words 単語に達したとき

        offset_sec: このチャンクの開始時刻（秒）。各アイテムの時刻に加算する。
        """
        segments: list[dict] = []
        current_words: list[tuple[str, float, float]] = []  # (text, start, end)
        seg_start: float | None = None

        def flush():
            nonlocal seg_start
            if not current_words:
                return
            text    = " ".join(w[0] for w in current_words)
            seg_end = current_words[-1][2]
            segments.append({"text": text, "timestamp": (seg_start, seg_end)})
            current_words.clear()
            seg_start = None

        for item in align_result:
            word = item.text.strip()
            if not word:
                continue

            w_start = item.start_time + offset_sec
            w_end   = item.end_time   + offset_sec

            if seg_start is None:
                seg_start = w_start

            current_words.append((word, w_start, w_end))

            ends_sentence = bool(re.search(r"[.!?]$", word))
            if ends_sentence or len(current_words) >= max_words:
                flush()

        flush()  # 残りのワードを確定
        return segments

    def transcribe(
        self,
        video_path: str,
        language: str = None,
    ) -> list[dict]:
        """
        動画を文字起こしし、ForcedAligner による正確なタイムスタンプ付きセグメントを返す。

        音声を MAX_ALIGN_SEC 秒ごとに分割して推論し、
        各チャンクのオフセットを ForcedAlignItem の時刻に加算することで
        動画全体の絶対時刻を正確に再現する。

        Args:
            video_path: 動画ファイルのパス
            language:   ISO 言語コード（"en"/"zh"/"ja" など）または None（自動検出）。
                        Qwen3-ASR が要求する言語名（"English" 等）への変換は内部で行う。

        Returns:
            [{"text": str, "timestamp": (start_sec, end_sec)}, ...]
        """
        # ISO コード → Qwen3-ASR が受け付ける言語名に変換
        lang_name = LANGUAGE_MAP.get(language) if language else None

        audio_path = self.extract_audio(video_path)
        try:
            data, sr = sf.read(audio_path, dtype="float32")
        finally:
            os.unlink(audio_path)

        # モノラル保証
        if data.ndim > 1:
            data = data.mean(axis=1)

        chunk_samples  = int(MAX_ALIGN_SEC * sr)
        total_chunks   = max(1, -(-len(data) // chunk_samples))  # ceil 除算
        segments: list[dict] = []

        for chunk_idx, start_sample in enumerate(range(0, len(data), chunk_samples)):
            chunk = data[start_sample : start_sample + chunk_samples]

            # 0.5 秒未満の端切れは無音とみなしてスキップ
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
                print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks} @ {start_sec:.1f}s: 結果なし（スキップ）")
                continue

            result = results[0]

            # イテラブルをリスト化してアライメント率を計算
            aligned_items = list(result.time_stamps) if result.time_stamps is not None else []
            total_words   = len(result.text.split()) if result.text else 0
            align_ratio   = len(aligned_items) / total_words if total_words > 0 else 0.0

            print(f"[ASR] チャンク {chunk_idx + 1}/{total_chunks}: text={total_words}単語, aligned={len(aligned_items)}単語, ratio={align_ratio:.0%}")

            if aligned_items:
                # ForcedAligner が成功した場合：単語レベルのタイムスタンプを使用
                chunk_segs = self._align_to_segments(aligned_items, offset_sec=start_sec)
                segments.extend(chunk_segs)
            else:
                # フォールバック：ForcedAligner が失敗した場合はチャンク全体を1セグメントに
                text = result.text.strip()
                if text:
                    end_sec = min(start_sample + chunk_samples, len(data)) / sr
                    segments.append({"text": text, "timestamp": (start_sec, end_sec)})

        return segments
