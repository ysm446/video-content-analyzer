# 進捗状況

最終更新: 2026-07-03

## UI デザインガイドライン（2026-07-03 完了）
- [x] デザインルールを `docs/design/ui-design-guidelines.md` に明文化
- [x] §9 の 1〜5・8・9 を実装（トークン化・意味色/青系/フォーカスの統一・絵文字除去・
      文字サイズ/角丸の是正）。7 は一部対応（トークン・フォーカス・バッジを common.css に集約）
- [ ] 残: 6（インラインスタイルのクラス抽出と common.css 分離）、10（ライトテーマ）
- [ ] 実アプリでの見た目確認（特にモデルピルのグラデ変更と辞書ツールチップの配色）は未実施

## チャット（Q&A）の高速化とマルチターン対応（2026-07-03 完了）
- [x] 分析キャッシュのサムネールを QA フレームに再利用（ffmpeg 不要で即開始）
- [x] uniform 抽出を並列シーク化（全編デコード廃止、実測 8枚 0.13秒/60秒動画）
- [x] 直近3ターンの履歴をプロンプトに同梱（動画切り替えでリセット）
- [x] 仕組みを docs/design/qa-chat.md にまとめた
- [ ] 実動画・実モデルでの体感確認は未実施（3-5 の時刻依存密サンプリングは任意のまま）

## ランタイムの手動選択対応（2026-07-03 完了）
- [x] llama-cpp: ビルド一覧（推奨マーク＝ドライバ CUDA 基準）から選択インストール
- [x] llama-cpp: インストール済みバージョンの切り替え（settings.json `llama_version`）
- [x] Whisper: モデル一覧（tiny〜large-v3-turbo）から選択インストール・使用モデル切り替え
- [x] ロジックはユニットテスト済み（推奨判定・選択解決・バリデーション）、live API 確認済み
- [ ] UI からの通し操作（切り替え→文字起こし/分析）の実機確認は未実施

## ランタイム管理（2026-07-03 完了）
- [x] `backend/runtime_manager.py`（状態検出・ダウンロード・展開・zip-slip 対策）
- [x] `GET /runtime/status` / `POST /runtime/install`（SSE 進捗）
- [x] llama-server の実行フォルダを `runtime/llama-server/` 配下の動的検出に変更
- [x] 設定モーダルに「ランタイム」セクション（状態表示＋インストールボタン＋進捗）
- [x] 状態検出・アセット選択・zip-slip はユニットテスト済み
- [ ] 実ダウンロードの通し確認（GitHub からの取得・展開）は未実施

## 設定 UI のポップアップ化（2026-07-03 完了）
- [x] 設定ページを廃止し、左ナビ＋右コンテンツの2カラム・ポップアップに変更
- [x] 背景の暗転＋ぼかしをモデル管理ポップアップと統一
- [x] Esc / 背景クリックで閉じる対応
- [ ] 実アプリでの表示確認（Electron 起動での見た目チェック）は未実施

## 翻訳モード切り替え（2026-07-03 完了）
- [x] quality（従来）/ fast（用語集・動画文脈なし、文脈2ペア＋先読み1行＋バッチ12行）
- [x] 設定 → 字幕 の「翻訳モード」で切り替え、`translation_mode` として永続化
- [ ] fast モードの実測（速度・品質差）は未確認

## 字幕翻訳の精度改善（2026-07-03 完了）
- [x] 設計を `docs/design/translation-accuracy.md` にまとめた
- [x] 1/2: 先読み文脈（次2行）＋構造化1メッセージ化（擬似会話履歴を廃止）
- [x] 7: 翻訳 system prompt に字幕制約を明文化、finish_reason 打ち切り検知
- [x] 3: 分析キャッシュ meta と用語集（json_schema 生成）を system prompt に注入
- [x] 5: 8行バッチ翻訳（json_schema、検証失敗時は行単位フォールバック）
- [ ] 4（文単位の再グルーピング）と 6（ASR 側改善）は効果を見て判断
- [ ] 実動画での品質確認（改善前後の japanese.srt 比較）は未実施

## AI 動画分析パイプラインのレビューと改善実装（2026-07-03 完了）
- [x] 現状把握（video_reviewer.py / server.py / llama_server.py / vram.py / app.html）
- [x] 改善提案を `docs/design/video-analysis-review.md` にまとめた（優先度・着手順付き）
- [x] 2-4: タイムスタンプ解析一本化（h:mm:ss 対応、scenes 全損バグ修正）
- [x] 2-3 / 3-2: json_schema 構造化出力 + finish_reason 検知と SSE 通知
- [x] 2-1 / 3-4: コンテキスト予算による自動削減 + 縮小の SSE 可視化
- [x] 2-2: transcript の時間等間隔サンプリング（長編対策）
- [x] 3-1 / 3-3 / 3-6: キャッシュキー修正・timestamp スナップ・入力バリデーション
- [x] CLAUDE.md の SSE 仕様を現状に同期
- [ ] 3-5（QA の質問依存フレーム選択）と 4節（低優先）は任意対応として保留

## `bin/` → `runtime/` リネーム（2026-07-03 完了）
- [x] フォルダ名変更（llama.cpp 一式は `runtime/llama-server/` 配下のまま）
- [x] `backend/llama_server.py` の `LLAMA_CPP_DIR` デフォルトパス更新
- [x] `.gitignore` を `runtime/` に更新

## チャプター保存を cache に一本化（2026-06-12 完了）
- [x] `.toc.json` の書き込みを廃止（保存は `video.cache/data.json` のみ）
- [x] `POST /review/toc/save` / `POST /review/toc/build`（フロント未使用のレガシー）を削除
- [x] `POST /review/toc/load` は旧 `.toc.json` の読み取り専用フォールバックとして存続
- [x] CLAUDE.md / README.md 更新、`py_compile` OK
- 副産物: 残課題だった refine ループ重複（analyze / toc/build）は toc/build 削除で解消

## コードレビュー指摘の修正（2026-06-12 完了）
- [x] API 保護: Origin/Host ガード追加、CORS 絞り込み、thumbnail filename サニタイズ、
      cache/image の `is_relative_to` 化（curl で全パターン実機検証済み）
- [x] 中止フラグ残留の解消（`sse_canceled()` で canceled 送出時に `clear_cancel()`）
- [x] `/review/toc/build` に `clear_cancel()` と `CanceledError` 処理を追加
- [x] QA の Enter 連打ガード、Markdown サニタイズ（XSS 対策）、サムネール `seeked` タイムアウト
- [x] lucide / marked を `frontend/vendor/` にローカル同梱（CDN 依存解消）
- [x] corrected.srt のみで字幕生成可、辞書 max_tokens 256、ffmpeg エラー詳細化、`qa_frames()` 削除
- [x] 残課題のうち refine ループ重複は toc/build 削除（2026-06-12）で解消
- [ ] 残課題: `vram.py` キャップの実効性見直し
      （llama-server・CTranslate2 には効かない。torch 依存の整理も検討）

## 実行中処理の中止ボタン（進行中の推論も即停止・2026-06-03 完了）
- [x] `backend/cancel.py`: グローバル中断フラグ（Event）＋ `CanceledError`
- [x] `server.py`: `POST /cancel`、各ハンドラで `clear_cancel()`＋`CanceledError`→`canceled` 送出
- [x] `llama_server.py`: `chat()` ストリーミング化、トークン行ごとに中断確認→接続クローズで生成停止
- [x] `asr.py`: セグメント走査で中断確認
- [x] `app.html`: 中止ボタンは `POST /cancel` 方式（fetch abort はしない＝推論中 unload 事故回避）
- [x] `py_compile` / 実 import / フラグ動作を検証
- 既知の制約: プロンプト処理中（最初のトークン前）は反映されず、生成開始後にほぼ即停止

## 動画分析「内容のまとめ」(detail) 追加（2026-06-03 完了）
- [x] `video_reviewer.py`: JSON スキーマ・指示文・salvage に `detail` を追加
- [x] `server.py`: `/review/analyze` result に `detail`、UISettings に `analysis_detail_expanded`（既定 false）
- [x] `app.html`: 概要の下に折りたたみ「内容のまとめ」セクション、meta 経由で受け渡し・Markdown 描画
- [x] 空のときはセクション非表示、既存キャッシュは影響なし、`py_compile` OK
- 未検証: 実モデルが detail を期待どおり充実して出力するかは実機確認が必要

## 文字起こしのローカル LLM 補正（2026-06-03 完了）
- [x] `translator.py`: `REFINE_SYSTEM_PROMPT`（保守的）＋ `refine()` 追加、`get_prompts()` に登録
- [x] `server.py`: `POST /refine`（セグメント単位・時刻保持・前後5件文脈）→ `video.corrected.srt`
- [x] `app.html`: 「補正」ボタン追加、翻訳は `correctedSrtPath` 優先、再オープン時 corrected 自動検出
- [x] `.gitignore` に `*.corrected.srt` 追加、生 ASR は残す方針
- [x] `py_compile` で構文検証OK
- 未検証: 実モデルでの補正品質（保守性・ハルシネーション有無）は実機確認が必要

## 字幕 SRT を cache フォルダに集約（2026-06-03 完了）
- [x] `subtitle.py`: `make_output_path()` を `{stem}.cache/` 配下に、`save_srt()` に親フォルダ自動作成を追加
- [x] `app.html` `tryAutoLoadSrt()`: cache 内優先＋旧・横置きフォールバック（後方互換）
- [x] `/translate` は親フォルダ基準のため変更不要、`.gitignore` も基底名一致で対応済み
- [x] パス生成を venv python で検証（`D:\foo\video.cache\video.original.srt`）

## Q&A の字幕参照改善（embedding なし・2026-06-02 完了）
- [x] 長尺で transcript が先頭3000字固定だった問題を、質問キーワードによる関連行抽出で解消
- [x] `video_reviewer.py`: `_tokenize_query` / `_select_relevant_transcript` 追加、`_build_qa_prompt` で使用
- [x] 一致ゼロ時は全編等間隔サンプリング、TS無し形式は先頭切り出しへフォールバック
- [x] ロジック検証OK（関連行抽出・均等サンプリング・短文素通し）
- 今後の選択肢: 必要ならシーン単位検索→字幕チャンク embedding へ段階的拡張

## ASR 移行（Qwen3-ASR → faster-whisper large-v3）

計画は [plan.md](plan.md) を参照。**2026-05-31: ASR は Whisper 一本化に決定**（Gemma 4 は不採用）。

### フェーズ 0: 事前検証・モデル選定（完了）
- [x] 検証用 venv `.venv-gemma`（torch 2.12.0+cu130 / Python 3.10.19）
- [x] Gemma 4 E2B を検証: 英語・日本語とも書き起こし品質は実用レベル（RTF 0.13–0.19, VRAM 10.4GB）
  - ただしタイムスタンプ非対応。プロンプト指示の時刻は不正確（[00:09]→実際21秒）で却下
- [x] faster-whisper large-v3 を検証: 品質◎・**単語時刻が正確**（「カメラ」21.94s≒正解21s）・RTF 0.10・VRAM ~3GB
- [x] 比較の結果 **Whisper を採用**（タイムスタンプ/長尺一括/軽量で優位）

作業ブランチ: `feat/asr-whisper`

### フェーズ 1: 環境整備（conda → venv 本番化）（完了）
- [x] 本番 venv `.venv` をクリーン構築（Python 3.10 / torch cu130 / requirements.txt）。`import backend.server` 成功
- [x] requirements.txt 更新（qwen-asr/torchaudio/soundfile/qwen-vl-utils 削除、transformers ピン解除、faster-whisper + nvidia-cublas/cudnn-cu12 追加）
- [x] start.bat（.venv 有効化）/ CLAUDE.md（環境・アーキ図・依存注意点）更新

### フェーズ 2: asr.py 書き換え（Qwen3-ASR → faster-whisper）（完了）
- [x] WhisperModel(large-v3-turbo) へ置換、CUDA DLL パス処理、ForcedAligner撤去
- [x] 単語タイムスタンプから句読点・長さ基準で字幕セグメント生成（_words_to_segments）
- [x] 本番 .venv で実機検証: 18セグメント・時刻正確（カメラ=21.76s）・RTF~0.02

### フェーズ 3: 後片付け・ドキュメント
- [x] CLAUDE.md アーキ図・依存注意点更新
- [x] アプリ全体（Electron UI → /transcribe）でのE2E動作確認
  （UI操作で /transcribe 200 → 18セグメント書き起こし → アンロード、エラーなし。turbo で高速）
- [x] feat/asr-whisper を main へマージ（fast-forward）
- [x] 長尺ポーズ時の字幕間延びを修正（GAP_FLUSH_SEC=8s で区切り。「あとね、」16秒→解消）
- [x] 検証用 .venv-gemma を削除（5.34GB 解放）
- [x] transformers/accelerate 依存を撤去（HFフォールバックを遅延import化、requirements/.venvから削除）
- [x] バグ修正: D&Dで分析済み動画を開くとサムネール/分析結果が読み込まれない（未定義関数呼び出し）
- [x] リファクタ: llama-server 管理を共通化（backend/llama_server.py に統合、重複~330行削減）
- [x] 機能: 設定画面にシステムプロンプト閲覧（読み取り専用、GET /prompts）を追加
- [x] 機能(フェーズB): システムプロンプトのユーザー上書き。data/prompts.json に保存、
  編集可は system 4つ（翻訳/辞書/分析/Q&A）、デフォルト不変・リセット可
- [x] 機能拡張: 名前付きプリセットを複数作成・切替・削除（旧単一上書きは自動移行）
- [ ] 旧Qwen関連の残骸確認、gemma検証スクリプト(scripts/test_gemma_asr.py)の要否
- [ ] リモートへ push（リファクタ＋プロンプト閲覧分）
