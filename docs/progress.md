# 進捗状況

最終更新: 2026-05-31

## ASR 移行（Qwen3-ASR → Gemma 4 audio）

計画は [plan.md](plan.md) を参照。

### フェーズ 0: 事前検証
- [ ] 検証用 venv 作成・Gemma 4 audio 動作確認
- [ ] 書き起こし精度・速度・VRAM 計測（vs Qwen3-ASR）
- [ ] タイムスタンプ取得方針の決定
- [ ] モデルサイズ選定

### フェーズ 1: 依存切替 + venv 本番化
- [ ] 本番 venv 化（conda → venv）
- [ ] requirements.txt 更新（qwen-asr 削除 / transformers>=5.5.0）
- [ ] start.bat / CLAUDE.md 環境記述更新

### フェーズ 2: asr.py 書き換え
- [ ] 未着手

### フェーズ 3: 品質改善（タイムスタンプ）
- [ ] 未着手

### フェーズ 4: 後片付け・ドキュメント
- [ ] 未着手
