# 字幕翻訳（文字起こし→日本語）の精度改善

作成日: 2026-07-03
対象コード: `backend/translator.py` / `backend/server.py`（/translate）/ `backend/subtitle.py`

## 対応状況（2026-07-03 更新）

| 項目 | 状態 |
|---|---|
| 1 先読み（lookahead）文脈 | ✅ 対応済み（次2行の原文を参考情報として同梱） |
| 2 擬似会話履歴 → 構造化1メッセージ | ✅ 対応済み（`_build_translate_user_message`） |
| 3 動画メタ＋用語集の注入 | ✅ 対応済み（data.json meta + `build_glossary`、失敗時は非致命でスキップ） |
| 4 文単位の再グルーピング | ⏸ 保留（1〜3 の効果を見て判断） |
| 5 json_schema バッチ翻訳 | ✅ 対応済み（8行バッチ＋検証失敗時は行単位フォールバック） |
| 6 ASR 側の改善（initial_prompt 等） | ⏸ 保留（別作業） |
| 7 プロンプト改善＋finish_reason 検知 | ✅ 対応済み（制約を明文化、translate_warning で通知） |

## 1. 現状の仕組み（要約）

```
/translate
  原文 SRT（.corrected.srt 優先）を srt_file_to_segments で読み込み
  → セグメントを1行ずつ translator.translate(text, context) で翻訳
     - context: 直前 5 件の (原文, 訳文) ペアを user/assistant の
       擬似会話履歴として渡す（server.py の CONTEXT_WINDOW=5）
     - system prompt: "You are a subtitle translator. ..."（2文のみ）
     - temperature=0 / max_tokens=256、GGUF は llama-server 経由
  → 空応答は原文で代替 → japanese.srt 保存、終了後モデルをアンロード
```

## 2. 問題点と改善方針（優先度順）

### 2-1. 先読み（lookahead）文脈がない【効果大・実装小】

- **問題**: SRT セグメントは文の途中で切れることが多く、英日は語順が逆なので
  「続きを知らずに1行だけ訳す」と誤訳になる。現在は過去の文脈しか渡していない。
- **方針**: 現在行の**次の 2 行の原文**をプロンプトに参考情報として含める。
  時刻・行の対応は変えない（訳すのは対象行のみ）。

### 2-2. 擬似会話履歴をやめて構造化1メッセージにする【効果中・実装小】

- **問題**: 過去の訳を assistant 発話として偽装する方式
  （`translator.py` の translate）は、初期の誤訳・悪い文体を few-shot として
  模倣し続けるドリフトの原因になる。
- **方針**: 「直前の行と訳（参考）／続きの行（参考）／翻訳対象の行」を
  ラベル付きで 1 つの user メッセージにまとめる。

### 2-3. 動画メタ＋用語集の注入【効果大・実装中】

- **問題**: 動画のジャンル・話題を知らずに訳すため訳語選択が不安定。
  固有名詞・専門用語が行ごとに訳ゆれする（前半は音写、後半は英字のまま等）。
- **方針**:
  1. 分析キャッシュ `{動画名}.cache/data.json` の meta（genre / summary / tags）が
     あれば system prompt に「動画の文脈」として付加する
  2. 翻訳開始前に transcript 全体（等間隔サンプル）から固有名詞・頻出語を抽出して
     「原語→日本語」の用語集を 1 回だけ生成（json_schema で構造化）し、
     全セグメントの system prompt に添付する。失敗しても翻訳は続行（非致命）

### 2-4. 文単位の再グルーピング【品質最高・実装大 → 保留】

- 句読点ベースで文単位にマージ → 文を翻訳 → 訳文を元のタイミング枠に再配分する2段方式。
- 表示タイミングと発話内容のずれが生じるトレードオフがあり、再配分ロジックの
  作り込みが必要。1〜3 の効果を見てから判断する。

### 2-5. json_schema バッチ翻訳【速度＋整合性・実装中】

- **問題**: 1行ずつの呼び出しはオーバーヘッドが大きく、バッチ内の文脈も共有されない。
- **方針**: 8 行をまとめて渡し、`{"translations": [{"index": 1, "japanese": "..."}, ...]}`
  の json_schema で受ける。**検証（行数・index の一致）に失敗したバッチは
  従来の行単位翻訳にフォールバック**する（品質の安全網）。
  進捗 SSE はバッチ完了ごとに更新。

### 2-6. ASR 側の改善【別作業として保留】

- faster-whisper の `initial_prompt` にキャッシュの tags 等のトピック語を渡す
- refine プロンプトにもトピック文脈を渡す

### 2-7. プロンプト改善＋finish_reason 検知【効果中・実装小】

- **問題**: system prompt が "natural Japanese suitable for subtitle display" のみで
  字幕の制約（長さ・文体・固有名詞の扱い）が伝わっていない。
  また `chat()` は finish_reason を捨てるため、長いセグメントで訳が
  max_tokens=256 で切れても検知できない。
- **方針**:
  - system prompt に「1行あたり〜40字目安」「話し言葉の自然な日本語」
    「固有名詞は用語集優先・無理に訳さない」「対象行の訳のみ出力」を明文化
    （デフォルト定数の変更。ユーザープリセットは従来どおり優先される）
  - 翻訳を `chat_with_meta` に切り替え、finish_reason=="length" を
    SSE `translate_warning` で通知

## 3. 実装後の /translate フロー

```
/translate
  ① data.json の meta から動画文脈を組み立て（あれば）
  ② building_glossary: transcript サンプルから用語集を生成（失敗時はスキップ）
  ③ 8 行ずつバッチ翻訳（json_schema）
     - 直前 5 ペア＋次 2 行を参考文脈として同梱
     - バッチ検証失敗 → その 8 行は行単位翻訳にフォールバック
     - finish_reason=="length" → translate_warning
  ④ japanese.srt 保存（従来どおり）

SSE: loading_model / building_glossary / translating {current,total}
     / translate_warning {message} / canceled / done / error
```

## 4. 評価方法（手動）

- 同じ動画で改善前後の japanese.srt を比較（特に「文が途中で切れる行」と
  「固有名詞を含む行」）
- 用語集が生成されたか、訳語が全編で統一されているかを確認
- バッチのフォールバック率をログで確認（高頻度ならスキーマ・行数を調整）
