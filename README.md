# Video Content Analyzer

動画から字幕を自動生成・2言語同時再生し、AI による動画内容分析・チャットができるデスクトップアプリです。
すべてローカル（オフライン）で完結し、外部 API に依存しません。

## 機能

### 字幕生成・再生
- **文字起こし**: faster-whisper（CTranslate2）で動画を直接書き起こし → 単語レベルのタイムスタンプ付き SRT 出力
- **字幕補正（任意）**: LLM による保守的な誤認識補正（時刻保持）
- **日本語翻訳**: GGUF モデル（llama.cpp）で原文字幕を日本語に翻訳 → 日本語 SRT 出力。
  先読み文脈・用語集・動画メタを使う「高品質」と軽量な「高速」の2モード
- **2言語プレイヤー**: 原文と日本語訳を同時表示（下部表示 / 動画に重ねるオーバーレイ）
- **単語ホバー辞書**: 原文字幕の単語にカーソルを乗せると品詞・意味・例文をツールチップ表示
- **再生速度変更**: 0.5×〜1.5× をワンクリックで切り替え

### 動画レビュー・チャプター管理
- **内容分析**: フレームサンプリング＋GGUF VL モデルで概要・チャプター・タグ・ジャンルを生成
  （json_schema による構造化出力、コンテキスト予算に応じたフレーム自動調整）
- **シーンサムネール**: チャプター開始時刻のフレームをサーバー側で正確に抽出して表示
- **フレームモード**: 均等サンプリング / シーン変化検出（ffmpeg）
- **解析モード**: speed / balanced / quality（長いチャプターの refine 再分析）
- **チャプター編集**: タイトル・時刻・概要を編集して保存。近接チャプターの自動統合

### チャット（Q&A）
- 動画のフレーム＋字幕を参照するマルチターンチャット（Markdown 表示・ストリーミング）
- 分析済み動画はサムネール再利用で即応答開始（ffmpeg 再抽出なし）
- テンプレート質問チップ: 固定（要約等）は即表示、内容ベースの質問はモデルロード済みのとき遅延生成

### ランタイム・モデル管理
- **設定 → ランタイム** から llama.cpp（GitHub 最新リリースの CUDA/CPU/Vulkan ビルドを選択）、
  Whisper モデル（tiny〜large-v3-turbo）、ffmpeg をダウンロード・切り替え
- モデル管理ポップアップで GGUF（VL / テキスト）のロード・アンロード

### キャッシュ
- 動画ファイルと同じ場所に `{動画名}.cache/` フォルダを作成
- SRT（原文・補正・日本語）、`data.json`（チャプター・メタ・文字起こし）、`thumbnails/` を保存
- 次回同じ動画を開いたとき自動復元

## 必要環境

| ソフトウェア | バージョン |
|---|---|
| Python | 3.10 以上 |
| Node.js + npm | Electron 実行用 |
| ffmpeg | PATH にあること（無い場合は 設定 → ランタイム からインストール可） |
| CUDA（任意） | GPU 推論を使う場合（強く推奨） |

## セットアップ

### 1. Python venv を作成して依存をインストール

```bash
python -m venv .venv
.venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cu130  # Blackwell(sm_120) 対応
pip install -r requirements.txt
```

### 2. モデルを配置

- **Whisper**: 初回の文字起こし時に自動ダウンロード（設定 → ランタイム から事前ダウンロードも可）
- **llama.cpp（llama-server）**: `runtime/llama-server/` に配置（設定 → ランタイム からインストール可）
- **GGUF モデル**: `models/` 配下にフォルダごと配置

```text
models/
├── Qwen3-VL-8B-GGUF/
│   ├── model.gguf
│   └── model.mmproj-f16.gguf   ← mmproj あり = VL（動画分析・チャット・翻訳共用）
└── SomeText-GGUF/
    └── model.gguf              ← mmproj なし = テキスト（翻訳・辞書）
```

### 3. npm パッケージをインストール

```bash
npm install
```

## 起動方法

```bat
start.bat
```

VRAM が解放されず残ったときは `release_vram.bat` で llama.cpp / バックエンドを停止できます。

起動後、`http://127.0.0.1:8765/health` で `{"status":"ok"}` が返れば準備完了です。

> **開発用**: アプリウィンドウ上で `Ctrl+R` または `F5` でページをリロードできます。

## API エンドポイント

詳細（SSE イベント仕様含む）は [CLAUDE.md](CLAUDE.md) を参照。

### 字幕生成

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/health` | 起動確認 |
| `POST` | `/cancel` | 実行中処理の中断要求（全 SSE 処理共通） |
| `GET/POST` | `/models` | 翻訳モデル一覧・切り替え |
| `POST` | `/transcribe` | 動画→原文SRT（SSE） |
| `POST` | `/refine` | 原文SRT→補正SRT（SSE・任意） |
| `POST` | `/translate` | 原文/補正SRT→日本語SRT（SSE。`mode: quality \| fast`） |
| `POST` | `/lookup` | 単語辞書検索 |

### 動画レビュー・チャット

| メソッド | パス | 説明 |
|---|---|---|
| `GET/POST` | `/review/models` | VL モデル一覧・切り替え |
| `POST` | `/review/load` / `/review/unload` | VL モデルのロード / 解放 |
| `POST` | `/review/analyze` | 動画分析（SSE） |
| `POST` | `/review/qa` | 動画への質問（SSE・マルチターン） |
| `POST` | `/review/questions` | テンプレート質問チップ用のおすすめ質問生成 |
| `POST` | `/review/toc/load` | 旧形式 `.toc.json` の読み込み（後方互換） |

### キャッシュ

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/cache/save` / `/cache/load` / `/cache/patch` | `data.json` の保存 / 読み込み / 部分マージ |
| `POST` | `/cache/thumbnails/generate` | シーンサムネールをサーバー側で生成 |
| `POST` | `/cache/thumbnail` | base64 画像の保存（手動シーン等） |
| `GET` | `/cache/image` | サムネール画像を返す |

### 設定・ランタイム

| メソッド | パス | 説明 |
|---|---|---|
| `GET/POST` | `/ui-settings` | UI 設定の取得 / 保存 |
| `GET` | `/runtime/status` | llama-cpp / Whisper / ffmpeg のインストール状態 |
| `GET` | `/runtime/llama/builds` | llama.cpp 最新リリースのビルド一覧（推奨付き） |
| `POST` | `/runtime/llama/select` / `/runtime/whisper/select` | 使用バージョン / モデルの切り替え |
| `POST` | `/runtime/install` | ランタイムのダウンロード・インストール（SSE） |
| `GET/POST` | `/prompts` ほか | システムプロンプトのプリセット管理 |

## 出力ファイル

```
video.mp4
└── video.cache/
    ├── video.original.srt   # 文字起こし結果
    ├── video.corrected.srt  # LLM 補正後の字幕（任意・翻訳はこれを優先）
    ├── video.japanese.srt   # 日本語翻訳字幕
    ├── data.json            # シーン・メタ・チャプター・文字起こしキャッシュ
    └── thumbnails/
        └── scene_N.jpg
```

## プロジェクト構成

```
video-content-analyzer/
├── models/                    # モデルファイル（gitignore）
│   ├── hub/                   # HuggingFace キャッシュ（Whisper 等）
│   └── {model-name}/          # GGUF モデルフォルダ
├── runtime/                   # 外部ランタイム（gitignore）
│   ├── llama-server/          # llama.cpp の Windows ビルド（複数バージョン可）
│   └── ffmpeg/                # ffmpeg（PATH に無い環境向け）
├── backend/
│   ├── asr.py                 # faster-whisper 音声書き起こし
│   ├── translator.py          # GGUF 翻訳・辞書・用語集
│   ├── subtitle.py            # SRT 生成・読み込み
│   ├── video_reviewer.py      # GGUF VL 動画分析・Q&A・サムネール
│   ├── llama_server.py        # llama-server プロセス管理（翻訳/VL 共用）
│   ├── runtime_manager.py     # ランタイムの状態検出・インストール
│   ├── model_catalog.py       # models/ フォルダスキャン
│   ├── prompts.py             # システムプロンプトのプリセット管理
│   ├── vram.py                # VRAM 制限ユーティリティ
│   ├── cancel.py              # 中断フラグ
│   └── server.py              # FastAPI サーバー（全エンドポイント）
├── frontend/
│   ├── pages/app.html         # 統合 UI
│   ├── css/common.css         # デザイントークン・共通コンポーネント
│   ├── vendor/                # lucide / marked（ローカル同梱）
│   ├── main.js                # Electron メインプロセス
│   └── preload.js             # IPC ブリッジ
├── docs/                      # ドキュメント（goals / plan / progress / changelog / design）
├── settings.json              # 永続化設定（自動生成）
├── run_backend.py             # uvicorn 起動エントリーポイント
├── start.bat                  # Windows 起動スクリプト
└── requirements.txt
```

## 使用モデル

| モデル | 用途 | VRAM 目安 |
|---|---|---|
| faster-whisper（tiny〜large-v3-turbo） | 音声認識（単語タイムスタンプ付き） | 〜3 GB（large 系 / float16） |
| GGUF テキストモデル（mmproj なし） | 翻訳・辞書検索 | モデルによる |
| GGUF VL モデル（mmproj あり） | 動画分析・チャット（翻訳と共用可） | モデルによる |

## VRAM 管理

- **CUDA ハードキャップ**: 総 VRAM の 90% を上限に設定
- **視覚トークン制限**: フレーム 1 枚あたり最大 256 トークン（`vram.py` で調整可能）。
  コンテキスト長に収まるようフレーム枚数×解像度を送信前に自動配分
- ASR は使用後自動アンロード（VL と VRAM を共有するため）
- VL モデルは分析・チャット後もロードしたまま常駐（モデル管理からアンロード）

## ドキュメント

- [docs/plan/goals.md](docs/plan/goals.md) — 目的・要件・確定方針
- [docs/plan/plan.md](docs/plan/plan.md) — ロードマップ
- [docs/plan/progress.md](docs/plan/progress.md) — 進捗・現在の状態
- [docs/changelog.md](docs/changelog.md) — 変更履歴
- [docs/design/](docs/design/) — 設計レビュー・UI デザインガイドライン
