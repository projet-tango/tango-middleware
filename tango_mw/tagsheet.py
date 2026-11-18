"""Printable AprilTags: vector SVG at real size, A4 print sheets, ZIP export.

Sizes are the side of the black square in millimetres (what you measure on the floor).
Each tag keeps a white margin of one bit around it (plus 2 mm to cut): the detector needs that white frame.
"""
from __future__ import annotations

import html
import io
import zipfile
from typing import Dict, Iterable, List, Optional

import cv2

from .vision.detector import render_tag
from .vision.quality import MODULES, tag_pattern


def tag_svg(family: str, tag_id: int, size_mm: float, caption: Optional[str] = None, front: bool = False) -> str:
    """One tag as a standalone SVG whose width/height are in millimetres."""
    n = MODULES.get(family, 8)
    bits = tag_pattern(family, tag_id, n)
    m = size_mm / n                    # one bit, in mm
    pad = m + 2.0                      # white margin: one bit (the detector needs it) + 2 mm to cut
    top = 7.0 if front else 0.0        # room for the "front" arrow
    bottom = 6.0 if caption else 0.0
    w, h = size_mm + 2 * pad, size_mm + 2 * pad + top + bottom
    rects = []
    for r in range(n):
        for c in range(n):
            if not bits[r, c]:
                rects.append(f'<rect x="{pad + c * m:.3f}" y="{top + pad + r * m:.3f}" width="{m + 0.01:.3f}" height="{m + 0.01:.3f}"/>')
    extra = []
    if front:
        cx = w / 2
        extra.append(f'<path d="M{cx - 4:.2f},{5.5:.2f} L{cx:.2f},{1:.2f} L{cx + 4:.2f},{5.5:.2f} Z" fill="#000"/>')
        extra.append(f'<text x="{cx + 6:.2f}" y="5" font-size="3.4" font-family="sans-serif">FRONT</text>')
    if caption:
        extra.append(f'<text x="{w / 2:.2f}" y="{h - 1.8:.2f}" font-size="3.2" text-anchor="middle" '
                     f'font-family="sans-serif" fill="#444">{html.escape(caption)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" viewBox="0 0 {w:.3f} {h:.3f}">'
            f'<rect width="100%" height="100%" fill="#fff"/>'
            f'<rect x="0.1" y="{top + 0.1:.2f}" width="{w - 0.2:.2f}" height="{w - 0.2:.2f}" fill="none" stroke="#bbb" '
            f'stroke-width="0.2" stroke-dasharray="1.5 1.5"/>'  # cut line
            f'<g fill="#000" shape-rendering="crispEdges">{"".join(rects)}</g>{"".join(extra)}</svg>')


def tag_png(family: str, tag_id: int, px: int = 800) -> bytes:
    img = render_tag(family, tag_id, px, quiet_zone=0.25)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def print_page(family: str, items: List[Dict], title: str = "Tango tags") -> str:
    """A4 sheet(s). items: {"id", "size_mm", "caption", "front"}. Print at 100 % ("actual size")."""
    cells = "".join(
        f'<div class="cell">{tag_svg(family, it["id"], it["size_mm"], it.get("caption"), it.get("front", False))}</div>'
        for it in items)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  @page {{ size: A4; margin: 10mm; }}
  body {{ margin: 0; font: 13px system-ui, sans-serif; color: #222; }}
  .help {{ background: #e3edff; border: 1px solid #b6cbf5; padding: 8px 12px; margin: 10px; border-radius: 6px; }}
  .sheet {{ display: flex; flex-wrap: wrap; gap: 4mm; align-content: flex-start; }}
  .cell {{ break-inside: avoid; page-break-inside: avoid; }}
  .cell svg {{ display: block; }}
  .ruler {{ margin-top: 6mm; break-inside: avoid; }}
  @media print {{ .help {{ display: none; }} }}
</style></head><body>
<div class="help"><b>Print at 100 % ("Actual size", no "Fit to page")</b>, on matte paper if you can.
Then measure the ruler below: it must be exactly 100 mm. Cut along the dashed lines, keeping the white margin.
Robot tags: stick them flat, the FRONT arrow towards the front of the robot.
<button onclick="print()">Print</button></div>
<div class="sheet">{cells}</div>
<div class="ruler"><svg width="110mm" height="12mm" viewBox="0 0 110 12">
  <line x1="5" y1="6" x2="105" y2="6" stroke="#000" stroke-width="0.4"/>
  {"".join(f'<line x1="{5 + i}" y1="{2 if i % 10 == 0 else 4}" x2="{5 + i}" y2="6" stroke="#000" stroke-width="0.2"/>' for i in range(0, 101, 5))}
  <text x="55" y="11" font-size="3" text-anchor="middle" font-family="sans-serif">100 mm — check with a ruler</text>
</svg></div>
</body></html>"""


def archive(family: str, items: Iterable[Dict]) -> bytes:
    """ZIP with an SVG (real size) and a PNG of every tag."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for it in items:
            base = f"{family}_{it['id']}"
            z.writestr(f"svg/{base}_{it['size_mm']:g}mm.svg", tag_svg(family, it["id"], it["size_mm"], it.get("caption"), it.get("front", False)))
            z.writestr(f"png/{base}.png", tag_png(family, it["id"]))
    return buf.getvalue()


def items_for(cfg, which: str, ids: Optional[List[int]] = None, size_mm: Optional[float] = None,
              robot_size_mm: Optional[float] = None) -> List[Dict]:
    """Tags of the configuration: which = world | robots | all | custom (with ids)."""
    world_mm = size_mm or cfg.world.tag_size * 1000
    robot_mm = robot_size_mm or size_mm or 60.0
    items: List[Dict] = []
    if which in ("world", "all"):
        for t in cfg.world.tags:
            items.append({"id": t.id, "size_mm": world_mm, "caption": f"#{t.id} · boundary at ({t.x:g}, {t.y:g}) m"})
    if which in ("robots", "all"):
        for r in cfg.robots:
            if r.tag_id >= 0:
                items.append({"id": r.tag_id, "size_mm": robot_mm, "caption": f"#{r.tag_id} · {r.name}", "front": True})
    if which == "custom":
        for i in ids or []:
            items.append({"id": int(i), "size_mm": world_mm, "caption": f"#{int(i)}"})
    return items
