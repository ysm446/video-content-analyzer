"""音声書き起こし（ASR）: faster-whisper (CTranslate2) バックエンド。

- Whisper がセグメント/単語単位のタイムスタンプを直接出力するため、
  旧 Qwen3-ASR + ForcedAligner のような別アライメントは不要。
- CTranslate2 は CUDA12 の cublas/cudnn DLL を要求するため、Windows では
  nvidia-*-cu12 wheel の bin を起動時に DLL 検索パスへ通す（_add_nvidia_dlls）。
"""
import gc
import os
import site

import re

from . import cancel

# モデル名: large-v3-turbo（高速）。精度重視なら WHISPER_MODEL=large-v3 で上書き可。
MODEL_ID = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
# モデルキャッシュ先（HF_HOME と同じ models/ 配下に置く）
DOWNLOAD_ROOT = os.environ.get("WHISPER_DOWNLOAD_ROOT", "models")

# 字幕セグメント分割のしきい値（単語タイムスタンプから再分割する際に使用）
SEG_MAX_SEC = 7.0       # 1 セグメントの最大長（秒）→ ここを超えたら強制 flush
SEG_MAX_CHARS = 40      # 1 セグメントの最大文字数 → ここを超えたら強制 flush
SEG_SOFT_SEC = 3.5      # この長さを超えていれば読点「、」でも flush（自然な区切り）
SEG_SOFT_CHARS = 16
# 単語間に極端に長い無音（ポーズ）があると、1セグメントが沈黙をまたいで間延びする
# （例: 「あと」49s と「ね」65s の間に約15秒の沈黙）。このギャップを超えたら
# ポーズを区切りとみなして flush する。数秒程度の自然な間で割らないよう高めに設定。
GAP_FLUSH_SEC = 8.0
# 末尾トークン単体が沈黙を吸収して長い場合の保険的な頭打ち。
MAX_TOKEN_DUR = 2.0
_SENTENCE_END = re.compile(r"[。．！？!?]$")   # 文末（常に flush）
_SOFT_END = re.compile(r"[、，,]$")            # 読点（ある程度の長さで flush）


def _add_nvidia_dlls() -> None:
    """faster-whisper(CTranslate2) が必要とする CUDA12 cublas/cudnn DLL を
    pip wheel(nvidia-*-cu12) の bin から DLL 検索パスに追加する（Windows 用）。"""
    bases = list(site.getsitepackages())
    try:
        bases.append(site.getusersitepackages())
    except Exception:
        pass
    for base in bases:
        nvidia = os.path.join(base, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pkg in os.listdir(nvidia):
            bindir = os.path.join(nvidia, pkg, "bin")
            if os.path.isdir(bindir):
                try:
                    os.add_dll_directory(bindir)
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dlls()


class ASRProcessor:
    def __init__(self):
        self.model = None

    def load(self):
        import ctranslate2
        from faster_whisper import WhisperModel

        if ctranslate2.get_cuda_device_count() > 0:
            device, compute = "cuda", "float16"
        else:
            device, compute = "cpu", "int8"

        self.model = WhisperModel(
            MODEL_ID,
            device=device,
            compute_type=compute,
            download_root=DOWNLOAD_ROOT,
        )
        print(f"[ASR] Loaded faster-whisper {MODEL_ID} ({device}/{compute})")

    def unload(self):
        if self.model is not None:
            del self.model
            self.model = None
            gc.collect()
            print("[ASR] モデルをアンロードしました")

    def transcribe(
        self,
        video_path: str,
        language: str = None,
    ) -> list[dict]:
        """動画/音声ファイルを書き起こし、セグメントのリストを返す。

        Whisper の単語タイムスタンプを使い、句読点・長さ基準で字幕向けに再分割する。

        Returns:
            [{"text": str, "timestamp": (start_sec, end_sec)}, ...]
        """
        # faster-whisper は動画ファイルから音声を直接デコードできる（ffmpeg 抽出不要）。
        segments, info = self.model.transcribe(
            video_path,
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
        )

        # segments は遅延ジェネレータ（反復で実際の書き起こしが進む）。
        # セグメントごとに中断要求を確認し、要求があれば即座に中断する。
        result: list[dict] = []
        for seg in segments:
            cancel.raise_if_canceled()
            words = list(seg.words or [])
            if words:
                result.extend(self._words_to_segments(words))
            else:
                text = (seg.text or "").strip()
                if text:
                    result.append({"text": text, "timestamp": (seg.start, seg.end)})

        print(
            f"[ASR] 書き起こし完了: {len(result)} セグメント "
            f"(言語={info.language}, p={info.language_probability:.2f})"
        )
        return result

    @staticmethod
    def _words_to_segments(words: list) -> list[dict]:
        """単語（start/end/word）を字幕セグメントへまとめる。
        句読点（。！？）で区切り、長さ・文字数の上限でも強制的に flush する。"""
        segments: list[dict] = []
        buf: list = []
        seg_start = None

        def flush():
            nonlocal buf, seg_start
            if not buf:
                return
            text = "".join(w.word for w in buf).strip()
            if text:
                last = buf[-1]
                # 末尾トークンがポーズを吸収して長すぎる場合は表示終了を頭打ちにする
                seg_end = last.end
                if last.end - last.start > MAX_TOKEN_DUR:
                    seg_end = last.start + MAX_TOKEN_DUR
                seg_end = max(seg_end, seg_start)
                segments.append({"text": text, "timestamp": (seg_start, seg_end)})
            buf = []
            seg_start = None

        for w in words:
            # 直前の単語との間に大きな無音があればそこで区切る（ポーズ＝境界）
            if buf and (w.start - buf[-1].end) > GAP_FLUSH_SEC:
                flush()
            if seg_start is None:
                seg_start = w.start
            buf.append(w)

            cur_text = "".join(x.word for x in buf).strip()
            dur = w.end - seg_start
            long_enough = dur >= SEG_SOFT_SEC or len(cur_text) >= SEG_SOFT_CHARS
            if (
                _SENTENCE_END.search(cur_text)                       # 文末は常に区切る
                or (long_enough and _SOFT_END.search(cur_text))      # 読点はある程度長ければ区切る
                or dur >= SEG_MAX_SEC                                 # 上限超過は強制
                or len(cur_text) >= SEG_MAX_CHARS
            ):
                flush()

        flush()
        return segments
