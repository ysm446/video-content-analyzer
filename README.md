# Video Content Analyzer

動画から字幕を自動生成・2言語同時再生し、AI による動画内容分析もできるデスクトップアプリです。

## 機能

### 字幕生成・再生
- **文字起こし**: Qwen3-ASR-1.7B + ForcedAligner で動画を文字起こし → 単語レベルのタイムスタンプ付き SRT 出力
- **日本語翻訳**: GGUF モデル（llama.cpp）で原文字幕を日本語に翻訳 → 日本語 SRT 出力
- **2言語プレイヤー**: 原文と日本語訳を同時表示（Electron）
- **単語ホバー辞書**: 原文字幕の単語にカーソルを乗せると品詞・意味・例文をツールチップ表示
- **再生速度変更**: 0.5× / 0.75× / 1× / 1.25× / 1.5× をワンクリックで切り替え

### 動画レビュー・チャプター管理
- **内容分析**: 動画からフレームをサンプリングし、GGUF VL モデルで概要・シーン構成・タグ・ジャンルを JSON で出力
- **シーンサムネール**: 各シーンの開始フレームをサムネール表示
- **フレームモード選択**: 均等サンプリングとシーン変化検出（ffmpeg）から選択可能
- **音声連携**: ASR 書き起こしを VL 分析に組み合わせて精度向上
- **Q&A**: 分析後にフレームを参照したまま自由質問が可能
- **チャプター下書き**: 分析直後は一時的な下書きとして保持し、保存ボタンを押したときだけ `.toc.json` とキャッシュに書き込み
- **チャプター編集**: シーン分析結果から自動生成したチャプター一覧をタイトル・時刻・概要を編集して保存
- **近接チャプター抑制**: 動画長に応じた最小チャプター間隔で、近すぎる分割を自動で統合
- **長区間の再分割**: 長すぎるチャプター候補は追加 refine して、後半だけ大きな空白が残りにくいよう補正

### キャッシュ
- 動画ファイルと同じ場所に `{動画名}.cache/` フォルダを作成
- `data.json` にシーン・メタ情報・文字起こしテキストを保存
- `thumbnails/` にシーンサムネール画像を保存
- チャプター保存後、次回同じ動画を開いたとき自動復元

### UI
- 左サイドバーによるページ切り替え（プレイヤー / 設定）
- サイドバー下部の CPU アイコンからモデル管理ポップアップを開いてロード・アンロード
- 下部ステータスバーに文字起こし・字幕生成・動画分析・Q&A の進捗を集約表示
- プレイヤー上部に動画 / 字幕 / 文字起こし / 字幕生成ボタン、チャプター上部に動画分析 / 保存ボタンを配置

## 必要環境

| ソフトウェア | バージョン |
|---|---|
| Python | 3.10 以上 |
| conda | 任意のバージョン |
| Node.js + npm | Electron 実行用 |
| ffmpeg | システムにインストール済みであること |
| CUDA（任意） | GPU 推論を使う場合（強く推奨） |

> ffmpeg のインストール: https://ffmpeg.org/download.html  
> インストール後、`ffmpeg -version` でパスが通っていることを確認してください。

## セットアップ

### 1. conda 環境を作成

```bash
conda create -n main python=3.11
conda activate main
```

### 2. Python 依存パッケージをインストール

```bash
pip install -r requirements.txt
```

### 3. モデルを配置

翻訳・動画レビュー用 GGUF は `models/` 配下に置いてください。

> 現在の ASR 実装メモ: Gemma 4 E4B を `llama.cpp` の `llama-server` 経由で音声入力付き `/v1/chat/completions` に接続する検証を行いましたが、2026-04-07 時点では文字起こし用途では安定動作していません。詳細は `docs/asr-gemma-notes.md` を参照してください。

翻訳・動画レビュー用 GGUF の配置例：

```text
models/
├── Huihui-Qwen3-VL-4B-GGUF/
│   ├── model.gguf
│   └── model.mmproj-f16.gguf
└── Huihui-Qwen3-VL-8B-GGUF/
    ├── model.gguf
    └── model.mmproj-f16.gguf
```

> `models/` 直下のサブフォルダを再帰的にスキャンし、mmproj ファイルが存在する GGUF をレビューモデル、それ以外を翻訳モデルとして認識します。

### 4. npm パッケージをインストール

```bash
npm install
```

## 起動方法

```bat
start.bat
```

VRAM が解放されず残ったときは、補助スクリプトで llama.cpp / バックエンドを停止できます。

```bat
release_vram.bat
```

または手動で：

```bash
conda activate main
npm start
```

> バックエンド（`run_backend.py`）は Electron メインプロセスが子プロセスとして自動起動・終了します。

起動後、`http://127.0.0.1:8765/health` で `{"status":"ok"}` が返れば準備完了です。

> **開発用**: アプリウィンドウ上で `Ctrl+R` または `F5` でページをリロードできます。

## API エンドポイント

### 字幕生成

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/health` | 起動確認 |
| `GET` | `/models` | 翻訳モデル一覧・状態 |
| `POST` | `/models` | 翻訳モデル切り替え |
| `POST` | `/transcribe` | 動画→原文SRT（SSE） |
| `POST` | `/translate` | 原文SRT→日本語SRT（SSE） |
| `POST` | `/lookup` | 単語辞書検索 |

### 動画レビュー

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/review/models` | VL モデル一覧・状態 |
| `POST` | `/review/models` | VL モデル切り替え |
| `POST` | `/review/load` | VL モデルを明示的にロード |
| `POST` | `/review/unload` | VL モデルを VRAM から解放 |
| `POST` | `/review/analyze` | 動画分析（SSE） |
| `POST` | `/review/qa` | 動画への質問（SSE） |
| `POST` | `/review/toc/build` | 動画分析→チャプター生成（SSE） |
| `POST` | `/review/toc/save` | チャプター JSON 保存 |
| `POST` | `/review/toc/load` | チャプター JSON 読み込み（旧形式） |

### キャッシュ

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/cache/save` | `data.json` を保存 |
| `POST` | `/cache/load` | `data.json` を読み込み |
| `POST` | `/cache/patch` | `data.json` に部分マージ |
| `POST` | `/cache/thumbnail` | サムネール画像を保存 |
| `GET` | `/cache/image` | サムネール画像を返す |

### 設定

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/ui-settings` | UI 設定取得 |
| `POST` | `/ui-settings` | UI 設定保存 |

## 出力ファイル

```
video.mp4
├── video.original.srt       # 文字起こし結果
├── video.japanese.srt       # 日本語翻訳字幕
├── video.toc.json           # 保存したチャプター情報
└── video.cache/
    ├── data.json            # 保存後のシーン・メタ・文字起こしキャッシュ
    └── thumbnails/
        ├── scene_0.jpg
        ├── scene_1.jpg
        └── ...
```

## プロジェクト構成

```
video-content-analyzer/
├── models/                    # モデルファイル（gitignore）
│   ├── hub/                   # HuggingFace キャッシュ
│   └── {model-name}/          # GGUF モデルフォルダ
├── backend/
│   ├── asr.py                 # Qwen3-ASR 推論
│   ├── translator.py          # GGUF 翻訳・辞書検索
│   ├── subtitle.py            # SRT 生成・読み込み
│   ├── video_reviewer.py      # GGUF VL 動画分析・Q&A
│   ├── model_catalog.py       # models/ フォルダスキャン
│   ├── vram.py                # VRAM 制限ユーティリティ
│   └── server.py              # FastAPI サーバー（全エンドポイント）
├── frontend/
│   ├── pages/app.html         # 統合 UI
│   ├── css/common.css
│   ├── main.js                # Electron メインプロセス
│   └── preload.js             # IPC ブリッジ
├── settings.json              # 永続化設定（自動生成）
├── run_backend.py             # uvicorn 起動エントリーポイント
├── start.bat                  # Windows 起動スクリプト
├── release_vram.bat           # VRAM 解放補助スクリプト
└── requirements.txt
```

## 使用モデル

| モデル | 用途 | VRAM 目安 |
|---|---|---|
| [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | 音声認識（多言語対応） | ~4 GB |
| [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | 単語タイムスタンプ生成 | ~1 GB |
| GGUF テキストモデル（mmproj なし） | 翻訳・辞書検索 | モデルによる |
| GGUF VL モデル（mmproj あり） | 動画レビュー・Q&A | モデルによる |

## ASR の補足

- Gemma 4 E4B GGUF + `llama-server` は、モデルロード自体は成功しても、音声入力付き `chat/completions` で `audio input is not supported` を返し、文字起こしには使えないケースを確認しています。
- そのため、ASR は現時点では Gemma 4 E4B を前提にせず、専用 ASR か別の音声対応モデルで構成する前提で考えるのが安全です。
- 検証ログと切り分け結果は `docs/asr-gemma-notes.md` にまとめています。

## VRAM 管理

- **CUDA ハードキャップ**: 総 VRAM の 90% を上限に設定
- **モデル重み制限**: `max_memory` で GPU への重み配置を 90% 以内に制限
- **視覚トークン制限**: フレーム 1 枚あたり最大 256 トークン（`vram.py` で調整可能）
- ASR と VL モデルは同時ロードされないよう設計（分析後 ASR は自動アンロード）
- VL モデルは分析・Q&A 後もロードしたまま常駐（サイドバーのアンロードボタンで解放）
