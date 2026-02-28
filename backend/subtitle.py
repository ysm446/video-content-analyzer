import re
from datetime import timedelta
from pathlib import Path

import srt


def _to_td(seconds: float) -> timedelta:
    return timedelta(seconds=max(0.0, float(seconds)))


def segments_to_srt(segments: list[dict]) -> str:
    """
    ASRセグメントリストを SRT 文字列に変換する。

    Args:
        segments: [{"text": str, "timestamp": (start, end)}, ...]
    """
    subtitles = []
    for i, seg in enumerate(segments, 1):
        start, end = seg["timestamp"]
        if end is None:
            end = start + 4.0  # 終端が不明な場合のフォールバック
        subtitles.append(
            srt.Subtitle(
                index=i,
                start=_to_td(start),
                end=_to_td(end),
                content=seg["text"].strip(),
            )
        )
    return srt.compose(subtitles)


def srt_file_to_segments(srt_path: str) -> list[dict]:
    """
    SRT ファイルを読み込み、セグメントリストに変換する。

    Returns:
        [{"text": str, "timestamp": (start_sec, end_sec)}, ...]
    """
    text = Path(srt_path).read_text(encoding="utf-8")
    return [
        {
            "text": sub.content,
            "timestamp": (
                sub.start.total_seconds(),
                sub.end.total_seconds(),
            ),
        }
        for sub in srt.parse(text)
    ]


def save_srt(content: str, path: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def split_long_segments(
    segments: list[dict],
    max_words: int = 12,
) -> list[dict]:
    """
    長いセグメントをより短い字幕単位に分割する。

    分割の優先順位:
      1. 文末句読点（. ! ?）で分割
      2. それでも max_words を超える場合はカンマで分割
      3. それでも超える場合は max_words 単語ごとに強制分割

    タイムスタンプは単語数に比例して再配分する。

    Args:
        segments: [{"text": str, "timestamp": (start_sec, end_sec)}, ...]
        max_words: 1セグメントの最大単語数
    """
    result = []
    for seg in segments:
        text = seg["text"].strip()
        start, end = seg["timestamp"]
        words = text.split()

        if len(words) <= max_words:
            result.append(seg)
            continue

        # ① 文末で分割（ピリオド・感嘆符・疑問符の後ろ）
        raw_chunks = re.split(r'(?<=[.!?])\s+', text)

        # ② それでも長いチャンクはカンマで再分割 → さらに長ければ強制分割
        chunks: list[str] = []
        for chunk in raw_chunks:
            if len(chunk.split()) <= max_words:
                chunks.append(chunk)
            else:
                sub = re.split(r'(?<=,)\s+', chunk)
                for s in sub:
                    if len(s.split()) <= max_words:
                        chunks.append(s)
                    else:
                        # ③ 強制分割
                        ws = s.split()
                        for i in range(0, len(ws), max_words):
                            chunks.append(' '.join(ws[i:i + max_words]))

        # タイムスタンプをチャンクの単語数比で配分
        total_words = sum(len(c.split()) for c in chunks) or 1
        duration = end - start
        t = start
        for chunk in chunks:
            n = len(chunk.split())
            chunk_end = t + duration * n / total_words
            result.append({"text": chunk, "timestamp": (t, chunk_end)})
            t = chunk_end

    return result


def make_output_path(video_path: str, suffix: str) -> str:
    """
    動画パスからSRT出力パスを生成する。
    例: video.mp4 → video.original.srt / video.japanese.srt
    """
    p = Path(video_path)
    return str(p.parent / f"{p.stem}.{suffix}.srt")
