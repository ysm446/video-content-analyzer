# 変更履歴

## 2026-05-31
- **実装（branch feat/asr-whisper）**: backend/asr.py を faster-whisper(large-v3-turbo) に全面書き換え
  - 単語タイムスタンプから句読点・長さ基準で字幕セグメント生成（ForcedAligner不要化）
  - CTranslate2 の CUDA12 DLL 問題に対応（_add_nvidia_dlls）
  - requirements.txt 整理（qwen-asr/torchaudio/soundfile/qwen-vl-utils 削除、transformersピン解除、faster-whisper+nvidia cu12 追加）
  - start.bat を venv 有効化に、CLAUDE.md を venv/Whisper 構成に更新
  - 本番 .venv をクリーン構築し import/実機ASR検証OK（カメラ=21.76s、RTF~0.02）
  - Electron UI からの E2E 確認OK（/transcribe 200 → 18セグメント → アンロード、エラーなし）
  - feat/asr-whisper を main へマージ（fast-forward）
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
