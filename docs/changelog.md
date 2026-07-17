# 変更履歴

## 2026-07-18
- **動画下の詳細エリアにスクロールバー修正と「スペック」セクションを追加**
  - `.player-left`（グリッドアイテム）に `min-height:0` が無く、詳細エリアが
    内容の高さまで伸びて画面外に切れ、スクロールバーが出なかったのを修正
  - `POST /video/info` を新設（ffprobe）。解像度・長さ・ファイルサイズ・
    映像コーデック/fps・音声コーデック/サンプリングレート/ch・ビットレート・
    形式を「スペック」セクション（折りたたみ可・`analysis_spec_expanded` で
    開閉状態を永続化）に表示。動画を開いたときに取得し、分析の有無に関係なく表示
  - 表示は縦のグリッドではなく横並びのチップ（タグと同系統の見た目・
    幅が足りなければ折り返し）で縦幅を取らない
  - 検証: TestClient ＋テスト動画で /video/info（200/404・音声なし動画の
    null 項目）を確認。JS は構文チェックのみ
- **チャットの「履歴をクリア」ボタンを改善**
  - eraser アイコンを付けて見つけやすくした（チャットパネル右上）
  - クリア時に、前の会話に基づくフォローアップ質問チップもリセットして
    再生成するようにした（以前は古い会話前提のチップが残っていた）
- **Q&A・おすすめ質問にユーザーのしおり（ブックマーク）を文脈として渡すように**
  - `/review/qa` と `/review/questions` のリクエストに `bookmarks`
    （`[{time_sec, title, comment}, ...]`）を追加し、フロントが transcript と
    同様に送信（フロントが文脈を渡す既存方針と一貫）
  - プロンプトには「[m:ss] タイトル — コメント」の行形式で、
    **「ユーザーが付けたしおり」と明示ラベル付き**のセクションとして挿入
    （客観情報の transcript・フレームと主観メモを混同させないため）。
    メモなしは「（メモなし）」、コメントは200文字・件数は30件で打ち切り。
    しおりが無い動画ではセクション自体を追加しない
  - おすすめ質問生成では「しおりの場面に関連する質問を候補に含めてもよい」と指示
  - 検証: `_format_bookmarks` / `_build_qa_prompt` を単体実行（不正エントリの
    スキップ・打ち切り・後方互換）、pydantic モデルの受理を確認。JS は構文チェックのみ
- **ステータスバーに処理対象のファイル名を表示**
  - 文字起こし・補正・字幕生成・動画分析の実行中（および完了・エラー・中断後）に、
    ステータスバーのタスク名の隣へ対象動画のファイル名をアクセント色で表示
  - 対象名はタスク開始時に記録するため、処理中に別の動画を開いても
    「どのファイルへの処理か」が正しく表示され続ける
- **±3秒スキップボタンをプレイヤーコントロールに追加**
  - 再生ボタンの両隣に「3秒戻る / 3秒進む」ボタンを配置（先頭・末尾でクランプ）
  - 既存の字幕ジャンプボタンとの区別: 時間スキップは YouTube 式の
    円形矢印＋数字「3」のカスタム SVG（相対移動）、字幕ジャンプは従来の
    縦棒付き skip アイコン（区切りへ移動）。さらに「内側＝±3秒＋再生、
    外側＝字幕ジャンプ」のグループ配置（`.ctrl-seek-group`）で視覚的にも分離
- **ユーザーブックマーク（しおり）機能を追加**
  - チャプターエリアをタブ切り替え（チャプター / ブックマーク）にし、再生位置で
    ボタンを押すとサムネール付きのしおりを記録できるようにした。タイトルと
    コメントはインライン編集（追加直後は編集モードで開く）
  - LLM 生成チャプターとは別レイヤー: data.json の `bookmarks[]` に持ち、
    再分析でも消えない。チャプターの下書き→保存方式と違い **即時保存**
    （`/cache/patch` で bookmarks キーのみパッチ）
  - 追加ボタンはプレイヤーコントロール列（bookmark-plus アイコン）と
    ブックマークタブ内の2箇所。行クリックでその時刻へシーク、「…」メニューから
    編集・削除（削除時はサムネールファイルも後始末）
  - タブ内の追加ボタンは全幅 sticky → タブ列右端の小さなアイコンボタン
    （bookmark-plus・ブックマークタブ表示中のみ）に変更。ヘッダー内なので
    リストをスクロールしても隠れず、場所も取らない
  - サムネールはサーバー側 ffmpeg 入力シークで生成する
    `POST /cache/bookmarks/thumbnail` を新設（`thumbnails/bookmark_{id}.jpg`・
    最大辺480px・末尾時刻はクランプ）。削除用に `POST /cache/thumbnail/delete` も
    新設（`bookmark_*.jpg` のみ許可し、シーンサムネールは対象外）
  - ファイル一覧の分析済みバッジ判定を data.json の存在チェックから内容
    （meta / scenes / toc の有無）に変更。ブックマークだけ付けた未分析動画に
    「分析済み」バッジが付く誤表示を防ぐ
  - 検証: 新エンドポイント（生成・パス検証 400・削除・analyzed 判定）を
    TestClient ＋テスト動画で確認。フロント JS は構文チェックのみ
- **ファイル一覧パネルに幅リサイズと「…」メニュー（名前変更・削除）を追加**
  - パネル右端のドラッグで幅を調整（180〜480px、`file_panel_width` として永続化、
    ダブルクリックで既定幅 272px に戻す）
  - 各行（動画・フォルダ）にホバーで「…」ボタンを表示。メニューから
    「名前を変更」（行内インライン編集。Enter 確定 / Esc・フォーカス喪失でキャンセル）と
    「ごみ箱に移動」を実行できる
  - リネームは `POST /file/rename` を新設。動画はサイドカーも一緒にリネーム
    （`.cache/` フォルダとその中の SRT・旧横置き SRT・`_screenshot/`・data.json の
    `video` フィールド）。動画本体を最初にリネームするため、ロック等で失敗しても
    サイドカーは無傷。名前の不正文字は 400、同名衝突は 409
  - 削除は完全削除ではなく **OS のごみ箱に移動**（Electron `shell.trashItem` の
    IPC `fs:trashItem` を新設）。動画はキャッシュ・字幕・スクリーンショットもまとめて移動
  - 開いている動画（またはそれを含むフォルダ）をリネーム・削除するときは、
    Windows のファイルロックを避けるため先にプレイヤーを閉じて実行
    （`closeCurrentVideo()` 新設）。リネーム後は新パスで自動再オープン
  - フォルダのリネーム・削除後は旧パス配下のツリーキャッシュ・展開状態を破棄して再取得
  - 検証: リネーム（サイドカー追随・data.json 更新・スペース入り名）・フォルダリネーム・
    404/400/409・幅クランプを実サーバー（別ポート）で curl 確認。JS は構文チェックのみ
- **ファイルマネージャー（左サイドバーの動画一覧）を追加**
  - ルートフォルダを選択すると、配下のフォルダ・動画を実階層ツリーで一覧表示
    （トップバー左端の folder-tree ボタンで表示/非表示。選択は `root_folder` として永続化）
  - バックエンドに `POST /folder/list`（フォルダ直下のみ・遅延読み込み用）と
    `POST /folder/search`（配下再帰・ファイル名部分一致・上限300件）を新設
  - 一覧の各動画に分析済みバッジ（scan-search 緑）・字幕バッジ（captions、
    日本語ありは青）・分析キャッシュのサムネール（scene_0.jpg）を表示。
    状態は存在チェックのみで判定するため軽量
  - `*.cache/`・`*_screenshot/`・隠しフォルダは一覧から除外（分析フォルダは
    動画のサイドカーとして扱い、ユーザーには見せない方針）
  - 検索ボックス入力で配下全体をフラット表示（相対フォルダ名を副行に表示）。
    クリアでツリーに戻る
  - 行クリックで動画を開く（既存の SRT/キャッシュ自動読み込みと同じ経路に共通化
    `openVideoFromPath()`）。再生中の動画はハイライト
  - 文字起こし・補正・翻訳・分析保存の完了時に表示中の親フォルダだけ再取得して
    バッジを最新化
  - Electron に `dialog:openFolder` IPC を追加
  - 検証: `/folder/list`・`/folder/search`・ui-settings 永続化を実サーバー（別ポート）
    ＋テスト用フォルダ構成で curl 確認、フロント JS は構文チェックのみ
    （Electron 実機での見た目確認は未実施）
- **アイコンの調整（フラット＋デフォルメ方向）**
  - モデルのアンロードボタン（トップバー・モデル管理ポップアップ）を
    イジェクト記号 ⏏（三角形＋バー）のカスタム SVG に変更
    （Lucide に eject が無いため。stroke 流儀・`class="lucide"` で既存 CSS と共通化）
  - 全体の stroke-width を太めに統一（基本 1.75→2.2、サイドバー 1.5→2、
    キャレット 1.8→2.4、no-video 1→1.4 等）で、フラットなまま少しデフォルメな印象に
  - 方針を CLAUDE.md の「アイコン」節に明文化
- **WhisperX 方式の字幕タイミング精密化（強制アライメント）を追加**
  - 文字起こしエンジンを 設定 → ランタイム で選択可能に:
    `faster-whisper（標準・従来通り）` / `faster-whisper + WhisperX 整列`
    （settings.json の `asr_engine`、`POST /runtime/asr/engine` で切り替え）
  - whisperx パッケージは **不採用**: `torch~=2.8.0` 固定が本プロジェクトの
    torch 2.12.0+cu130（Blackwell 必須）と衝突し、pip 導入すると CPU 版 torch に
    ダウングレードされるため。pyannote-audio / torchcodec の連鎖依存も回避
  - 代わりに WhisperX (github.com/m-bain/whisperX, BSD-2-Clause, © 2022 Max Bain) の
    wav2vec2 CTC 強制アライメントを `backend/align.py` に移植（torch + transformers のみ。
    torchaudio / pandas / nltk 不使用）。帰属はファイルヘッダに明記
  - `/transcribe` に SSE イベント追加: `loading_align_model` / `aligning {current,total}` /
    `align_warning`（未対応言語・整列失敗時は元の時刻で続行し、文字起こし自体は失敗させない）
  - 整列モデル（ja 約1.2GB / en 約360MB、全34言語対応）は初回文字起こし時に
    HF から `models/hub/` へ自動ダウンロード
  - `asr.transcribe()` が検出言語を返すよう変更（整列モデルの言語選択に使用）
  - 判明した問題と対処: asr.py の `_add_nvidia_dlls()`（CTranslate2 用 pip cuDNN）と
    torch 同梱 cuDNN が混在し wav2vec2 の CUDA 実行が
    `CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH` で落ちる → wav2vec2 推論時のみ
    `torch.backends.cudnn.flags(enabled=False)` で cuDNN を無効化して回避
  - requirements.txt に `transformers` を追加（align.py は遅延 import のまま）
  - 検証済み: 合成 emission でのアルゴリズム単体テスト、TTS 音声での E2E
    （開始時刻が発話開始にスナップされることを確認）、`/transcribe` SSE 通しテスト、
    既定エンジンの回帰（従来と同一イベント）、エンジン切り替えの永続化

## 2026-07-06
- **F12 キーで動画スクリーンショットを保存する機能を追加**
  - `POST /screenshot` を新設。再生位置のフレームを ffmpeg の入力シークで
    フル解像度抽出し、動画と同じ場所の `{動画名}_screenshot/` フォルダに保存
    （PIL 再エンコードなし・png は無劣化）
  - ファイル名は `{動画名}_{HH-MM-SS.mmm}.{png|jpg}`（再生位置を名前に含む。
    同時刻は同フレームなので上書き）
  - 動画末尾（currentTime == duration）や範囲外の時刻は `duration - 0.1` に
    クランプしてフレーム取得失敗を防止
  - 設定ポップアップに「プレイヤー」セクションを新設し、保存形式
    （PNG 無劣化 / JPG 軽量）を選択可能（ui-settings の `screenshot_format`）
  - 保存結果・エラーはステータスバーに表示。合成動画で png/jpg・先頭・末尾・
    超過時刻・存在しない動画（404）をエンドポイント経由でテスト済み
- **字幕0件で翻訳/補正を実行すると UI が固まるバグを修正**
  - 原因: 字幕が0件のとき `/translate`・`/refine` は `HTTPException(400)` を返すが、
    これは SSE ではなく通常の HTTP エラーレスポンス。フロントの `readSSE` は
    `response.ok` を確認せず body を読むため、`data:` 行が無く何のイベントも
    発火せず、進捗が「準備中...」のまま止まって見えていた
  - `readSSE` の先頭で非 200 を検知し、`{status:'error', message}` イベントに
    変換して各ハンドラへ届けるよう修正（全 SSE エンドポイント共通の対策）
  - あわせてバックエンドの0件時メッセージを利用者向けの文言に変更
- **docs / README を最新状態に更新**
  - `docs/plan/plan.md` を現在のロードマップ（実機確認・改善候補）に全面書き換え。
    完了済みの ASR 移行計画は `docs/plan/archive/asr-whisper-migration.md` へ移動
    （Gemma 4 audio 不採用の判断記録として保存）
  - `docs/plan/goals.md` の確定方針を最新化（ランタイム管理・構造化出力・翻訳モード・
    チャット方針・キャッシュ方針・UI デザイン基準を追記）
  - `docs/plan/progress.md` の先頭に「現在の状態」サマリを追加
  - `README.md` を全面更新: Qwen3-ASR / conda 時代の記述を faster-whisper / venv に修正、
    存在しない `docs/asr-gemma-notes.md` への参照を削除、新機能（翻訳モード・チャット・
    ランタイム管理・設定ポップアップ）と新 API（runtime / questions / thumbnails/generate 等）、
    プロジェクト構成（runtime/ / llama_server.py / runtime_manager.py 等）を反映
- **シーンサムネールの重複・前チャプター混入を修正（サーバー側生成に変更）**
  - 原因: フロントの `<video>` シーク＋canvas キャプチャは、`seeked` イベントと
    フレーム描画のレースで直前のフレーム（前チャプターの絵）を拾うことがあり、
    シーク不発時のタイムアウトでは前回の画像が残って重複に見えていた
  - `POST /cache/thumbnails/generate` を新設し、ffmpeg の入力シーク
    （QA 高速化で導入した `_grab_frame_at`）でサーバー側が正確なフレームを
    抽出して `thumbnails/scene_N.jpg` に保存（最大辺 480px / JPEG 85、並列4）
  - 副次改善: 分析後にプレイヤーの再生位置が飛ばなくなった。サムネール解像度が
    160px→480px になり QA フレーム再利用時の画質も向上。`GET /cache/image` に
    Cache-Control: no-store を付与（再分析時の古い画像の残留防止）
  - 合成動画での実生成テスト済み（4シーンすべて異なる画像・不正時刻はスキップ）
- **チャットにテンプレート質問チップを追加（YouTube AI チャット風・遅延生成方式）**
  - 入力欄の上に右寄せのチップを表示。固定2種（「この動画を要約して」
    「重要なポイントを3つ挙げて」）は即表示
  - 内容ベースの質問（最大3つ）は `POST /review/questions` で遅延生成して差し替え。
    **モデル未ロード時はロードを誘発せず固定チップのみ**（YouTube と同様の遅延表示）。
    生成はテキストのみの軽量推論（分析キャッシュの meta＋字幕サンプル＋直近の会話）
  - 回答完了後・モデルロード完了後に再生成し、会話を踏まえたフォローアップ質問に更新
  - 質問はキャッシュ（data.json）には保存しない（当初の分析時生成・meta.questions 保存は
    方針変更により撤回）
  - チップのクリックで即送信。回答生成中はクリック無効。フェードイン付き

## 2026-07-03
- **UI: 設定アイコンをトップバー右端へ移動、プレイヤータブボタンを削除**
  - 設定のポップアップ化でタブがプレイヤーのみになり、切り替えボタンが無意味だったため削除
  - 設定アイコンは右端（パネル切り替えボタンの右、仕切り線の後ろ）に配置
  - 不要になった switchTab / sidebar-left を削除
- **UI デザインの統一（ガイドライン §9 の 1〜5・8・9 を実施）**
  - common.css にトークンを追加: `--surface3` / `--border-strong` / `--focus-border` /
    `--accent-dim` / 状態色ティント（`--accent/success/warning/error-tint-bg/border`）
  - 生 HEX をトークンに置換（フォーカス枠・ホバー・シーン項目・モデルピルのグラデ/シマー・
    辞書ツールチップ・モーダル選択色・リンク・ランタイム状態ドット等 20 箇所超）
  - 意味色を統一: 赤は `--error`、オレンジは `--warning` の各1系統に
    （中止ボタン・sysinfo・保存ボタンで別々の赤/橙を使っていた）
  - フォーカス表示を common.css に集約（button は accent リング、入力系は枠明色化のみ）
  - 小粒: チャットエラーの絵文字 `❌` をテキスト化、10px 文字を 11px に、角丸 7px を 6px に
  - ガイドライン §9 を対応状況付きに更新（残: インラインスタイルの分離・ライトテーマ）
- **UI デザインガイドラインを docs/design に追加**
  - `docs/design/ui-design-guidelines.md` 新規。カラートークン・タイポグラフィ・
    角丸/余白・エレベーション・アイコン・モーション・コンポーネント規約を明文化
    （コンセプト: シンプルかつスタイリッシュ。他アプリと共有する共通デザインの基準）
  - 改善点も §9 に列挙: 生 HEX の氾濫（20箇所超）、意味色（赤3種・橙3種）と
    青系のゆらぎ、フォーカス表示の不統一、インラインスタイル依存、
    common.css への汎用コンポーネント分離、絵文字の残存 等
- **UI: モデルロード時の挙動を改善**
  - モデル管理ポップアップの「ロード」を押すと即座にポップアップを閉じ、
    サイドバーのモデル選択バー（ピル）にシマー（流れるハイライト）＋
    アイコン点滅のローディングアニメーションを表示
  - ロード中はポーリング等による表示の上書きを抑止（`_modelLoading` ガード）。
    完了/失敗はステータスバーに通知
- **UI: セレクトボックス等のフォーカスリングを控えめに**
  - select / テキスト入力 / textarea のフォーカス時に OS のアクセント色
    （環境によって明るいオレンジ等）のネイティブリングが表示されて目立ちすぎたため、
    枠線を #555 に明色化する控えめな表示に置き換え
- **チャット（動画 Q&A）の高速化とマルチターン対応（docs/design/qa-chat.md 新規）**
  - チャット開始が遅い主因（質問前のフレーム抽出＝動画全編デコード）を解消:
    1. 分析キャッシュのサムネール（data.json の scenes + thumbnails/）を
       QA フレームに再利用（ffmpeg 起動ゼロで即開始。シーン4枚未満は不採用）
    2. uniform 抽出を fps フィルタの全編デコードから「-ss 入力シーク×並列4」に変更
       （60秒動画8枚で 0.13 秒。長編ほど効果大。分析側の抽出も同時に高速化）
  - マルチターン対応: フロントが直近3ターンの {question, answer} を `history` として送り、
    プロンプトに「これまでのやり取り」として同梱（回答は400字で切り詰め）。
    履歴は動画切り替え・履歴クリアでリセット
  - チャットの仕組み全体を `docs/design/qa-chat.md` にまとめた
- **翻訳モード（高品質 / 高速）の切り替えを追加**
  - 精度改善（用語集生成・文脈拡大）で字幕生成が遅くなったため、軽量な fast モードを追加
  - quality（既定）: 用語集＋動画文脈＋文脈5ペア＋先読み2行＋バッチ8行（従来どおり）
  - fast: 用語集・動画文脈を省略（呼び出し1回削減＋プロンプト縮小）、
    文脈2ペア＋先読み1行＋バッチ12行
  - 設定 → 字幕 の「翻訳モード」で切り替え（ui-settings の `translation_mode` に永続化、
    `/translate` の `mode` パラメータで指定）
- **ランタイムを手動選択式に拡張（llama-cpp のビルド選択・Whisper のモデル選択）**
  - llama-cpp: `GET /runtime/llama/builds` で最新リリースの Windows ビルド一覧
    （cuda-x.y / cpu / vulkan 等）を取得し、選んでインストールできるように変更。
    推奨マークは nvidia-smi のドライバ対応 CUDA バージョン以下で最大の CUDA ビルド
    （NVIDIA GPU なしなら CPU）を自動判定
  - llama-cpp: インストール済みバージョンをプルダウンで切り替え
    （`POST /runtime/llama/select` → settings.json の `llama_version`。
    `LLAMA_CPP_DIR` 環境変数が最優先、未設定時は最新を自動検出）
  - Whisper: tiny / base / small / medium / large-v3 / large-v3-turbo から選んで
    インストール（`POST /runtime/install` の `model` 指定）。使用モデルの切り替えは
    `POST /runtime/whisper/select`（settings.json の `whisper_model`、次回文字起こしから反映）。
    未インストールのモデルを「適用」すると自動でダウンロード → 切り替え
  - `asr.py`: 使用モデルを `ASRProcessor.model_id` に持たせ `set_model_id` で切り替え
    （起動時に settings.json から復元）
  - 実 GitHub API で b9864 の7ビルドから cuda-13.3 が推奨判定されることを確認
- **設定にランタイム項目を追加（llama-cpp / Whisper / ffmpeg のインストール対応）**
  - `backend/runtime_manager.py` 新規: 状態検出（インストール済み判定・バージョン・パス）と
    ダウンロード・展開（cancel 対応、zip-slip 対策）
  - llama-cpp は GitHub の ggml-org/llama.cpp 最新リリースから CUDA/CPU ビルドを自動選択
    （CUDA 時は cudart 同梱 zip も取得）して `runtime/llama-server/` に展開。
    `llama_server.py` は固定パス（b8763）をやめて配下を動的検出するように変更
    （インストール直後から再起動なしで新バージョンを使用）
  - Whisper は faster-whisper のモデル重み（large-v3-turbo）を `models/` に事前ダウンロード
  - ffmpeg は BtbN ビルドを `runtime/ffmpeg/` に展開し、起動時に PATH へ追加
  - API: `GET /runtime/status` / `POST /runtime/install`（SSE で進捗ストリーミング）
  - 設定モーダルに「ランタイム」ナビを追加。状態ドット・バージョン・パス表示と
    インストール/更新ボタン、行内のダウンロード進捗表示（ステータスバーの中止ボタンで中断可）
- **設定をページからポップアップウインドウに変更**
  - 左に項目ナビ（動画分析 / 字幕 / プロンプト / 情報）、右にパラメータの2カラム構成
  - 背景は暗転＋ぼかし（backdrop-filter: blur(4px)）。モデル管理ポップアップの背景も
    同じぼかしに統一
  - Esc キー・背景クリック・×ボタンで閉じる（Esc はモデル管理ポップアップにも対応）
  - 既存の設定要素の ID は維持（保存・復元ロジックは変更なし）。
    旧 `#tab-settings` ページと `.settings-main` CSS を削除
- **字幕翻訳の精度改善を実装（docs/design/translation-accuracy.md の 1/2/3/5/7）**
  - **先読み文脈**: 翻訳対象の次 2 行の原文を参考情報として同梱（文の途中で切れた
    セグメントの誤訳対策）
  - **構造化メッセージ**: 過去の訳を擬似会話履歴として渡す方式を廃止し、
    「直前の行と訳／続きの行／翻訳対象」をラベル付き 1 メッセージに変更
    （初期の誤訳を模倣し続けるドリフトを防止）
  - **動画メタ＋用語集**: 分析キャッシュの meta（genre/summary/tags）と、翻訳前に
    字幕全編サンプルから生成した用語集（原語→日本語、最大30語）を system prompt に
    付加。固有名詞・専門用語の訳ゆれを防ぐ。生成失敗時は warning を出して続行
  - **バッチ翻訳**: 8 行ずつ json_schema で構造化受信（呼び出し回数 約1/8）。
    検証失敗バッチは行単位翻訳にフォールバック
  - **プロンプト改善＋打ち切り検知**: system prompt に字幕制約を明文化、
    finish_reason=="length" を SSE translate_warning で通知
  - SSE に building_glossary / glossary_done / translate_warning を追加し、
    フロントのステータス表示に対応。CLAUDE.md に /translate の SSE 仕様を追記
- **動画分析レビューの改善提案を実装（docs/design/video-analysis-review.md の 2-1〜3-6）**
  - **2-4 バグ修正**: タイムスタンプ解析を `parse_timestamp_seconds` に一本化し
    h:mm:ss / 分3桁（105:30）対応。1時間超動画で `_dedup_scenes` が scenes を
    全損させるリスクを解消
  - **2-3 / 3-2**: 分析出力を llama-server の `response_format`（json_schema → GBNF）で
    構文制約。非ストリーム推論にも `chat_with_meta` を追加して finish_reason / usage を取得し、
    トークン上限打ち切りを SSE `analyze_warning` / `qa_warning` で通知
  - **2-1 / 3-4**: 送信前にコンテキスト予算（枚数×トークン/枚＋テキスト＋出力）を見積もり、
    超過時は解像度→枚数の順に自動削減（`_fit_frame_budget`）。縮小・間引き・
    画像処理エラーリトライをすべて SSE でユーザーに通知
  - **2-2**: analyze の transcript を先頭3000字切り捨てから全編の時間等間隔サンプリングに
    変更（長編動画で後半の音声が分析に反映されない問題を解消）
  - **3-1**: フレームキャッシュのキーを実抽出パラメータ（coarse_frames）に修正
  - **3-3**: scenes[].timestamp をモデルに送ったフレーム時刻の最近傍にスナップ
  - **3-6**: ReviewRequest / QARequest の max_frames / min_interval に Pydantic Field で境界宣言
  - CLAUDE.md の SSE 仕様を現状に合わせて更新（旧 include_audio 記述を削除、
    warning / answer_delta イベントを追記）。3-5 と 4節（低優先）は未対応のまま
- **AI 動画分析パイプラインのレビューを実施、改善提案を docs/design に追加**
  - `docs/design/video-analysis-review.md` を新規作成
  - 優先度高: ctx 16384 超過リスク（60枚×256トークン）、transcript 先頭3000字切り捨て、
    json_schema 未使用、`_parse_ts` の h:mm:ss 非対応バグ（1時間超動画で scenes 全損リスク）
  - 優先度中: フレームキャッシュキー不整合、finish_reason 廃棄、timestamp スナップ、
    縮小リトライの不可視性、QA フレーム選択、入力バリデーション
- **`bin/` フォルダを `runtime/` にリネーム**
  - 今後 ffmpeg 等の外部実行時コンポーネントを集約する置き場として意図が伝わる名前に変更
  - llama.cpp 一式は `runtime/llama-server/llama-b8763-bin-win-cuda-13.1-x64/` に配置
  - `backend/llama_server.py` の `LLAMA_CPP_DIR` デフォルトパスと `.gitignore`（`bin/` → `runtime/`）を更新
  - `LLAMA_CPP_DIR` 環境変数による上書きは従来どおり有効

## 2026-06-12
- **チャプター保存を `video.cache/data.json` に一本化（`.toc.json` の書き込み廃止）**
  - 保存ボタンが `.toc.json`（動画の横）と `data.json`（cache 内）へ同じ内容を二重に
    書き込んでいた冗長を解消。`app.html` の `persistTocData()` は `/cache/save`
    （`saveDraftArtifacts()`）のみ使用するように変更
  - `server.py` から `POST /review/toc/save` と、フロントエンドから一切呼ばれていなかった
    レガシーの `POST /review/toc/build` を削除（`TOCBuildRequest` / `TOCSaveRequest` も削除）
  - `POST /review/toc/load` は旧動画の `.toc.json` を読むための後方互換として
    読み取り専用で存続（cache が無い場合のフォールバック動作は従来どおり）
  - これに伴い残課題だった「analyze / toc/build の refine ループ重複」は toc/build 削除で解消
  - CLAUDE.md / README.md の API 一覧・出力ファイル構成を更新、`py_compile` OK
- **コードレビューで見つかった不具合・脆弱性をまとめて修正**
  - **API 保護（ローカル API がブラウザ上の任意サイトから叩けた問題）**:
    - `server.py`: CORS を `allow_origins=["*"]` → `["null", "file://"]`（Electron の file:// レンダラーは
      `Origin: null`）に変更し、Origin が外部サイトのリクエストと Host がローカル以外のリクエスト
      （DNS リバインディング対策）を 403 で拒否するミドルウェアを追加
    - `POST /cache/thumbnail`: `filename` のパストラバーサル対策（パス区切りを含む名前を 400 で拒否）。
      従来は `..\..\foo.exe` 等で任意の場所に任意バイナリを書き込めた
    - `GET /cache/image`: パス検証を文字列 `startswith` → `Path.is_relative_to` に変更
      （`video.cache_evil` のような兄弟フォルダ素通りを防止）
    - curl で実機検証済み（Origin null/外部・Host 偽装・filename `/`・`\` トラバーサル全パターン）
  - **中止フラグの残留バグ**: 中止完了後もフラグが立ったままで、次の SSE 処理開始まで
    `/lookup`（ホバー辞書）が `CanceledError` で全滅していた。`canceled` イベント送出時に
    `clear_cancel()` する `sse_canceled()` ヘルパーを導入し全ハンドラで使用
  - **`/review/toc/build` の中断処理欠落**: `clear_cancel()` が無く、`CanceledError` も
    `error` として報告されていたのを他ハンドラと同様に修正
  - **Q&A の Enter 連打ガード**: 回答生成中に Enter で並行リクエストが走り、後発の
    `clear_cancel()` が先行ジョブの中止を無効化し得た。`submitQuestion()` 冒頭でガード
  - **Markdown サニタイズ（XSS 対策）**: marked は生 HTML を素通しするため、モデル出力
    （動画内容由来＝信頼できないテキスト）を innerHTML に入れる前に許可タグのみ残す
    `sanitizeHtml()` を追加（属性は `<a href="http(s)://">` 以外すべて除去）
  - **CDN 依存をローカル同梱に変更**: lucide / marked を `frontend/vendor/` に同梱し、
    オフラインでもアイコン・Markdown 描画が動くようにした
  - **サムネールキャプチャのハング対策**: `seeked` が発火しないケース（同一時刻への
    シーク等）で Promise が永久に未解決になるのを 2 秒タイムアウトで回避
  - **corrected.srt のみ存在する場合に字幕生成できなかった問題**: 翻訳ボタンの判定を
    `correctedSrtPath || origSrtPath` に変更、自動読込時も翻訳ボタンを有効化
  - **小修正**: 辞書検索の `max_tokens` 128→256（例文が途中で切れるため）、
    フレーム抽出 ffmpeg 失敗時に stderr 末尾を含めてエラー報告、未使用の
    `VideoReviewer.qa_frames()` を削除
  - 既知の残課題（今回見送り）: `/review/analyze` と `/review/toc/build` の refine ループ
    約 80 行の重複解消、`vram.py` のハードキャップが llama-server / CTranslate2 に
    効かない件（torch 依存の整理含む）

## 2026-06-03
- **実行中処理の「中止」ボタンを追加（進行中の推論も即停止）**: 文字起こし・補正・字幕生成・
  動画分析・Q&A の SSE 処理を、進行中の推論ごと中断できるようにした
  - `backend/cancel.py`（新規）: 単一ユーザー前提のグローバル `threading.Event`。
    `request_cancel`/`clear_cancel`/`is_canceled`/`raise_if_canceled` と `CanceledError`
  - `server.py`: `POST /cancel` を追加。各 SSE ハンドラは開始時に `clear_cancel()`、
    推論呼び出しの `CanceledError` を捕捉して `{status:'canceled'}` を送出（VRAM は `finally` で解放）
  - `llama_server.py`: `chat()` をストリーミング受信に変更し、`stream_chat_with_meta()` と共に
    トークン行ごとに `is_canceled()` を確認。中断時は HTTP 接続を閉じて llama-server の生成を止め
    `CanceledError` を送出（翻訳・補正・辞書・VL 分析・Q&A をカバー）
  - `asr.py`: 書き起こしの遅延ジェネレータ走査でセグメントごとに `raise_if_canceled()`
  - `app.html`: ステータスバー右側に「中止」ボタン（実行中のみ表示）。`AbortController` ではなく
    `POST /cancel` を呼ぶ方式に変更（推論が安全に止まってから unload するため、特に ASR の
    CTranslate2 モデルを推論中に `del` してクラッシュする事故を回避）。`canceled` 受信で
    「中止しました」と中立表示（`markCanceled`）
  - 制約: プロンプト処理中（最初のトークン生成前。多フレーム VL 等）はトークン行が来ないため、
    最初のトークンが出るまで中断は反映されない。生成が始まればほぼ即停止
- **動画分析に「内容のまとめ」(detail) を追加**: 概要（1〜2文）とは別に、内容を詳しく
  まとめた `detail` を生成し、概要の下に折りたたみセクションで表示
  - `video_reviewer.py`: `_ANALYZE_JSON_FORMAT` に `detail` を追加、分析指示文（visual/audio）に
    「detail は概要より詳しく要点ごとに」を明記、`_salvage_analysis_fields` も `detail` を抽出
  - `server.py`: `/review/analyze` の result に `detail` を追加。`UISettings` に
    `analysis_detail_expanded`（既定 false=折りたたみ）を追加し GET/POST で永続化
  - `app.html`: 概要の下に「内容のまとめ」セクション（`detailSection`、既定折りたたみ）を追加。
    `detail` を `meta` 経由で受け渡し（`buildTocFromAnalysis` / `analysisFromTocData` /
    キャッシュ load）、Markdown で描画。値が空のときはセクション非表示
  - 既存キャッシュ（detail なし）は非表示になるだけで影響なし
- **文字起こしのローカル LLM 補正を追加**: ASR の認識誤りを翻訳用 GGUF テキストモデルで
  保守的に補正するステップを新設（任意・字幕生成の前段）
  - `translator.py`: `REFINE_SYSTEM_PROMPT`（明らかな誤認識・誤字・句読点のみ直し、言語/意味/
    語順は保持）と `refine()` メソッドを追加。`get_prompts()` に `refine` を登録し設定画面で閲覧可
  - `server.py`: `POST /refine` を追加。`/translate` と同型で**セグメント単位に補正＋時刻はそのまま
    保持**（前後5件を文脈として渡す）。出力は `video.cache/video.corrected.srt`
  - `app.html`: 「補正」ボタン（`refineBtn`）を文字起こし⇄字幕生成の間に追加。
    補正後は翻訳が `correctedSrtPath` を優先使用。再オープン時は `tryAutoLoadSrt` が
    `corrected.srt` を自動検出して表示・翻訳元に採用。補正実行で `transcript` キャッシュも更新
  - `.gitignore` に `*.corrected.srt` を追加
  - 生 ASR（`original.srt`）は残すため、補正が不適切でも元に戻せる
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
