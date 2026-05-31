# ASR 移行計画: Qwen3-ASR → Gemma 4 (audio)

作成日: 2026-05-31

## 目的

音声書き起こし（ASR）パイプラインを Qwen3-ASR から Gemma 4 の audio 機能へ移行する。
将来的には ASR・翻訳・VL レビューを Gemma 4 系に寄せ、モデル基盤を統一することを見据える。

## 背景・調査結果

### Gemma 4 audio でできること
- 音声書き起こし（ASR、多言語）と音声翻訳（AST）に対応
- 公式実装は **Transformers**（`AutoProcessor` + `AutoModelForMultimodalLM`）
- 対応モデル: `gemma-4-E2B-it` / `E4B-it` / `31B-it` / `26B-A4B-it`

### 決定的な制約 / 前提
| 項目 | 内容 | 出典 |
|---|---|---|
| transformers バージョン | Gemma 4 は **`transformers >= 5.5.0`** が必須 | 公式 / llm-compressor #2562 |
| qwen-asr との非互換 | qwen-asr は **`transformers==4.57.6` 固定**。同一環境で同居不可 | requirements.txt / CLAUDE.md |
| 音声入力長 | **最大 30 秒/回**。長尺はチャンク分割必須 | Gemma audio docs |
| 入力形式 | mono / 16kHz / 32bit float [-1,1] | Gemma audio docs |
| llama.cpp 経由は不可 | `llama-server` は `input_audio` 非対応(#21868 "not planned")、公開 mmproj も音声エンコーダ未同梱。**Transformers 直叩き必須** | llama.cpp #21868 / unsloth GGUF discussion |

### 影響範囲
- transformers に依存しているのは **`backend/asr.py` のみ**
- 翻訳 (`translator.py`) と VL レビュー (`video_reviewer.py`) は **llama-server (subprocess) 方式**で transformers 非依存 → **本移行の影響を受けない**
- したがって `transformers` を 5.5.0+ に上げても壊れるのは ASR だけ。Qwen-ASR を捨てる前提なら衝突は解消する

### 過去の実測検証（2026-04-07）
`ggml-org/gemma-4-E4B-it-GGUF` を `llama-server` で起動し、OpenAI 互換 `/v1/chat/completions` に音声入力を渡す構成を実機で試行 → **失敗**。

- モデル本体・mmproj のロードは成功し、ログに `has audio encoder` も表示される
- しかし音声入力付きリクエストで最終的に次のエラー:
  ```text
  audio input is not supported - hint: if this is unexpected, you may need to provide the mmproj
  ```
- 試した content type: `audio_url` / `audio` / `input_audio` いずれも不可
  - `unsupported content[].type` … JSON 形式が server の期待とずれる段階
  - `audio input is not supported` … 形式は受理されたが server 実装側に音声経路が無い段階（今回はここまで到達）
- **結論: llama-server ベースの Gemma ASR は見送り。Transformers 直叩き経路が必須**（本計画の前提を実測で裏付け）
- 参考: [llama.cpp multimodal docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md) / [audio input discussion #13759](https://github.com/ggml-org/llama.cpp/discussions/13759)

## 最大の技術リスク: タイムスタンプ

現行 `asr.py` は **Qwen3-ForcedAligner** で単語単位タイムスタンプを取得し、SRT のセグメント境界を生成している（`_align_to_segments`）。

**Gemma 4 audio は「書き起こしテキスト」を返すモデルで、単語/セグメント単位のタイムスタンプ出力は仕様化されていない。** 字幕生成にはタイムスタンプが必須なため、ここが移行の最重要課題。

### タイムスタンプ取得の選択肢
1. **チャンク粒度のタイムスタンプ（暫定・最小実装）**
   - 30秒（または更に短い 10〜15秒）窓で区切り、各チャンクの開始/終了秒をそのままセグメントに割当
   - 実装容易だが字幕の同期精度が粗い
2. **外部 forced aligner を併用（推奨・本命）**
   - Gemma でテキスト書き起こし → `ctc-forced-aligner` / WhisperX 等で音声とテキストを強制アライメント
   - 単語単位タイムスタンプを再現でき、現行品質を維持できる
   - 追加依存とVRAM/CPUコストが増える
3. **Gemma にタイムスタンプ付き出力をプロンプト指示**
   - 信頼性が低く非推奨（実験的に検証のみ）

→ **方針: まず案1で疎通 → 品質不足なら案2を導入。**

## 環境方針: conda → venv へ移行

本移行を機に、実行環境を conda 環境 `main` から **venv** に切り替える。

**理由**
- Gemma 4 (`transformers>=5.5.0`) で qwen-asr を完全置換すれば最終形は単一環境で済み、`requirements.txt` ベースで再現できる
- フェーズ0の検証は使い捨て venv を別に立てれば本番環境を汚さない
- conda インストールを利用者に要求せずに済む

**conda 依存箇所（要変更・実質2か所）**
- `start.bat:8` … `call conda activate main` → `call .venv\Scripts\activate.bat`
- `CLAUDE.md`「環境」セクション … conda 環境名 `main` の記述を venv に更新

**注意点**
- PyTorch は CPU 版が入らないよう CUDA ホイールを明示インデックス指定でインストール
  （例: `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121`）。
  システム側は NVIDIA ドライバのみ必要。llama-server は同梱 cuda ビルドなので無関係
- Python バージョンを固定・明記する（transformers 5.x / Gemma 4 が要求する 3.10〜3.12 を想定）

## 移行ステップ

### フェーズ 0: 事前検証（コード変更なし・使い捨て venv）
- [ ] 検証用 venv を作成（本番環境を汚さない）
  ```
  python -m venv .venv-gemma
  .venv-gemma\Scripts\activate
  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
  pip install "transformers>=5.5.0" accelerate soundfile ffmpeg-python Pillow
  # qwen-asr / transformers==4.57.6 は入れない
  ```
- [ ] `gemma-4-E4B-it`（または E2B）をロードし、30秒以下の音声で書き起こし精度・速度・VRAM を計測
- [ ] 日本語・英語・中国語など実利用言語での精度を Qwen3-ASR と比較
- [ ] タイムスタンプ案1（チャンク粒度）の字幕同期が許容範囲か体感確認
- [ ] モデルサイズ選定: E2B（軽量）/ E4B（精度）/ 31B（高精度・高VRAM）
- [ ] 動作する Python / torch / transformers のバージョン組合せを記録

### フェーズ 1: 依存関係の切替 + venv 本番化
- [ ] 本番 venv `.venv` を作成し、フェーズ0で確定した手順で依存を導入
- [ ] `requirements.txt`:
  - 削除: `qwen-asr`、`transformers==4.57.6` ピン
  - 追加: `transformers>=5.5.0`
  - `qwen-vl-utils` の要否を確認（ASR 専用なら削除候補）
  - torch の CUDA ホイール導入手順を README/コメントで明記
- [ ] `start.bat`: `conda activate main` → `.venv\Scripts\activate.bat` に変更
- [ ] CLAUDE.md「環境」セクションを venv 記述に更新
- [ ] CLAUDE.md の「依存パッケージの注意点」セクションを更新（qwen-asr 由来のピン記述を撤去）

### フェーズ 2: asr.py の書き換え
- [ ] `Qwen3ASRModel` → `AutoModelForMultimodalLM` + `AutoProcessor` に置換
- [ ] `extract_audio`（ffmpeg → 16kHz/mono/wav）は流用可。読み込み後 float32 [-1,1] 正規化を保証
- [ ] チャンクサイズを `MAX_ALIGN_SEC=90` → **30秒以下**へ変更（定数名も見直し）
- [ ] チャンク境界で文が切れる対策（オーバーラップ窓 or 無音検出での分割）を検討
- [ ] タイムスタンプ生成（案1 を実装、`_align_to_segments` 相当を置換）
- [ ] `LANGUAGE_MAP`（言語名指定）を Gemma のプロンプト形式へ移植
- [ ] `vram.py` の `max_memory_map()` / dtype 指定を Gemma ロードに適用
- [ ] ロード/アンロード（VRAM 解放）ロジックを維持（VL と VRAM 共有のため使用後アンロード）

### フェーズ 3: 品質改善（必要に応じて）
- [ ] タイムスタンプ案2（外部 forced aligner）を導入し SRT 同期精度を回復
- [ ] チャンク分割の最適化（無音区間検出による自然な境界）

### フェーズ 4: 後片付け・ドキュメント
- [ ] `/transcribe` SSE イベント仕様に変更があれば CLAUDE.md を更新
- [ ] アーキテクチャ図（CLAUDE.md）の ASR 記述を Qwen3-ASR → Gemma 4 に更新
- [ ] 旧 Qwen 関連コード・モデルキャッシュの整理

## 留意点・未確定事項
- **Gemma 4 のタイムスタンプ出力可否**を一次情報で再確認（モデルカード / transformers の gemma4 ドキュメント）。出力できるなら案2が不要になる
- **VRAM**: Transformers の BF16 ロードは GGUF 量子化より重い。E4B/31B のVRAM実測がモデル選定の鍵
- **速度**: 30秒チャンク × 多数 の逐次推論になるため、長尺動画のスループットを Qwen3-ASR と比較しておく
- **ロールバック**: 移行中は qwen-asr と transformers のバージョン衝突で同居不可。フェーズ完了まで git ブランチで作業し、main は Qwen3-ASR のまま温存する

## 参考リンク
- Gemma audio docs: https://ai.google.dev/gemma/docs/capabilities/audio
- transformers gemma4 model doc: https://github.com/huggingface/transformers/blob/main/docs/source/en/model_doc/gemma4.md
- transformers>=5.5.0 必須の根拠: https://github.com/vllm-project/llm-compressor/issues/2562
- llama-server 音声非対応: https://github.com/ggml-org/llama.cpp/issues/21868
