# CLAUDE.md

Claude Code がこのプロジェクトで作業する際の参照ドキュメント。

## プロジェクト概要

動画から字幕を自動生成・2言語同時再生、および GGUF VL モデルによる動画内容分析ができるデスクトップアプリ。

- **バックエンド**: Python + FastAPI（ポート 8765）
- **フロントエンド**: Electron
- **モデル**: faster-whisper（音声認識）、GGUF テキストモデル（翻訳・辞書）、GGUF VL モデル（動画レビュー）

## 環境

- Python: venv `.venv`（`python -m venv .venv` → `pip install -r requirements.txt`）。Python 3.10 基準
- torch は Blackwell(sm_120) 対応のため cu130 ホイール: `pip install torch --index-url https://download.pytorch.org/whl/cu130`
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
| `runtime/llama-server/` | llama.cpp の Windows ビルド（`backend/llama_server.py` が配下を自動検出。設定 → ランタイム からインストール可） |
| `runtime/ffmpeg/` | ffmpeg が PATH に無い環境向けの同梱先（起動時に PATH へ追加） |
| `backend/runtime_manager.py` | ランタイムの状態検出・ダウンロード・展開 |
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
  → [backend/asr.py] faster-whisper（CTranslate2）→ セグメント（時刻付き）
  → [backend/subtitle.py] SRT 生成 → video.cache/video.original.srt

video.cache/video.original.srt
  → (オプション) [backend/translator.py] GGUF テキストモデルで保守的補正（時刻保持）
  → [backend/subtitle.py] SRT 生成 → video.cache/video.corrected.srt

video.cache/video.{original|corrected}.srt
  → [backend/translator.py] GGUF テキストモデル（thinking OFF）→ 日本語テキスト
  → [backend/subtitle.py] SRT 生成 → video.cache/video.japanese.srt

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
- `POST /cancel` — 実行中処理の中断要求（全 SSE 処理共通。`backend/cancel.py` のフラグを立てる）
- `GET  /models` — 翻訳モデル一覧・状態
- `POST /models` — 翻訳モデル切り替え（`{translator: model_id}`）
- `POST /transcribe` — 動画→原文SRT（SSE）
- `POST /refine` — 原文SRT→補正SRT（翻訳モデルで保守的補正・時刻保持・SSE、任意）
- `POST /translate` — 原文/補正SRT→日本語SRT（SSE）
- `POST /lookup` — 単語辞書検索（翻訳モデルを共用）

### 動画レビュー
- `GET  /review/models` — VL モデル一覧・状態
- `POST /review/models` — VL モデル切り替え
- `POST /review/load` — VL モデルを明示的にロード
- `POST /review/unload` — VL モデルを VRAM から解放
- `POST /review/analyze` — 動画分析（SSE）
- `POST /review/qa` — 動画への質問（SSE）
- `POST /review/toc/load` — 旧形式 `.toc.json` の読み込み（後方互換・読み取り専用。保存は `/cache/save` に一本化済み）

### キャッシュ
- `POST /cache/save` — `{動画名}.cache/data.json` を保存（上書き）
- `POST /cache/load` — `data.json` を読み込み（404 = キャッシュなし）
- `POST /cache/patch` — 既存 `data.json` に部分マージ（transcript 更新等）
- `POST /cache/thumbnail` — base64 画像を `thumbnails/{filename}` に保存
- `GET  /cache/image?video_path=...&name=...` — サムネール画像ファイルを返す

### 設定
- `GET  /ui-settings` — UI 設定取得（volume / playback_rate / frame_mode 等）
- `POST /ui-settings` — UI 設定保存

### ランタイム
- `GET  /runtime/status` — llama-cpp / Whisper モデル / ffmpeg のインストール状態
- `POST /runtime/install` — `{component: "llama-cpp" | "whisper" | "ffmpeg"}` をダウンロード・
  インストール（SSE: resolving / downloading / extracting / canceled / done / error）。
  llama-cpp は GitHub 最新リリースから CUDA/CPU ビルドを自動選択、ffmpeg は BtbN ビルド、
  Whisper は faster-whisper モデル重みを models/ へ事前ダウンロード

## キャッシュデータ形式（`{動画名}.cache/data.json`）

```json
{
  "video": "video.mp4",
  "duration": 3600.0,
  "transcript": "文字起こし全文テキスト",
  "meta": {
    "genre": "ジャンル",
    "summary": "動画全体の概要（1〜2文）",
    "detail": "内容のまとめ（概要より詳しい複数文／箇条書き）",
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

### `/translate`
```
loading_model     → Translator モデルが未ロードの場合のみ
building_glossary → 用語集を生成中（字幕全編の等間隔サンプルから固有名詞・専門用語を抽出）
glossary_done     → {terms: int}（生成失敗時はこのイベントなしで続行）
translating       → {current, total}（8行バッチ単位で進捗更新）
translate_warning → {message}（トークン上限打ち切り・バッチ検証失敗→行単位フォールバック等）
canceled          → ユーザーが POST /cancel で中断したとき
done              → {srt_path, total}
error             → {message}
```

### `/review/analyze`
```
loading_model     → VL モデルが未ロードの場合のみ
extracting_frames → {pass: "coarse"} / refine 時は {pass: "refine", current, total, range}
analyzing         → {count, interval, duration, mode, pass, analysis_mode}
analyze_warning   → {pass, message}（トークン上限打ち切り・コンテキスト予算による
                     フレーム間引き/解像度削減・画像処理エラーの縮小リトライを通知）
refine_warning    → {message, current, total, range}（refine 失敗時、coarse 結果で継続）
canceled          → ユーザーが POST /cancel で中断したとき
done              → {result: {summary, detail, scenes, tags, genre}, meta}
error             → {message}
```
※ transcript はフロントエンドがリクエストで送る（analyze 内で ASR は行わない）

### `/review/qa`
```
loading_model
extracting_frames
answering        → {count}
answer_delta     → {delta}（ストリーミング回答）
qa_warning       → {message}（トークン上限打ち切り・フレーム縮小の通知）
canceled         → ユーザーが POST /cancel で中断したとき
done             → {answer, meta}（meta: usage / finish_reason / tokens_per_sec 等）
error            → {message}
```

※ `canceled` は `/transcribe` `/refine` `/translate` `/review/analyze` `/review/qa` 共通の中断イベント。

## 依存パッケージの注意点

- ASR は `faster-whisper`（CTranslate2 エンジン、`transformers` 非依存）。`asr.py` は `WhisperModel` を使用
- CTranslate2 は CUDA12 の `cublas64_12.dll` / `cudnn64_9.dll` を要求するため、
  `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` の wheel が必要。`asr.py` の `_add_nvidia_dlls()` が
  起動時に DLL 検索パスを通す（Windows）
- `transformers` は `translator.py` の HF フォールバック経路でのみ使用（GGUF 翻訳が既定なので通常は不使用）。バージョンピンは不要
- torch は Blackwell(sm_120) 対応のため **cu130 ホイール**を使う

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
- 動画分析の JSON 出力は llama-server の `response_format`（json_schema → GBNF）で構文制約。
  JSON サルベージ機構は打ち切り・旧サーバー向けの保険として存続
- フレーム枚数×1枚あたり解像度は送信前に `LLAMA_CPP_CTX` に収まるよう自動配分
  （`video_reviewer.py` の `_fit_frame_budget`。超過時は解像度→枚数の順に削減し SSE で通知）
- analyze の transcript は先頭切り捨てではなく全編から時間等間隔サンプリング（長編対策）
- scenes[].timestamp はモデルに送ったフレーム時刻の最近傍にサーバー側でスナップ
- 字幕翻訳は 8 行ずつの json_schema バッチ（検証失敗時は行単位フォールバック）。
  参考文脈として直前 5 ペア＋次 2 行の原文を構造化した 1 メッセージで渡す
  （擬似会話履歴方式は廃止）。分析キャッシュの meta（genre/summary/tags）と
  翻訳前に生成した用語集を system prompt に付加して訳語を統一する

## 出力ファイル命名規則

```
video.mp4
└── video.cache/
    ├── video.original.srt   （ASR 生成）
    ├── video.corrected.srt  （LLM 補正・任意。翻訳はこれを優先して使う）
    ├── video.japanese.srt   （翻訳生成）
    ├── data.json            （シーン・メタ・transcript キャッシュ）
    └── thumbnails/
        └── scene_N.jpg
```

字幕も解析成果物として `video.cache/` に集約する。読み込み時は cache 内を優先し、
旧・動画の横（`video.original.srt` 等）にあれば後方互換でフォールバックする。

## フロントエンド構成

| 要素 | 説明 |
|---|---|
| `frontend/pages/app.html` | 統合UI（字幕生成・2言語プレイヤー・動画レビュー・Q&A・チャプター編集） |
| 左サイドバー | プレイヤータブ + 設定ボタン（設定ポップアップを開く）+ モデル管理ボタン |
| 設定ポップアップ | 左に項目ナビ（動画分析 / 字幕 / プロンプト / ランタイム / 情報）、右にパラメータの2カラム構成。背景は暗転＋ぼかし（backdrop-filter）。Esc / 背景クリックで閉じる |
| ランタイム設定 | llama-cpp / Whisper モデル / ffmpeg の状態表示とインストールボタン。ダウンロード進捗は行内に表示し、ステータスバーの中止ボタンで中断可（Whisper を除く） |
| モデル管理ポップアップ | VL モデルと翻訳モデルの選択・ロード・アンロード |
| ステータスログ | 文字起こし・字幕生成・動画分析の状態を1行ずつ表示 |
| 中止ボタン | ステータスバー右側。実行中のみ表示。`POST /cancel` で `backend/cancel.py` のフラグを立て、推論ループ（ASR セグメント走査・llama.cpp ストリーム読取）が安全に停止してから unload する。停止すると SSE で `{status:'canceled'}` が届き各ハンドラが `markCanceled`。fetch の abort は使わない（推論中 unload 事故を避けるため） |
| Ctrl+R / F5 | 開発用リロードショートカット（main.js で登録） |

## アイコン

Lucide Icons（`frontend/vendor/` にローカル同梱。marked も同様）を使用。絵文字は使わない。
`lucide.createIcons()` をスクリプト末尾と動的生成後に呼ぶこと。

## セキュリティ注意

- バックエンド API には Origin/Host ガードあり（`server.py` の `_local_only_guard`）。
  Electron レンダラー（file:// ページ）の Origin は `null` なので許可リストに含めている。
  外部サイトの Origin と非ローカル Host（DNS リバインディング）は 403
- モデル出力（動画内容由来＝信頼できないテキスト）を innerHTML に入れるときは必ず
  `renderMarkdown()`（内部で `sanitizeHtml()`）を通す
- `/cache/thumbnail` の `filename` はパス区切りを含む名前を拒否。`/cache/image` は
  `Path.is_relative_to` でキャッシュフォルダ外参照を拒否
