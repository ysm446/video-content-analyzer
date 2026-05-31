"""比較検証用: faster-whisper (large-v3) で書き起こし＋タイムスタンプを取得する。

使い方（.venv-gemma で実行）:
    .venv-gemma\\Scripts\\python.exe scripts\\test_whisper.py <音声 or 動画> [--lang ja] [--model large-v3]

- 書き起こし＋セグメント/単語タイムスタンプを _whisper_transcript.txt(UTF-8) に保存
- ロード時間・処理時間・RTF・VRAM を表示
"""
import argparse
import os
import site
import time

os.environ.setdefault("HF_HOME", "models")


def _add_nvidia_dlls():
    """faster-whisper(CTranslate2) が必要とする CUDA12 cublas/cudnn DLL を
    pip wheel(nvidia-*-cu12) の場所から DLL 検索パスに追加する。"""
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
                except OSError:
                    pass
                # CTranslate2 の実行時 LoadLibrary 用に PATH にも前置
                os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dlls()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--model", default="large-v3")
    args = ap.parse_args()

    import torch
    from faster_whisper import WhisperModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"

    print(f"[load] faster-whisper {args.model} ({device}/{compute})")
    t0 = time.time()
    model = WhisperModel(args.model, device=device, compute_type=compute,
                         download_root="models")
    print(f"[load] done in {time.time()-t0:.1f}s")

    t1 = time.time()
    segments, info = model.transcribe(
        args.media, language=args.lang, word_timestamps=True, vad_filter=True,
    )
    lines = []
    word_lines = []
    audio_end = 0.0
    for seg in segments:
        lines.append(f"[{seg.start:06.2f}-{seg.end:06.2f}] {seg.text.strip()}")
        audio_end = max(audio_end, seg.end)
        for w in (seg.words or []):
            word_lines.append(f"  {w.start:06.2f}-{w.end:06.2f} {w.word}")
    dt = time.time() - t1

    out = os.path.join(os.path.dirname(__file__), "_whisper_transcript.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n\n--- word-level ---\n")
        f.write("\n".join(word_lines))

    print(f"[saved] {out}  ({len(lines)} segments)")
    print(f"[lang] detected={info.language} (p={info.language_probability:.2f})")
    print(f"[time] transcribe {dt:.1f}s for ~{audio_end:.0f}s audio (RTF={dt/max(audio_end,1):.2f})")
    if torch.cuda.is_available():
        print(f"[vram] peak {torch.cuda.max_memory_allocated()/1e9:.1f} GB")


if __name__ == "__main__":
    main()
