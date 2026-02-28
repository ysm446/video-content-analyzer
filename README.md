# Movie Review

動画から字幕を自動生成・2言語同時再生し、AI による動画内容分析もできるデスクトップアプリです。

## 機能

### 字幕生成・再生
- **字幕生成**: Qwen3-ASR-1.7B + ForcedAligner で動画を文字起こし → 単語レベルのタイムスタンプ付き SRT 出力
- **日本語翻訳**: Qwen3（1.7B / 4B / 8B / 14B から選択）で原文字幕を日本語に翻訳 → 日本語 SRT 出力
- **2言語プレイヤー**: 原文と日本語訳を同時表示（Electron）
- **単語ホバー辞書**: 原文字幕の単語にカーソルを乗せると日本語の品詞・意味・例文をツールチップ表示
- **再生速度変更**: 0.5× / 0.75× / 1× / 1.25× / 1.5× をワンクリックで切り替え
- **モデル切り替え**: 翻訳モデルと辞書モデルを UI から個別に選択・永続化（settings.json）

### 動画レビュー（Qwen3-VL）
- **内容分析**: 動画から均等にフレームをサンプリングし、概要・シーン構成・タグ・ジャンルを JSON で出力
- **シーン自動検出**: 場面転換を検出して各シーンを内容ベースのタイトルで説明（固定3区分ではなく可変）
- **音声連携**: 「音声も解析する」チェックで ASR 書き起こしを VL 分析に組み合わせ精度向上
- **Q&A**: 分析後にフレームを参照したまま自由質問が可能（音声書き起こしも活用）
- **モデル選択**: 4B（速い）/ 8B（高品質）を切り替え可能
- **VRAM 解放**: 分析後に VL モデルを手動でアンロードできる

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

> `transformers==4.57.6` に固定されています（Qwen3-ASR の `qwen-asr` パッケージとの互換性のため）。

### 3. モデルをダウンロード

初回起動時に自動ダウンロードされます。手動でダウンロードする場合:

```bash
set HF_HOME=./models

# ASR モデル
python -c "from qwen_asr import Qwen3ASRModel; Qwen3ASRModel.from_pretrained('Qwen/Qwen3-ASR-1.7B', forced_aligner='Qwen/Qwen3-ForcedAligner-0.6B')"

# 翻訳モデル（例: 1.7B）
python -c "from transformers import AutoModelForCausalLM; AutoModelForCausalLM.from_pretrained('Qwen/Qwen3-1.7B')"

# VL モデル（例: 4B）
python -c "from transformers import Qwen3VLForConditionalGeneration; Qwen3VLForConditionalGeneration.from_pretrained('huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated')"
```

> モデルは `models/hub/` 以下に保存されます（gitignore 済み）。

### 4. npm パッケージをインストール

```bash
npm install
```

## 起動方法

```bat
start.bat
```

または手動で：

```bash
# バックエンド
conda activate main
python run_backend.py

# フロントエンド（別ターミナル）
npm start
```

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
| `GET` | `/review/models` | VL モデル一覧・状態 |
| `POST` | `/review/models` | VL モデル切り替え |
| `POST` | `/review/unload` | VL モデルを VRAM から解放 |
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
│   ├── translator.py          # Qwen3 翻訳・辞書検索
│   ├── subtitle.py            # SRT 生成・読み込みユーティリティ
│   ├── video_reviewer.py      # Qwen3-VL 動画フレーム分析・Q&A
│   ├── vram.py                # VRAM 使用量制限ユーティリティ
│   └── server.py              # FastAPI サーバー（全エンドポイント）
├── frontend/
│   ├── pages/
│   │   ├── transcribe.html    # 字幕生成ページ
│   │   ├── player.html        # 2言語プレイヤー
│   │   └── review.html        # 動画レビュー（分析・Q&A）
│   ├── css/
│   │   └── common.css
│   ├── main.js                # Electron メインプロセス
│   ├── preload.js             # IPC ブリッジ
│   └── package.json
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
| [Qwen/Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) | 日本語翻訳・辞書検索（省メモリ） | ~3.5 GB |
| [Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B) | 日本語翻訳・辞書検索（高品質） | ~8 GB |
| [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | 日本語翻訳・辞書検索 | ~16 GB |
| [Qwen/Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B) | 日本語翻訳・辞書検索（最高品質） | ~28 GB |
| [huihui-ai/Huihui-Qwen3-VL-4B](https://huggingface.co/huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated) | 動画レビュー・Q&A（速い） | ~10 GB |
| [huihui-ai/Huihui-Qwen3-VL-8B](https://huggingface.co/huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated) | 動画レビュー・Q&A（高品質） | ~18 GB |

## VRAM 管理

本アプリは大容量 VRAM の GPU での使用を想定しており、以下の制限を自動で適用します：

- **CUDA ハードキャップ**: 総 VRAM の 90% を上限に設定（KV キャッシュ含む）
- **モデル重み制限**: `max_memory` で GPU への重み配置を 90% 以内に制限
- **視覚トークン制限**: フレーム 1 枚あたり最大 256 トークン（`vram.py` で調整可能）

ASR と VL モデルは同時にロードされないよう設計されています（分析後 ASR は自動アンロード）。
