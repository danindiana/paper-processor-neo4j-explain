#!/usr/bin/env python3
"""
gen_treemap.py — squarified treemap of the AI-ML_Papers corpus by category,
sized by number of processed papers (metadata.json count per top-level dir).
Emits a neon/black SVG straight from disk — no browser needed.

  python3 gen_treemap.py                 # → corpus_treemap.svg
  python3 gen_treemap.py --top 40        # cap categories, fold the rest into "other"
"""
import argparse, html, os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROCESSED = Path.home() / "Documents" / "AI-ML_Papers" / "_processed"
OUT = HERE / "corpus_treemap.svg"
W, H, PAD_TOP = 1600, 900, 96

# neon palette cycled across tiles
PALETTE = ["#5fffcf", "#7ab8ff", "#ff7ad9", "#ffd45f", "#9affae",
           "#ffa45f", "#c0a0ff", "#ff6b6b", "#7aa0c0", "#5fd0ff"]


def scan_categories():
    cats = []
    if PROCESSED.exists():
        for d in sorted(PROCESSED.iterdir()):
            if d.is_dir():
                n = sum(1 for _ in d.rglob("metadata.json"))
                if n:
                    cats.append((d.name, n))
    cats.sort(key=lambda c: -c[1])
    return cats


# ── squarified treemap (Bruls, Huizing, van Wijk) — canonical implementation ──
# Areas are pre-normalized to the container area; labels ride along each value.
def _layoutrow(areas, x, y, dx, dy):
    cover = sum(a for a, _ in areas)
    width = cover / dy
    rects, yy = [], y
    for a, lab in areas:
        rects.append((x, yy, width, a / width, lab))
        yy += a / width
    return rects, (x + width, y, dx - width, dy)


def _layoutcol(areas, x, y, dx, dy):
    cover = sum(a for a, _ in areas)
    height = cover / dx
    rects, xx = [], x
    for a, lab in areas:
        rects.append((xx, y, a / height, height, lab))
        xx += a / height
    return rects, (x, y + height, dx, dy - height)


def _layout(areas, x, y, dx, dy):
    return _layoutrow(areas, x, y, dx, dy) if dx >= dy else _layoutcol(areas, x, y, dx, dy)


def _worst(areas, x, y, dx, dy):
    rects, _ = _layout(areas, x, y, dx, dy)
    return max(max(w / h, h / w) for (_, _, w, h, _) in rects if w > 0 and h > 0)


def squarify(values, x, y, dx, dy):
    """values: list of (size, label), descending. Returns (x,y,w,h,label,size)."""
    total = sum(v for v, _ in values) or 1
    scale = (dx * dy) / total
    areas = [(v * scale, (label, v)) for v, label in values]
    out = []
    _squarify(areas, x, y, dx, dy, out)
    return [(rx, ry, rw, rh, lab[0], lab[1]) for (rx, ry, rw, rh, lab) in out]


def _squarify(areas, x, y, dx, dy, out):
    if not areas:
        return
    if len(areas) == 1:
        rects, _ = _layout(areas, x, y, dx, dy)
        out.extend(rects)
        return
    i = 1
    while i < len(areas) and _worst(areas[:i], x, y, dx, dy) >= _worst(areas[:i + 1], x, y, dx, dy):
        i += 1
    current, remaining = areas[:i], areas[i:]
    rects, (nx, ny, ndx, ndy) = _layout(current, x, y, dx, dy)
    out.extend(rects)
    _squarify(remaining, nx, ny, ndx, ndy, out)


def render_svg(cats, top):
    total_papers = sum(n for _, n in cats)
    if top and len(cats) > top:
        head = cats[:top]
        other = sum(n for _, n in cats[top:])
        cats = head + [("other (%d cats)" % (len(cats) - top), other)]
    rects = squarify([(n, name) for name, n in cats], 0, PAD_TOP, W, H - PAD_TOP)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="ui-monospace,Menlo,monospace">',
        f'<rect width="{W}" height="{H}" fill="#06090f"/>',
        f'<text x="20" y="34" fill="#5fffcf" font-size="22" font-weight="700">'
        f'🦞 AI-ML_Papers corpus — {len(cats)} categories · {total_papers} processed papers</text>',
        f'<text x="20" y="60" fill="#7aa" font-size="13">tile area ∝ processed-paper count · '
        f'top categories labelled · generated from _processed/*/metadata.json</text>',
        f'<line x1="0" y1="{PAD_TOP-8}" x2="{W}" y2="{PAD_TOP-8}" stroke="#163"/>',
    ]
    for i, (x, y, w, h, label, v) in enumerate(rects):
        c = PALETTE[i % len(PALETTE)]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(0,w-2):.1f}" height="{max(0,h-2):.1f}" '
            f'rx="4" fill="#0d141d" stroke="{c}" stroke-width="1.5"/>')
        if w > 58 and h > 26:
            fs = 11 if w < 130 else 13
            parts.append(
                f'<text x="{x+7:.1f}" y="{y+18:.1f}" fill="{c}" font-size="{fs}">'
                f'{html.escape(label)}</text>')
            if h > 42:
                parts.append(
                    f'<text x="{x+7:.1f}" y="{y+34:.1f}" fill="#9ab" font-size="11">{v}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=45, help="max labelled categories")
    a = ap.parse_args()
    cats = scan_categories()
    OUT.write_text(render_svg(cats, a.top))
    print(f"wrote {OUT.name} · {len(cats)} categories · {sum(n for _,n in cats)} papers")
