# 進捗状況

最終更新: 2026-05-31

## ASR 移行（Qwen3-ASR → faster-whisper large-v3）

計画は [plan.md](plan.md) を参照。**2026-05-31: ASR は Whisper 一本化に決定**（Gemma 4 は不採用）。

### フェーズ 0: 事前検証・モデル選定（完了）
- [x] 検証用 venv `.venv-gemma`（torch 2.12.0+cu130 / Python 3.10.19）
- [x] Gemma 4 E2B を検証: 英語・日本語とも書き起こし品質は実用レベル（RTF 0.13–0.19, VRAM 10.4GB）
  - ただしタイムスタンプ非対応。プロンプト指示の時刻は不正確（[00:09]→実際21秒）で却下
- [x] faster-whisper large-v3 を検証: 品質◎・**単語時刻が正確**（「カメラ」21.94s≒正解21s）・RTF 0.10・VRAM ~3GB
- [x] 比較の結果 **Whisper を採用**（タイムスタンプ/長尺一括/軽量で優位）

### フェーズ 1: 環境整備（conda → venv 本番化）
- [ ] 本番 venv `.venv` 作成
- [ ] requirements.txt 更新（qwen-asr/transformers ピン削除、faster-whisper + nvidia-cublas-cu12/cudnn-cu12 追加）
- [ ] start.bat / CLAUDE.md 環境記述更新

### フェーズ 2: asr.py 書き換え（Qwen3-ASR → faster-whisper）
- [ ] WhisperModel への置換、CUDA DLL パス処理、セグメント/時刻マップ、ForcedAligner撤去
- [ ] 未着手

### フェーズ 3: 後片付け・ドキュメント
- [ ] CLAUDE.md アーキ図更新、transformers依存の撤去可否確認、旧Qwenコード整理
- [ ] 未着手
