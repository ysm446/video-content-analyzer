"""ユーザー操作による処理中断の共有フラグ。

単一ユーザー前提のグローバルな ``threading.Event``。実行中の推論ループ
（ASR のセグメント走査・llama.cpp のストリーム読み取り）が ``is_canceled()`` を
ポーリングし、True なら ``CanceledError`` を送出して即座に中断する。

中断はワーカースレッド側で安全に行われる（推論が止まってから呼び出し側で
モデルを unload するため、推論中 unload による事故を避けられる）。

- ``POST /cancel`` が ``request_cancel()`` を呼んでフラグを立てる
- 各 SSE ハンドラは処理開始時に ``clear_cancel()`` でフラグをクリアする
"""
import threading


class CanceledError(Exception):
    """ユーザーが処理を中止したことを表す。"""


_event = threading.Event()


def request_cancel() -> None:
    """中断を要求する（フラグを立てる）。"""
    _event.set()


def clear_cancel() -> None:
    """フラグをクリアする（新しい処理の開始時に呼ぶ）。"""
    _event.clear()


def is_canceled() -> bool:
    return _event.is_set()


def raise_if_canceled() -> None:
    if _event.is_set():
        raise CanceledError()
