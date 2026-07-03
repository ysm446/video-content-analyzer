# AI 動画分析パイプライン レビューと改善提案

作成日: 2026-07-03
対象コード: `backend/video_reviewer.py` / `backend/server.py`（/review/analyze・/review/qa）/ `backend/llama_server.py` / `backend/vram.py`

## 対応状況（2026-07-03 更新）

| 項目 | 状態 |
|---|---|
| 2-1 コンテキスト予算管理 | ✅ 対応済み（`_fit_frame_budget` で解像度→枚数の順に自動削減、SSE 通知） |
| 2-2 transcript 先頭切り捨て | ✅ 対応済み（時間等間隔サンプリングに変更。map-reduce 要約は将来枠） |
| 2-3 json_schema 構造化出力 | ✅ 対応済み（response_format で GBNF 制約。サルベージは保険で存続） |
| 2-4 `_parse_ts` h:mm:ss バグ | ✅ 対応済み（`parse_timestamp_seconds` に一本化） |
| 3-1 フレームキャッシュキー | ✅ 対応済み（実抽出パラメータでキャッシュ） |
| 3-2 finish_reason 廃棄 | ✅ 対応済み（`chat_with_meta` 追加、length 打ち切りを SSE 通知） |
| 3-3 timestamp スナップ | ✅ 対応済み（`_snap_scene_timestamps`、許容誤差=サンプリング間隔） |
| 3-4 縮小リトライの可視化 | ✅ 対応済み（analyze_warning / qa_warning） |
| 3-5 QA フレーム選択 | ⏸ 未対応（任意。効果を見て判断） |
| 3-6 入力バリデーション | ✅ 対応済み（Pydantic Field で境界宣言） |
| 4節（低優先） | ⏸ 未対応（ドキュメント乖離のみ CLAUDE.md を更新済み） |

## 1. 現状の仕組み（要約）

```
/review/analyze
  ① フレーム抽出（uniform: fps フィルタ均等 / scene: ffmpeg scene検出 閾値0.35）
     analysis_mode に応じて coarse 枚数を削減（speed=max_frames全量, balanced/quality=半分程度）
  ② coarse 分析: 全フレーム＋transcript（先頭3000字）を VL モデルに1回投げ、
     JSON（summary/detail/scenes/tags/genre）をテキストとして生成させる
  ③ JSON 復旧: コードフェンス除去 → 括弧バランス抽出 → 正規表現サルベージ
  ④ scenes → TOC エントリ化（timestamp 解析・秒単位 dedup・end_sec 補完）
  ⑤ refine（balanced/quality）: 長いチャプターを選び、区間フレーム＋区間 transcript で
     scenes のみ再分析 → coarse とマージ
/review/qa
  フレームは analyze と同一パラメータならメモリキャッシュを流用。
  transcript はキーワード（2-gram）スコアで質問関連行を3000字予算内に選別。
  ストリーミング応答（finish_reason / usage / tokens_per_sec を取得）
推論共通
  llama-server(-c 16384) に /v1/chat/completions、temperature=0、cache_prompt=true。
  1フレーム最大 256×28×28 px ≒ 視覚トークン 256/枚。失敗時は 12枚×128トークンに縮小リトライ。
```

全体として、フォールバックが多段に用意された堅実な構成。特に refine の2段解析、
QA の transcript 関連行選別、キャンセル対応は良くできている。
以下は「壊れうる箇所」と「品質を上げられる箇所」を優先度順に挙げる。

## 2. 改善提案（優先度: 高）

### 2-1. コンテキスト予算の管理がない（設定次第で ctx 16384 を超える）

- **問題**: UI の枚数スライダーは最大 60（`app.html:468`）。speed モードでは
  `coarse_frames = max_frames` そのまま（`server.py:201-209`）なので、
  60枚 × 256トークン = **15,360 視覚トークン**。これに transcript（≤3000字）・指示文・
  出力予算 3072 が加わり、`LLAMA_CPP_CTX=16384`（`llama_server.py:27`）を確実に超える。
  超過時の挙動（llama-server のエラー or コンテキストシフトによる黙った欠落）を
  検知・通知する仕組みがない。
- **提案**:
  1. 送信前に概算トークン数（枚数×256 ＋ transcript長/2 ＋ 固定分 ＋ max_tokens）を計算し、
     ctx に収まるよう **枚数と 1枚あたりピクセル数を動的に配分**する
     （枚数が少ないときは高解像度、多いときは低解像度）。`MAX_PIXELS_PER_FRAME` 固定
     （`vram.py:31`）をやめ、`_frame_to_data_url(max_pixels=...)` に予算を渡す口は既にある。
  2. 収まらない場合は SSE で warning を出して自動減枚する（黙って劣化させない）。

### 2-2. analyze の transcript が「先頭3000字」切り捨て（長編で後半が無視される）

- **問題**: `_truncate_transcript()`（`video_reviewer.py:525-529`）は先頭 3000 字のみ残す。
  1時間の動画では**冒頭 5〜10 分の音声しか分析に反映されない**。summary/detail/genre/tags が
  冒頭に引きずられ、coarse の scenes も後半の裏付けを失う。QA 側には関連行選別
  （`_select_relevant_transcript`）があるのに analyze 側は素朴なままで非対称。
- **提案**（いずれか、または段階的に）:
  1. **時間等間隔サンプリング**: `[m:ss]` 行を全編から等間隔に予算内で拾う
     （QA の「一致なし」フォールバックと同じロジックが流用できる: `video_reviewer.py:614-622`）。
  2. **チャンク map-reduce**: transcript を時間窓で分割し、テキストモデル（translator が常駐）で
     窓ごとに要約 → 要約列を VL 分析の入力にする。長編の detail 品質が大きく上がる。
  3. 少なくとも refine と同様に、coarse でも「フレームのカバー範囲に対応する時間帯」を
     優先して透過的に配分する。

### 2-3. 構造化出力（json_schema / GBNF）を使っていない

- **問題**: JSON はプロンプト指示のみで生成させ、失敗時にフェンス除去 → 括弧バランス抽出 →
  正規表現サルベージ（`video_reviewer.py:199-266`）と3段のリカバリを持っている。
  サルベージに落ちると **scenes が空になり**、チャプターが全損する。
- **提案**: llama-server の `/v1/chat/completions` は `response_format: {type: "json_schema", ...}`
  （内部で GBNF 文法に変換）をサポートしている。スキーマを渡せば**構文的に壊れた JSON が
  原理的に出なくなり**、サルベージ機構の大半を削除できる。`_open_stream()`
  （`llama_server.py:193-210`）の payload に `response_format` を追加するだけで済む。

### 2-4. 【バグ】1時間超の動画で scenes が潰れる可能性（`_parse_ts` が h:mm:ss 非対応）

- **問題**: `_dedup_scenes()` が使う `_parse_ts()`（`video_reviewer.py:629-637`）は
  `m:ss`（コロン1個）しか解釈せず、それ以外は **0.0 を返す**。フレームのラベルは
  `_fmt_ts()` の `75:30` 形式だが、モデルが `1:15:30` 形式に正規化して返すことは十分ある。
  その場合、全 h:mm:ss シーンが `int(0.0)=0` の同一キーになり、
  **dedup で最初の1件以外すべて捨てられる**。後段の `_parse_timestamp_seconds()`
  （`server.py:116-126`）は h:mm:ss 対応済みなので、不整合はこの1箇所。
- **提案**: `_parse_ts` を `_parse_timestamp_seconds` 相当に統一する（共通化して1実装にする）。

## 3. 改善提案（優先度: 中）

### 3-1. フレームキャッシュのキーが実態と食い違っている

- **問題**: analyze は `plan["coarse_frames"]`（例: balanced で 15枚）で抽出したフレームを、
  `req.max_frames`（30）をキーに保存する（`server.py:1060`）。一方 QA は自分の
  `req.max_frames`（UI スライダー値）で照合する（`server.py:1102`）。つまり
  ①キーの中身が嘘（30枚と称して15枚）②analyze 直後の QA でヒットしても
  **coarse の間引きフレームで回答する**ことになり、QA 品質が黙って落ちる。
- **提案**: キャッシュキーは「実際の抽出パラメータ」（mode / 実枚数 / 実 interval）で作る。
  さらに QA は analyze の coarse フレーム流用で良いのか（枚数・解像度要件が違う）を
  明示的に設計し、流用するなら SSE meta にその旨を出す。

### 3-2. max_tokens 打ち切りを検知できない（非ストリームの `chat()` が finish_reason を捨てる）

- **問題**: analyze は `chat()`（`llama_server.py:212-242`）を使うが、これは content を
  連結して返すだけで `finish_reason` / `usage` を捨てる。出力上限 3072（scenes 大量時に
  現実的に到達しうる）で切れても `length` を検知できず、壊れた JSON としてサルベージ行きになる。
- **提案**: `chat()` も `stream_chat_with_meta()` と同じくメタを返し、`finish_reason=="length"`
  なら SSE warning ＋（可能なら）継続生成 or scenes を分割リクエスト。2-3 の json_schema と
  併用すれば「切り捨て＝即検知」になる。

### 3-3. モデル出力の timestamp をフレーム時刻にスナップしていない

- **問題**: プロンプトで「フレームのタイムスタンプを使え」と指示しているだけで
  （`_ts_hint`: `video_reviewer.py:654-662`）、VL モデルは平気で中間の時刻を捏造する。
  現状は duration へのクランプのみ（`server.py:140`）。チャプター開始が実映像とずれる。
- **提案**: サーバー側で scenes[].timestamp を**実際に送ったフレーム timestamps の
  最近傍値に吸着**させる（許容誤差を超えるものは前後フレームに丸める）。
  candidate リストは meta["timestamps"] として既に持っている。

### 3-4. 縮小リトライがユーザーに見えない

- **問題**: 画像処理エラー時に 12枚 × 128トークンへ黙って縮小する
  （`video_reviewer.py:299-317`）。60枚指定でも結果は12枚分の分析になるが、
  print ログのみで SSE には何も流れない。
- **提案**: リトライ発生を SSE の warning イベント（`refine_warning` と同様の形）で通知し、
  done の meta に実際に使った枚数・解像度を含める。

### 3-5. QA のフレーム選択が質問と無関係

- **問題**: QA は常に動画全体の等間隔（or scene）フレームを使う。「15:30 あたりで何をしてる？」
  「イントロの次のチャプターは？」のような時刻・区間依存の質問でも全体フレームで答える。
  transcript 側は関連行選別があるのに映像側は無選別、という非対称。
- **提案**: 質問から時刻表現（`m:ss` / 「〜分あたり」）やチャプター名（cache の scenes と照合）を
  抽出できた場合は `extract_frames_between()`（実装済み: `video_reviewer.py:493-523`）で
  該当区間を密にサンプリングする。ヒットしなければ従来どおり全体フレーム。

### 3-6. API 入力のバリデーションが甘い

- **問題**: `ReviewRequest.max_frames` / `min_interval` に上下限がない（`server.py:376-382`）。
  UI 経由では 5〜60 に収まるが、API 直叩きで `max_frames=10000` を渡すと
  ffmpeg 抽出・base64 化・プロンプト構築まで進んでしまう。
- **提案**: Pydantic の `Field(ge=1, le=120)` 等で境界を宣言する（QARequest も同様）。

## 4. 改善提案（優先度: 低 / 将来）

- **scene 検出閾値の適応化**: 0.35 固定（`video_reviewer.py:26`）。カット割りの少ない講義系では
  検出ゼロ→uniform フォールバックになりがち。検出数が目標枚数から大きく外れたら閾値を
  1回だけ調整して再実行する、または scene 検出結果に uniform の下限フロア
  （「最低でも N 分に1枚」）を合成する。
- **長編での自動モード選択**: 2時間動画を 30 枚（4分に1枚）の uniform で見るのは粗すぎる。
  duration に応じて scene モードや refine 段数を自動昇格する仕組み。
- **transcript 検索の embedding 化**: 現在の 2-gram キーワードスコア
  （`video_reviewer.py:531-622`）は同義語・言い換えに弱い。小型 embedding モデル
  （GGUF で llama-server の `--embedding` が使える）による意味検索を任意機能として追加。
- **テレメトリの統一**: QA は usage / tokens_per_sec を取るが analyze は経過秒のみ。
  分析にも usage を記録し、done の meta で返すと 2-1 の予算チューニングに使える。
- **confidence の実質化**: `_build_toc_entries` の confidence は 0.8/0.5 の固定値
  （`server.py:160,183`）。使わないなら削除、使うなら refine 済み/coarse 由来で差をつける。
- **ドキュメント乖離の解消**: CLAUDE.md の `/review/analyze` SSE 仕様には
  `loading_asr` / `transcribing` / `asr_done`（include_audio）とあるが、現行実装は
  transcript をフロントから受け取る方式で ASR は行わない。また `pass: coarse/refine` や
  `refine_warning` イベントが未記載。CLAUDE.md / README の SSE 仕様を現状に合わせる。

## 5. 推奨着手順

1. **2-4（h:mm:ss バグ）** — 数行の修正で全損リスクを消せる。最初にやる
2. **2-3（json_schema）＋ 3-2（finish_reason）** — サルベージ機構の削減とセットで効果大
3. **2-1（コンテキスト予算）＋ 3-4（縮小リトライの可視化）** — 「黙った劣化」をなくす
4. **2-2（transcript サンプリング）** — 長編の分析品質に最も効く
5. **3-1（キャッシュキー）/ 3-3（timestamp スナップ）/ 3-6（バリデーション）** — 小粒で独立
6. 3-5 と 4 節は効果を見ながら任意で
