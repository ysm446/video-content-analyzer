"""フェーズ0 検証用: Gemma 4 audio で書き起こしを試す使い捨てスクリプト。

使い方（.venv-gemma で実行）:
    .venv-gemma\\Scripts\\python.exe scripts\\test_gemma_asr.py <音声 or 動画ファイル> [--lang Japanese] [--sec 30]

- HF_HOME=models を前提に、既にキャッシュ済みの google/gemma-4-e2b-it をロード
- ffmpeg で先頭 N 秒を 16kHz/mono に変換して入力
- 書き起こしテキストと所要時間・VRAM を表示
"""
import argparse
import os
import subprocess
import tempfile
import time

os.environ.setdefault("HF_HOME", "models")

import soundfile as sf  # noqa: E402
import torch  # noqa: E402

MODEL_ID = os.environ.get("GEMMA_MODEL_ID", "google/gemma-4-e2b-it")


def extract_audio(src: str, max_sec: int) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", str(max_sec),
         "-ar", "16000", "-ac", "1", "-f", "wav", tmp.name],
        check=True, capture_output=True,
    )
    return tmp.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media", help="音声 or 動画ファイル")
    ap.add_argument("--lang", default=None, help="言語名（例: Japanese, English）")
    ap.add_argument("--sec", type=int, default=30, help="先頭何秒を使うか（<=30 推奨）")
    args = ap.parse_args()

    # transformers 5.5+ のマルチモーダル API
    from transformers import AutoProcessor
    try:
        from transformers import AutoModelForMultimodalLM as AutoModel
    except ImportError:
        from transformers import AutoModelForCausalLM as AutoModel  # フォールバック

    print(f"[load] {MODEL_ID}")
    t0 = time.time()
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(
        MODEL_ID, torch_dtype=torch.bfloat16, device_map="auto",
    )
    print(f"[load] done in {time.time()-t0:.1f}s")

    wav = extract_audio(args.media, args.sec)
    data, sr = sf.read(wav, dtype="float32")
    print(f"[audio] {len(data)/sr:.1f}s @ {sr}Hz")

    prompt = "Transcribe the following speech segment"
    if args.lang:
        prompt += f" in {args.lang}"
    prompt += "."

    messages = [{
        "role": "user",
        "content": [
            {"type": "audio", "audio": wav},
            {"type": "text", "text": prompt},
        ],
    }]

    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device)

    t1 = time.time()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=512, do_sample=False)
    gen = out[0][inputs["input_ids"].shape[-1]:]
    text = processor.decode(gen, skip_special_tokens=True)
    dt = time.time() - t1

    os.unlink(wav)

    print("\n===== TRANSCRIPT =====")
    print(text.strip())
    print("======================")
    print(f"[time] generate {dt:.1f}s for {args.sec}s audio (RTF={dt/args.sec:.2f})")
    if torch.cuda.is_available():
        print(f"[vram] peak {torch.cuda.max_memory_allocated()/1e9:.1f} GB")


if __name__ == "__main__":
    main()
