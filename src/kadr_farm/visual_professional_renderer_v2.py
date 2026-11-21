"""Browser-based HTML/CSS/SVG scene compositor for KadrFarm Visual Story V2.

This renderer replaces the experimental full-canvas-PNG-per-object approach for
professional output. A shot is one layered HTML composition: stills and video
remain native media elements; typography, charts and diagrams are vector/HTML.
Playwright records the composed scene and FFmpeg normalizes it to H.264.
"""
from __future__ import annotations

import asyncio
import html
import json
import math
import mimetypes
import os
import re
import subprocess
import time
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .visual_professional_assets_v2 import ResolvedMedia
from .visual_story_v2 import Shot, VisualEvent, VisualObject, VisualStoryPlan


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SAFE_FONT = re.compile(r"^[A-Za-z0-9 ,.'_-]{1,100}$")
_EASINGS = {
    "linear": "linear",
    "ease": "ease",
    "ease-in": "ease-in",
    "ease-out": "ease-out",
    "ease-in-out": "ease-in-out",
    "snappy": "cubic-bezier(.2,.8,.2,1)",
    "cinematic": "cubic-bezier(.22,.61,.36,1)",
    "spring": "cubic-bezier(.175,.885,.32,1.275)",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if not math.isfinite(result):
        result = default
    return max(low, min(high, result))


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


def _color(value: Any, default: str) -> str:
    candidate = str(value or default).strip()
    return candidate if _HEX.fullmatch(candidate) else default


def _font(value: Any, default: str) -> str:
    candidate = str(value or default).strip()
    return candidate if _SAFE_FONT.fullmatch(candidate) else default


def _uri(path: Path) -> str:
    return _esc(path.resolve().as_uri())


def _palette(plan: VisualStoryPlan) -> dict[str, str]:
    style = dict(plan.style or {})
    palette = style.get("palette") or []
    defaults = ["#F3F5F7", "#AAB5C4", "#5E7CE2", "#18A999", "#F1A35B"]
    values = [_color(v, defaults[i % len(defaults)]) for i, v in enumerate(palette[:8])]
    while len(values) < 5:
        values.append(defaults[len(values)])
    return {
        "bg": _color(style.get("background"), _color(plan.background, "#101827")),
        "fg": _color(style.get("foreground"), "#F5F7FA"),
        "muted": _color(style.get("muted"), "#AAB5C4"),
        "accent": _color(style.get("accent"), values[2]),
        "accent2": _color(style.get("accent_secondary"), values[3]),
        "surface": _color(style.get("surface"), "#1C2736"),
        "surface2": _color(style.get("surface_secondary"), "#263548"),
    }


def _svg_shape(obj: VisualObject, accent: str) -> str:
    c = obj.content
    a = obj.appearance
    kind = str(c.get("shape_type", "rectangle"))
    fill = _color(a.get("fill"), "#263548")
    stroke = _color(a.get("stroke"), accent)
    sw = _number(a.get("stroke_width"), 4, 0, 30)
    common = f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" vector-effect="non-scaling-stroke"'
    if kind == "rectangle":
        shape = f'<rect x="3" y="3" width="994" height="494" rx="0" {common}/>'
    elif kind == "rounded_rectangle":
        radius = _number(c.get("radius"), 28, 0, 120)
        shape = f'<rect x="3" y="3" width="994" height="494" rx="{radius}" {common}/>'
    elif kind in {"circle", "ellipse"}:
        shape = f'<ellipse cx="500" cy="250" rx="470" ry="220" {common}/>'
    elif kind in {"line", "arrow"}:
        start = c.get("start", [0.04, 0.5])
        end = c.get("end", [0.96, 0.5])
        x1 = _number(start[0], 0.04, 0, 1) * 1000
        y1 = _number(start[1], 0.5, 0, 1) * 500
        x2 = _number(end[0], 0.96, 0, 1) * 1000
        y2 = _number(end[1], 0.5, 0, 1) * 500
        marker_id = "arrowhead_" + re.sub(r"[^A-Za-z0-9_-]", "_", obj.object_id)
        marker = f' marker-end="url(#{marker_id})"' if kind == "arrow" else ""
        shape = (
            f'<defs><marker id="{marker_id}" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto">'
            f'<path d="M0 0 L8 4 L0 8" fill="none" stroke="{stroke}" stroke-width="1.5"/></marker></defs>'
            f'<path class="draw-path" d="M{x1:.2f},{y1:.2f} L{x2:.2f},{y2:.2f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round"{marker}/>'
        )
    elif kind == "polygon":
        points = c.get("points") or []
        converted = []
        for point in points[:64]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue
            px = _number(point[0], 0.5, 0, 1) * 1000
            py = _number(point[1], 0.5, 0, 1) * 500
            converted.append(f"{px:.2f},{py:.2f}")
        shape = f'<polygon points="{" ".join(converted)}" {common}/>' if len(converted) >= 3 else ""
    else:
        shape = ""
    return f'<svg class="shape-svg" viewBox="0 0 1000 500" preserveAspectRatio="none" aria-hidden="true">{shape}</svg>'


def _text_html(obj: VisualObject, palette: dict[str, str], style: dict[str, Any]) -> str:
    c, a = obj.content, obj.appearance
    raw = str(c.get("text", ""))
    eyebrow = str(c.get("eyebrow", ""))
    subtitle = str(c.get("subtitle", ""))
    size = _int(c.get("font_size"), 64, 12, 320)
    weight = _int(c.get("font_weight"), 750, 100, 950)
    color = _color(a.get("color"), palette["fg"])
    align = c.get("alignment", "left")
    if align not in {"left", "center", "right", "justify"}:
        align = "left"
    valign = c.get("vertical_align", "center")
    valign = {"top": "flex-start", "center": "center", "bottom": "flex-end"}.get(valign, "center")
    family = _font(c.get("font_family"), _font(style.get("font_display"), "Arial, Segoe UI"))
    body_family = _font(style.get("font_body"), "Arial, Segoe UI")
    line_height = _number(c.get("line_height"), 1.02, 0.8, 1.8)
    letter_spacing = _number(c.get("letter_spacing"), -1.2, -8, 24)
    transform = "uppercase" if c.get("uppercase") else "none"
    text = _esc(raw)
    # Explicit line breaks are retained; ordinary text wraps naturally.
    text = text.replace("\n", "<br>")
    eyebrow_html = f'<div class="type-eyebrow">{_esc(eyebrow)}</div>' if eyebrow else ""
    subtitle_html = f'<div class="type-subtitle">{_esc(subtitle)}</div>' if subtitle else ""
    background = _color(a.get("background"), "#000000") if a.get("background") else "transparent"
    radius = _int(a.get("border_radius"), 0, 0, 100)
    padding = _int(c.get("padding"), 0, 0, 120)
    italic = "italic" if c.get("italic") else "normal"
    shadow = "0 14px 48px rgba(0,0,0,.28)" if a.get("shadow") else "none"
    return (
        f'<div class="type-layout" style="--type-color:{color};--type-bg:{background};'
        f'--type-radius:{radius}px;--type-padding:{padding}px;--type-align:{align};'
        f'--type-valign:{valign};--type-font:{_esc(family)};--type-size:{size}px;'
        f'--type-weight:{weight};--type-line:{line_height};--type-track:{letter_spacing}px;'
        f'--type-transform:{transform};--type-italic:{italic};--type-shadow:{shadow};'
        f'--body-font:{_esc(body_family)}">{eyebrow_html}'
        f'<div class="type-main">{text}</div>{subtitle_html}</div>'
    )


def _media_html(obj: VisualObject, media: ResolvedMedia, palette: dict[str, str]) -> str:
    c, a = obj.content, obj.appearance
    focal = c.get("focal_point", [0.5, 0.5])
    if not isinstance(focal, (list, tuple)) or len(focal) != 2:
        focal = [0.5, 0.5]
    fx = _number(focal[0], 0.5, 0, 1) * 100
    fy = _number(focal[1], 0.5, 0, 1) * 100
    radius = _int(a.get("border_radius"), 0, 0, 120)
    fit = {"contain": "contain", "cover": "cover", "stretch": "fill"}.get(obj.layer.fit, "cover")
    shadow = "media-shadow" if a.get("shadow", True) else ""
    motion = str(c.get("camera_motion", "static"))
    motion_class = {
        "slow_zoom": "camera-slow-zoom",
        "zoom_in": "camera-slow-zoom",
        "zoom_out": "camera-zoom-out",
        "pan_left": "camera-pan-left",
        "pan_right": "camera-pan-right",
    }.get(motion, "")
    uri = media.as_uri()
    if media.media_type == "video":
        poster = c.get("poster")
        poster_attr = f' poster="{_esc(Path(str(poster)).resolve().as_uri())}"' if poster else ""
        loop = "loop" if c.get("loop_allowed", True) else ""
        controls = "controls" if c.get("controls") else ""
        element = (
            f'<video class="media-element {shadow} {motion_class}" src="{uri}" muted playsinline {loop} '
            f'{controls}{poster_attr} preload="auto" style="object-fit:{fit};object-position:{fx:.1f}% {fy:.1f}%"></video>'
        )
    else:
        alt = _esc(c.get("alt", obj.object_id))
        element = (
            f'<img class="media-element {shadow} {motion_class}" src="{uri}" alt="{alt}" '
            f'style="object-fit:{fit};object-position:{fx:.1f}% {fy:.1f}%" loading="eager">'
        )
    overlay = "media-overlay" if c.get("gradient_overlay", False) else ""
    return (
        f'<div class="media-frame" style="--media-radius:{radius}px">{element}'
        f'<div class="{overlay}"></div></div>'
    )


def _table_html(obj: VisualObject, palette: dict[str, str]) -> str:
    c = obj.content
    cols = c.get("columns") or []
    rows = c.get("rows") or []
    header = "".join(f'<th>{_esc(col.get("title", ""))}</th>' for col in cols)
    body_rows = []
    for index, row in enumerate(rows):
        cells = row.get("cells", []) if isinstance(row, dict) else row
        body_rows.append(
            f'<tr data-row-index="{index}">' + "".join(
                f'<td>{_esc(value)}</td>' for value in cells[:len(cols)]
            ) + "</tr>"
        )
    return (
        '<div class="table-shell"><table class="editorial-table">'
        f'<thead><tr>{header}</tr></thead><tbody>{"".join(body_rows)}</tbody>'
        '</table></div>'
    )


def _chart_html(obj: VisualObject, palette: dict[str, str]) -> str:
    """Create a labelled, responsive SVG chart with grid and data labels."""
    c = obj.content
    chart_type = c.get("chart_type", "bar")
    series = c.get("series") or []
    if not series:
        return '<div class="engine-error">Chart has no series</div>'
    colors = [_color(v, palette["accent"]) for v in (c.get("colors") or [])]
    if not colors:
        colors = [palette["accent"], palette["accent2"], "#B58AE8", "#E88B66"]
    values = []
    for item in series:
        values.extend([_number(v, 0, -1e12, 1e12) for v in item.get("values", [])])
    if not values:
        return '<div class="engine-error">Chart series has no values</div>'
    labels = c.get("categories") or c.get("labels") or []
    title = _esc(c.get("title", ""))
    subtitle = _esc(c.get("subtitle", ""))
    unit = _esc(c.get("unit", c.get("value_suffix", "")))
    mode = c.get("data_mode", "provided")
    width, height = 1000, 520
    left, right, top, bottom = 92, 35, 56, 88
    plot_w, plot_h = width - left - right, height - top - bottom
    min_v = min(0.0, min(values))
    max_v = max(1.0, max(values))
    span = max(1e-9, max_v - min_v)
    grid_parts = []
    for i in range(5):
        val = min_v + span * i / 4
        y = top + plot_h - plot_h * i / 4
        grid_parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="chart-grid"/>'
            f'<text x="{left-16}" y="{y+6:.1f}" class="chart-axis-label" text-anchor="end">{val:g}{unit}</text>'
        )
    marks: list[str] = []
    if chart_type == "bar":
        n = max(len(item.get("values", [])) for item in series)
        series_count = len(series)
        group_w = plot_w / max(1, n)
        bar_w = group_w * 0.68 / max(1, series_count)
        for si, item in enumerate(series):
            for i, raw in enumerate(item.get("values", [])):
                value = float(raw)
                x = left + i * group_w + group_w * 0.16 + si * bar_w
                y0 = top + plot_h - (0 - min_v) / span * plot_h
                yv = top + plot_h - (value - min_v) / span * plot_h
                y, h = min(y0, yv), max(2.0, abs(y0-yv))
                marks.append(
                    f'<rect class="chart-bar draw-mark" x="{x:.1f}" y="{y:.1f}" width="{bar_w*0.88:.1f}" height="{h:.1f}" '
                    f'rx="8" fill="{colors[si % len(colors)]}" style="--mark-delay:{(i*0.08+si*0.05):.2f}s"/>'
                )
        for i in range(n):
            label = _esc(labels[i] if i < len(labels) else str(i+1))
            x = left + (i+0.5) * group_w
            marks.append(f'<text x="{x:.1f}" y="{height-42}" class="chart-axis-label" text-anchor="middle">{label}</text>')
    elif chart_type in {"line", "area"}:
        for si, item in enumerate(series):
            arr = item.get("values", [])
            if len(arr) < 2:
                continue
            pts = []
            for i, raw in enumerate(arr):
                x = left + (plot_w * i / max(1, len(arr)-1))
                y = top + plot_h - (float(raw)-min_v) / span * plot_h
                pts.append((x,y))
            path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x,y in pts)
            color = colors[si % len(colors)]
            if chart_type == "area":
                area_path = path + f" L{pts[-1][0]:.1f},{top+plot_h:.1f} L{pts[0][0]:.1f},{top+plot_h:.1f} Z"
                marks.append(f'<path d="{area_path}" fill="{color}" opacity=".16"/>')
            marks.append(f'<path class="chart-line draw-path" d="{path}" fill="none" stroke="{color}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" pathLength="1000"/>')
            for x,y in pts:
                marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{palette["bg"]}" stroke="{color}" stroke-width="5"/>')
        for i in range(max(len(item.get("values", [])) for item in series)):
            label = _esc(labels[i] if i < len(labels) else str(i+1))
            x = left + (plot_w * i / max(1, max(len(item.get("values", [])) for item in series)-1))
            marks.append(f'<text x="{x:.1f}" y="{height-42}" class="chart-axis-label" text-anchor="middle">{label}</text>')
    elif chart_type in {"pie", "donut"}:
        first = [max(0.0, float(v)) for v in series[0].get("values", [])]
        total = sum(first)
        if total <= 0:
            return '<div class="engine-error">Pie chart values must sum to a positive number</div>'
        cx, cy, radius = width/2, height/2, min(plot_h, plot_w)*0.34
        angle = -math.pi/2
        for i, value in enumerate(first):
            next_angle = angle + 2*math.pi*value/total
            x1, y1 = cx+radius*math.cos(angle), cy+radius*math.sin(angle)
            x2, y2 = cx+radius*math.cos(next_angle), cy+radius*math.sin(next_angle)
            large = 1 if next_angle-angle > math.pi else 0
            d = f"M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{radius:.1f},{radius:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z"
            marks.append(f'<path class="chart-slice draw-path" pathLength="1000" d="{d}" fill="{colors[i % len(colors)]}" stroke="{palette["bg"]}" stroke-width="5"/>')
            angle = next_angle
        if chart_type == "donut":
            marks.append(f'<circle cx="{cx}" cy="{cy}" r="{radius*.57:.1f}" fill="{palette["bg"]}"/>')
            marks.append(f'<text x="{cx}" y="{cy+12}" class="donut-total" text-anchor="middle">{sum(first):g}</text>')
    else:
        return f'<div class="engine-error">Unsupported chart type: {_esc(chart_type)}</div>'
    legend = ""
    if chart_type in {"pie", "donut"}:
        category_items = []
        for i, label in enumerate(labels[:len(series[0].get("values", []))]):
            category_items.append(
                f'<span class="legend-item"><i style="background:{colors[i%len(colors)]}"></i>{_esc(label)}</span>'
            )
        legend = f'<div class="chart-legend chart-category-legend">{"".join(category_items)}</div>'
    elif len(series) > 1 or c.get("show_legend"):
        items = []
        for i, item in enumerate(series):
            name = _esc(item.get("name", f"Ряд {i+1}"))
            items.append(f'<span class="legend-item"><i style="background:{colors[i%len(colors)]}"></i>{name}</span>')
        legend = f'<div class="chart-legend">{"".join(items)}</div>'
    disclosure = (
        '<div class="chart-disclosure">ИЛЛЮСТРАТИВНЫЕ ДАННЫЕ</div>'
        if mode == "illustrative" else ""
    )
    header = f'<div class="chart-heading"><div><div class="chart-title">{title}</div><div class="chart-subtitle">{subtitle}</div></div>{legend}</div>' if title or subtitle or legend else ""
    grid = "".join(grid_parts) if chart_type in {"bar", "line", "area"} and c.get("show_grid", True) else ""
    return (
        f'<div class="chart-card">{header}<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img">'
        f'<g>{grid}</g><g class="chart-marks">{"".join(marks)}</g></svg>{disclosure}</div>'
    )


def _graph_html(obj: VisualObject, palette: dict[str, str]) -> str:
    c = obj.content
    nodes = c.get("nodes") or []
    edges = c.get("edges") or []
    if not nodes:
        return '<div class="engine-error">Graph has no nodes</div>'
    positions: dict[str, tuple[float,float]] = {}
    layout = c.get("layout", "left_to_right")
    for i, node in enumerate(nodes):
        if "x" in node and "y" in node:
            px = _number(node.get("x"), .5, 0, 1) * 1000
            py = _number(node.get("y"), .5, 0, 1) * 500
        elif layout == "top_to_bottom":
            px = 500
            py = (i+.5) * 500 / len(nodes)
        else:
            px = (i+.5) * 1000 / len(nodes)
            py = 250 + (0 if len(nodes) <= 3 else ((-1)**i)*min(70, len(nodes)*8))
        positions[str(node["node_id"])] = (px,py)
    edge_svg = []
    graph_marker = "graph_arrow_" + re.sub(r"[^A-Za-z0-9_-]", "_", obj.object_id)
    for edge in edges:
        start, end = positions.get(str(edge.get("from"))), positions.get(str(edge.get("to")))
        if not start or not end:
            continue
        edge_svg.append(
            f'<path class="graph-edge draw-path" d="M{start[0]:.1f},{start[1]:.1f} L{end[0]:.1f},{end[1]:.1f}" '
            f'pathLength="1000" marker-end="url(#{graph_marker})"/>'
        )
    node_html = []
    for node in nodes:
        node_id = str(node["node_id"])
        x,y = positions[node_id]
        label = _esc(node.get("label", node_id))
        sub = _esc(node.get("subtitle", ""))
        node_html.append(
            f'<div class="graph-node" style="left:{x/10:.2f}%;top:{y/5:.2f}%">'
            f'<div class="graph-node-title">{label}</div>'
            f'{f"<div class=\"graph-node-subtitle\">{sub}</div>" if sub else ""}</div>'
        )
    return (
        '<div class="graph-stage"><svg class="graph-lines" viewBox="0 0 1000 500" preserveAspectRatio="none">'
        f'<defs><marker id="{graph_marker}" markerWidth="14" markerHeight="14" refX="10" refY="7" orient="auto">'
        f'<path d="M0 0 L12 7 L0 14" fill="none" stroke="{palette["accent"]}" stroke-width="2"/></marker></defs>'
        f'{"".join(edge_svg)}</svg>{"".join(node_html)}</div>'
    )


def _object_inner(obj: VisualObject, assets: dict[str, ResolvedMedia], palette: dict[str, str], style: dict[str, Any], depth: int = 0) -> str:
    if depth > 8:
        return '<div class="engine-error">Nested group depth exceeded</div>'
    if obj.kind in {"image", "video"}:
        media = assets.get(obj.object_id)
        if media is None:
            raise RuntimeError(f"{obj.object_id}: unresolved media asset")
        return _media_html(obj, media, palette)
    if obj.kind == "text":
        return _text_html(obj, palette, style)
    if obj.kind == "shape":
        return _svg_shape(obj, palette["accent"])
    if obj.kind == "icon":
        icon = str(obj.content.get("icon_id", ""))
        paths = {
            "check": "M14 52 L40 76 L88 22",
            "cross": "M20 20 L80 80 M80 20 L20 80",
            "play": "M28 14 L84 50 L28 86 Z",
            "warning": "M50 10 L94 88 L6 88 Z M50 34 L50 60 M50 73 L50 75",
        }
        if icon not in paths:
            raise RuntimeError(f"Unsupported icon_id: {icon}")
        return f'<svg class="icon-svg" viewBox="0 0 100 100"><path d="{paths[icon]}"/></svg>'
    if obj.kind == "callout":
        text = _esc(obj.content.get("text", ""))
        eyebrow = _esc(obj.content.get("eyebrow", ""))
        return f'<div class="callout"><div class="callout-eyebrow">{eyebrow}</div><div class="callout-copy">{text}</div></div>'
    if obj.kind == "counter":
        value = obj.content.get("value", 0)
        label = _esc(obj.content.get("label", ""))
        suffix = _esc(obj.content.get("suffix", ""))
        return (
            f'<div class="counter"><div class="counter-value" data-counter-value="{_esc(value)}">{_esc(value)}{suffix}</div>'
            f'<div class="counter-label">{label}</div></div>'
        )
    if obj.kind == "formula":
        expr = _esc(obj.content.get("expression", ""))
        caption = _esc(obj.content.get("caption", ""))
        return f'<div class="formula"><div class="formula-expression">{expr}</div><div class="formula-caption">{caption}</div></div>'
    if obj.kind == "table":
        return _table_html(obj, palette)
    if obj.kind == "chart":
        return _chart_html(obj, palette)
    if obj.kind == "node_graph":
        return _graph_html(obj, palette)
    if obj.kind == "group":
        children = [VisualObject.model_validate(x) for x in obj.content.get("children", [])]
        children.sort(key=lambda child: int(child.layer.z_index))
        child_html = "".join(_object_html(child, assets, palette, style, depth+1) for child in children)
        return f'<div class="group-content">{child_html}</div>'
    raise RuntimeError(f"No renderer registered for object kind: {obj.kind}")


def _object_html(obj: VisualObject, assets: dict[str, ResolvedMedia], palette: dict[str, str], style: dict[str, Any], depth: int = 0) -> str:
    layer = obj.layer
    x = _number(layer.x, .5, 0, 1) * 100
    y = _number(layer.y, .5, 0, 1) * 100
    width = _number(layer.width, .8, .001, 1) * 100
    height = _number(layer.height, .5, .001, 1) * 100
    opacity = _number(layer.opacity, 1, 0, 1)
    z = _int(layer.z_index, 0, -10000, 10000)
    initial = "visible" if obj.initial_state == "visible" else "hidden"
    appearance = obj.appearance
    radius = _int(appearance.get("border_radius"), 0, 0, 160)
    shadow_value = appearance.get("shadow", "")
    shadow_name = "soft" if shadow_value is True else str(shadow_value or "")
    shadows = {
        "soft": "0 18px 60px rgba(0,0,0,.24)",
        "deep": "0 28px 90px rgba(0,0,0,.42)",
        "glow": f"0 0 32px {palette['accent']}55",
    }
    shadow = shadows.get(shadow_name, "none") if shadow_name else "none"
    style_attr = (
        f"left:{x:.4f}%;top:{y:.4f}%;width:{width:.4f}%;height:{height:.4f}%;"
        f"z-index:{z};opacity:{opacity};visibility:{'visible' if initial == 'visible' else 'hidden'};"
        f"border-radius:{radius}px;box-shadow:{shadow};"
    )
    role = _esc(obj.role)
    oid = _esc(obj.object_id)
    content = _object_inner(obj, assets, palette, style, depth)
    return f'<div class="scene-object object-{_esc(obj.kind)} role-{role}" data-object-id="{oid}" style="{style_attr}">{content}</div>'


def _ease(event: VisualEvent) -> str:
    raw = str(event.params.get("easing", "cinematic"))
    return _EASINGS.get(raw, "cubic-bezier(.22,.61,.36,1)")


def _event_css(shot: Shot, event_offsets: dict[str, float] | None = None) -> tuple[str, list[dict[str, Any]]]:
    keyframes: list[str] = []
    animations: dict[str, list[str]] = {}
    row_rules: list[str] = []
    scripts: list[dict[str, Any]] = []
    object_map = {obj.object_id: obj for obj in shot.objects}
    for index, event in enumerate(shot.events):
        name = f"kf_{index}"
        selector = f'[data-object-id="{_esc(event.target_id)}"]'
        delay = max(0.0, float((event_offsets or {}).get(event.event_id, event.anchor.offset_sec)))
        duration = max(0.001, float(event.duration_sec))
        easing = _ease(event)
        p = event.params
        animation = None
        if event.action == "fade_in":
            keyframes.append(f"@keyframes {name}{{from{{opacity:0;visibility:visible}}to{{opacity:1;visibility:visible}}}}")
            animation = f"{name} {duration:.4f}s {easing} {delay:.4f}s both"
        elif event.action == "fade_out":
            keyframes.append(f"@keyframes {name}{{from{{opacity:1}}to{{opacity:0}}}}")
            animation = f"{name} {duration:.4f}s {easing} {delay:.4f}s both"
        elif event.action == "appear":
            keyframes.append(f"@keyframes {name}{{0%{{opacity:0;visibility:hidden}}1%{{visibility:visible}}100%{{opacity:1;visibility:visible}}}}")
            animation = f"{name} {duration:.4f}s steps(1,end) {delay:.4f}s both"
        elif event.action == "move":
            target_obj = object_map[event.target_id]
            x0 = _number(target_obj.layer.x, .5, 0, 1) * 100
            y0 = _number(target_obj.layer.y, .5, 0, 1) * 100
            x1 = _number(p.get("x"), .5, 0, 1) * 100
            y1 = _number(p.get("y"), .5, 0, 1) * 100
            keyframes.append(f"@keyframes {name}{{from{{left:{x0:.4f}%;top:{y0:.4f}%}}to{{left:{x1:.4f}%;top:{y1:.4f}%}}}}")
            animation = f"{name} {duration:.4f}s {easing} {delay:.4f}s both"
        elif event.action == "scale":
            factor = _number(p.get("factor"), 1.0, .1, 4)
            keyframes.append(f"@keyframes {name}{{from{{scale:1}}to{{scale:{factor:.4f}}}}}")
            animation = f"{name} {duration:.4f}s {easing} {delay:.4f}s both"
        elif event.action == "highlight":
            color = _color(p.get("color"), palette_accent_placeholder())
            keyframes.append(f"@keyframes {name}{{0%,100%{{filter:drop-shadow(0 0 0 transparent)}}35%,75%{{filter:drop-shadow(0 0 24px {color})}}}}")
            animation = f"{name} {duration:.4f}s ease-in-out {delay:.4f}s both"
        elif event.action == "draw":
            keyframes.append(
                f"@keyframes {name}{{from{{clip-path:inset(100% 0 0 0)}}to{{clip-path:inset(0 0 0 0)}}}}"
                f"@keyframes draw_{name}{{to{{stroke-dashoffset:0}}}}"
                f"@keyframes bar_{name}{{from{{transform:scaleY(0)}}to{{transform:scaleY(1)}}}}"
            )
            animation = f"{name} {duration:.4f}s {easing} {delay:.4f}s both"
            keyframes.append(
                f"{selector} .draw-path{{stroke-dasharray:1000;stroke-dashoffset:1000;"
                f"animation:draw_{name} {duration:.4f}s {easing} {delay:.4f}s both;}}"
                f"{selector} .draw-mark{{transform-box:fill-box;transform-origin:bottom;"
                f"animation:bar_{name} .55s {easing} calc({delay:.4f}s + var(--mark-delay,0s)) both;}}"
            )
        elif event.action == "reveal_row":
            row_index = _int(p.get("row_index"), 0, 0, 1000)
            keyframes.append(f"@keyframes {name}{{from{{opacity:0;transform:translateY(18px)}}to{{opacity:1;transform:translateY(0)}}}}")
            row_rules.append(
                f"{selector} [data-row-index=\"{row_index}\"]{{opacity:0;"
                f"animation:{name} {duration:.4f}s {easing} {delay:.4f}s both;}}"
            )
        elif event.action in {"count_up", "update_text"}:
            scripts.append({
                "action": event.action,
                "target": event.target_id,
                "start": delay,
                "duration": duration,
                "params": dict(p),
            })
        if animation:
            animations.setdefault(event.target_id, []).append(animation)
    selector_rules = [
        f'[data-object-id="{_esc(target)}"]{{animation:{", ".join(parts)};}}'
        for target, parts in animations.items()
    ]
    return "\n".join(keyframes + row_rules + selector_rules), scripts


def palette_accent_placeholder() -> str:
    # Kept as a separate helper so event colours are always validated.
    return "#6D9EFF"


def _recording_frame_window(
    recorded_frames: int,
    target_frames: int,
    preroll_frames: int = 0,
) -> tuple[int, int]:
    """Return (leading_preroll_frames, trailing_pad_frames).

    The recorder begins when Playwright creates the page, before its HTML/assets
    are ready. ``preroll_frames`` is measured around that page lifecycle and is
    therefore distinct from extra frames captured after the authored shot ends.
    Extra tail frames must never be mistaken for preroll and trimmed from the
    beginning of the shot.

    The browser is deliberately recorded beyond the authored duration. A maximum
    one-frame tail shortfall is tolerated as a mux/timestamp boundary condition;
    larger gaps remain hard failures.
    """
    recorded_frames = int(recorded_frames)
    target_frames = int(target_frames)
    preroll_frames = int(preroll_frames)
    if target_frames <= 0:
        raise ValueError("target_frames must be positive")
    if recorded_frames < 0:
        raise ValueError("recorded_frames cannot be negative")
    if preroll_frames < 0:
        raise ValueError("preroll_frames cannot be negative")
    available_frames = max(0, recorded_frames - preroll_frames)
    shortfall = target_frames - available_frames
    if shortfall > 1:
        raise RuntimeError(
            f"Browser recording is short by {shortfall} frames after preroll "
            f"({recorded_frames} recorded - {preroll_frames} preroll = "
            f"{available_frames}/{target_frames}); refusing to fabricate the missing scene."
        )
    return preroll_frames, max(0, shortfall)


_BASE_CSS = r"""
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#101827}
body{font-family:Arial,'Segoe UI',sans-serif;color:#f4f6fa}
.scene-canvas{position:relative;width:100vw;height:100vh;overflow:hidden;isolation:isolate;background:var(--bg)}
.scene-canvas:before{content:'';position:absolute;inset:0;z-index:9000;pointer-events:none;opacity:var(--grain-opacity,.035);background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.86' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.55'/%3E%3C/svg%3E")}
.scene-canvas:after{content:'';position:absolute;inset:0;z-index:8999;pointer-events:none;background:radial-gradient(ellipse at center,transparent 45%,rgba(0,0,0,.22) 100%);opacity:var(--vignette-opacity,.45)}
.scene-object{position:absolute;transform:translate(-50%,-50%);transform-origin:center center;overflow:visible;will-change:opacity,transform,left,top,filter;contain:layout style}
.scene-object[style*='visibility:hidden']{pointer-events:none}
.media-frame{position:relative;width:100%;height:100%;overflow:hidden;border-radius:var(--media-radius,0px);background:#151c27}
.media-element{width:100%;height:100%;display:block;object-position:50% 50%;background:#151c27}
.media-shadow{filter:drop-shadow(0 22px 48px rgba(0,0,0,.34))}
.media-overlay{position:absolute;inset:0;background:linear-gradient(90deg,rgba(4,10,18,.75),rgba(4,10,18,.12) 70%),linear-gradient(0deg,rgba(4,10,18,.44),transparent 55%);pointer-events:none}
/* Camera moves run from the single explicit scene-start signal, never during page load. */
.camera-slow-zoom{animation:kf-camera-in var(--shot-duration) linear both}
.camera-zoom-out{animation:kf-camera-out var(--shot-duration) linear both}
.camera-pan-left{animation:kf-camera-left var(--shot-duration) linear both}
.camera-pan-right{animation:kf-camera-right var(--shot-duration) linear both}
@keyframes kf-camera-in{from{transform:scale(1)}to{transform:scale(1.095)}}
@keyframes kf-camera-out{from{transform:scale(1.095)}to{transform:scale(1)}}
@keyframes kf-camera-left{from{transform:scale(1.08) translateX(2.5%)}to{transform:scale(1.08) translateX(-2.5%)}}
@keyframes kf-camera-right{from{transform:scale(1.08) translateX(-2.5%)}to{transform:scale(1.08) translateX(2.5%)}}
html:not(.playing) *{animation-play-state:paused!important}
html.playing *{animation-play-state:running!important}
.type-layout{height:100%;width:100%;display:flex;flex-direction:column;justify-content:var(--type-valign,center);align-items:stretch;text-align:var(--type-align,left);padding:var(--type-padding,0px);border-radius:var(--type-radius,0px);background:var(--type-bg,transparent);color:var(--type-color);text-shadow:var(--type-shadow);font-family:var(--type-font);overflow:hidden}
.type-eyebrow{font:700 clamp(14px,1.3vw,26px)/1.1 var(--body-font);letter-spacing:.18em;text-transform:uppercase;color:var(--accent);margin-bottom:clamp(12px,1.5vw,28px)}
.type-main{font-size:var(--type-size);font-weight:var(--type-weight);line-height:var(--type-line);letter-spacing:var(--type-track);text-transform:var(--type-transform);font-style:var(--type-italic);text-wrap:balance;overflow-wrap:anywhere}
.type-subtitle{font:400 clamp(18px,1.6vw,34px)/1.35 var(--body-font);color:var(--muted);margin-top:clamp(14px,1.5vw,30px);max-width:94%;text-wrap:pretty}
.shape-svg,.icon-svg{width:100%;height:100%;display:block;overflow:visible}
.icon-svg path{fill:none;stroke:var(--accent);stroke-width:8;stroke-linecap:round;stroke-linejoin:round}
.callout{width:100%;height:100%;padding:5%;display:flex;flex-direction:column;justify-content:center;background:linear-gradient(135deg,var(--surface),var(--surface2));border:1px solid rgba(255,255,255,.15);border-radius:22px;box-shadow:0 22px 68px rgba(0,0,0,.28)}
.callout-eyebrow{font-size:clamp(13px,1vw,22px);font-weight:800;letter-spacing:.17em;color:var(--accent);text-transform:uppercase;margin-bottom:14px}
.callout-copy{font-size:clamp(20px,2.3vw,48px);font-weight:720;line-height:1.12;text-wrap:balance}
.counter{width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;gap:10px}
.counter-value{font-size:clamp(64px,9vw,190px);font-weight:850;letter-spacing:-.065em;line-height:.95;color:var(--accent);font-variant-numeric:tabular-nums}
.counter-label{font-size:clamp(17px,1.8vw,36px);line-height:1.2;color:var(--muted);font-weight:600}
.formula{width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;gap:16px;padding:24px 34px;border:1px solid rgba(255,255,255,.16);border-radius:20px;background:linear-gradient(135deg,rgba(255,255,255,.08),rgba(255,255,255,.02));box-shadow:0 18px 48px rgba(0,0,0,.22)}
.formula-expression{font:600 clamp(28px,3.2vw,68px)/1.2 'Cambria Math',Cambria,serif;letter-spacing:.015em;text-align:center}
.formula-caption{font:500 clamp(14px,1.2vw,24px)/1.3 Arial,sans-serif;color:var(--muted);text-align:center}
.table-shell{width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden}
.editorial-table{width:100%;border-collapse:separate;border-spacing:0;border-radius:16px;overflow:hidden;font-size:clamp(14px,1.3vw,26px);font-variant-numeric:tabular-nums;background:rgba(255,255,255,.035);box-shadow:0 18px 60px rgba(0,0,0,.2)}
.editorial-table th{padding:22px 24px;text-align:left;background:rgba(255,255,255,.12);color:var(--accent);font-size:.82em;letter-spacing:.08em;text-transform:uppercase}
.editorial-table td{padding:19px 24px;border-top:1px solid rgba(255,255,255,.1);color:#f2f4f7;font-weight:560}
.editorial-table tr:nth-child(even) td{background:rgba(255,255,255,.035)}
.chart-card{width:100%;height:100%;padding:24px 28px 18px;border-radius:22px;background:linear-gradient(145deg,rgba(255,255,255,.075),rgba(255,255,255,.018));border:1px solid rgba(255,255,255,.12);box-shadow:0 18px 60px rgba(0,0,0,.2);display:flex;flex-direction:column;overflow:hidden}
.chart-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;min-height:55px}
.chart-title{font-size:clamp(20px,2vw,38px);font-weight:780;letter-spacing:-.025em}
.chart-subtitle{font-size:clamp(13px,1vw,20px);color:var(--muted);margin-top:6px}
.chart-svg{width:100%;height:calc(100% - 64px);overflow:visible}
.chart-grid{stroke:rgba(255,255,255,.14);stroke-width:1.4;stroke-dasharray:5 8}
.chart-axis-label{fill:rgba(240,244,250,.66);font:500 22px Arial,sans-serif}
.chart-bar{filter:drop-shadow(0 8px 14px rgba(0,0,0,.14))}
.chart-line{filter:drop-shadow(0 5px 10px rgba(0,0,0,.18))}
.chart-legend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px 18px;font-size:clamp(12px,.9vw,18px);color:var(--muted)}.chart-category-legend{justify-content:center;margin-top:8px}
.legend-item{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}.legend-item i{display:inline-block;width:10px;height:10px;border-radius:50%}
.chart-disclosure{align-self:flex-end;margin-top:8px;color:#e8ba74;font-size:13px;font-weight:750;letter-spacing:.12em}
.donut-total{fill:var(--fg);font:800 60px Arial,sans-serif}
.graph-stage{position:relative;width:100%;height:100%;overflow:visible}
.graph-lines{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.graph-edge{fill:none;stroke:var(--accent);stroke-width:5;stroke-linecap:round;opacity:.85;filter:drop-shadow(0 0 8px rgba(109,158,255,.25))}
.graph-node{position:absolute;transform:translate(-50%,-50%);min-width:175px;max-width:31%;padding:22px 25px;border:1px solid rgba(255,255,255,.18);border-radius:16px;background:linear-gradient(145deg,#263548,#182536);box-shadow:0 18px 36px rgba(0,0,0,.3);text-align:center}
.graph-node-title{font-size:clamp(17px,1.55vw,30px);font-weight:780;line-height:1.12;text-wrap:balance}.graph-node-subtitle{font-size:clamp(12px,.9vw,18px);color:var(--muted);margin-top:8px}
.group-content{position:relative;width:100%;height:100%;overflow:visible}
.engine-error{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#ff8f8f;background:rgba(80,0,0,.2);font:600 20px Arial}
"""


def build_shot_html(
    plan: VisualStoryPlan,
    shot: Shot,
    assets: dict[str, ResolvedMedia],
    event_offsets: dict[str, float] | None = None,
    duration_sec: float = 8.0,
) -> tuple[str, float]:
    """Build a self-contained HTML document for one shot."""
    palette = _palette(plan)
    style = dict(plan.style or {})
    object_ids = {obj.object_id for obj in shot.objects}
    event_css, script_events = _event_css(shot, event_offsets)
    for event in shot.events:
        if event.target_id not in object_ids:
            raise ValueError(f"{shot.shot_id}: missing animation target {event.target_id}")
    layers = "".join(_object_html(obj, assets, palette, style) for obj in shot.objects)
    grain = _number(style.get("grain_opacity"), .025, 0, .12)
    vignette = _number(style.get("vignette_opacity"), .35, 0, .8)
    background = _color(style.get("background"), palette["bg"])
    bg2 = _color(style.get("background_secondary"), "#182536")
    font_body = _font(style.get("font_body"), "Arial, Segoe UI")
    font_display = _font(style.get("font_display"), "Arial, Segoe UI")
    title = _esc(plan.title)
    events_json = json.dumps(script_events, ensure_ascii=False).replace("</", "<\\/")
    script = f"""
    const jobs = {events_json};
    function startTimedJobs() {{
      for (const job of jobs) {{
        const target = document.querySelector('[data-object-id="' + CSS.escape(job.target) + '"]');
        if (!target) continue;
        const begin = job.start * 1000;
        if (job.action === 'update_text') {{
          window.setTimeout(() => {{
            const node = target.querySelector('.type-main,.callout-copy,.counter-value') || target;
            node.textContent = String(job.params.text ?? '');
          }}, begin);
        }} else if (job.action === 'count_up') {{
          const node = target.querySelector('.counter-value') || target;
          const from = Number(job.params.from ?? 0);
          const to = Number(job.params.to ?? 0);
          const duration = Math.max(1, job.duration * 1000);
          const decimals = Math.max(0, Math.min(6, Number(job.params.decimals ?? 0)));
          const suffix = String(job.params.suffix ?? '');
          window.setTimeout(() => {{
            const start = performance.now();
            function tick(now) {{
              const p = Math.max(0, Math.min(1, (now - start) / duration));
              const eased = 1 - Math.pow(1-p, 3);
              node.textContent = (from + (to-from)*eased).toFixed(decimals) + suffix;
              if (p < 1) requestAnimationFrame(tick);
            }}
            requestAnimationFrame(tick);
          }}, begin);
        }}
      }}
    }}
    window.addEventListener('load', async () => {{
      await document.fonts.ready;
      const imageReady = Array.from(document.images).map((img) => {{
        if (img.complete) return Promise.resolve(img.naturalWidth > 0);
        return new Promise((resolve) => {{
          img.addEventListener('load', () => resolve(true), {{once:true}});
          img.addEventListener('error', () => resolve(false), {{once:true}});
        }});
      }});
      const videoReady = Array.from(document.querySelectorAll('video')).map((media) => {{
        if (media.readyState >= 2) return Promise.resolve(true);
        return new Promise((resolve) => {{
          media.addEventListener('loadeddata', () => resolve(true), {{once:true}});
          media.addEventListener('error', () => resolve(false), {{once:true}});
          media.load();
        }});
      }});
      const readiness = await Promise.all([...imageReady, ...videoReady]);
      window.__KADRFARM_MEDIA_ERRORS__ = readiness
        .map((ok, index) => ok ? null : ('media_' + index + '_failed'))
        .filter(Boolean);
      // The renderer starts media playback and all CSS/JS animation clocks together
      // only after Playwright has marked the page ready for capture.
      window.__KADRFARM_READY__ = true;
    }});
    window.addEventListener('kadrfarm:start', () => {{
      document.documentElement.classList.add('playing');
      for (const media of document.querySelectorAll('video')) {{
        media.muted = true;
        const play = media.play();
        if (play && play.catch) play.catch(() => {{}});
      }}
      startTimedJobs();
    }}, {{once:true}});
    """
    shot_duration = _number(duration_sec, 8.0, 0.1, 3600.0)
    html_doc = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width={plan.width},height={plan.height},initial-scale=1">
<title>{title}</title>
<style>
 :root{{--bg:{background};--bg2:{bg2};--fg:{palette['fg']};--muted:{palette['muted']};--accent:{palette['accent']};--accent2:{palette['accent2']};--surface:{palette['surface']};--surface2:{palette['surface2']};--grain-opacity:{grain};--vignette-opacity:{vignette};--font-body:'{font_body}';--font-display:'{font_display}';--shot-duration:{shot_duration:.4f}s;}}
{_BASE_CSS}
{event_css}
</style></head><body><main class="scene-canvas">{layers}</main><script>{script}</script></body></html>"""
    # Shot duration is the union of speech units. A caller injects the measured
    # duration through render_shot(); return 0 here for a convenient API shape.
    return html_doc, 0.0


class ProfessionalBrowserRenderer:
    """Record one HTML-composited shot with Playwright and normalize via FFmpeg."""

    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        fps: int = 25,
        crf: int = 18,
        ffprobe: str = "ffprobe",
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.fps = _int(fps, 25, 1, 60)
        self.crf = _int(crf, 18, 14, 28)

    @staticmethod
    async def _record(
        html_document: str,
        served_assets: dict[str, Path],
        record_dir: Path,
        width: int,
        height: int,
        duration: float,
        tail_pad_sec: float = 0.75,
    ) -> tuple[Path, float]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for professional rendering. Install with: "
                "python -m pip install 'playwright>=1.50,<2' && python -m playwright install chromium"
            ) from exc

        record_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            launch_args = [
                "--force-color-profile=srgb",
                "--disable-background-timer-throttling",
            ]
            # Root Linux containers need Chromium's sandbox disabled; Windows does not.
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": launch_args,
            }
            custom_chromium = os.getenv("KADRFARM_CHROMIUM_PATH", "").strip()
            if custom_chromium:
                executable = Path(custom_chromium).expanduser()
                if not executable.is_file():
                    raise RuntimeError(
                        f"KADRFARM_CHROMIUM_PATH does not exist: {executable}"
                    )
                launch_options["executable_path"] = str(executable.resolve())

            browser = await playwright.chromium.launch(**launch_options)
            context = None
            video = None
            capture_offset = 0.0
            try:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    record_video_dir=str(record_dir),
                    record_video_size={"width": width, "height": height},
                    device_scale_factor=1,
                )
                # Playwright starts the page recording during new_page(). Use the
                # midpoint of that async call as the best client-side estimate of
                # the recording origin; starting the clock before or after the call
                # can shift the trim by several frames on a busy Windows machine.
                page_create_started = time.monotonic()
                page = await context.new_page()
                page_create_finished = time.monotonic()
                recording_started = (
                    page_create_started + page_create_finished
                ) / 2.0
                page.set_default_timeout(45000)
                # Media are fulfilled from the whitelisted local cache. This is
                # independent of browser file:// restrictions and supports Range.
                media_bytes: dict[str, bytes] = {}

                async def fulfill_asset(route) -> None:
                    parsed = urlsplit(route.request.url)
                    token = parsed.path.rsplit("/", 1)[-1]
                    media_path = served_assets.get(token)
                    if media_path is None or not media_path.is_file():
                        await route.fulfill(status=404, body=b"not found")
                        return
                    payload = media_bytes.get(token)
                    if payload is None:
                        payload = media_path.read_bytes()
                        media_bytes[token] = payload
                    total = len(payload)
                    range_header = route.request.headers.get("range", "")
                    status = 200
                    start, end = 0, total - 1
                    response_headers = {
                        "Content-Type": mimetypes.guess_type(media_path.name)[0] or "application/octet-stream",
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "no-store",
                    }
                    if range_header.startswith("bytes="):
                        try:
                            left, right = range_header[6:].split(",", 1)[0].split("-", 1)
                            start = int(left) if left else 0
                            end = int(right) if right else total - 1
                            if start < 0 or start >= total or end < start:
                                raise ValueError("invalid byte range")
                            end = min(end, total - 1)
                            status = 206
                            response_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
                        except ValueError:
                            await route.fulfill(
                                status=416,
                                headers={"Content-Range": f"bytes */{total}"},
                                body=b"",
                            )
                            return
                    body = payload[start:end + 1]
                    response_headers["Content-Length"] = str(len(body))
                    await route.fulfill(status=status, headers=response_headers, body=body)

                await page.route("http://kadrfarm.local/asset/*", fulfill_asset)
                await page.set_content(html_document, wait_until="load")
                await page.wait_for_function(
                    "window.__KADRFARM_READY__ === true",
                    timeout=45000,
                )
                media_errors = await page.evaluate("window.__KADRFARM_MEDIA_ERRORS__ || []")
                if media_errors:
                    raise RuntimeError(
                        "Browser could not decode one or more visual assets: "
                        + ", ".join(media_errors)
                    )
                # Do not let CSS delays or video playback elapse while assets/fonts load.
                capture_offset = max(0.0, time.monotonic() - recording_started)
                await page.evaluate("window.dispatchEvent(new Event('kadrfarm:start'))")
                # CSS/JS motion runs for the authored duration. Keep Chromium
                # recording a little longer so its final frames are flushed even
                # when the desktop/browser is briefly behind real time. The
                # normalizer below trims this tail and keeps exactly target_frames.
                tail_pad_sec = max(0.0, min(float(tail_pad_sec), 2.0))
                capture_duration = duration + tail_pad_sec
                await page.wait_for_timeout(max(1, round(capture_duration * 1000)))
                video = page.video
            finally:
                if context is not None:
                    try:
                        await context.close()  # flush the WebM recording
                    finally:
                        await browser.close()
                else:
                    await browser.close()

            if video is None:
                raise RuntimeError("Playwright did not create a video recording")
            return Path(await video.path()).resolve(), capture_offset

    def render_shot(
        self,
        plan: VisualStoryPlan,
        shot: Shot,
        assets: dict[str, ResolvedMedia],
        duration_sec: float,
        output_path: str | Path,
        work_dir: str | Path | None = None,
        event_offsets: dict[str, float] | None = None,
    ) -> Path:
        if duration_sec <= 0 or not math.isfinite(duration_sec):
            raise ValueError("duration_sec must be a finite positive value")
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        work = Path(work_dir).resolve() if work_dir else output_path.parent / "_professional_render"
        work.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", shot.shot_id)
        html_path = work / f"{safe_id}.html"
        record_dir = work / f"{safe_id}_record"
        # The caller passes a measured speech plan; event offsets supplied to
        # this renderer are already expressed relative to the shot timeline.
        html_doc, _ = build_shot_html(plan, shot, assets, event_offsets, duration_sec)
        html_path.write_text(html_doc, encoding="utf-8")

        # Register media and poster files by opaque tokens. Browser requests are
        # fulfilled by Playwright, so the scene never needs file:// or localhost navigation.
        served_assets: dict[str, Path] = {}
        served_html = html_doc
        for media in assets.values():
            token = hashlib.sha256(str(media.path.resolve()).encode("utf-8")).hexdigest()[:24]
            served_assets[token] = media.path.resolve()
            served_html = served_html.replace(
                media.as_uri(),
                f"http://kadrfarm.local/asset/{token}",
            )

        def register_posters(obj: VisualObject, depth: int = 0) -> None:
            if depth > 8:
                return
            poster = obj.content.get("poster")
            if poster:
                poster_path = Path(str(poster))
                if not poster_path.is_absolute():
                    poster_path = Path.cwd() / poster_path
                poster_path = poster_path.resolve()
                if poster_path.is_file():
                    token = hashlib.sha256(str(poster_path).encode("utf-8")).hexdigest()[:24]
                    served_assets[token] = poster_path
                    nonlocal_html[0] = nonlocal_html[0].replace(
                        poster_path.as_uri(),
                        f"http://kadrfarm.local/asset/{token}",
                    )
            if obj.kind == "group":
                for child_data in obj.content.get("children", []):
                    register_posters(VisualObject.model_validate(child_data), depth + 1)

        nonlocal_html = [served_html]
        for obj in shot.objects:
            register_posters(obj)
        served_html = nonlocal_html[0]

        webm_path, wall_clock_preroll = asyncio.run(
            self._record(
                served_html,
                served_assets,
                record_dir,
                plan.width,
                plan.height,
                duration_sec,
            )
        )
        target_frames = max(1, round(duration_sec * plan.fps))
        source_probe = subprocess.run(
            [
                self.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames,avg_frame_rate", "-of", "json",
                str(webm_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if source_probe.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed reading browser recording for {shot.shot_id}: "
                f"{source_probe.stderr[-3000:]}"
            )
        try:
            source_streams = json.loads(source_probe.stdout).get("streams", [])
            recorded_frames = int(source_streams[0].get("nb_read_frames") or 0)
        except (ValueError, TypeError, IndexError, AttributeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not determine decoded frame count for browser recording {shot.shot_id}"
            ) from exc
        preroll_frames = max(0, int(math.ceil(wall_clock_preroll * plan.fps - 1e-6)))
        preroll_frames, pad_frames = _recording_frame_window(
            recorded_frames,
            target_frames,
            preroll_frames=preroll_frames,
        )
        print(
            f"[Visual Pro Capture] {shot.shot_id}: "
            f"recorded={recorded_frames} frames | preroll={preroll_frames} frames "
            f"({wall_clock_preroll:.3f}s) | capture_tail=0.750s | "
            f"tail_pad={pad_frames} frame(s) | target={target_frames} frames"
        )
        filters = [
            f"trim=start_frame={preroll_frames}:end_frame={preroll_frames + target_frames}",
            "setpts=PTS-STARTPTS",
            f"fps={plan.fps}",
        ]
        # Add an exact frame count after CFR conversion. A time-based tpad before
        # fps can be rounded away by the fps filter on Chromium WebM timestamps.
        if pad_frames:
            filters.append(f"tpad=stop_mode=clone:stop={pad_frames}")
        filters.extend([
            f"scale={plan.width}:{plan.height}:flags=lanczos",
            "setsar=1",
            "format=yuv420p",
        ])
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(webm_path),
            "-vf", ",".join(filters),
            "-frames:v", str(target_frames),
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(self.crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(plan.fps),
            "-movflags", "+faststart", str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed normalizing {shot.shot_id}:\n{result.stderr[-5000:]}")
        if not output_path.is_file() or output_path.stat().st_size < 1024:
            raise RuntimeError(f"No usable MP4 was produced for {shot.shot_id}")
        output_probe = subprocess.run(
            [
                self.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "json", str(output_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if output_probe.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed verifying normalized segment {shot.shot_id}: "
                f"{output_probe.stderr[-3000:]}"
            )
        try:
            output_streams = json.loads(output_probe.stdout).get("streams", [])
            actual_frames = int(output_streams[0].get("nb_read_frames") or 0)
        except (ValueError, TypeError, IndexError, AttributeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not verify normalized frame count for {shot.shot_id}"
            ) from exc
        if actual_frames != target_frames:
            raise RuntimeError(
                f"{shot.shot_id}: normalized segment contains {actual_frames} frames; "
                f"expected exactly {target_frames}. No audio mux will be performed."
            )
        print(
            f"[Visual Pro Capture] {shot.shot_id}: encoded {actual_frames}/{target_frames} frames"
        )
        return output_path
