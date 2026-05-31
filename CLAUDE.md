# CLAUDE.md

Claude Code がこのプロジェクトで作業する際の参照ドキュメント。

## プロジェクト概要

動画から字幕を自動生成・2言語同時再生、および GGUF VL モデルによる動画内容分析ができるデスクトップアプリ。

- **バックエンド**: Python + FastAPI（ポート 8765）
- **フロントエンド**: Electron
- **モデル**: Qwen3-ASR（音声認識）、GGUF テキストモデル（翻訳・辞書）、GGUF VL モデル（動画レビュー）

## 環境

- Python: conda 環境名 `main`
- OS: Windows 11
- Shell: bash（Unix 構文を使う）
- GPU: CUDA があれば使用、なければ CPU にフォールバック

## ⚠️ 作業ごとに docs/ を更新する（最重要ルール）
**まとまった作業（機能追加・修正・方針変更など）が終わるたびに、必ず `docs/` 内の
該当ファイルを更新すること。** 更新を忘れない。

- [docs/goals.md](docs/goals.md) … 目的・要件・確定方針。**要件や方針が変わったときだけ**更新。
- [docs/plan.md](docs/plan.md) … これからやること（ロードマップ）。着手・完了で項目を移動する。
- [docs/progress.md](docs/progress.md) … 現在の状態・完了/未完了チェックリスト。**毎回**「最終更新」日付を更新。
- [docs/changelog.md](docs/changelog.md) … 変更履歴。**毎回**、その日の作業内容を新しい順（上）に追記。

日付は実際の当日の日付（YYYY-MM-DD）を使う。相対表現（「今日」等）は使わない。

## 重要なパス

| パス | 説明 |
|---|---|
| `models/hub/` | HuggingFace モデルキャッシュ（`HF_HOME=./models`） |
| `models/{name}/` | GGUF モデルフォルダ（サブフォルダを再帰スキャン） |
| `backend/` | Python バックエンドパッケージ |
| `backend/vram.py` | VRAM 制限ユーティリティ（全モデル共通） |
| `backend/model_catalog.py` | models/ フォルダスキャン・モデル一覧生成 |
| `run_backend.py` | uvicorn 起動エントリーポイント（CUDA キャップ設定） |
| `start.bat` | Windows 起動スクリプト |
| `settings.json` | モデル選択・UI 設定の永続化（自動生成） |

## モデル管理の仕組み

- `model_catalog.py` が `models/` 配下を再帰スキャンして GGUF を検出
- mmproj ファイルが存在する GGUF → **VL モデル**（動画レビュー用）
- mmproj ファイルがない GGUF → **テキストモデル**（翻訳・辞書用）
- モデルの選択・ロード・アンロードは UI のモデル管理ポップアップから操作
- 翻訳と辞書検索は同じ `Translator` インスタンス（`translator`）を共用
- VL モデルは分析・Q&A 後もロードしたまま常駐（明示的にアンロードするまで保持）

## アーキテクチャ

```
【字幕生成パイプライン】
動画ファイル
  → [backend/asr.py] ffmpeg で音声抽出 → Qwen3-ASR → セグメント
  → [backend/subtitle.py] SRT 生成 → video.original.srt

video.original.srt
  → [backend/translator.py] GGUF テキストモデル（thinking OFF）→ 日本語テキスト
  → [backend/subtitle.py] SRT 生成 → video.japanese.srt

【動画レビューパイプライン】
動画ファイル
  → [backend/video_reviewer.py] ffmpeg でフレームサンプリング（PIL.Image）
  → (オプション) [backend/asr.py] 音声書き起こし → transcript テキスト
  → GGUF VL モデル（フレーム画像 + transcript）→ JSON（summary / scenes / tags / genre）
  → Q&A: フレーム + 質問 + transcript → 自由回答

【キャッシュパイプライン】
分析完了後
  → フロントエンドで各シーンのフレームをキャプチャ
  → POST /cache/thumbnail でサムネール保存
  → POST /cache/save で data.json 保存（シーン・メタ・transcript）
動画オープン時
  → POST /cache/load で data.json 復元
  → GET /cache/image でサムネール表示

FastAPI SSE でフロントエンドに進捗をストリーミング
asyncio.run_in_executor(None, ...) でブロッキング推論を非同期化
```

## API エンドポイント

### 字幕生成
- `GET  /health` — 起動確認
- `GET  /models` — 翻訳モデル一覧・状態
- `POST /models` — 翻訳モデル切り替え（`{translator: model_id}`）
- `POST /transcribe` — 動画→原文SRT（SSE）
- `POST /translate` — 原文SRT→日本語SRT（SSE）
- `POST /lookup` — 単語辞書検索（翻訳モデルを共用）

### 動画レビュー
- `GET  /review/models` — VL モデル一覧・状態
- `POST /review/models` — VL モデル切り替え
- `POST /review/load` — VL モデルを明示的にロード
- `POST /review/unload` — VL モデルを VRAM から解放
- `POST /review/analyze` — 動画分析（SSE）
- `POST /review/qa` — 動画への質問（SSE）
- `POST /review/toc/build` — 動画分析→チャプター生成（SSE）
- `POST /review/toc/save` — チャプター JSON 保存（旧形式、互換用）
- `POST /review/toc/load` — チャプター JSON 読み込み（旧形式、互換用）

### キャッシュ
- `POST /cache/save` — `{動画名}.cache/data.json` を保存（上書き）
- `POST /cache/load` — `data.json` を読み込み（404 = キャッシュなし）
- `POST /cache/patch` — 既存 `data.json` に部分マージ（transcript 更新等）
- `POST /cache/thumbnail` — base64 画像を `thumbnails/{filename}` に保存
- `GET  /cache/image?video_path=...&name=...` — サムネール画像ファイルを返す

### 設定
- `GET  /ui-settings` — UI 設定取得（volume / playback_rate / frame_mode 等）
- `POST /ui-settings` — UI 設定保存

## キャッシュデータ形式（`{動画名}.cache/data.json`）

```json
{
  "video": "video.mp4",
  "duration": 3600.0,
  "transcript": "文字起こし全文テキスト",
  "meta": {
    "genre": "ジャンル",
    "summary": "動画全体の概要",
    "tags": ["タグ1", "タグ2"]
  },
  "scenes": [
    {
      "id": "scene_0",
      "start_sec": 0.0,
      "end_sec": 142.5,
      "label": "イントロダクション",
      "description": "説明",
      "thumbnail": "thumbnails/scene_0.jpg",
      "source": "auto"
    }
  ],
  "toc": [...],
  "bookmarks": []
}
```

- `source`: `"auto"`（分析生成）または `"manual"`（手動追加）
- キャッシュが存在しない場合は旧形式の `.toc.json` にフォールバック

## SSE イベント仕様

### `/review/analyze`
```
loading_asr      → include_audio=true かつ ASR 未ロード時
transcribing     → 音声書き起こし中
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
- `transformers` を 5.x 系にアップグレードすると `qwen-asr` が壊れる
- `asr.py` は `transformers.pipeline` ではなく `qwen_asr.Qwen3ASRModel` を使用

## VRAM 管理（backend/vram.py）

3段階の制限で VRAM 枯渇を防ぐ：

1. **`set_process_memory_fraction(0.9)`** — 起動時に設定。CUDA アロケータ全体へのハードキャップ
2. **`max_memory_map()`** — `from_pretrained` の `max_memory` 引数に渡す
3. **`MAX_PIXELS_PER_FRAME = 256 * 28 * 28`** — VL モデルの視覚トークン数を制限

調整は `vram.py` の定数を変更するだけで全モデルに反映される。

## 翻訳・VL の実装方針

- system prompt に `/no_think` を付与して thinking モードを無効化
- VL モデルは Q&A のために分析後も VRAM に保持（モデル管理ポップアップでアンロード）
- ASR は使用後即アンロード（VL と VRAM を共有するため）
- 翻訳と辞書検索は `translator` インスタンスを共用（`translator_lookup` は廃止済み）

## 出力ファイル命名規則

```
video.mp4
├── video.original.srt       （ASR 生成）
├── video.japanese.srt       （翻訳生成）
└── video.cache/
    ├── data.json            （シーン・メタ・transcript キャッシュ）
    └── thumbnails/
        └── scene_N.jpg
```

## フロントエンド構成

| 要素 | 説明 |
|---|---|
| `frontend/pages/app.html` | 統合UI（字幕生成・2言語プレイヤー・動画レビュー・Q&A・チャプター編集） |
| 左サイドバー | ページ切り替え（プレイヤー / 設定）+ モデル管理ボタン |
| モデル管理ポップアップ | VL モデルと翻訳モデルの選択・ロード・アンロード |
| ステータスログ | 文字起こし・字幕生成・動画分析の状態を1行ずつ表示 |
| Ctrl+R / F5 | 開発用リロードショートカット（main.js で登録） |

## アイコン

Lucide Icons（CDN）を使用。絵文字は使わない。
`lucide.createIcons()` をスクリプト末尾と動的生成後に呼ぶこと。
