# Movie Review

動画から字幕を自動生成・2言語同時再生し、AI による動画内容分析もできるデスクトップアプリです。

## 機能

### 字幕生成・再生
- **字幕生成**: Qwen3-ASR-1.7B + ForcedAligner で動画を文字起こし → 単語レベルのタイムスタンプ付き SRT 出力
- **日本語翻訳**: `llama.cpp` サーバー経由で Qwen3.5 GGUF（9B / 35B）を使って原文字幕を日本語に翻訳 → 日本語 SRT 出力
- **2言語プレイヤー**: 原文と日本語訳を同時表示（Electron）
- **単語ホバー辞書**: 原文字幕の単語にカーソルを乗せると `Qwen3.5 2B GGUF` による日本語の品詞・意味・例文をツールチップ表示
- **再生速度変更**: 0.5× / 0.75× / 1× / 1.25× / 1.5× をワンクリックで切り替え
- **モデル切り替え**: 翻訳モデルと辞書モデルを UI から個別に選択・永続化（settings.json）

### 動画レビュー（Qwen3.5 GGUF）
- **内容分析**: 動画からフレームをサンプリングし、`llama.cpp` サーバー経由の Qwen3.5 GGUF で概要・シーン構成・タグ・ジャンルを JSON で出力
- **フレームモード選択**: 均等サンプリング（秒間隔指定）とシーン変化検出（ffmpeg）から選択可能
- **シーン自動検出**: 場面転換を検出して各シーンを内容ベースのタイトルで説明（可変件数）
- **音声連携**: 「音声も解析する」チェックで ASR 書き起こしを VL 分析に組み合わせ精度向上
- **Q&A**: 分析後にフレームを参照したまま自由質問が可能（音声書き起こしも活用、出力上限 2048 トークン）
- **目次（TOC）生成**: シーン分析結果から自動的にチャプター一覧を生成。タイトル・開始時刻・概要を編集したうえで JSON ファイルに保存できる
- **モデル選択**: 9B（速い）/ 35B（高品質）を切り替え可能
- **フレーム再利用**: 同じアプリ起動中は切り出し済みフレームをメモリ上にキャッシュし、再質問時に再利用
- **VRAM 解放**: 分析後に動画レビュー用モデルを手動でアンロードできる

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

> `transformers==4.57.6` は ASR 用です。翻訳モデルは HuggingFace Transformers 版ではなく `llama.cpp` + GGUF を使用します。

### 3. モデルを配置

ASR モデルは初回起動時に自動ダウンロードされます。翻訳・動画レビュー用 GGUF は `models/` 配下に置いてください。

```bash
set HF_HOME=./models

# ASR モデル
python -c "from qwen_asr import Qwen3ASRModel; Qwen3ASRModel.from_pretrained('Qwen/Qwen3-ASR-1.7B', forced_aligner='Qwen/Qwen3-ForcedAligner-0.6B')"

```

翻訳・辞書・動画レビュー用 GGUF は次の配置を前提にしています。

```text
models/
├── Qwen3.5-2B-GGUF/
│   └── Qwen3.5-2B-Q4_K_M.gguf
├── Huihui-Qwen3.5-9B-abliterated-GGUF/
│   ├── Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf
│   └── Huihui-Qwen3.5-9B-abliterated.mmproj-f16.gguf
└── Huihui-Qwen3.5-35B-A3B-abliterated-GGUF/
    ├── Huihui-Qwen3.5-35B-A3B-abliterated.Q4_K_M.gguf
    └── Huihui-Qwen3.5-35B-A3B-abliterated.mmproj-f16.gguf
```

`llama.cpp` は既定で `D:\GitHub\llama-b8466-bin-win-cuda-13.1-x64` を参照します。変更する場合は `LLAMA_CPP_DIR` 環境変数を設定してください。

> HuggingFace のモデルは `models/hub/` 以下、GGUF は `models/` 直下の各フォルダ以下に保存されます。

### 4. npm パッケージをインストール

```bash
npm install
```

## 起動方法

```bat
start.bat
```

> Windows では `start.bat` 内で `chcp 65001` を実行し、コンソール出力を UTF-8 に統一しています。

または手動で：

```bash
# Electron 起動（バックエンドは自動起動）
conda activate main
npm start
```

> バックエンド（`run_backend.py`）は Electron メインプロセスが子プロセスとして起動します。  
> アプリケーション終了時にバックエンドも自動停止します。

起動後、`http://127.0.0.1:8765/health` で `{"status":"ok"}` が返れば準備完了です。

## API エンドポイント

### 字幕生成

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/health` | 起動確認 |
| `GET` | `/models` | 翻訳モデル一覧・状態 |
| `POST` | `/models` | 翻訳・辞書モデル切り替え |
| `POST` | `/transcribe` | 動画→原文SRT（SSE） |
| `POST` | `/translate` | 原文SRT→日本語SRT（SSE） |
| `POST` | `/lookup` | 単語辞書検索 |

### 動画レビュー

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/review/models` | 動画レビュー用モデル一覧・状態 |
| `POST` | `/review/models` | 動画レビュー用モデル切り替え |
| `POST` | `/review/unload` | 動画レビュー用モデルを VRAM から解放 |
| `POST` | `/review/analyze` | 動画分析（SSE） |
| `POST` | `/review/qa` | 動画への質問（SSE） |

#### `/review/analyze` リクエスト

```json
{
  "video_path": "C:/path/to/video.mp4",
  "max_frames": 30,
  "min_interval": 5.0,
  "include_audio": false
}
```

- `max_frames`: サンプリングする最大フレーム数（デフォルト 30）
- `min_interval`: フレーム間の最小秒数（デフォルト 5.0）
- `include_audio`: `true` にすると ASR で音声書き起こしを行い、VL 分析に組み込む

#### `/review/qa` リクエスト

```json
{
  "video_path": "C:/path/to/video.mp4",
  "question": "何人の人物が登場しますか？",
  "max_frames": 20,
  "min_interval": 5.0,
  "transcript": ""
}
```

- `transcript`: 分析時に取得した音声書き起こし（フロントエンドが自動でセット）

## 出力ファイル

```
動画ファイル: movie.mp4
  → movie.original.srt    # Qwen3-ASR で生成した原文字幕
  → movie.japanese.srt    # Qwen3 で翻訳した日本語字幕
```

## プロジェクト構成

```
movie-review/
├── models/                    # HuggingFace モデルキャッシュ（gitignore）
├── backend/
│   ├── asr.py                 # Qwen3-ASR-1.7B + ForcedAligner 推論
│   ├── translator.py          # Qwen3.5 GGUF 翻訳・辞書検索
│   ├── subtitle.py            # SRT 生成・読み込みユーティリティ
│   ├── video_reviewer.py      # Qwen3.5 GGUF 動画フレーム分析・Q&A
│   ├── vram.py                # VRAM 使用量制限ユーティリティ
│   └── server.py              # FastAPI サーバー（全エンドポイント）
├── frontend/
│   ├── pages/
│   │   └── app.html           # 統合UI（字幕生成・プレイヤー・レビュー・Q&A・目次編集）
│   ├── css/
│   │   └── common.css
│   ├── main.js                # Electron メインプロセス
│   └── preload.js             # IPC ブリッジ
├── settings.json              # モデル選択の永続化（自動生成）
├── run_backend.py             # uvicorn 起動エントリーポイント
├── start.bat                  # Windows 起動スクリプト
└── requirements.txt
```

## 使用モデル

| モデル | 用途 | VRAM 目安 |
|---|---|---|
| [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | 音声認識（多言語対応） | ~4 GB |
| [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | 単語レベルのタイムスタンプ生成 | ~1 GB |
| Qwen3.5 2B GGUF | 辞書検索（省メモリ） | ~3 GB |
| Qwen3.5 9B GGUF | 日本語翻訳（速い） | ~8 GB |
| Qwen3.5 35B GGUF | 日本語翻訳（高品質） | ~24 GB |
| Qwen3.5 9B Vision GGUF | 動画レビュー・Q&A（速い） | ~10 GB |
| Qwen3.5 35B Vision GGUF | 動画レビュー・Q&A（高品質） | ~26 GB |

## VRAM 管理

本アプリは大容量 VRAM の GPU での使用を想定しており、以下の制限を自動で適用します：

- **CUDA ハードキャップ**: 総 VRAM の 90% を上限に設定（KV キャッシュ含む）
- **モデル重み制限**: `max_memory` で GPU への重み配置を 90% 以内に制限
- **視覚トークン制限**: フレーム 1 枚あたり最大 256 トークン（`vram.py` で調整可能）

ASR と動画レビュー用モデルは同時にロードされないよう設計されています（分析後 ASR は自動アンロード）。
