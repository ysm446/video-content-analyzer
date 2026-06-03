# 変更履歴

## 2026-06-03
- **字幕 SRT を `{動画名}.cache/` に集約**: これまで動画の横に出力していた
  `video.original.srt` / `video.japanese.srt` を、解析成果物として cache フォルダ内に保存
  - `subtitle.py`: `make_output_path()` を `{stem}.cache/{stem}.{suffix}.srt` に変更。
    `save_srt()` に親フォルダ自動作成（`mkdir(parents=True, exist_ok=True)`）を追加
  - `/translate` は入力 SRT の親フォルダ基準で出力するため、cache 内入力なら自動で cache 内出力（変更なし）
  - `app.html` `tryAutoLoadSrt()`: cache 内を優先し、無ければ旧・横置きにフォールバック（後方互換）
  - `.gitignore` の `*.original.srt` / `*.japanese.srt` はベース名一致のため cache 内でも引き続き無視
  - 外部プレイヤー連携は使わない（解析目的）ため横置きの利点はなく、フォルダ集約を優先

## 2026-06-02
- **Q&A チャットを Markdown 表示に変更**: 回答を平文（`textContent`）ではなく Markdown
  レンダリングで表示
  - `app.html` に marked@12（CDN, lucide と同方式）を読み込み、`renderMarkdown()` を追加
    （marked 未読込時は改行保持の素テキストにフォールバック）
  - ストリーミングは生テキストを `qaRaw` に蓄積し、delta ごと／done 時に
    `ans.innerHTML = renderMarkdown(qaRaw)` で再描画
  - `.qa-answer` の `white-space:pre-wrap` を解除し、見出し/リスト/コード/引用/表/水平線/
    リンク等の Markdown 要素用スタイルを追加
- **Q&A の字幕参照を関連部分優先に改善（embedding なし）**: 長尺動画で transcript が
  先頭3000文字に固定切り出しされ、後半の質問に字幕的根拠が渡らない問題を修正
  - `video_reviewer.py` に `_tokenize_query`（英数字トークン＋CJK 2-gram、形態素解析不要）と
    `_select_relevant_transcript` を追加。`[m:ss] 行` を質問キーワードでスコア付けし、
    高スコア行＋前後1行を時系列順に予算（3000字）内で収集
  - 一致ゼロ時は全編から等間隔サンプリング（先頭偏重を回避）、全文が予算内ならそのまま、
    タイムスタンプ無し形式は従来の先頭切り出しにフォールバック
  - `_build_qa_prompt` を `_truncate_transcript` → `_select_relevant_transcript(質問)` に変更
  - 検証: 11375字の字幕で深い位置（25:00付近）の関連行を正しく抽出（先頭切り出しでない）、
    非一致時は 0–1980秒へ均等サンプリング、短文は素通しを確認

## 2026-05-31
- **実装（branch feat/asr-whisper）**: backend/asr.py を faster-whisper(large-v3-turbo) に全面書き換え
  - 単語タイムスタンプから句読点・長さ基準で字幕セグメント生成（ForcedAligner不要化）
  - CTranslate2 の CUDA12 DLL 問題に対応（_add_nvidia_dlls）
  - requirements.txt 整理（qwen-asr/torchaudio/soundfile/qwen-vl-utils 削除、transformersピン解除、faster-whisper+nvidia cu12 追加）
  - start.bat を venv 有効化に、CLAUDE.md を venv/Whisper 構成に更新
  - 本番 .venv をクリーン構築し import/実機ASR検証OK（カメラ=21.76s、RTF~0.02）
  - Electron UI からの E2E 確認OK（/transcribe 200 → 18セグメント → アンロード、エラーなし）
  - feat/asr-whisper を main へマージ（fast-forward）
- 長尺ポーズで字幕が間延びする問題を修正（単語間ギャップ8秒超で区切り。「あとね、」16秒表示を解消）
- 検証用 .venv-gemma を削除（5.34GB 解放）
- goals.md を整備（目的・主要機能・確定方針を明文化。ASR=Whisper 等を反映）
- リソースモニターで GPU/VRAM が出ない不具合を修正: /system-stats が使う pynvml が
  requirements 未記載で .venv に無かった。nvidia-ml-py（+psutil 明示）を requirements に追加
- 動画分析プロセスのレビュー指摘を修正:
  - シーン抽出で max_frames=1 のゼロ除算をガード
  - シーン検出 ffmpeg 失敗を検知してログ＋均等サンプリングへフォールバック
  - _get_duration を format.duration 欠落コンテナ向けにフォールバック対応
  - 未使用のデッドコード（_infer_stream / qa_frames_stream）を削除
  - refine パスを scenes-only 化（summary/tags/genre の無駄生成を止めトークン削減）
- 機能追加: 設定画面にシステムプロンプト閲覧セクションを追加（読み取り専用）。
  translator/video_reviewer に get_prompts() を持たせ GET /prompts で集約、設定タブにカテゴリ別表示。
  対象: 翻訳/辞書/分析(system)/Q&A(system)＋分析の出力指示・JSONフォーマット（計9件）
- 機能追加（フェーズB）: システムプロンプトのユーザー上書き（差し替え）に対応。
  backend/prompts.py で「デフォルト不変＋上書きレイヤー」を実装し、上書きは data/prompts.json に保存（gitignore）。
  編集可は system プロンプト4つ（翻訳/辞書/分析/Q&A）のみ。出力フォーマット系は閲覧専用（解析破壊防止）。
- 機能拡張: 1キーにつき**複数の名前付きプリセット**を作成・切替・削除できるように。
  data/prompts.json を { key: {active, presets:[{id,name,text}]} } 形式へ拡張（旧単一上書き形式は自動移行）。
  API: GET /prompts(presets/active付与) / POST /prompts/preset(作成・更新) / /prompts/active(切替) / /prompts/delete。
  設定UIにプリセット選択ドロップダウン＋名前入力＋新規／保存／削除を追加。resolve(key) は選択中プリセットを使用
- リファクタ: llama-server 管理を共通化。translator の LlamaCppServerManager と
  video_reviewer の LlamaCppVisionServerManager（重複~80%）を backend/llama_server.py の
  LlamaServerManager に統合（port / meta_resolver / mmproj有無 / label / timeout でパラメタ化）。
  重複コード ~330行削減。両マネージャの実起動・停止を E4B モデルで検証OK
- バグ修正: ドラッグ&ドロップで分析済み動画を開くとシーン/サムネール/分析結果が読み込まれない問題。
  D&D 経路が未定義関数 tryAutoLoadToc を呼んでおり ReferenceError でキャッシュ読込が走っていなかった
  → tryAutoLoadCache に修正（「動画を選択」ボタン経路と同一に）。バックエンドのキャッシュ配信は正常と確認済み
- transformers / accelerate 依存を撤去: translator.py の HF フォールバック経路を遅延 import 化し、
  トップレベル import を除去。requirements と .venv から削除（GGUF 経路のみ使用のため実害なし）。
  transformers 無しで import backend.server が成功することを確認
- フェーズ0検証: `.venv-gemma`（transformers 5.9.0 / torch 2.12.0+cu130）で Gemma 4 E2B の
  音声書き起こしパイプラインが動作することを確認
  - sample.mp4 先頭30秒の英語を正確に書き起こし。RTF=0.19 / ピークVRAM 10.4GB / ロード19s
  - 追加依存 `torchvision`・`librosa` が必要と判明（フェーズ1で requirements に反映予定）
  - Gemma 出力はタイムスタンプなしのプレーンテキスト → 字幕用のタイムスタンプ対策が引き続き課題
- 検証用スクリプト `scripts/test_gemma_asr.py` を追加
- 方針決定: Gemma 4(BF16) を第一候補に検証し、日本語/長尺で実用に届かなければ Whisper へ切替。
  Whisper はタイムスタンプをネイティブ出力でき字幕用途に有利な点を判断材料に記録（plan.md に追記）
- 日本語検証: terrain動画30秒で自然な書き起こし・ループ/幻覚/欠落なし（RTF 0.13, VRAM 10.4GB）。
  外部記事の否定材料は再現せず → Gemma 第一候補を継続と判定。残課題はタイムスタンプのみ
- test_gemma_asr.py: 書き起こしを UTF-8 ファイル(_last_transcript.txt)に保存するよう修正（Windowsコンソール文字化け回避）
- タイムスタンプ案3（プロンプト指示）を検証 → 不採用。形式は守るが時刻が不正確
  （[00:09]と出力した発話が実際は21秒目）。案1（粗い）か案2（forced aligner）の二択に絞り込み
- test_gemma_asr.py に --timestamps オプションを追加（案3検証用）
- Whisper一本化を検証: faster-whisper large-v3 を日本語動画(109s)で実行
  - 書き起こし品質◎（「できるようになってます」を正確に。Gemmaは「できるになってます」と脱字）
  - **単語タイムスタンプが正確**: 「カメラ」を21.94秒と判定＝正解(21秒)とほぼ一致。
    ポーズも正しく反映（あと=15.7s / ね=21.8s）。Gemmaプロンプト時刻(9s)とは段違い
  - 速度 RTF 0.10、長尺をチャンク不要で一括処理。CUDA12 cublas/cudnn 同梱wheelで動作
  - scripts/test_whisper.py を追加（DLL検索パス対応・単語時刻出力）
- **決定: ASR は Whisper(faster-whisper large-v3) 一本化**。Gemma 4 は不採用（タイムスタンプ非対応が決め手）。
  翻訳・VL は llama.cpp(GGUF) のまま。plan.md を全面改訂し Gemma 検討は付録として記録
  - 副次効果: ForcedAligner不要化、transformersバージョン衝突問題が消滅（faster-whisperはCTranslate2でtransformers非依存）
- ASR 移行計画（Qwen3-ASR → Gemma 4 audio）を [plan.md](plan.md) に策定
  - transformers>=5.5.0 要件 / 30秒制限 / タイムスタンプ課題 / llama-server 音声非対応の実測検証を整理
- 環境方針を conda → venv 移行とする方針を plan.md に追記（フェーズ0/1）
- docs/ 運用ルールを CLAUDE.md に追加（goals/plan/progress/changelog）
- 旧 asr-gemma-notes.md の検証記録を plan.md に統合し削除
