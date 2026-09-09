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
  4. 概要・タグ・章ごとのまとめ・画像・末尾にブックマークを Markdown に組み立てる

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


def gray_thumb(img: Image.Image, n: int = 16) -> list[int]:
    return list(img.convert("L").resize((n, n), Image.Resampling.LANCZOS).getdata())


def pixel_mad(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)


class Signature:
    """間引き判定用の特徴（dHash＋16x16 グレースケール）。"""

    __slots__ = ("hash", "gray")

    def __init__(self, img: Image.Image):
        self.hash = dhash(img)
        self.gray = gray_thumb(img)

    def same_as(self, other: "Signature", hash_distance: int) -> bool:
        return hamming(self.hash, other.hash) <= hash_distance or pixel_mad(self.gray, other.gray) <= PIXEL_MAD_SAME

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
) -> tuple[list[list[float]], dict]:
    """各章に載せる時刻のリストを決める。戻り値は章ごとの時刻リストと統計。"""
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
        progress({"status": "selecting", "current": i + 1, "total": len(per_chapter), "picked": len(picked)})

    stats = {
        "scene_changes": len(detected),
        "filled": fill_count,
        "dropped_similar": dropped_similar,
        "dropped_blank": dropped_blank,
        "selected": sum(len(s) for s in selected),
    }
    return selected, stats


# ---------- Markdown ----------

def _md_escape(text: str) -> str:
    return str(text or "").replace("\r", "").strip()


def _anchor(index: int) -> str:
    return f"ch{index:02d}"


def build_markdown(
    video_name: str,
    duration: float,
    meta: dict,
    chapters: list[ChapterOut],
    bookmarks: list[dict],
    bookmark_images: dict[str, str],
    generated_at: str,
) -> str:
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

    lines.append("## 目次")
    lines.append("")
    for ch in chapters:
        lines.append(f"{ch.index}. [{_md_escape(ch.title)}](#{_anchor(ch.index)}) — {fmt_ts(ch.start_sec)}")
    lines.append("")

    for ch in chapters:
        lines.append(f'<a id="{_anchor(ch.index)}"></a>')
        lines.append("")
        lines.append(f"## {ch.index}. {_md_escape(ch.title)}")
        lines.append("")
        lines.append(f"*{fmt_ts(ch.start_sec)} 〜 {fmt_ts(ch.end_sec)}*")
        lines.append("")
        if ch.summary:
            lines.append(_md_escape(ch.summary))
            lines.append("")
        for im in ch.images:
            cap = im.caption or fmt_ts(im.ts)
            lines.append(f"![{_md_escape(cap)}]({im.file})")
            lines.append("")
            lines.append(f"*{_md_escape(cap)}*")
            lines.append("")

    if bookmarks:
        lines.append("## ブックマーク")
        lines.append("")
        for bm in bookmarks:
            ts = float(bm.get("time_sec") or 0.0)
            title = _md_escape(bm.get("title") or "")
            comment = _md_escape(bm.get("comment") or "")
            head = f"### {fmt_ts(ts)}" + (f" {title}" if title else "")
            lines.append(head)
            lines.append("")
            img = bookmark_images.get(str(bm.get("id") or ""))
            if img:
                lines.append(f"![{title or fmt_ts(ts)}]({img})")
                lines.append("")
            if comment:
                lines.append(comment)
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
.range{color:var(--dim);font-size:13px;margin:0 0 8px}p{margin:0 0 10px;white-space:pre-wrap}
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
    chapters = structure.get("chapters") or []
    o.append("<h2>目次</h2><nav><ol>")
    for ch in chapters:
        o.append(f"<li><a href=\"#{_anchor(int(ch['index']))}\">{_h(ch.get('title'))}</a><span class=\"ts\">{fmt_ts(float(ch.get('start_sec') or 0))}</span></li>")
    o.append("</ol></nav>")
    for ch in chapters:
        o.append(f"<h2 id=\"{_anchor(int(ch['index']))}\">{ch['index']}. {_h(ch.get('title'))}</h2>")
        o.append(f"<div class=\"range\">{fmt_ts(float(ch.get('start_sec') or 0))} 〜 {fmt_ts(float(ch.get('end_sec') or 0))}</div>")
        if ch.get("summary"):
            o.append(f"<p>{_h(ch['summary'])}</p>")
        for im in ch.get("images") or []:
            cap = im.get("caption") or fmt_ts(float(im.get("time_sec") or 0))
            o.append(f"<figure><img src=\"{src(im['file'])}\" alt=\"{_h(cap)}\" loading=\"lazy\"><figcaption>{_h(cap)}</figcaption></figure>")
    bms = structure.get("bookmarks") or []
    if bms:
        o.append("<h2>ブックマーク</h2>")
        for bm in bms:
            ts = fmt_ts(float(bm.get("time_sec") or 0))
            head = ts + (f" {_h(bm['title'])}" if bm.get("title") else "")
            o.append(f"<h3>{head}</h3>")
            if bm.get("image"):
                o.append(f"<figure><img src=\"{src(bm['image'])}\" alt=\"{head}\" loading=\"lazy\"></figure>")
            if bm.get("comment"):
                o.append(f"<p>{_h(bm['comment'])}</p>")
    o.append("</main></body></html>")
    return "".join(o)


# ---------- エントリーポイント ----------

def generate_report(
    video_path: str,
    chapters: list[dict],
    meta: dict | None,
    bookmarks: list[dict] | None,
    max_per_chapter: int = DEFAULT_MAX_IMAGES_PER_CHAPTER,
    image_max_side: int = DEFAULT_IMAGE_MAX_SIDE,
    hash_distance: int = DEFAULT_HASH_DISTANCE,
    progress: ProgressFn | None = None,
) -> dict:
    """レポートを生成して {report_path, html_path, dir, chapters, images, stats} を返す。"""
    progress = progress or (lambda _e: None)
    meta = meta or {}
    bookmarks = [b for b in (bookmarks or []) if isinstance(b, dict)]
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
    if not norm:
        norm = [{"start_sec": 0.0, "title": "全体", "summary": str(meta.get("summary") or "")}]
    for i, c in enumerate(norm):
        nxt = norm[i + 1]["start_sec"] if i + 1 < len(norm) else duration
        c["end_sec"] = max(c["start_sec"], nxt if nxt > c["start_sec"] else duration)

    selected, stats = select_images(str(p), norm, duration, max_per_chapter, hash_distance, progress)

    out_dir = report_dir_for(p)
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    # 前回の生成物を消す（章構成が変わって古い画像が残らないように）
    for old in img_dir.glob("*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass

    # 本番解像度で抽出・保存
    jobs: list[tuple[int, int, float]] = [(ci, k, ts) for ci, lst in enumerate(selected) for k, ts in enumerate(lst)]
    bm_jobs: list[tuple[str, float]] = []
    for bm in bookmarks:
        try:
            bm_jobs.append((str(bm.get("id") or f"bm_{len(bm_jobs)}"), float(bm.get("time_sec") or 0.0)))
        except (TypeError, ValueError):
            continue
    total = len(jobs) + len(bm_jobs)
    progress({"status": "extracting", "phase": "images", "current": 0, "total": total})
    done_count = 0
    lock_files: dict[tuple, str] = {}

    def work(job):
        if cancel.is_canceled():
            return None
        kind, key, ts = job
        ts_c = min(max(0.0, ts), max(duration - 0.1, 0.0)) if duration else ts
        img = grab_frame(str(p), ts_c, image_max_side)
        if img is None:
            return None
        if kind == "ch":
            ci, k = key
            name = f"ch{ci+1:02d}_{k+1}_{_file_stamp(ts)}.jpg"
        else:
            name = f"bookmark_{re.sub(r'[^A-Za-z0-9_-]', '', key)}_{_file_stamp(ts)}.jpg"
        img.convert("RGB").save(img_dir / name, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return (kind, key, f"images/{name}")

    all_jobs = [("ch", (ci, k), ts) for ci, k, ts in jobs] + [("bm", bid, ts) for bid, ts in bm_jobs]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(work, all_jobs):
            done_count += 1
            progress({"status": "extracting", "phase": "images", "current": done_count, "total": total})
            if res:
                lock_files[(res[0], res[1])] = res[2]
    cancel.raise_if_canceled()

    chapters_out: list[ChapterOut] = []
    for ci, c in enumerate(norm):
        ch = ChapterOut(index=ci + 1, title=c["title"], start_sec=c["start_sec"], end_sec=c["end_sec"], summary=c["summary"])
        for k, ts in enumerate(selected[ci]):
            f = lock_files.get(("ch", (ci, k)))
            if f:
                ch.images.append(ChapterImage(ts=ts, file=f, caption=fmt_ts(ts)))
        chapters_out.append(ch)
    bookmark_images = {bid: lock_files[("bm", bid)] for bid, _ in bm_jobs if ("bm", bid) in lock_files}

    progress({"status": "writing"})
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = build_markdown(p.stem, duration, meta, chapters_out, bookmarks, bookmark_images, generated_at)
    report_path = out_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")

    structure = {
        "video": p.name,
        "duration": duration,
        "generated_at": generated_at,
        "meta": {k: meta.get(k) for k in ("genre", "summary", "detail", "tags") if meta.get(k)},
        "chapters": [
            {
                "index": ch.index, "title": ch.title, "start_sec": ch.start_sec, "end_sec": ch.end_sec,
                "summary": ch.summary,
                "images": [{"time_sec": im.ts, "file": im.file, "caption": im.caption} for im in ch.images],
            }
            for ch in chapters_out
        ],
        "bookmarks": [
            {**{k: bm.get(k) for k in ("id", "time_sec", "title", "comment")}, "image": bookmark_images.get(str(bm.get("id") or ""))}
            for bm in bookmarks
        ],
        "stats": stats,
        "options": {"max_images_per_chapter": max_per_chapter, "image_max_side": image_max_side, "hash_distance": hash_distance},
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
