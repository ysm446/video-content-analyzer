"""動画レポート（1ページ Markdown＋画像）の生成。

分析キャッシュのチャプター（章立て）を骨格に、各章の代表画像を選んで
`{動画名}_report/report.md` と `images/` に書き出す。

第1段階（このモジュール）は LLM を使わない:
  1. ffmpeg のシーン変化検出で「絵が切り替わった時刻」を候補にする
     （等間隔サンプリングだと同じ絵が並びやすい。スライド・画面共有に強い）
  2. 候補フレームに dHash（知覚ハッシュ）を取り、ハミング距離が近いものを
     レポート全体で間引く（章をまたいでも同じ絵を二度出さない）
  3. 章ごとに上限枚数まで時間的に散らして選び、選ばれた時刻だけを
     入力シークで長辺 1280px の JPEG として保存する
  4. 概要・タグ・章ごとのまとめ・画像を Markdown に組み立てる

生成した構造は `report.json` にも保存し、後段（LLM による本文生成・単一 HTML 書き出し・
章単位の再生成）が同じ材料を再利用できるようにする。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageStat

from . import cancel

# 既定値（リクエストで上書き可）
DEFAULT_MAX_IMAGES_PER_CHAPTER = 3
DEFAULT_IMAGE_MAX_SIDE = 1280
DEFAULT_HASH_DISTANCE = 12        # dHash(64bit) のハミング距離がこれ以下なら「同じ絵」とみなす
LOOSE_HASH_DISTANCE = 20          # これ以下は「似た絵」= 補欠扱い（章に他の候補が無いときだけ使う）
PIXEL_MAD_SAME = 8.0              # 16x16 グレースケールの平均絶対差がこれ以下なら「同じ絵」
                                  # （講演者が少し動いただけの同一スライドは dHash より確実に拾える）
SCENE_THRESHOLD = 0.30            # ffmpeg scene 検出の閾値（分析の 0.35 より少し敏感に）
MAX_SCENE_CANDIDATES = 1500       # 高速カットの動画で候補が爆発しないための上限
CANDIDATE_MIN_GAP_SEC = 2.0       # 同じ章の候補どうしの最小間隔
MIN_LUMA = 12.0                   # これより暗い（フェード中など）フレームは捨てる
MIN_STDDEV = 6.0                  # 単色に近いフレームは捨てる
JPEG_QUALITY = 88

ProgressFn = Callable[[dict], None]


def report_dir_for(video_path: str | Path) -> Path:
    p = Path(video_path)
    return p.parent / (p.stem + "_report")


@dataclass
class ChapterImage:
    ts: float
    file: str  # report フォルダからの相対パス
    caption: str = ""


@dataclass
class ChapterOut:
    index: int
    title: str
    start_sec: float
    end_sec: float
    summary: str
    images: list[ChapterImage] = field(default_factory=list)
    points: list[str] = field(default_factory=list)


# ---------- 画像ユーティリティ ----------

def dhash(img: Image.Image, size: int = 8) -> int:
    """差分ハッシュ。9x8 のグレースケールに縮めて隣接画素の大小を 64bit に畳む。"""
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def gray_thumb(img: Image.Image, n: int = 16) -> np.ndarray:
    return np.asarray(img.convert("L").resize((n, n), Image.Resampling.LANCZOS), dtype=np.int16).ravel()


def pixel_mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean()) if a.size else 0.0


# 画素比較（pixel_mad）はハッシュがこの距離以内のときだけ行う。同一スライドで講演者が
# 動いただけならハッシュも近いので取りこぼしは無く、候補 1500 枚 × 採用済み全件の
# 総当たりを避けられる
PIXEL_CHECK_HASH_DISTANCE = 28


class Signature:
    """間引き判定用の特徴（dHash＋16x16 グレースケール）。"""

    __slots__ = ("hash", "gray")

    def __init__(self, img: Image.Image):
        self.hash = dhash(img)
        self.gray = gray_thumb(img)

    def same_as(self, other: "Signature", hash_distance: int) -> bool:
        d = hamming(self.hash, other.hash)
        if d <= hash_distance:
            return True
        return d <= PIXEL_CHECK_HASH_DISTANCE and pixel_mad(self.gray, other.gray) <= PIXEL_MAD_SAME

    def similar_to(self, other: "Signature") -> bool:
        return hamming(self.hash, other.hash) <= LOOSE_HASH_DISTANCE

    def min_distance(self, others: list["Signature"]) -> int:
        return min([hamming(self.hash, o.hash) for o in others] or [99])


def is_blank_frame(img: Image.Image) -> bool:
    """真っ黒・単色に近いフレーム（フェード・区切り）を除外する。"""
    stat = ImageStat.Stat(img.convert("L"))
    return stat.mean[0] < MIN_LUMA or stat.stddev[0] < MIN_STDDEV


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _file_stamp(sec: float) -> str:
    h, rest = divmod(max(0.0, sec), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}-{int(m):02d}-{s:06.3f}"


# ---------- ffmpeg ----------

def probe_duration(video_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def detect_scene_changes(video_path: str, threshold: float = SCENE_THRESHOLD) -> list[tuple[float, Image.Image]]:
    """シーン変化のあった時刻とその縮小フレーム（ハッシュ用）を返す。

    全編を 1 回デコードするため長い動画では数十秒かかる。ハッシュ用なので
    幅 320px に縮小し、ディスク I/O を抑える。
    """
    results: list[tuple[float, Image.Image]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        outpattern = str(Path(tmpdir) / "c_%05d.jpg")
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"select=eq(n\\,0)+gt(scene\\,{threshold}),scale=320:-2,showinfo",
            "-vsync", "vfr", "-frames:v", str(MAX_SCENE_CANDIDATES),
            "-vcodec", "mjpeg", "-q:v", "4",
            outpattern, "-y",
        ]
        # 全編デコードは長い動画で数十秒かかるので、中止フラグを監視しながら待つ
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
        stderr = ""
        while True:
            try:
                _, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_canceled():
                    proc.kill()
                    proc.communicate()
                    raise cancel.CanceledError()
        if proc.returncode != 0:
            tail = " / ".join(stderr.strip().splitlines()[-3:])
            raise RuntimeError(f"シーン検出 ffmpeg が失敗しました: {tail}")
        ts_re = re.compile(r"pts_time:(\d+\.?\d*)")
        stamps = [float(m.group(1)) for line in stderr.splitlines() if (m := ts_re.search(line))]
        files = sorted(f for f in os.listdir(tmpdir) if f.startswith("c_") and f.endswith(".jpg"))
        for i, fname in enumerate(files):
            if i >= len(stamps):
                break
            try:
                img = Image.open(Path(tmpdir) / fname)
                img.load()
                results.append((stamps[i], img.copy()))
            except Exception:
                continue
    return results


def grab_frame(video_path: str, ts: float, max_side: int) -> Image.Image | None:
    """指定時刻へ入力シークして 1 フレームを長辺 max_side 以下で取り出す。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = str(Path(tmpdir) / "f.jpg")
        cmd = [
            "ffmpeg", "-ss", f"{max(0.0, ts):.3f}", "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
            "-q:v", "2", out, "-y",
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not Path(out).exists():
            return None
        img = Image.open(out)
        img.load()
        return img.copy()


# ---------- 候補の選定 ----------

def _spread_pick(items: list, n: int) -> list:
    """時間順のリストから n 個を、先頭を含めてなるべく等間隔に選ぶ。"""
    if len(items) <= n:
        return list(items)
    if n <= 1:
        return [items[0]]
    step = (len(items) - 1) / (n - 1)
    idx = sorted({min(round(i * step), len(items) - 1) for i in range(n)})
    return [items[i] for i in idx]


def select_images(
    video_path: str,
    chapters: list[dict],
    duration: float,
    max_per_chapter: int,
    hash_distance: int,
    progress: ProgressFn,
    detected: list[tuple[float, Image.Image]] | None = None,
) -> tuple[list[list[float]], list[list[float]], dict]:
    """各章に載せる時刻を決める。戻り値は (機械選定の時刻, VL に見せる候補の時刻, 統計)。
    detected を渡すとシーン検出を省略する（章立ての作り直しで先に検出済みのとき）。"""
    if detected is None:
        progress({"status": "detecting_scenes"})
        detected = detect_scene_changes(video_path)
    cancel.raise_if_canceled()

    # 章ごとに候補を振り分け、候補が乏しい章には章の先頭・中間を補う
    bounds = [(float(c["start_sec"]), float(c["end_sec"])) for c in chapters]

    def chapter_of(ts: float) -> int | None:
        for i, (s, e) in enumerate(bounds):
            if s <= ts < e:
                return i
        return len(bounds) - 1 if bounds and ts >= bounds[-1][1] - 0.5 else None

    per_chapter: list[list[tuple[float, Image.Image | None]]] = [[] for _ in chapters]
    for ts, img in detected:
        ci = chapter_of(ts)
        if ci is None:
            continue
        if per_chapter[ci] and ts - per_chapter[ci][-1][0] < CANDIDATE_MIN_GAP_SEC:
            continue
        per_chapter[ci].append((ts, img))

    fill_count = 0
    for i, (s, e) in enumerate(bounds):
        span = max(0.0, e - s)
        want = [s + 0.5] if span > 1.0 else [s]
        if span > 30:
            want.append(s + span / 2)
        for ts in want:
            ts = min(ts, max(duration - 0.1, 0.0))
            if any(abs(ts - c[0]) < CANDIDATE_MIN_GAP_SEC for c in per_chapter[i]):
                continue
            if len(per_chapter[i]) >= 2:
                continue
            per_chapter[i].append((ts, None))
            fill_count += 1
        per_chapter[i].sort(key=lambda c: c[0])

    # 補充分（画像未取得）のハッシュ用フレームを取り出す
    fills = [(i, k) for i, lst in enumerate(per_chapter) for k, c in enumerate(lst) if c[1] is None]
    if fills:
        progress({"status": "extracting", "phase": "candidates", "current": 0, "total": len(fills)})

        def grab(idx: tuple[int, int]):
            if cancel.is_canceled():
                return None
            i, k = idx
            return grab_frame(video_path, per_chapter[i][k][0], 320)

        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(grab, fills))
        cancel.raise_if_canceled()
        for (i, k), img in zip(fills, got):
            per_chapter[i][k] = (per_chapter[i][k][0], img)

    # 全体で知覚ハッシュにより間引く（時間順に見て、既に採用した絵と近ければ捨てる）。
    # 「同じ絵」は捨て、「似た絵」（講演者のアップの繰り返し等）は補欠に回し、
    # その章に他の候補が無いときだけ 1 枚使う
    kept: list[Signature] = []
    selected: list[list[float]] = []
    candidates: list[list[float]] = []
    dropped_similar = 0
    dropped_blank = 0
    for i, lst in enumerate(per_chapter):
        cancel.raise_if_canceled()
        uniq: list[float] = []
        reserve: list[tuple[int, float, Signature]] = []
        for ts, img in lst:
            if img is None:
                continue
            if is_blank_frame(img):
                dropped_blank += 1
                continue
            sig = Signature(img)
            if any(sig.same_as(k, hash_distance) for k in kept):
                dropped_similar += 1
                continue
            if any(sig.similar_to(k) for k in kept):
                reserve.append((sig.min_distance(kept), ts, sig))
                continue
            kept.append(sig)
            uniq.append(ts)
        if not uniq and reserve:
            # 補欠のうち既出から最も遠いものを 1 枚だけ採用
            best = max(reserve, key=lambda r: r[0])
            uniq = [best[1]]
            kept.append(best[2])
            dropped_similar += len(reserve) - 1
        elif reserve:
            dropped_similar += len(reserve)
        if not uniq and lst:
            # 全候補が単色だった章: 場面が分かるように先頭を 1 枚だけ確保する
            uniq = [lst[0][0]]
        picked = _spread_pick(uniq, max_per_chapter)
        selected.append(picked)
        candidates.append(_spread_pick(uniq, MAX_CANDIDATES_PER_CHAPTER))
        progress({"status": "selecting", "current": i + 1, "total": len(per_chapter), "picked": len(picked)})

    stats = {
        "scene_changes": len(detected),
        "filled": fill_count,
        "dropped_similar": dropped_similar,
        "dropped_blank": dropped_blank,
        "selected": sum(len(s) for s in selected),
    }
    return selected, candidates, stats


# ---------- VL モデルによる本文生成と画像選定（第2段階） ----------

MAX_CANDIDATES_PER_CHAPTER = 6   # モデルに見せる候補フレームの上限
CANDIDATE_MAX_SIDE = 640         # モデルに渡す候補フレームの長辺（VRAM 側で更に縮小される）
CHAPTER_TRANSCRIPT_MAX_CHARS = 1800

# レポートの形式: 時系列の章だけ / 内容から再構成したテーマ別だけ / 両方（既定）
REPORT_LAYOUTS = ("both", "timeline", "thematic")
DEFAULT_LAYOUT = "both"
SYNTHESIS_TRANSCRIPT_MAX_CHARS = 6000   # 再構成パスに渡す字幕サンプル（章本文と併せて ctx 16k に収める）
SYNTHESIS_MAX_THEMES = 8
SYNTHESIS_IMAGES_PER_THEME = 2


def slice_transcript_rows(rows: list[tuple[float, str]], start_sec: float, end_sec: float, max_chars: int = CHAPTER_TRANSCRIPT_MAX_CHARS) -> str:
    """時刻付き字幕行から区間内のものを取り出し、長ければ等間隔に間引いて返す。"""
    picked = [(t, txt) for t, txt in rows if start_sec <= t < end_sec and txt.strip()]
    if not picked:
        return ""
    total = sum(len(txt) + 8 for _, txt in picked)
    if total > max_chars:
        keep = max(1, int(len(picked) * max_chars / total))
        picked = _spread_pick(picked, keep)
    return "\n".join(f"[{fmt_ts(t)}] {txt.strip()}" for t, txt in picked)


def _parse_label_ts(value) -> float | None:
    """"[m:ss]" / "m:ss" / "h:mm:ss" を秒に。解釈できなければ None。"""
    if value is None:
        return None
    s = str(value).strip().strip("[]")
    m = re.match(r"^(?:(\d+):)?(\d+):(\d{2})(?:\.(\d+))?$", s)  # 100 分以降の章もラベルは m:ss（分は桁数制限なし）
    if not m:
        return None
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _video_context_text(meta: dict) -> str:
    parts = []
    if meta.get("genre"):
        parts.append(f"ジャンル: {meta['genre']}")
    if meta.get("summary"):
        parts.append(f"概要: {meta['summary']}")
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str)]
    if tags:
        parts.append("タグ: " + ", ".join(tags[:12]))
    return "\n".join(parts)


def generate_chapter_texts(
    video_path: str,
    reviewer,
    norm: list[dict],
    candidates: list[list[float]],
    fallback: list[list[float]],
    meta: dict,
    transcript_rows: list[tuple[float, str]],
    max_per_chapter: int,
    progress: ProgressFn,
) -> tuple[list[dict], list[list[tuple[float, str]]]]:
    """章ごとに VL モデルで本文と掲載画像を決める。

    戻り値: (章ごとの {summary, points}, 章ごとの [(time_sec, caption)])。
    失敗した章は progress に report_warning を流し、機械選定の結果（fallback）を使う。
    """
    video_context = _video_context_text(meta)
    texts: list[dict] = []
    picks: list[list[tuple[float, str]]] = []
    total = len(norm)
    for ci, c in enumerate(norm):
        cancel.raise_if_canceled()
        progress({"status": "generating", "current": ci + 1, "total": total, "title": c["title"]})
        cand_ts = candidates[ci] or fallback[ci]
        fb = [(ts, fmt_ts(ts)) for ts in fallback[ci]]
        if not cand_ts:
            texts.append({})
            picks.append(fb)
            continue
        # 候補フレームを並列シークで取得（モデル用なので長辺 640px）
        def grab(ts: float):
            if cancel.is_canceled():
                return None
            return grab_frame(video_path, ts, CANDIDATE_MAX_SIDE)
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(grab, cand_ts))
        cancel.raise_if_canceled()
        frames = [im for im in got if im is not None]
        ts_ok = [ts for ts, im in zip(cand_ts, got) if im is not None]
        if not frames:
            texts.append({})
            picks.append(fb)
            continue
        transcript = slice_transcript_rows(transcript_rows, c["start_sec"], c["end_sec"])
        try:
            res = reviewer.report_chapter(
                frames, ts_ok, c["title"], c["start_sec"], c["end_sec"],
                transcript=transcript, max_images=max_per_chapter, video_context=video_context,
            )
        except cancel.CanceledError:
            raise
        except Exception as e:
            progress({"status": "report_warning", "message": f"章 {ci + 1}「{c['title']}」の本文生成に失敗したため機械選定で続行します: {e}", "current": ci + 1, "total": total})
            texts.append({})
            picks.append(fb)
            continue
        gm = res.get("_meta") or {}
        if gm.get("finish_reason") == "length":
            progress({"status": "report_warning", "message": f"章 {ci + 1}: 出力がトークン上限で打ち切られました", "current": ci + 1, "total": total})
        summary = str(res.get("summary") or "").strip()
        points = [str(x).strip() for x in (res.get("points") or []) if str(x).strip()][:6]
        chosen: list[tuple[float, str]] = []
        seen: set[float] = set()
        for im in res.get("images") or []:
            if not isinstance(im, dict):
                continue
            t = _parse_label_ts(im.get("time"))
            if t is None:
                continue
            # 候補の最近傍にスナップ（ラベルは秒単位なので ±1 秒程度のずれを吸収）
            snap = min(ts_ok, key=lambda x: abs(x - t))
            if abs(snap - t) > 2.0 or snap in seen:
                continue
            seen.add(snap)
            chosen.append((snap, str(im.get("caption") or "").strip()))
            if len(chosen) >= max_per_chapter:
                break
        if not chosen:
            chosen = fb[:1] or [(ts_ok[0], fmt_ts(ts_ok[0]))]
        chosen.sort(key=lambda x: x[0])
        texts.append({"summary": summary, "points": points})
        picks.append(chosen)
    return texts, picks


# ---------- Markdown ----------

# ---------- 内容の再構成（テーマ別まとめ） ----------

SYNTHESIS_SYSTEM = (
    "/no_think\n"
    "あなたは動画の内容を読者向けの報告書に再構成する編集者です。"
    "時系列の章ごとのまとめと字幕を材料に、動画全体を横断して「結局何が語られたか」を整理します。"
    "章の順番に縛られず、同じ話題は 1 つのテーマにまとめ、テーマの見出しは内容を表す名詞句にします。"
    "字幕と章のまとめに書かれていることだけを根拠にし、推測で補わないでください。"
)

_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "key_messages": {"type": "array", "items": {"type": "string"}},
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "times": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "body", "times"],
            },
        },
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "genre": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["key_messages", "themes", "key_facts", "summary", "genre", "tags"],
}


def _sample_rows_text(rows: list[tuple[float, str]], max_chars: int) -> str:
    """字幕行を全編から等間隔に間引いて "[m:ss] text" のテキストにする（先頭切り捨てにしない）。"""
    rows = [(t, x) for t, x in rows if x and x.strip()]
    if not rows:
        return ""
    total = sum(len(x) + 9 for _, x in rows)
    if total > max_chars:
        keep = max(2, int(len(rows) * max_chars / total))
        step = len(rows) / keep
        rows = [rows[int(i * step)] for i in range(keep)]
    return "\n".join(f"[{fmt_ts(t)}] {x.strip()}" for t, x in rows)


def build_synthesis_prompt(meta: dict, chapters: list["ChapterOut"], transcript_rows: list[tuple[float, str]], duration: float) -> str:
    parts: list[str] = [f"動画の長さ: {fmt_ts(duration)}"]
    ctx = _video_context_text(meta)
    if ctx:
        parts.append(f"動画全体の情報:\n{ctx}")
    if chapters:
        rows = []
        for ch in chapters:
            line = f"[{fmt_ts(ch.start_sec)}〜{fmt_ts(ch.end_sec)}] {ch.title}"
            if ch.summary:
                line += f": {ch.summary}"
            if ch.points:
                line += "\n  - " + "\n  - ".join(ch.points)
            rows.append(line)
        parts.append("時系列の章ごとのまとめ:\n" + "\n".join(rows))
    sample = _sample_rows_text(transcript_rows, SYNTHESIS_TRANSCRIPT_MAX_CHARS)
    if sample:
        parts.append(f"字幕（全編から抜粋）:\n{sample}")
    parts.append(
        "次の JSON のみを出力してください。\n"
        "- key_messages: この動画の主要なメッセージ（3〜5 個・各 1 文。結論・主張・最も重要な発表を先に）\n"
        f"- themes: 内容をテーマ別に再構成したまとめ（3〜{SYNTHESIS_MAX_THEMES} 個）。title は内容を表す名詞句、"
        "body は 2〜5 文で、数値・固有名詞・比較は字幕に基づいて具体的に。times はそのテーマが語られている"
        "時刻（章の範囲や字幕の [m:ss] から 1〜3 個。m:ss 形式）\n"
        "- key_facts: 主要な事実・数値・固有名詞・製品名など（最大 10 個・各 1 文。無ければ空配列）\n"
        "- summary: 動画全体の概要（1〜2 文）。genre: ジャンル（短く）。tags: 内容を表すタグ（3〜8 個）"
    )
    return "\n\n".join(parts)


def generate_synthesis(
    reviewer,
    meta: dict,
    chapters: list["ChapterOut"],
    transcript_rows: list[tuple[float, str]],
    duration: float,
) -> dict | None:
    """動画全体を横断した再構成（要点・テーマ別まとめ・主要な事実）をテキストのみ推論で生成する。

    戻り値: {key_messages, themes:[{title, body, times:[sec], images:[{time_sec,file,caption}]}], key_facts,
             summary, genre, tags}。失敗時は例外（呼び出し側で警告にして続行）。
    テーマの画像は章の画像から時刻が最も近いものを流用する（追加抽出はしない）。
    """
    prompt = build_synthesis_prompt(meta, chapters, transcript_rows, duration)
    raw, gen_meta = reviewer.text_infer(
        SYNTHESIS_SYSTEM, prompt, max_new_tokens=2048,
        response_format={"type": "json_schema", "json_schema": {"name": "report_synthesis", "schema": _SYNTHESIS_SCHEMA}},
    )
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    if not isinstance(data, dict):
        raise RuntimeError("再構成の JSON を解釈できませんでした")

    def _strs(v, limit: int) -> list[str]:
        return [str(x).strip() for x in (v or []) if isinstance(v, list) and str(x).strip()][:limit]

    all_images = [im for ch in chapters for im in ch.images]
    themes: list[dict] = []
    used_files: set[str] = set()
    for t in (data.get("themes") or [])[:SYNTHESIS_MAX_THEMES]:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").strip()
        body = str(t.get("body") or "").strip()
        if not title or not body:
            continue
        times: list[float] = []
        for v in (t.get("times") or [])[:3]:
            sec = _parse_label_ts(v)
            if sec is not None and 0 <= sec <= max(duration, 0) + 1:
                times.append(float(sec))
        images: list[dict] = []
        for sec in times:
            if not all_images:
                break
            near = min(all_images, key=lambda im: abs(im.ts - sec))
            if abs(near.ts - sec) > 180.0 or near.file in used_files:
                continue
            used_files.add(near.file)
            images.append({"time_sec": near.ts, "file": near.file, "caption": near.caption})
            if len(images) >= SYNTHESIS_IMAGES_PER_THEME:
                break
        themes.append({"title": title, "body": body, "times": times, "images": images})

    return {
        "key_messages": _strs(data.get("key_messages"), 6),
        "themes": themes,
        "key_facts": _strs(data.get("key_facts"), 10),
        "summary": str(data.get("summary") or "").strip(),
        "genre": str(data.get("genre") or "").strip(),
        "tags": _strs(data.get("tags"), 8),
        "_meta": gen_meta,
    }


def _md_escape(text: str) -> str:
    """段落・箇条書き用。改行は空白に畳む（モデル出力の改行で箇条書きが途切れないように）。"""
    return re.sub(r"\s*\n\s*", " ", str(text or "").replace("\r", "")).strip()


def _md_inline(text: str) -> str:
    """画像 alt / キャプション用。[ ] ( ) * を含むと画像構文が壊れるので落とす。"""
    return re.sub(r"[\[\]()*]", "", _md_escape(text)).strip()


def _anchor(index: int) -> str:
    return f"ch{index:02d}"


def build_markdown(
    video_name: str,
    duration: float,
    meta: dict,
    chapters: list[ChapterOut],
    generated_at: str,
    synthesis: dict | None = None,
    layout: str = DEFAULT_LAYOUT,
) -> str:
    show_timeline = layout != "thematic"
    show_synthesis = layout != "timeline" and bool(synthesis)
    lines: list[str] = []
    lines.append(f"# {_md_escape(video_name)}")
    lines.append("")
    info = [f"長さ: {fmt_ts(duration)}"]
    if meta.get("genre"):
        info.append(f"ジャンル: {_md_escape(meta['genre'])}")
    info.append(f"生成: {generated_at}")
    lines.append(" ／ ".join(info))
    lines.append("")
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and t.strip()]
    if tags:
        lines.append(" ".join(f"`{_md_escape(t)}`" for t in tags))
        lines.append("")
    if meta.get("summary"):
        lines.append("## 概要")
        lines.append("")
        lines.append(_md_escape(meta["summary"]))
        lines.append("")
    if meta.get("detail"):
        lines.append(_md_escape(meta["detail"]))
        lines.append("")

    if show_synthesis:
        syn = synthesis or {}
        if syn.get("key_messages"):
            lines.append("## 要点")
            lines.append("")
            for m in syn["key_messages"]:
                lines.append(f"- {_md_escape(m)}")
            lines.append("")
        if syn.get("themes"):
            lines.append("## テーマ別のまとめ")
            lines.append("")
            for th in syn["themes"]:
                lines.append(f"### {_md_escape(th.get('title'))}")
                lines.append("")
                if th.get("times"):
                    lines.append("*" + " / ".join(fmt_ts(t) for t in th["times"]) + "*")
                    lines.append("")
                lines.append(_md_escape(th.get("body")))
                lines.append("")
                for im in th.get("images") or []:
                    cap = im.get("caption") or fmt_ts(float(im.get("time_sec") or 0))
                    lines.append(f"![{_md_inline(cap)}]({im['file']})")
                    lines.append("")
                    lines.append(f"*{_md_inline(cap)}*")
                    lines.append("")
        if syn.get("key_facts"):
            lines.append("## 主要な事実・数値")
            lines.append("")
            for f in syn["key_facts"]:
                lines.append(f"- {_md_escape(f)}")
            lines.append("")

    if show_timeline:
        lines.append("## 目次" if not show_synthesis else "## 時系列の章")
        lines.append("")
        for ch in chapters:
            lines.append(f"{ch.index}. [{_md_escape(ch.title)}](#{_anchor(ch.index)}) — {fmt_ts(ch.start_sec)}")
        lines.append("")

    for ch in (chapters if show_timeline else []):
        lines.append(f'<a id="{_anchor(ch.index)}"></a>')
        lines.append("")
        lines.append(f"## {ch.index}. {_md_escape(ch.title)}")
        lines.append("")
        lines.append(f"*{fmt_ts(ch.start_sec)} 〜 {fmt_ts(ch.end_sec)}*")
        lines.append("")
        if ch.summary:
            lines.append(_md_escape(ch.summary))
            lines.append("")
        if ch.points:
            for pt in ch.points:
                lines.append(f"- {_md_escape(pt)}")
            lines.append("")
        for im in ch.images:
            cap = im.caption or fmt_ts(im.ts)
            lines.append(f"![{_md_inline(cap)}]({im.file})")
            lines.append("")
            lines.append(f"*{_md_inline(cap)}*")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- 単一 HTML ----------

_HTML_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--dim:#59636e;--line:#d0d7de;--chip:#f6f8fa}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--dim:#9198a1;--line:#30363d;--chip:#161b22}}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif;line-height:1.7}
main{max-width:900px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:26px;margin:0 0 6px}h2{font-size:20px;margin:40px 0 8px;padding-top:12px;border-top:1px solid var(--line)}h3{font-size:16px;margin:24px 0 6px}
.info{color:var(--dim);font-size:13px}.tags{margin:8px 0 0}.tag{display:inline-block;background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:1px 8px;font-size:12px;margin:0 6px 6px 0}
.range{color:var(--dim);font-size:13px;margin:0 0 8px}p{margin:0 0 10px;white-space:pre-wrap}ul{margin:0 0 10px;padding-left:1.4em}
figure{margin:14px 0}figure img{max-width:100%;height:auto;border-radius:8px;border:1px solid var(--line);display:block}
figcaption{color:var(--dim);font-size:12px;margin-top:4px}
nav ol{padding-left:1.4em;margin:0}nav a{color:inherit}nav .ts{color:var(--dim);font-size:12px;margin-left:6px}
"""


def _h(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _img_data_uri(path: Path) -> str | None:
    try:
        return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def build_html(structure: dict, out_dir: Path, embed_images: bool = True) -> str:
    """report.json の構造から単一 HTML を組み立てる。画像は既定で base64 埋め込み
    （フォルダを離れても 1 ファイルで開ける）。embed_images=False なら相対パス参照。"""
    def src(rel: str) -> str:
        if embed_images:
            uri = _img_data_uri(out_dir / rel)
            if uri:
                return uri
        return rel

    meta = structure.get("meta") or {}
    title = Path(structure.get("video") or "report").stem
    o: list[str] = []
    o.append(f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    o.append(f"<title>{_h(title)}</title><style>{_HTML_CSS}</style></head><body><main>")
    o.append(f"<h1>{_h(title)}</h1>")
    info = [f"長さ: {fmt_ts(float(structure.get('duration') or 0))}"]
    if meta.get("genre"):
        info.append(f"ジャンル: {_h(meta['genre'])}")
    info.append(f"生成: {_h(structure.get('generated_at') or '')}")
    o.append(f"<div class=\"info\">{' ／ '.join(info)}</div>")
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and t.strip()]
    if tags:
        o.append("<div class=\"tags\">" + "".join(f"<span class=\"tag\">{_h(t)}</span>" for t in tags) + "</div>")
    if meta.get("summary") or meta.get("detail"):
        o.append("<h2>概要</h2>")
        if meta.get("summary"):
            o.append(f"<p>{_h(meta['summary'])}</p>")
        if meta.get("detail"):
            o.append(f"<p>{_h(meta['detail'])}</p>")
    layout = structure.get("layout") or DEFAULT_LAYOUT
    syn = structure.get("synthesis") or {}
    show_timeline = layout != "thematic"
    show_synthesis = layout != "timeline" and bool(syn)
    if show_synthesis:
        if syn.get("key_messages"):
            o.append("<h2>要点</h2><ul>" + "".join(f"<li>{_h(m)}</li>" for m in syn["key_messages"]) + "</ul>")
        if syn.get("themes"):
            o.append("<h2>テーマ別のまとめ</h2>")
            for th in syn["themes"]:
                o.append(f"<h3>{_h(th.get('title'))}</h3>")
                if th.get("times"):
                    o.append("<div class=\"range\">" + " / ".join(fmt_ts(float(t)) for t in th["times"]) + "</div>")
                o.append(f"<p>{_h(th.get('body'))}</p>")
                for im in th.get("images") or []:
                    cap = im.get("caption") or fmt_ts(float(im.get("time_sec") or 0))
                    o.append(f"<figure><img src=\"{src(im['file'])}\" alt=\"{_h(cap)}\" loading=\"lazy\"><figcaption>{_h(cap)}</figcaption></figure>")
        if syn.get("key_facts"):
            o.append("<h2>主要な事実・数値</h2><ul>" + "".join(f"<li>{_h(f)}</li>" for f in syn["key_facts"]) + "</ul>")
    chapters = (structure.get("chapters") or []) if show_timeline else []
    if chapters:
        o.append(("<h2>時系列の章</h2>" if show_synthesis else "<h2>目次</h2>") + "<nav><ol>")
        for ch in chapters:
            o.append(f"<li><a href=\"#{_anchor(int(ch['index']))}\">{_h(ch.get('title'))}</a><span class=\"ts\">{fmt_ts(float(ch.get('start_sec') or 0))}</span></li>")
        o.append("</ol></nav>")
    for ch in chapters:
        o.append(f"<h2 id=\"{_anchor(int(ch['index']))}\">{ch['index']}. {_h(ch.get('title'))}</h2>")
        o.append(f"<div class=\"range\">{fmt_ts(float(ch.get('start_sec') or 0))} 〜 {fmt_ts(float(ch.get('end_sec') or 0))}</div>")
        if ch.get("summary"):
            o.append(f"<p>{_h(ch['summary'])}</p>")
        if ch.get("points"):
            o.append("<ul>" + "".join(f"<li>{_h(pt)}</li>" for pt in ch["points"]) + "</ul>")
        for im in ch.get("images") or []:
            cap = im.get("caption") or fmt_ts(float(im.get("time_sec") or 0))
            o.append(f"<figure><img src=\"{src(im['file'])}\" alt=\"{_h(cap)}\" loading=\"lazy\"><figcaption>{_h(cap)}</figcaption></figure>")
    o.append("</main></body></html>")
    return "".join(o)


# ---------- エントリーポイント ----------

def generate_report(
    video_path: str,
    chapters: list[dict],
    meta: dict | None,
    max_per_chapter: int = DEFAULT_MAX_IMAGES_PER_CHAPTER,
    image_max_side: int = DEFAULT_IMAGE_MAX_SIDE,
    hash_distance: int = DEFAULT_HASH_DISTANCE,
    progress: ProgressFn | None = None,
    reviewer=None,
    transcript_rows: list[tuple[float, str]] | None = None,
    detected: list[tuple[float, Image.Image]] | None = None,
    layout: str = DEFAULT_LAYOUT,
) -> dict:
    """レポートを生成して {report_path, html_path, dir, chapters, images, stats} を返す。

    reviewer（VideoReviewer・ロード済み）を渡すと章ごとに VL モデルで本文と掲載画像を生成し
    （第2段階）、layout が timeline 以外なら全体を横断した再構成（要点・テーマ別・主要な事実）も
    作る。渡さなければ機械選定のみ（第1段階）。transcript_rows は [(sec, text)] の字幕行。
    """
    progress = progress or (lambda _e: None)
    meta = dict(meta or {})
    if layout not in REPORT_LAYOUTS:
        layout = DEFAULT_LAYOUT
    p = Path(video_path)
    duration = probe_duration(str(p))

    # 章の正規化（開始順に並べ、終了時刻は次章の開始から算出。フロントの toc の end_sec は
    # 「start+1」等の仮値なので使わない）
    norm: list[dict] = []
    for i, c in enumerate(chapters):
        if not isinstance(c, dict):
            continue
        try:
            s = float(c.get("start_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        norm.append({"start_sec": max(0.0, s), "title": str(c.get("title") or f"チャプター{i+1}").strip(),
                     "summary": str(c.get("summary") or "").strip()})
    norm.sort(key=lambda c: c["start_sec"])
    # 同じ開始時刻の章が 2 つあると後の章の end が duration まで伸びて全体と重なるので、先勝ちで 1 つにする
    dedup: list[dict] = []
    for c in norm:
        if dedup and abs(c["start_sec"] - dedup[-1]["start_sec"]) < 1e-3:
            continue
        dedup.append(c)
    norm = dedup
    if not norm:
        norm = [{"start_sec": 0.0, "title": "全体", "summary": str(meta.get("summary") or "")}]
    for i, c in enumerate(norm):
        nxt = norm[i + 1]["start_sec"] if i + 1 < len(norm) else duration
        c["end_sec"] = max(c["start_sec"], nxt if nxt > c["start_sec"] else duration)

    selected, candidates, stats = select_images(str(p), norm, duration, max_per_chapter, hash_distance, progress, detected=detected)
    texts: list[dict] = [{} for _ in norm]
    captions: list[dict[float, str]] = [{} for _ in norm]
    if reviewer is not None:
        texts, picks = generate_chapter_texts(
            str(p), reviewer, norm, candidates, selected, meta, transcript_rows or [], max_per_chapter, progress
        )
        selected = [[ts for ts, _ in pk] for pk in picks]
        captions = [{ts: cap for ts, cap in pk} for pk in picks]
        stats["selected"] = sum(len(s) for s in selected)
        stats["llm"] = True

    out_dir = report_dir_for(p)
    img_dir = out_dir / "images"
    # 新しい画像は一時フォルダに書き、全部そろってから images/ と入れ替える。
    # 先に消してしまうと、中止・失敗時に前回の report.json が存在しない画像を指したままになる
    new_img_dir = out_dir / "images.new"
    if new_img_dir.exists():
        for old in new_img_dir.glob("*"):
            try:
                old.unlink()
            except OSError:
                pass
    new_img_dir.mkdir(parents=True, exist_ok=True)

    # 本番解像度で抽出・保存
    jobs: list[tuple[int, int, float]] = [(ci, k, ts) for ci, lst in enumerate(selected) for k, ts in enumerate(lst)]
    total = len(jobs)
    progress({"status": "extracting", "phase": "images", "current": 0, "total": total})
    done_count = 0
    lock_files: dict[tuple, str] = {}

    def work(job):
        if cancel.is_canceled():
            return None
        ci, k, ts = job
        ts_c = min(max(0.0, ts), max(duration - 0.1, 0.0)) if duration else ts
        img = grab_frame(str(p), ts_c, image_max_side)
        if img is None:
            return None
        name = f"ch{ci+1:02d}_{k+1}_{_file_stamp(ts)}.jpg"
        img.convert("RGB").save(new_img_dir / name, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return ((ci, k), f"images/{name}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(work, jobs):
            done_count += 1
            progress({"status": "extracting", "phase": "images", "current": done_count, "total": total})
            if res:
                lock_files[res[0]] = res[1]
    cancel.raise_if_canceled()

    # 抽出が最後まで終わったので、前回の画像を消して新しい画像に差し替える
    if img_dir.exists():
        for old in img_dir.glob("*"):
            try:
                old.unlink()
            except OSError:
                pass
        try:
            img_dir.rmdir()
        except OSError:
            pass
    new_img_dir.rename(img_dir)

    chapters_out: list[ChapterOut] = []
    for ci, c in enumerate(norm):
        t = texts[ci] or {}
        ch = ChapterOut(index=ci + 1, title=c["title"], start_sec=c["start_sec"], end_sec=c["end_sec"],
                        summary=t.get("summary") or c["summary"], points=list(t.get("points") or []))
        for k, ts in enumerate(selected[ci]):
            f = lock_files.get((ci, k))
            if f:
                cap = captions[ci].get(ts) or ""
                ch.images.append(ChapterImage(ts=ts, file=f, caption=(f"{cap}（{fmt_ts(ts)}）" if cap else fmt_ts(ts))))
        chapters_out.append(ch)

    # 内容の再構成（要点・テーマ別まとめ・主要な事実）。章本文と字幕を材料にテキストのみ推論で 1 回。
    # 分析していない動画（meta が空）の概要・ジャンル・タグもここから補う
    synthesis: dict | None = None
    if reviewer is not None and layout != "timeline":
        cancel.raise_if_canceled()
        progress({"status": "synthesizing"})
        try:
            synthesis = generate_synthesis(reviewer, meta, chapters_out, transcript_rows or [], duration)
        except cancel.CanceledError:
            raise
        except Exception as e:
            progress({"status": "report_warning", "message": f"内容の再構成に失敗したため時系列の章だけで続行します: {e}"})
            synthesis = None
        if synthesis:
            if (synthesis.get("_meta") or {}).get("finish_reason") == "length":
                progress({"status": "report_warning", "message": "内容の再構成の出力がトークン上限で打ち切られました"})
            for k in ("summary", "genre", "tags"):
                if not meta.get(k) and synthesis.get(k):
                    meta[k] = synthesis[k]
            synthesis = {k: v for k, v in synthesis.items() if k != "_meta"}
            stats["themes"] = len(synthesis.get("themes") or [])
    if synthesis is None and layout == "thematic":
        layout = "timeline"  # 再構成が無いのに章も出さないと空のレポートになる

    progress({"status": "writing"})
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = build_markdown(p.stem, duration, meta, chapters_out, generated_at, synthesis=synthesis, layout=layout)
    report_path = out_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")

    structure = {
        "video": p.name,
        "duration": duration,
        "generated_at": generated_at,
        "layout": layout,
        "synthesis": synthesis,
        "meta": {k: meta.get(k) for k in ("genre", "summary", "detail", "tags") if meta.get(k)},
        "chapters": [
            {
                "index": ch.index, "title": ch.title, "start_sec": ch.start_sec, "end_sec": ch.end_sec,
                "summary": ch.summary,
                "points": ch.points,
                "images": [{"time_sec": im.ts, "file": im.file, "caption": im.caption} for im in ch.images],
            }
            for ch in chapters_out
        ],
        "stats": stats,
        "options": {"max_images_per_chapter": max_per_chapter, "image_max_side": image_max_side, "hash_distance": hash_distance, "layout": layout},
    }
    (out_dir / "report.json").write_text(json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8")
    # ビューワに依存せず開けるよう、画像を埋め込んだ単一 HTML も書き出す（md が正本、HTML は派生）
    html_path = out_dir / "report.html"
    html_path.write_text(build_html(structure, out_dir, embed_images=True), encoding="utf-8")
    print(f"[Report] 生成完了: {report_path} (章 {len(chapters_out)} / 画像 {stats['selected']} / 類似除外 {stats['dropped_similar']})")
    return {
        "report_path": str(report_path),
        "html_path": str(html_path),
        "dir": str(out_dir),
        "chapters": len(chapters_out),
        "images": stats["selected"],
        "stats": stats,
    }
