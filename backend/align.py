"""wav2vec2 CTC 強制アライメント（WhisperX の移植）。

faster-whisper が生成した字幕セグメントの開始/終了時刻を、wav2vec2 の音素認識
出力に対する CTC 強制アライメントで精密化する。

このモジュールのアルゴリズム（trellis / backtrack / merge_repeats・テキスト正規化・
ワイルドカード処理・言語→モデル対応表）は WhisperX
(https://github.com/m-bain/whisperX, BSD-2-Clause License,
Copyright (c) 2022, Max Bain) の whisperx/alignment.py から移植したもの。
trellis 系のコードの原典は torchaudio の強制アライメントチュートリアル。

whisperx パッケージ本体は torch~=2.8.0 に固定されており本プロジェクトの
cu130 ホイール（Blackwell 対応）と衝突するため導入せず、torch + transformers
のみで動くようにアルゴリズムだけを移植している。torchaudio / pyannote /
pandas / nltk には依存しない。
"""
import gc
import subprocess
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
LANGUAGES_WITHOUT_SPACES = ("ja", "zh")

ENGINE_FASTER_WHISPER = "faster-whisper"
ENGINE_WHISPERX = "whisperx"
ASR_ENGINES = (ENGINE_FASTER_WHISPER, ENGINE_WHISPERX)

# 言語コード → HF の wav2vec2 CTC モデル。
# WhisperX の DEFAULT_ALIGN_MODELS_HF を転記し、torchaudio パイプラインで
# 提供されていた en/fr/de/es/it は同一チェックポイントの HF 版に置き換えた。
ALIGN_MODELS = {
    "en": "facebook/wav2vec2-base-960h",
    "fr": "facebook/wav2vec2-base-10k-voxpopuli-ft-fr",
    "de": "facebook/wav2vec2-base-10k-voxpopuli-ft-de",
    "es": "facebook/wav2vec2-base-10k-voxpopuli-ft-es",
    "it": "facebook/wav2vec2-base-10k-voxpopuli-ft-it",
    "ja": "jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
    "zh": "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn",
    "nl": "jonatasgrosman/wav2vec2-large-xlsr-53-dutch",
    "uk": "Yehor/wav2vec2-xls-r-300m-uk-with-small-lm",
    "pt": "jonatasgrosman/wav2vec2-large-xlsr-53-portuguese",
    "ar": "jonatasgrosman/wav2vec2-large-xlsr-53-arabic",
    "cs": "comodoro/wav2vec2-xls-r-300m-cs-250",
    "ru": "jonatasgrosman/wav2vec2-large-xlsr-53-russian",
    "pl": "jonatasgrosman/wav2vec2-large-xlsr-53-polish",
    "hu": "jonatasgrosman/wav2vec2-large-xlsr-53-hungarian",
    "fi": "jonatasgrosman/wav2vec2-large-xlsr-53-finnish",
    "fa": "jonatasgrosman/wav2vec2-large-xlsr-53-persian",
    "el": "jonatasgrosman/wav2vec2-large-xlsr-53-greek",
    "tr": "mpoyraz/wav2vec2-xls-r-300m-cv7-turkish",
    "da": "saattrupdan/wav2vec2-xls-r-300m-ftspeech",
    "he": "imvladikon/wav2vec2-xls-r-300m-hebrew",
    "vi": "nguyenvulebinh/wav2vec2-base-vi-vlsp2020",
    "ko": "kresnik/wav2vec2-large-xlsr-korean",
    "ur": "kingabzpro/wav2vec2-large-xls-r-300m-Urdu",
    "te": "anuragshas/wav2vec2-large-xlsr-53-telugu",
    "hi": "theainerd/Wav2Vec2-large-xlsr-hindi",
    "ca": "softcatala/wav2vec2-large-xlsr-catala",
    "ml": "gvs/wav2vec2-large-xlsr-malayalam",
    "no": "NbAiLab/nb-wav2vec2-1b-bokmaal-v2",
    "nn": "NbAiLab/nb-wav2vec2-1b-nynorsk",
    "sk": "comodoro/wav2vec2-xls-r-300m-sk-cv8",
    "sl": "anton-l/wav2vec2-large-xlsr-53-slovenian",
    "hr": "classla/wav2vec2-xls-r-parlaspeech-hr",
    "ro": "gigant/romanian-wav2vec2",
    "eu": "stefan-it/wav2vec2-large-xlsr-53-basque",
    "gl": "ifrz/wav2vec2-large-xlsr-galician",
    "ka": "xsway/wav2vec2-large-xlsr-georgian",
    "lv": "jimregan/wav2vec2-large-xlsr-latvian-cv",
    "tl": "Khalsuu/filipino-wav2vec2-l-xls-r-300m-official",
    "sv": "KBLab/wav2vec2-large-voxrex-swedish",
    "id": "cahya/wav2vec2-large-xlsr-indonesian",
}

# wav2vec2 の畳み込みフロントエンドが受け付ける最小サンプル数
_MIN_INPUT_SAMPLES = 400


def load_audio(path: str, sr: int = SAMPLE_RATE) -> np.ndarray:
    """ffmpeg で動画/音声を 16kHz mono float32 PCM にデコードして返す。

    torchaudio を使わない WhisperX load_audio 相当。ffmpeg は起動時に
    PATH が通っている前提（runtime/ffmpeg 同梱分を含む）。
    """
    cmd = [
        "ffmpeg", "-nostdin", "-threads", "0",
        "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(sr), "-vn", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg での音声デコードに失敗しました: {tail}")
    return np.frombuffer(proc.stdout, np.float32).copy()


@dataclass
class Point:
    token_index: int
    time_index: int
    score: float


@dataclass
class CharSegment:
    label: str
    start: int
    end: int
    score: float


def get_trellis(emission, tokens, blank_id=0):
    import torch

    num_frame = emission.size(0)
    num_tokens = len(tokens)

    trellis = torch.empty((num_frame + 1, num_tokens + 1))
    trellis[0, 0] = 0
    trellis[1:, 0] = torch.cumsum(emission[:, blank_id], 0)
    trellis[0, -num_tokens:] = -float("inf")
    trellis[-num_tokens:, 0] = float("inf")

    for t in range(num_frame):
        trellis[t + 1, 1:] = torch.maximum(
            # Score for staying at the same token
            trellis[t, 1:] + emission[t, blank_id],
            # Score for changing to the next token
            trellis[t, :-1] + emission[t, tokens],
        )
    return trellis


def backtrack(trellis, emission, tokens, blank_id=0):
    import torch

    j = trellis.size(1) - 1
    t_start = torch.argmax(trellis[:, j]).item()

    path = []
    for t in range(t_start, 0, -1):
        stayed = trellis[t - 1, j] + emission[t - 1, blank_id]
        changed = trellis[t - 1, j - 1] + emission[t - 1, tokens[j - 1]]

        prob = emission[t - 1, tokens[j - 1] if changed > stayed else blank_id].exp().item()
        path.append(Point(j - 1, t - 1, prob))

        if changed > stayed:
            j -= 1
            if j == 0:
                break
    else:
        # 先頭トークンまで辿り着けなかった（テキストが音声窓に収まらない等）
        return None

    return path[::-1]


def merge_repeats(path, transcript):
    i1, i2 = 0, 0
    segments = []
    while i1 < len(path):
        while i2 < len(path) and path[i1].token_index == path[i2].token_index:
            i2 += 1
        score = sum(path[k].score for k in range(i1, i2)) / (i2 - i1)
        segments.append(
            CharSegment(
                transcript[path[i1].token_index],
                path[i1].time_index,
                path[i2 - 1].time_index + 1,
                score,
            )
        )
        i1 = i2
    return segments


class Aligner:
    """wav2vec2 アライメントモデルのロード・実行・解放（ASRProcessor と同じ規律）。"""

    def __init__(self):
        self.model = None
        self.dictionary = None  # {小文字化した文字: token_id}
        self.language = None
        self.device = None

    @staticmethod
    def model_for_language(language: str | None) -> str | None:
        return ALIGN_MODELS.get((language or "").lower())

    def load(self, language: str):
        lang = (language or "").lower()
        model_id = ALIGN_MODELS.get(lang)
        if model_id is None:
            raise ValueError(f"言語 '{language}' のアライメントモデルはありません")
        if self.model is not None and self.language == lang:
            return

        self.unload()
        try:
            from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
        except ImportError as e:
            raise RuntimeError(
                "WhisperX アライメントには transformers パッケージが必要です。"
                ".venv で `pip install transformers` を実行してください"
            ) from e
        import torch

        # HF_HOME=models/ が server.py で設定済みのため models/hub/ にキャッシュされる
        processor = Wav2Vec2Processor.from_pretrained(model_id)
        model = Wav2Vec2ForCTC.from_pretrained(model_id)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            model = model.to(device)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            device = "cpu"
            model = model.to(device)
        model.eval()

        self.model = model
        self.dictionary = {
            char.lower(): code for char, code in processor.tokenizer.get_vocab().items()
        }
        self.language = lang
        self.device = device
        print(f"[Align] モデルをロードしました: {model_id} ({device})")

    def unload(self):
        if self.model is not None:
            del self.model
            self.model = None
            self.dictionary = None
            self.language = None
            self.device = None
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            print("[Align] モデルをアンロードしました")

    def align_segment(self, seg: dict, audio: np.ndarray) -> tuple[dict, str | None]:
        """1 セグメントの開始/終了時刻をアライメントで精密化する。

        戻り値は (セグメント, 警告 or None)。整列できない場合は元の時刻の
        セグメントをそのまま返し、理由を警告文字列で返す（例外は投げない）。
        """
        import torch

        text = seg.get("text") or ""
        t1, t2 = seg.get("timestamp", (None, None))
        if t1 is None or t2 is None or t2 <= t1:
            return seg, "時刻が不正なため整列をスキップ"
        if t1 >= len(audio) / SAMPLE_RATE:
            return seg, "開始時刻が音声の長さを超えているため整列をスキップ"

        # --- テキスト正規化（WhisperX align() 前処理の移植） ---
        num_leading = len(text) - len(text.lstrip())
        num_trailing = len(text) - len(text.rstrip())

        clean_char = []
        for cdx, char in enumerate(text):
            char_ = char.lower()
            if self.language not in LANGUAGES_WITHOUT_SPACES:
                char_ = char_.replace(" ", "|")
            if cdx < num_leading or cdx > len(text) - num_trailing - 1:
                continue
            if char_ in self.dictionary:
                clean_char.append(char_)
            elif char_ not in (" ", "|"):
                # vocab 外の文字（数字・記号・OOV漢字）はワイルドカードとして残す
                clean_char.append(char_)

        if not clean_char:
            return seg, "整列可能な文字が無いため元の時刻を使用"
        text_clean = "".join(clean_char)

        # --- 音声窓の切り出し ---
        f1 = int(t1 * SAMPLE_RATE)
        f2 = min(int(t2 * SAMPLE_RATE), len(audio))
        waveform = torch.from_numpy(audio[f1:f2]).unsqueeze(0)
        if waveform.shape[-1] < _MIN_INPUT_SAMPLES:
            waveform = torch.nn.functional.pad(
                waveform, (0, _MIN_INPUT_SAMPLES - waveform.shape[-1])
            )

        # --- emission（OOM 時は CPU に切り替えてリトライ） ---
        try:
            emission = self._emission(waveform)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            self.model = self.model.to("cpu")
            self.device = "cpu"
            emission = self._emission(waveform)

        blank_id = 0
        for char, code in self.dictionary.items():
            if char == "[pad]" or char == "<pad>":
                blank_id = code

        # vocab 外文字用のワイルドカード列（フレームごとの非 blank 最大値）
        has_wildcard = any(c not in self.dictionary for c in text_clean)
        if has_wildcard:
            non_blank_mask = torch.ones(emission.size(1), dtype=torch.bool)
            non_blank_mask[blank_id] = False
            wildcard_col = emission[:, non_blank_mask].max(dim=1).values
            emission = torch.cat([emission, wildcard_col.unsqueeze(1)], dim=1)
            wildcard_id = emission.size(1) - 1
            tokens = [self.dictionary.get(c, wildcard_id) for c in text_clean]
        else:
            tokens = [self.dictionary[c] for c in text_clean]

        trellis = get_trellis(emission, tokens, blank_id)
        path = backtrack(trellis, emission, tokens, blank_id)
        if path is None:
            return seg, "アライメント失敗（backtrack）のため元の時刻を使用"

        char_segments = merge_repeats(path, text_clean)
        if not char_segments:
            return seg, "アライメント結果が空のため元の時刻を使用"

        # trellis フレーム番号 → 秒（WhisperX と同じ換算。バッチ次元は 1）
        ratio = (t2 - t1) / (trellis.size(0) - 1)
        new_start = round(char_segments[0].start * ratio + t1, 3)
        new_end = round(char_segments[-1].end * ratio + t1, 3)
        if new_end <= new_start:
            return seg, "整列後の時刻が不正なため元の時刻を使用"

        return {**seg, "timestamp": (new_start, new_end)}, None

    def _emission(self, waveform):
        import torch

        # asr.py の _add_nvidia_dlls() が CTranslate2 用の pip 版 cuDNN を DLL パスに
        # 通すため、torch 同梱の cuDNN と混在して SUBLIBRARY_VERSION_MISMATCH で落ちる。
        # wav2vec2 の畳み込みは cuDNN なしのフォールバックカーネルで問題なく動くので、
        # この推論の間だけ cuDNN を無効化する。
        with torch.inference_mode(), torch.backends.cudnn.flags(enabled=False):
            logits = self.model(waveform.to(self.device)).logits
            emission = torch.log_softmax(logits, dim=-1)
        return emission[0].cpu().detach()
