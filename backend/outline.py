"""動画の種類（プリセット）と、字幕主導の「話題アウトライン」生成。

動画分析（/review/analyze）とレポート（/report/generate）の両方が使う共通モジュール。

- 動画分析のチャプターは従来、サンプリングしたフレームを VL モデルに見せて
  「絵が変わったところ」を scenes として返させていた（映像主導）。
  プレゼン・対談のように絵より話の切れ目で章を分けたい動画では細切れ・重複になりやすい。
- ここでは動画の種類ごとに「章分けの基準（話題／場面／両方）」「粒度」「章名の付け方」を
  プリセットとして持ち、話題主導のときは字幕全文（等間隔サンプル）をテキストのみの推論に渡して
  時間範囲付きのアウトラインを 1 回で作らせる（フレーム不要・数秒）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ---------- 動画の種類 ----------

BASIS_TOPIC = "topic"     # 字幕主導（話題の切れ目）
BASIS_VISUAL = "visual"   # 映像主導（従来の scenes）
BASIS_BOTH = "both"       # 字幕主導＋場面変化を境界のヒントに


@dataclass(frozen=True)
class VideoKind:
    id: str
    label: str
    basis: str
    target_sec: float      # 1 章あたりの目安の長さ（章数 = duration / target_sec）
    min_chapters: int
    max_chapters: int
    min_span_sec: float    # これより短い章は前後に統合
    naming: str            # 章名の付け方の指示
    focus: str             # まとめで重視する内容


VIDEO_KINDS: dict[str, VideoKind] = {
    "auto": VideoKind(
        "auto", "自動", BASIS_TOPIC, 120.0, 4, 15, 45.0,
        "章名はその区間で扱われている話題を表す名詞句にする",
        "話者の主張・説明の内容",
    ),
    "presentation": VideoKind(
        "presentation", "プレゼン・講演・解説", BASIS_BOTH, 120.0, 4, 18, 45.0,
        "章名はその区間で扱われている話題（発表内容・製品・テーマ）を表す名詞句にする。"
        "「登壇」「スライド」「画面」のような見た目の描写は使わない",
        "発表内容・主張・数値・固有名詞",
    ),
    "talk": VideoKind(
        "talk", "対談・インタビュー・配信", BASIS_TOPIC, 300.0, 3, 12, 90.0,
        "章名はその区間の話題（質問・テーマ）を表す名詞句にする。映像の描写は使わない",
        "誰が何を主張したか・質問と回答",
    ),
    "tutorial": VideoKind(
        "tutorial", "チュートリアル・画面操作", BASIS_BOTH, 120.0, 4, 20, 45.0,
        "章名は手順を表す「〜する」「〜の設定」の形にする",
        "操作手順・設定値・注意点",
    ),
    "footage": VideoKind(
        "footage", "映像作品・Vlog・スポーツ", BASIS_VISUAL, 120.0, 4, 20, 30.0,
        "章名は場面の内容を表す短いタイトルにする",
        "何が起きるか・場所・登場人物",
    ),
}
DEFAULT_KIND = "auto"


def kind_options() -> list[dict]:
    return [{"id": k.id, "label": k.label} for k in VIDEO_KINDS.values()]


def resolve_kind(kind_id: str | None, transcript: str) -> VideoKind:
    """auto は字幕の有無で決める（字幕なし → 映像主導、あり → 話題主導＋場面ヒント）。"""
    kind = VIDEO_KINDS.get(kind_id or DEFAULT_KIND) or VIDEO_KINDS[DEFAULT_KIND]
    if kind.id != "auto":
        return kind
    if not _has_timestamped_rows(transcript):
        return VIDEO_KINDS["footage"]
    return VIDEO_KINDS["presentation"]


def chapter_target(kind: VideoKind, duration: float) -> int:
    n = int(round(max(1.0, duration) / kind.target_sec))
    return max(kind.min_chapters, min(kind.max_chapters, n))


# ---------- 字幕の扱い ----------

_ROW_RE = re.compile(r"^\[(?:(\d+):)?(\d+):(\d{2})\]\s*(.*)$")  # [m:ss] の分は 100 以上もあり得る（フロントは分を桁数制限なしで出す）


def parse_rows(transcript: str) -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []
    for line in (transcript or "").splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        h = int(m.group(1) or 0)
        sec = h * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        rows.append((float(sec), m.group(4).strip()))
    return rows


def rows_to_text(rows: list[tuple[float, str]]) -> str:
    return "\n".join(f"[{fmt_ts(t)}] {txt}" for t, txt in rows if txt)


def _has_timestamped_rows(transcript: str) -> bool:
    return any(_ROW_RE.match(l.strip()) for l in (transcript or "").splitlines()[:50])


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip().strip("[]")
    m = re.match(r"^(?:(\d+):)?(\d+):(\d{2})(?:\.\d+)?$", s)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def sample_rows(rows: list[tuple[float, str]], max_chars: int) -> list[tuple[float, str]]:
    """全編を等間隔に間引いて max_chars に収める（先頭切り捨てにしない）。"""
    total = sum(len(t) + 9 for _, t in rows)
    if total <= max_chars or not rows:
        return rows
    keep = max(2, int(len(rows) * max_chars / total))
    step = (len(rows) - 1) / (keep - 1)
    idx = sorted({min(round(i * step), len(rows) - 1) for i in range(keep)})
    return [rows[i] for i in idx]


# ---------- アウトライン生成 ----------

OUTLINE_SYSTEM = (
    "/no_think\n"
    "あなたは動画の構成を整理する編集者です。"
    "時刻付きの字幕から、視聴者が目次として使える章立てを作ります。"
    "章は話題の切れ目で分け、見た目の変化ではなく話の内容で判断してください。"
)

_OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["start", "title", "summary"],
            },
        }
    },
    "required": ["chapters"],
}

OUTLINE_TRANSCRIPT_MAX_CHARS = 9000   # フレーム無しなので分析（3000）より多く渡せる（ctx 16k 前提）


def build_outline_prompt(rows: list[tuple[float, str]], duration: float, kind: VideoKind, scene_hints: list[float] | None, output_lang: str = "ja") -> str:
    target = chapter_target(kind, duration)
    lang = "日本語" if output_lang == "ja" else "英語"
    parts = [f"[字幕]（動画の長さ {fmt_ts(duration)}）\n{rows_to_text(sample_rows(rows, OUTLINE_TRANSCRIPT_MAX_CHARS))}"]
    if scene_hints:
        hints = "、".join(fmt_ts(t) for t in scene_hints[:80])
        parts.append(f"[映像が切り替わった時刻（境界の参考。必ず使う必要はない）]\n{hints}")
    parts.append(
        "字幕全体を話題ごとの章に分けて、次の JSON のみを出力してください。\n"
        f"- 章数の目安は {target} 章（±2）。1 章は最短でも {int(kind.min_span_sec)} 秒以上\n"
        "- start は章が始まる字幕の時刻（[m:ss] または [h:mm:ss]）をそのまま使う。最初の章は動画の先頭から\n"
        f"- title: {kind.naming}。{lang}で 25 文字以内\n"
        f"- summary: その章で語られる内容を 1〜2 文で。{kind.focus}を具体的に。{lang}で書く\n"
        "- 同じ話題が続く間は章を分けない。挨拶・雑談だけの短い区間は前後の章に含める"
    )
    return "\n\n".join(parts)


def normalize_chapters(raw: list, duration: float, kind: VideoKind) -> list[dict]:
    """モデル出力を [{start_sec, end_sec, title, summary, timestamp}] に整える。
    時刻順・重複除去・最短長未満の章は前の章へ統合・先頭は 0 秒から。"""
    rows: list[dict] = []
    for i, ch in enumerate(raw or []):
        if not isinstance(ch, dict):
            continue
        t = parse_ts(ch.get("start"))
        if t is None:
            continue
        t = max(0.0, min(float(duration or t), float(t)))
        rows.append({"start_sec": t, "title": str(ch.get("title") or f"チャプター{i+1}").strip(), "summary": str(ch.get("summary") or "").strip()})
    rows.sort(key=lambda r: r["start_sec"])
    if not rows:
        return []
    rows[0]["start_sec"] = 0.0
    merged: list[dict] = []
    for r in rows:
        if merged and r["start_sec"] - merged[-1]["start_sec"] < kind.min_span_sec:
            # 短すぎる章は前の章に吸収（説明が空なら後ろのものを使う）
            if not merged[-1]["summary"]:
                merged[-1]["summary"] = r["summary"]
            continue
        merged.append(r)
    # 最後の章が短すぎる場合も前に吸収
    if len(merged) >= 2 and float(duration) - merged[-1]["start_sec"] < kind.min_span_sec:
        merged.pop()
    out: list[dict] = []
    for i, r in enumerate(merged):
        end = merged[i + 1]["start_sec"] if i + 1 < len(merged) else float(duration)
        out.append({
            "id": f"ch{i+1:03d}",
            "start_sec": round(r["start_sec"], 3),
            "end_sec": round(max(r["start_sec"], end), 3),
            "title": r["title"],
            "summary": r["summary"],
            "timestamp": fmt_ts(r["start_sec"]),
            "confidence": 0.8,
            "basis": "topic",
        })
    return out


def generate_outline(reviewer, transcript: str, duration: float, kind: VideoKind, scene_hints: list[float] | None = None, output_lang: str = "ja") -> list[dict]:
    """字幕から章立てを生成する。字幕が無い・失敗したときは [] を返す（呼び出し側で映像主導にフォールバック）。

    reviewer は VideoReviewer（ロード済み）。テキストのみの推論なのでフレームは不要。
    """
    rows = parse_rows(transcript)
    if len(rows) < 3:
        return []
    prompt = build_outline_prompt(rows, duration, kind, scene_hints, output_lang)
    raw, _meta = reviewer.text_infer(
        OUTLINE_SYSTEM, prompt, max_new_tokens=1536,
        response_format={"type": "json_schema", "json_schema": {"name": "video_outline", "schema": _OUTLINE_SCHEMA}},
    )
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    return normalize_chapters(data.get("chapters") if isinstance(data, dict) else [], duration, kind)
