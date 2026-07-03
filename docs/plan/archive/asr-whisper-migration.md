# ASR 移行計画: Qwen3-ASR → Whisper (faster-whisper large-v3)

> **✅ 完了済み（アーカイブ）**: この計画は実施済み。現行の ASR は faster-whisper
> （既定 large-v3-turbo、設定 → ランタイム でモデル切り替え可）。
> Gemma 4 audio を不採用とした判断記録として保存している。

作成日: 2026-05-31 / アーカイブ: 2026-07-04

## 目的

音声書き起こし（ASR）パイプラインを Qwen3-ASR から **faster-whisper (large-v3)** へ移行する。
翻訳・VL レビューは引き続き llama.cpp(GGUF) で行い、ASR のみ Whisper エンジンを使うハイブリッド構成とする。

## 決定: ASR は Whisper 一本化（2026-05-31）

当初は Gemma 4 audio への移行を検証したが（経緯は末尾の付録）、実機比較の結果 **faster-whisper large-v3 を採用**する。

### 比較結果（同一の日本語動画 109s で実測）
| 項目 | Gemma 4 E2B (BF16) | **faster-whisper large-v3** |
|---|---|---|
| 書き起こし品質 | ◎（軽微な脱字あり） | ◎（やや上） |
| タイムスタンプ | ❌ ネイティブ非対応（プロンプト時刻は約12秒ズレ） | ✅ **単語単位で正確**（「カメラ」21.94s≒正解21s） |
| 長尺処理 | 30秒チャンク必須 | ✅ 一括処理（内部VAD） |
| 速度 | RTF 0.13–0.19 | ✅ RTF 0.10 |
| VRAM | 10.4GB | ~3GB（large-v3 float16, CTranslate2） |
| 実績 | 新しい | ✅ 字幕用途で豊富 |

→ 字幕に必須の **正確なタイムスタンプ・長尺一括・軽量** で Whisper が明確に優位。
Gemma の利点「モデル統一」は品質/機能上の利点ではないため、ASR は Whisper に決定。

### この決定による副次効果
- 現行 `asr.py` の **Qwen3-ForcedAligner（単語アライメント）が不要**になる（Whisper が時刻を出す）
- faster-whisper は **CTranslate2 エンジン**で `transformers` に依存しない
  → qwen-asr の `transformers==4.57.6` 固定問題が**そもそも消滅**。Gemma 検証で必要だった `transformers>=5.5.0` も ASR には不要
- 翻訳・VL は llama-server(GGUF) のままなので、**backend から transformers 依存自体を撤去できる可能性**（要確認）

## 実装方針（Whisper 固有）

- モデル: `faster-whisper large-v3`（速度重視なら `large-v3-turbo` も選択肢）。`download_root=models`
- 出力: `model.transcribe(..., word_timestamps=True, vad_filter=True)` で
  セグメント（start/end/text）＋単語時刻が直接得られる → 既存の `{"text", "timestamp": (start,end)}` 形式へマップ
- 言語指定: Whisper は2文字コード（`ja`/`en`…）をそのまま受け付ける
  → 現行 `LANGUAGE_MAP`（言語名変換）は**不要化・簡素化**
- **Windows の CUDA DLL 問題**: CTranslate2 は CUDA12 の `cublas64_12.dll` / `cudnn64_9.dll` を要求。
  `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` の wheel を入れ、起動時に各 `nvidia/*/bin` を
  `os.add_dll_directory` + `PATH` 前置で通す（検証スクリプト `scripts/test_whisper.py` の `_add_nvidia_dlls()` 参照）
- VRAM: CTranslate2 は PyTorch アロケータ外（`torch.cuda.max_memory_allocated` では 0 と出る）。
  large-v3 で実測 ~3GB。VL（llama-server 別プロセス）と共有するため、**ASR は使用後アンロード**する設計を維持
- SRT セグメント分割: Whisper のセグメントをそのまま使うか、単語時刻から現行ロジック（文末/最大語数で区切り）を再現するか要検討

## 移行ステップ

### フェーズ 0: 事前検証（完了）
- [x] 検証用 venv `.venv-gemma`（torch 2.12.0+cu130 / Python 3.10）
- [x] faster-whisper large-v3 を日本語動画で実行 → 品質◎・単語時刻が正確（21.94s≒正解）・RTF 0.10
- [x] Gemma 4 との比較で Whisper 採用を決定

### フェーズ 1: 環境整備（conda → venv 本番化）
- [ ] 本番 venv `.venv` を作成（Python 3.10 基準）
- [ ] 依存導入:
  ```
  pip install torch --index-url https://download.pytorch.org/whl/cu130   # vram.py が使用（要否確認）
  pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 ffmpeg-python soundfile srt
  ```
- [ ] `requirements.txt` 更新:
  - 削除: `qwen-asr`、`transformers==4.57.6` ピン、（不要なら）`transformers` / `qwen-vl-utils`
  - 追加: `faster-whisper`、`nvidia-cublas-cu12`、`nvidia-cudnn-cu12`
  - torch の cu130 ホイール導入手順をコメントで明記
- [ ] `start.bat`: `conda activate main` → `.venv\Scripts\activate.bat`
- [ ] CLAUDE.md「環境」「依存パッケージの注意点」を更新（conda→venv、qwen-asr由来の記述撤去）

### フェーズ 2: asr.py の書き換え
- [ ] `Qwen3ASRModel` + `Qwen3-ForcedAligner` → `faster_whisper.WhisperModel` に置換
- [ ] 起動時に CUDA DLL 検索パスを通す処理（`_add_nvidia_dlls` 相当）を追加
- [ ] `extract_audio`（ffmpeg→16kHz/mono）は流用可。faster-whisper は mp4 直接入力も可能なので簡素化検討
- [ ] `transcribe()`: Whisper のセグメント/単語時刻を既存の segment 形式へマップ
- [ ] `LANGUAGE_MAP` を Whisper の言語コード方式へ簡素化（or 撤去）
- [ ] ロード/アンロード（VRAM 解放）ロジックを CTranslate2 向けに調整（使用後アンロード維持）
- [ ] 不要化した ForcedAligner / `_align_to_segments` / チャンク分割ロジックを整理

### フェーズ 3: 後片付け・ドキュメント
- [ ] `/transcribe` SSE イベント仕様に変更があれば CLAUDE.md を更新
- [ ] アーキテクチャ図（CLAUDE.md）の ASR 記述を Qwen3-ASR → Whisper に更新
- [ ] backend から transformers 依存が消せるか確認し、消せれば requirements から除去
- [ ] 旧 Qwen 関連コード・モデルキャッシュの整理
- [ ] 検証スクリプト（scripts/test_*.py）の扱いを決定（残す/消す）

## 留意点・未確定事項
- **モデルサイズ**: large-v3 で十分高速（RTF 0.10）。turbo は更に速いが精度差を要確認
- **torch の要否**: ASR が faster-whisper になると torch は `vram.py` 等だけが使う可能性。残すか整理するか確認
- **VL との VRAM 共有**: ASR(~3GB) + VL(GGUF, llama-server) の同時ロード可否。基本は ASR 使用後アンロード
- **ロールバック**: フェーズ完了まで git ブランチで作業し、main は Qwen3-ASR のまま温存する

---

## 付録: Gemma 4 audio を採用しなかった経緯（記録）

ASR 移行先として Gemma 4 audio を先に検証したが、最終的に不採用とした。判断材料を記録する。

### Gemma 4 audio の特性
- 音声書き起こし（多言語）・音声翻訳に対応。公式実装は Transformers（`AutoModelForMultimodalLM`）
- 制約: **音声入力は最大30秒/回**（長尺はチャンク必須）、`transformers>=5.5.0` 必須
- **llama-server 経由は不可**: `/v1/chat/completions` が `input_audio` 非対応（#21868 "not planned"）、
  公開 mmproj に音声エンコーダ未同梱。2026-04-07 の実機検証でも `audio input is not supported` で失敗
  → Transformers 直叩き必須

### 検証で分かったこと（当環境: BF16 + RTX PRO 5000 Blackwell 48GB）
- 書き起こし品質自体は英語・日本語とも実用レベル（外部記事の量子化GGUF/8GBでの破綻は再現せず）
- ただし **タイムスタンプをネイティブ出力しない**のが致命的:
  - 案1（チャンク粒度）: 粗い
  - 案2（外部 forced aligner 併用）: 高精度だが Gemma+アライナーの二重構成になり、Whisper 単体に対する優位がない
  - 案3（プロンプトで時刻指示）: 形式は守るが**時刻が不正確**（[00:09]と出力した発話が実際は21秒）→ 却下
- 結論: 字幕用途では Whisper がタイムスタンプ・長尺・軽量で上回り、Gemma を ASR に使う動機が無い

### 参考リンク
- Gemma audio docs: https://ai.google.dev/gemma/docs/capabilities/audio
- transformers>=5.5.0 必須の根拠: https://github.com/vllm-project/llm-compressor/issues/2562
- llama-server 音声非対応: https://github.com/ggml-org/llama.cpp/issues/21868
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
