# 進捗状況

最終更新: 2026-06-03

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
