# CLAUDE.md

Claude Code がこのプロジェクトで作業する際の参照ドキュメント。

## プロジェクト概要

動画から字幕を自動生成・2言語同時再生、および Qwen3-VL による動画内容分析ができるデスクトップアプリ。

- **バックエンド**: Python + FastAPI（ポート 8765）
- **フロントエンド**: Electron
- **モデル**: Qwen3-ASR（音声認識）、Qwen3（翻訳・辞書）、Qwen3-VL（動画レビュー）

## 環境

- Python: conda 環境名 `main`
- OS: Windows 11
- Shell: bash（Unix 構文を使う）
- GPU: CUDA があれば使用、なければ CPU にフォールバック

## 重要なパス

| パス | 説明 |
|---|---|
| `models/hub/` | HuggingFace モデルキャッシュ（`HF_HOME=./models`） |
| `backend/` | Python バックエンドパッケージ |
| `backend/vram.py` | VRAM 制限ユーティリティ（全モデル共通） |
| `run_backend.py` | uvicorn 起動エントリーポイント（CUDA キャップ設定） |
| `start.bat` | Windows 起動スクリプト |
| `settings.json` | モデル選択の永続化（自動生成） |

## モデル

| モデルID | 用途 | ローカルパス |
|---|---|---|
| `Qwen/Qwen3-ASR-1.7B` | 音声認識 | `models/hub/models--Qwen--Qwen3-ASR-1.7B/` |
| `Qwen/Qwen3-ForcedAligner-0.6B` | 単語タイムスタンプ | `models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/` |
| `Qwen/Qwen3-1.7B` | 翻訳・辞書（省メモリ） | `models/hub/` |
| `Qwen/Qwen3-4B` | 翻訳・辞書（高品質） | `models/hub/` |
| `Qwen/Qwen3-8B` | 翻訳・辞書（最高品質） | `models/hub/` |
| `Qwen/Qwen3-14B` | 翻訳・辞書（超高品質） | `models/hub/` |
| `huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated` | 動画レビュー（速い） | `models/hub/models--huihui-ai--Huihui-Qwen3-VL-4B-Instruct-abliterated/` |
| `huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated` | 動画レビュー（高品質） | `models/hub/` |

## アーキテクチャ

```
【字幕生成パイプライン】
動画ファイル
  → [backend/asr.py] ffmpeg で音声抽出 → Qwen3-ASR → セグメント
  → [backend/subtitle.py] SRT 生成 → video.original.srt

video.original.srt
  → [backend/translator.py] Qwen3（thinking OFF）→ 日本語テキスト
  → [backend/subtitle.py] SRT 生成 → video.japanese.srt

【動画レビューパイプライン】
動画ファイル
  → [backend/video_reviewer.py] ffmpeg で均等フレームサンプリング（PIL.Image）
  → (オプション) [backend/asr.py] 音声書き起こし → transcript テキスト
  → Qwen3-VL（フレーム画像 + transcript）→ JSON（summary / scenes / tags / genre）
  → Q&A: フレーム + 質問 + transcript → 自由回答

FastAPI SSE でフロントエンドに進捗をストリーミング
asyncio.run_in_executor(None, ...) でブロッキング推論を非同期化
```

## API エンドポイント

### 字幕生成
- `GET  /health` — 起動確認
- `GET  /models` — 翻訳モデル一覧・状態
- `POST /models` — 翻訳・辞書モデル切り替え
- `POST /transcribe` — 動画→原文SRT（SSE）
- `POST /translate` — 原文SRT→日本語SRT（SSE）
- `POST /lookup` — 単語辞書検索

### 動画レビュー
- `GET  /review/models` — VL モデル一覧・状態
- `POST /review/models` — VL モデル切り替え
- `POST /review/unload` — VL モデルを VRAM から解放
- `POST /review/analyze` — 動画分析（SSE）
- `POST /review/qa` — 動画への質問（SSE）

## SSE イベント仕様

### `/review/analyze`
```
loading_asr      → include_audio=true かつ ASR 未ロード時
transcribing     → 音声書き起こし中（include_audio=true 時）
asr_done         → 書き起こし完了 {chars: int}
loading_model    → VL モデルが未ロードの場合のみ
extracting_frames
analyzing        → {count, interval, duration}
done             → {result: {summary, scenes, tags, genre}, meta, transcript}
error            → {message}
```

### `/review/qa`
```
loading_model
extracting_frames
answering        → {count}
done             → {answer}
error            → {message}
```

## 依存パッケージの注意点

- `qwen-asr`（公式パッケージ）を使用するため `transformers==4.57.6` に固定
- `qwen-asr` は conda 環境にインストール済み（`pip install qwen-asr`）
- `transformers` を 5.x 系にアップグレードすると `qwen-asr` が壊れる
- `asr.py` は `transformers.pipeline` ではなく `qwen_asr.Qwen3ASRModel` を使用
- Qwen3-VL には `qwen-vl-utils` と `Pillow` が必要

## VRAM 管理（backend/vram.py）

3段階の制限で VRAM 枯渇を防ぐ：

1. **`set_process_memory_fraction(0.9)`** — `run_backend.py` で起動時に設定。CUDA アロケータ全体（重み + KV キャッシュ + アクティベーション）へのハードキャップ
2. **`max_memory_map()`** — `from_pretrained` の `max_memory` 引数に渡す。モデル重みの GPU 配置制限（`{0: VRAM*90%, "cpu": "32GiB"}`）
3. **`MAX_PIXELS_PER_FRAME = 256 * 28 * 28`** — VL モデルの視覚トークン数を制限（最大 256 トークン/枚）

調整は `vram.py` の定数を変更するだけで全モデルに反映される。

## 翻訳・VL の実装方針

- system prompt に `/no_think` を付与して thinking モードを無効化
- VL モデルは Q&A のために分析後も VRAM に保持（明示的に「VRAM 解放」ボタンで解放）
- ASR は使用後即アンロード（VL と VRAM を共有するため）
- VL の `attn_implementation="sdpa"` で O(N²) → O(N) メモリ削減

## シーン分析の出力形式

```json
{
  "summary": "動画全体の概要（2〜4文）",
  "scenes": [
    {"label": "場面の内容を表す短いタイトル", "description": "説明"},
    ...（場面転換ごと、3〜10 場面）
  ],
  "tags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5"],
  "genre": "ジャンル"
}
```

## 出力ファイル命名規則

```
video.mp4 → video.original.srt  （ASR生成）
          → video.japanese.srt  （翻訳生成）
```

## フロントエンドページ

| ファイル | 役割 |
|---|---|
| `frontend/pages/transcribe.html` | 字幕生成UI（動画選択・進捗・言語選択） |
| `frontend/pages/player.html` | 2言語プレイヤー（字幕オーバーレイ・辞書） |
| `frontend/pages/review.html` | 動画レビューUI（分析・Q&A） |
