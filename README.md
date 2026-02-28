# Movie Review

動画から字幕を自動生成し、2言語同時再生できるデスクトップアプリです。

## 機能

- **字幕生成**: Qwen3-ASR-1.7B + ForcedAligner で動画を文字起こし → 単語レベルのタイムスタンプ付き SRT 出力
- **日本語翻訳**: Qwen3（1.7B / 4B / 8B から選択）で原文字幕を日本語に翻訳 → 日本語 SRT 出力
- **2言語プレイヤー**: 原文と日本語訳を同時表示（Electron）
- **単語ホバー辞書**: 原文字幕の単語にカーソルを乗せると日本語の品詞・意味・例文をツールチップ表示
- **再生速度変更**: 0.5× / 0.75× / 1× / 1.25× / 1.5× をワンクリックで切り替え
- **モデル切り替え**: 翻訳モデルと辞書モデルを UI から個別に選択・永続化（settings.json）

## 必要環境

| ソフトウェア | バージョン |
|---|---|
| Python | 3.10 以上 |
| conda | 任意のバージョン |
| Node.js + npm | Electron 実行用 |
| ffmpeg | システムにインストール済みであること |
| CUDA（任意） | GPU 推論を使う場合 |

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

> `transformers==4.57.6` に固定されています（Qwen3-ASR の `qwen-asr` パッケージとの互換性のため）。

### 3. モデルをダウンロード

初回起動時に自動ダウンロードされます。手動でダウンロードする場合:

```bash
set HF_HOME=./models

python -c "
from qwen_asr import Qwen3ASRModel
Qwen3ASRModel.from_pretrained('Qwen/Qwen3-ASR-1.7B', forced_aligner='Qwen/Qwen3-ForcedAligner-0.6B')
"
python -c "
from transformers import AutoModelForCausalLM
AutoModelForCausalLM.from_pretrained('Qwen/Qwen3-1.7B')
"
```

> モデルは `models/hub/` 以下に保存されます（gitignore 済み）。

### 4. npm パッケージをインストール

```bash
cd frontend
npm install
```

## 起動方法

### バックエンド（FastAPI）を起動

```bash
start.bat
```

または手動で：

```bash
conda activate main
python run_backend.py
```

起動後、`http://127.0.0.1:8765/health` で `{"status":"ok"}` が返れば準備完了です。

### フロントエンド（Electron）を起動

```bash
cd frontend
npm start
```

## API エンドポイント

### `GET /health`
サーバーの起動確認。

### `GET /models`
利用可能な翻訳モデルの一覧と、現在の選択・ロード状態を返す。

### `POST /models`
翻訳モデル・辞書モデルを切り替える。

```json
{
  "translator": "Qwen/Qwen3-4B",
  "lookup": "Qwen/Qwen3-1.7B"
}
```

### `POST /transcribe`
動画を文字起こしして `.original.srt` を生成する。

```json
{
  "video_path": "C:/path/to/video.mp4",
  "language": null
}
```

- `language`: `"en"` / `"zh"` / `"ko"` など。`null` で自動検出。
- 進捗は **SSE（Server-Sent Events）** でストリーミングされる。

### `POST /translate`
`.original.srt` を日本語に翻訳して `.japanese.srt` を生成する。

```json
{
  "srt_path": "C:/path/to/video.original.srt"
}
```

- セグメントごとに翻訳し、進捗を SSE でストリーミングする。

### `POST /lookup`
英単語の日本語定義を返す。

```json
{ "word": "ephemeral" }
```

## 出力ファイル

```
動画ファイル: movie.mp4
  → movie.original.srt    # Qwen3-ASR で生成した原文字幕
  → movie.japanese.srt    # Qwen3 で翻訳した日本語字幕
```

## プロジェクト構成

```
language-caption-player/
├── models/                  # HuggingFace モデルキャッシュ（gitignore）
├── backend/
│   ├── asr.py               # Qwen3-ASR-1.7B + ForcedAligner 推論
│   ├── translator.py        # Qwen3 翻訳・辞書検索
│   ├── subtitle.py          # SRT 生成・読み込みユーティリティ
│   └── server.py            # FastAPI サーバー
├── frontend/
│   ├── pages/
│   │   ├── transcribe.html  # 字幕生成ページ
│   │   └── player.html      # 2言語プレイヤー
│   ├── css/
│   │   └── common.css
│   ├── main.js              # Electron メインプロセス
│   └── package.json
├── settings.json            # モデル選択の永続化（自動生成）
├── run_backend.py           # uvicorn 起動エントリーポイント
├── start.bat                # Windows 起動スクリプト
└── requirements.txt
```

## 使用モデル

| モデル | 用途 |
|---|---|
| [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | 音声認識（多言語対応） |
| [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | 単語レベルのタイムスタンプ生成 |
| [Qwen/Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) | 日本語翻訳・辞書検索（省メモリ） |
| [Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B) | 日本語翻訳・辞書検索（高品質） |
| [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | 日本語翻訳・辞書検索（最高品質） |
