# Gemma 4 E4B ASR Notes

更新日: 2026-04-07

## 結論

`ggml-org/gemma-4-E4B-it-GGUF` を `llama.cpp` の `llama-server` で起動し、OpenAI 互換の `/v1/chat/completions` に音声入力を渡して文字起こしする構成は、手元環境では動作しませんでした。

モデル本体と mmproj のロード自体は成功しましたが、音声入力付きリクエスト時に最終的に次のエラーになりました。

```text
audio input is not supported - hint: if this is unexpected, you may need to provide the mmproj
```

## 確認できたこと

- `llama-server` は起動し、Gemma 4 E4B の GGUF を正常にロードできる
- ログ上で `has audio encoder` が出る
- mmproj もロードされる
- しかし `/v1/chat/completions` で音声入力を送ると失敗する

## 試したこと

1. ローカル GGUF + `--mmproj` で `llama-server` を起動
2. `-hf ggml-org/gemma-4-E4B-it-GGUF` で `llama-server` を起動
3. 音声 content の形式を複数試行

- `type: "audio_url"`
- `type: "audio"`
- `type: "input_audio"`

## 切り分け結果

- `unsupported content[].type`
  `content` JSON の形式が `llama-server` 側の期待とずれている段階
- `audio input is not supported`
  JSON 形式は受理されたが、モデルまたはサーバー実装側で音声入力経路が有効になっていない段階

今回の検証では最終的に後者まで進みました。

## 判断

2026-04-07 時点では、Gemma 4 E4B を `llama-server` ベースの ASR に使うのは見送りが安全です。

実用方針は次のどちらかが現実的です。

- 専用 ASR を使う
- `llama-server` ではなく、Gemma 向け別経路の CLI / 推論実装を使う

## 参考

- llama.cpp multimodal docs
  https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md
- llama.cpp audio input discussion
  https://github.com/ggml-org/llama.cpp/discussions/13759
- Gemma 3n docs
  https://ai.google.dev/gemma/docs/gemma-3n
