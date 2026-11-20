"""Pillow visual renderer for KadrFarm Visual Story V2."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageOps,
)

from .visual_story_v2 import VisualObject


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for name in candidates:
        if Path(name).is_file():
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass

    return ImageFont.load_default()


def _rgba(color: str, alpha: int = 255):
    from PIL import ImageColor

    rgb = ImageColor.getrgb(color)

    if len(rgb) == 4:
        return (*rgb[:3], alpha)

    return (*rgb, alpha)


class SceneRenderer:
    def __init__(
        self,
        width: int = 1920,
        height: int = 1080,
        background: str = "#101827",
    ):
        self.width = width
        self.height = height
        self.background = background

    def render(
        self,
        objects: list[VisualObject],
        assets: dict[str, Any] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
        frame_index: int = 0,
    ) -> Image.Image:
        canvas = Image.new(
            "RGBA",
            (self.width, self.height),
            self.background,
        )

        assets = assets or {}
        states = states or {}

        for obj in sorted(
            objects,
            key=lambda item: item.layer.z_index,
        ):
            self._render_object(
                canvas,
                obj,
                assets,
                states,
                frame_index,
            )

        return canvas.convert("RGB")

    def _render_object(
        self,
        canvas: Image.Image,
        obj: VisualObject,
        assets: dict[str, Any],
        states: dict[str, dict[str, Any]],
        frame_index: int,
    ):
        state = states.get(obj.object_id, {})

        visible = state.get(
            "visible",
            obj.initial_state == "visible",
        )

        if not visible:
            return

        if obj.kind == "group":
            for child_data in obj.content.get("children", []):
                child = VisualObject.model_validate(child_data)

                self._render_object(
                    canvas,
                    child,
                    assets,
                    states,
                    frame_index,
                )

            return

        layer = obj.layer

        opacity = float(
            state.get(
                "opacity",
                layer.opacity,
            )
        )

        if opacity <= 0:
            return

        x = float(layer.x) + float(state.get("dx", 0.0))
        y = float(layer.y) + float(state.get("dy", 0.0))

        scale = float(state.get("scale", 1.0))

        width = max(
            1,
            int(self.width * layer.width * scale),
        )

        height = max(
            1,
            int(self.height * layer.height * scale),
        )

        center_x = int(x * self.width)
        center_y = int(y * self.height)

        x0 = center_x - width // 2
        y0 = center_y - height // 2
        x1 = x0 + width
        y1 = y0 + height

        overlay = Image.new(
            "RGBA",
            (self.width, self.height),
            (0, 0, 0, 0),
        )

        draw = ImageDraw.Draw(
            overlay,
            "RGBA",
        )

        c = obj.content
        appearance = obj.appearance

        if obj.kind in ("image", "video"):
            self._draw_media(
                overlay,
                obj,
                assets,
                (x0, y0, x1, y1),
                frame_index,
            )

        elif obj.kind == "text":
            self._draw_text(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        elif obj.kind == "shape":
            self._draw_shape(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        elif obj.kind == "icon":
            self._draw_icon(
                draw,
                obj,
                (x0, y0, x1, y1),
            )

        elif obj.kind == "callout":
            self._draw_callout(
                draw,
                obj,
                (x0, y0, x1, y1),
            )

        elif obj.kind == "counter":
            self._draw_counter(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        elif obj.kind == "formula":
            self._draw_formula(
                draw,
                obj,
                (x0, y0, x1, y1),
            )

        elif obj.kind == "table":
            self._draw_table(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        elif obj.kind == "chart":
            self._draw_chart(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        elif obj.kind == "node_graph":
            self._draw_graph(
                draw,
                obj,
                (x0, y0, x1, y1),
                state,
            )

        alpha = overlay.getchannel("A")

        alpha = alpha.point(
            lambda value: int(
                value * max(0.0, min(1.0, opacity))
            )
        )

        overlay.putalpha(alpha)

        canvas.alpha_composite(overlay)

        if state.get("highlighted"):
            highlight = ImageDraw.Draw(canvas)

            color = state.get(
                "highlight_color",
                "#F4B860",
            )

            highlight.rounded_rectangle(
                (x0 - 8, y0 - 8, x1 + 8, y1 + 8),
                radius=22,
                outline=color,
                width=8,
            )

    def _draw_media(
        self,
        overlay,
        obj,
        assets,
        box,
        frame_index,
    ):
        asset = assets.get(obj.object_id)

        if asset is None:
            raise RuntimeError(
                f"{obj.object_id}: media was not prepared"
            )

        if isinstance(asset, list):
            if not asset:
                raise RuntimeError(
                    f"{obj.object_id}: empty video frame sequence"
                )

            index = min(
                frame_index,
                len(asset) - 1,
            )

            path = asset[index]

        else:
            path = asset

        image = Image.open(path).convert("RGBA")

        x0, y0, x1, y1 = box

        target_width = max(1, x1 - x0)
        target_height = max(1, y1 - y0)

        fit = obj.layer.fit

        if fit == "cover":
            image = ImageOps.fit(
                image,
                (target_width, target_height),
                method=Image.Resampling.LANCZOS,
            )

        elif fit == "stretch":
            image = image.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )

        else:
            image.thumbnail(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )

            x0 += (target_width - image.width) // 2
            y0 += (target_height - image.height) // 2

        overlay.alpha_composite(
            image,
            (x0, y0),
        )

    def _draw_text(
        self,
        draw,
        obj,
        box,
        state,
    ):
        x0, y0, x1, y1 = box
        content = obj.content

        text = state.get(
            "text",
            content["text"],
        )

        font_size = int(
            content.get("font_size", 64)
        )

        font = _font(font_size)

        max_width = max(
            20,
            x1 - x0,
        )

        # Approximate line wrapping by available pixel width.
        words = str(text).split()

        lines = []
        current = ""

        for word in words:
            candidate = f"{current} {word}".strip()

            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)

                current = word

        if current:
            lines.append(current)

        text = "\n".join(lines)

        color = obj.appearance.get(
            "color",
            "#FFFFFF",
        )

        stroke_color = obj.appearance.get(
            "stroke_color",
            "#000000",
        )

        draw.multiline_text(
            (x0, y0),
            text,
            font=font,
            fill=_rgba(color),
            spacing=int(content.get("spacing", 12)),
            align=content.get("alignment", "left"),
            stroke_width=int(content.get("stroke_width", 0)),
            stroke_fill=_rgba(stroke_color),
        )

    def _draw_shape(
        self,
        draw,
        obj,
        box,
        state,
    ):
        x0, y0, x1, y1 = box
        content = obj.content
        appearance = obj.appearance

        shape = content["shape_type"]

        fill = appearance.get("fill", "#263449")
        stroke = appearance.get("stroke", "#F4B860")

        stroke_width = int(
            appearance.get("stroke_width", 5)
        )

        progress = float(
            state.get("draw_progress", 1.0)
        )

        if shape == "rectangle":
            draw.rectangle(
                (x0, y0, x1, y1),
                fill=_rgba(fill),
                outline=_rgba(stroke),
                width=stroke_width,
            )

        elif shape == "rounded_rectangle":
            draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=int(content.get("radius", 24)),
                fill=_rgba(fill),
                outline=_rgba(stroke),
                width=stroke_width,
            )

        elif shape in ("circle", "ellipse"):
            draw.ellipse(
                (x0, y0, x1, y1),
                fill=_rgba(fill) if shape == "circle" else None,
                outline=_rgba(stroke),
                width=stroke_width,
            )

        elif shape in ("line", "arrow"):
            end_x = int(x0 + (x1 - x0) * progress)
            end_y = int(y0 + (y1 - y0) * progress)

            draw.line(
                (x0, y0, end_x, end_y),
                fill=_rgba(stroke),
                width=stroke_width,
            )

            if shape == "arrow" and progress >= 0.98:
                angle = math.atan2(
                    y1 - y0,
                    x1 - x0,
                )

                radius = 26

                p1 = (
                    x1 - radius * math.cos(angle - 0.5),
                    y1 - radius * math.sin(angle - 0.5),
                )

                p2 = (
                    x1 - radius * math.cos(angle + 0.5),
                    y1 - radius * math.sin(angle + 0.5),
                )

                draw.polygon(
                    [(x1, y1), p1, p2],
                    fill=_rgba(stroke),
                )

        elif shape == "polygon":
            points = content.get("points", [])

            converted = [
                (
                    x0 + int(float(point[0]) * (x1 - x0)),
                    y0 + int(float(point[1]) * (y1 - y0)),
                )
                for point in points
            ]

            if len(converted) >= 3:
                draw.polygon(
                    converted,
                    fill=_rgba(fill),
                    outline=_rgba(stroke),
                )

    def _draw_icon(self, draw, obj, box):
        x0, y0, x1, y1 = box
        icon = obj.content.get("icon_id")

        color = obj.appearance.get(
            "color",
            "#F4B860",
        )

        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2

        if icon == "check":
            draw.line(
                (x0, cy, cx - 10, y1 - 10, x1, y0),
                fill=color,
                width=12,
                joint="curve",
            )

        elif icon == "cross":
            draw.line(
                (x0, y0, x1, y1),
                fill=color,
                width=12,
            )
            draw.line(
                (x1, y0, x0, y1),
                fill=color,
                width=12,
            )

        elif icon == "warning":
            draw.polygon(
                [
                    (cx, y0),
                    (x1, y1),
                    (x0, y1),
                ],
                fill="#5B352A",
                outline=color,
            )

            draw.line(
                (cx, y0 + 30, cx, y1 - 55),
                fill="#FFFFFF",
                width=10,
            )

            draw.ellipse(
                (cx - 6, y1 - 40, cx + 6, y1 - 28),
                fill="#FFFFFF",
            )

        elif icon == "play":
            draw.polygon(
                [
                    (x0, y0),
                    (x1, cy),
                    (x0, y1),
                ],
                fill=color,
            )

        else:
            raise RuntimeError(
                f"Unknown icon_id: {icon}"
            )

    def _draw_callout(self, draw, obj, box):
        x0, y0, x1, y1 = box

        fill = obj.appearance.get(
            "fill",
            "#263449",
        )

        stroke = obj.appearance.get(
            "stroke",
            "#F4B860",
        )

        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=24,
            fill=_rgba(fill),
            outline=_rgba(stroke),
            width=4,
        )

        draw.multiline_text(
            (x0 + 24, y0 + 22),
            str(obj.content.get("text", "")),
            font=_font(
                int(obj.content.get("font_size", 36))
            ),
            fill="#FFFFFF",
            spacing=10,
        )

    def _draw_counter(self, draw, obj, box, state):
        x0, y0, x1, y1 = box

        value = state.get(
            "value",
            obj.content.get("value"),
        )

        font_size = int(
            obj.content.get("font_size", 100)
        )

        draw.text(
            (x0, y0),
            str(value),
            font=_font(font_size),
            fill=obj.appearance.get("color", "#F4B860"),
        )

        label = obj.content.get("label")

        if label:
            draw.text(
                (x0, y0 + font_size + 10),
                str(label),
                font=_font(30),
                fill="#FFFFFF",
            )

    def _draw_formula(self, draw, obj, box):
        x0, y0, x1, y1 = box

        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=20,
            fill="#18263A",
            outline=obj.appearance.get("stroke", "#F4B860"),
            width=4,
        )

        draw.multiline_text(
            (x0 + 24, y0 + 22),
            str(obj.content.get("expression", "")),
            font=_font(
                int(obj.content.get("font_size", 48))
            ),
            fill="#FFFFFF",
            spacing=12,
        )

    def _draw_table(self, draw, obj, box, state):
        x0, y0, x1, y1 = box

        columns = obj.content["columns"]
        rows = obj.content["rows"]

        visible_rows = state.get(
            "revealed_rows",
            len(rows),
        )

        visible_rows = max(
            0,
            min(len(rows), int(visible_rows)),
        )

        rows = rows[:visible_rows]

        column_count = len(columns)
        row_count = len(rows) + 1

        cell_width = (x1 - x0) / column_count
        cell_height = (y1 - y0) / max(1, row_count)

        all_rows = [
            [column.get("title", "") for column in columns]
        ]

        all_rows += [
            row.get("cells", [])
            for row in rows
        ]

        for row_index, row in enumerate(all_rows):
            for column_index in range(column_count):
                xx = x0 + column_index * cell_width
                yy = y0 + row_index * cell_height

                value = (
                    row[column_index]
                    if column_index < len(row)
                    else ""
                )

                draw.rectangle(
                    (
                        xx,
                        yy,
                        xx + cell_width,
                        yy + cell_height,
                    ),
                    fill=(
                        "#31445C"
                        if row_index == 0
                        else "#19283A"
                    ),
                    outline="#60758D",
                    width=2,
                )

                draw.text(
                    (xx + 12, yy + 10),
                    str(value),
                    font=_font(
                        int(obj.content.get("font_size", 24))
                    ),
                    fill="#FFFFFF",
                )

    def _draw_chart(self, draw, obj, box, state):
        x0, y0, x1, y1 = box

        content = obj.content

        series = content["series"]

        chart_type = content["chart_type"]

        if chart_type in ("pie", "donut"):
            values = [
                max(0.0, float(value))
                for value in series[0].get("values", [])
            ]

            total = sum(values)

            if total <= 0:
                return

            colors = content.get(
                "colors",
                ["#F4B860", "#3E9C91", "#6688C9", "#B477C7"],
            )

            start = 0.0

            diameter = min(
                x1 - x0,
                y1 - y0,
            )

            box_size = (
                (x0 + x1 - diameter) // 2,
                (y0 + y1 - diameter) // 2,
                (x0 + x1 + diameter) // 2,
                (y0 + y1 + diameter) // 2,
            )

            for index, value in enumerate(values):
                end = start + 360.0 * value / total

                draw.pieslice(
                    box_size,
                    start=start,
                    end=end,
                    fill=colors[index % len(colors)],
                )

                start = end

            if chart_type == "donut":
                cx = (box_size[0] + box_size[2]) // 2
                cy = (box_size[1] + box_size[3]) // 2
                radius = int(diameter * 0.23)

                draw.ellipse(
                    (
                        cx - radius,
                        cy - radius,
                        cx + radius,
                        cy + radius,
                    ),
                    fill="#101827",
                )

        elif chart_type in ("bar", "line", "area"):
            all_values = [
                float(value)
                for item in series
                for value in item.get("values", [])
            ]

            if not all_values:
                return

            maximum = max(
                abs(value)
                for value in all_values
            ) or 1.0

            progress = max(
                0.0,
                min(1.0, float(state.get("draw_progress", 1.0))),
            )

            colors = content.get(
                "colors",
                ["#F4B860", "#3E9C91", "#6688C9"],
            )

            if chart_type == "bar":
                values = [
                    float(value)
                    for value in series[0].get("values", [])
                ]

                count = max(
                    1,
                    len(values),
                )

                visible_count = max(
                    1,
                    math.ceil(count * progress),
                )

                gap = 16
                bar_width = (
                    (x1 - x0 - gap * (count - 1))
                    / count
                )

                for index, value in enumerate(values[:visible_count]):
                    bar_height = (
                        (y1 - y0 - 40)
                        * value
                        / maximum
                    )

                    xx = x0 + index * (bar_width + gap)

                    draw.rounded_rectangle(
                        (
                            int(xx),
                            int(y1 - bar_height),
                            int(xx + bar_width),
                            y1,
                        ),
                        radius=8,
                        fill=colors[0],
                    )

            else:
                for series_index, item in enumerate(series):
                    values = [
                        float(value)
                        for value in item.get("values", [])
                    ]

                    if len(values) < 2:
                        continue

                    points = []

                    for index, value in enumerate(values):
                        px = x0 + (
                            (x1 - x0)
                            * index
                            / (len(values) - 1)
                        )

                        py = y1 - (
                            (y1 - y0 - 30)
                            * value
                            / maximum
                        )

                        points.append(
                            (int(px), int(py))
                        )

                    segment_count = max(
                        1,
                        math.ceil(
                            (len(points) - 1) * progress
                        ),
                    )

                    visible_points = points[
                        :segment_count + 1
                    ]

                    draw.line(
                        visible_points,
                        fill=colors[
                            series_index % len(colors)
                        ],
                        width=7,
                    )

                    for px, py in visible_points:
                        draw.ellipse(
                            (
                                px - 8,
                                py - 8,
                                px + 8,
                                py + 8,
                            ),
                            fill=colors[
                                series_index % len(colors)
                            ],
                        )

        if content.get("data_mode") == "illustrative":
            draw.rounded_rectangle(
                (x0, y1 - 45, x1, y1),
                radius=8,
                fill="#334155",
            )

            draw.text(
                (x0 + 12, y1 - 36),
                "ИЛЛЮСТРАТИВНЫЙ ПРИМЕР",
                font=_font(20),
                fill="#FFFFFF",
            )

    def _draw_graph(self, draw, obj, box, state):
        x0, y0, x1, y1 = box

        content = obj.content

        nodes = content["nodes"]
        edges = content.get("edges", [])

        if not nodes:
            return

        positions = {}

        layout = content.get(
            "layout",
            "left_to_right",
        )

        count = len(nodes)

        for index, node in enumerate(nodes):
            node_id = node["node_id"]

            if "x" in node and "y" in node:
                px = x0 + int(float(node["x"]) * (x1 - x0))
                py = y0 + int(float(node["y"]) * (y1 - y0))

            elif layout == "top_to_bottom":
                px = (x0 + x1) // 2
                py = y0 + int(
                    (index + 0.5) * (y1 - y0) / count
                )

            else:
                px = x0 + int(
                    (index + 0.5) * (x1 - x0) / count
                )
                py = (y0 + y1) // 2

            positions[node_id] = (px, py)

        progress = max(
            0.0,
            min(1.0, float(state.get("draw_progress", 1.0))),
        )

        visible_edges = math.ceil(
            len(edges) * progress
        )

        for edge in edges[:visible_edges]:
            start = positions.get(edge["from"])
            end = positions.get(edge["to"])

            if start and end:
                draw.line(
                    (*start, *end),
                    fill="#F4B860",
                    width=6,
                )

                angle = math.atan2(
                    end[1] - start[1],
                    end[0] - start[0],
                )

                radius = 20

                p1 = (
                    end[0] - radius * math.cos(angle - 0.5),
                    end[1] - radius * math.sin(angle - 0.5),
                )

                p2 = (
                    end[0] - radius * math.cos(angle + 0.5),
                    end[1] - radius * math.sin(angle + 0.5),
                )

                draw.polygon(
                    [end, p1, p2],
                    fill="#F4B860",
                )

        for node in nodes:
            px, py = positions[node["node_id"]]

            radius_x = int(
                content.get("node_width", 210) / 2
            )

            radius_y = int(
                content.get("node_height", 90) / 2
            )

            draw.rounded_rectangle(
                (
                    px - radius_x,
                    py - radius_y,
                    px + radius_x,
                    py + radius_y,
                ),
                radius=18,
                fill="#263449",
                outline="#F4B860",
                width=4,
            )

            label = str(
                node.get("label", node["node_id"])
            )

            font = _font(
                int(content.get("font_size", 23))
            )

            bounds = draw.textbbox(
                (0, 0),
                label,
                font=font,
            )

            text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]

            draw.text(
                (
                    px - text_width // 2,
                    py - text_height // 2,
                ),
                label,
                font=font,
                fill="#FFFFFF",
            )