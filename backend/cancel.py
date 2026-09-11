"""ユーザー操作による処理中断の共有フラグ。

単一ユーザー前提のグローバルな ``threading.Event``。実行中の推論ループ
（ASR のセグメント走査・llama.cpp のストリーム読み取り）が ``is_canceled()`` を
ポーリングし、True なら ``CanceledError`` を送出して即座に中断する。

中断はワーカースレッド側で安全に行われる（推論が止まってから呼び出し側で
モデルを unload するため、推論中 unload による事故を避けられる）。

- ``POST /cancel`` が ``request_cancel()`` を呼んでフラグを立てる
- 各 SSE ハンドラは処理開始時に ``clear_cancel()`` でフラグをクリアし、
  ``begin_job()`` / ``end_job()`` で実行中であることを登録する
- 実行中のジョブが無いときの ``request_cancel()`` は無視する（フラグが残ったままだと
  ``/lookup`` などの非 SSE 推論が次の SSE 処理まで ``CanceledError`` で失敗し続けるため）
- ジョブ終了時（正常・エラー問わず）にはフラグをクリアする
"""
import threading


class CanceledError(Exception):
    """ユーザーが処理を中止したことを表す。"""


_event = threading.Event()
_lock = threading.Lock()
_running = 0


def request_cancel() -> bool:
    """中断を要求する（フラグを立てる）。実行中のジョブが無ければ何もせず False を返す。"""
    with _lock:
        if _running <= 0:
            return False
        _event.set()
        return True


def clear_cancel() -> None:
    """フラグをクリアする（新しい処理の開始時に呼ぶ）。"""
    _event.clear()


def begin_job() -> None:
    """SSE 処理の開始を登録する（フラグもクリアする）。"""
    global _running
    with _lock:
        _running += 1
        _event.clear()


def end_job(*, keep_flag: bool = False) -> None:
    """SSE 処理の終了を登録する。最後のジョブが終わったらフラグもクリアする。

    keep_flag=True はクライアント切断でワーカースレッドがまだ走っているとき用
    （フラグを残してワーカーを止める。次の begin_job() でクリアされる）。
    """
    global _running
    with _lock:
        _running = max(0, _running - 1)
        if _running == 0 and not keep_flag:
            _event.clear()


def is_running() -> bool:
    return _running > 0


def is_canceled() -> bool:
    return _event.is_set()


def raise_if_canceled() -> None:
    if _event.is_set():
        raise CanceledError()
