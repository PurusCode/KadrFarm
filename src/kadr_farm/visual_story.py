"""Stage I: strict, executable contract for whole-video visual direction.

The LLM authors editorial structure (story arc, motifs, compositions, layers,
semantic speech anchors and typed actions). Deterministic engines execute it.
The contract deliberately contains no model-authored timestamps or pixel
coordinates. Wall-clock timing can only be resolved from measured WAV units.

This module is intentionally independent from the current production pipeline.
"""
from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from math import isfinite
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Zone = Literal["bg", "left", "center", "right", "top", "bottom", "full"]
Primitive = Literal[
    "image", "video", "bar_chart", "line_chart", "table", "node_graph",
    "formula", "counter", "callout", "typography",
]
Relation = Literal["extends", "contrasts", "resolves", "callbacks", "introduces"]
ObjectState = Literal["visible", "hidden", "emphasized", "dimmed", "collapsed", "expanded", "transformed", "revealed"]
_ID = r"^[a-z][a-z0-9_]{2,39}$"
_TEXT_PRIMITIVES = {"typography", "callout", "counter", "formula"}
_HEX = r"^#[0-9a-fA-F]{6}$"
_SHA256 = r"^[0-9a-fA-F]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StyleContract(StrictModel):
    palette: list[str] = Field(min_length=2, max_length=6)
    font_family: str = Field(min_length=2, max_length=60)
    tone: Literal["warm", "neutral", "dark"] = "warm"
    grain: bool = True
    caption_style: Literal["card", "bar"] = "card"

    @model_validator(mode="after")
    def validate_palette(self) -> StyleContract:
        if len(self.palette) != len(set(color.lower() for color in self.palette)):
            raise ValueError("palette colors must be unique")
        for color in self.palette:
            if not re.fullmatch(_HEX, color):
                raise ValueError(f"palette color must be #rrggbb: {color}")
        return self


class Motif(StrictModel):
    motif_id: str = Field(pattern=_ID)
    kind: Literal["object", "chart", "table", "graph", "formula", "counter", "photo_region"]
    description: str = Field(min_length=5, max_length=300)
    first_scene: str = Field(min_length=1, max_length=100)
    returns_in: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_returns(self) -> Motif:
        if len(self.returns_in) != len(set(self.returns_in)):
            raise ValueError("motif returns_in contains duplicate scenes")
        if self.first_scene in self.returns_in:
            raise ValueError("motif first_scene cannot also be in returns_in")
        return self


class ApprovedFact(StrictModel):
    """Trusted upstream fact; this registry must be assembled outside the LLM plan."""

    fact_id: str = Field(pattern=_ID)
    value: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=40)
    source_url: str = Field(min_length=10, max_length=2048)
    source_title: str = Field(min_length=3, max_length=300)
    status: Literal["verified", "editorial_estimate", "review_required"]
    verified_by_human: bool = False

    @model_validator(mode="after")
    def validate_fact(self) -> ApprovedFact:
        _parse_decimal(self.value, f"fact {self.fact_id}")
        _parse_unit(self.unit)
        try:
            parsed_url = urlsplit(self.source_url)
            hostname = parsed_url.hostname
        except ValueError as exc:
            raise ValueError("approved fact source_url must be an absolute HTTP(S) URL") from exc
        if (parsed_url.scheme not in {"http", "https"} or not hostname
                or any(char.isspace() for char in parsed_url.netloc)):
            raise ValueError("approved fact source_url must be an absolute HTTP(S) URL")
        if self.status == "verified" and not self.verified_by_human:
            raise ValueError("verified fact requires verified_by_human=true")
        if self.verified_by_human and self.status != "verified":
            raise ValueError("verified_by_human=true requires status=verified")
        return self


class DataProvenance(StrictModel):
    """LLM-side reference to an approved fact or a deterministic calculation."""

    value_id: str = Field(pattern=_ID)
    kind: Literal["literal", "derived"]
    value: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=40)
    fact_id: str | None = Field(default=None, pattern=_ID)
    formula: str | None = Field(default=None, max_length=512)
    inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_provenance_shape(self) -> DataProvenance:
        _parse_decimal(self.value, f"value {self.value_id}")
        _parse_unit(self.unit)
        if self.kind == "literal":
            if not self.fact_id or self.formula is not None or self.inputs:
                raise ValueError("literal requires fact_id and forbids formula/inputs")
        else:
            if self.fact_id is not None or not self.formula or not self.inputs:
                raise ValueError("derived requires formula/inputs and forbids fact_id")
            if len(self.inputs) != len(set(self.inputs)):
                raise ValueError("derived inputs must be unique")
            for input_id in self.inputs:
                if not re.fullmatch(_ID, input_id):
                    raise ValueError(f"invalid derived input id: {input_id}")
        return self


def _parse_decimal(value: str, label: str) -> Decimal:
    if len(value) > 64:
        raise ValueError(f"{label}: decimal string is too long")
    try:
        result = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise ValueError(f"{label}: value must be a decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{label}: NaN and Infinity are forbidden")
    if abs(result.as_tuple().exponent) > 50 or len(result.as_tuple().digits) > 50:
        raise ValueError(f"{label}: decimal precision/exponent exceeds the contract limit")
    return result


def _parse_unit(unit: str) -> dict[str, int]:
    """Parse a deliberately small unit grammar: RUB, month, RUB/month, RUB*person."""
    if unit == "1":
        return {}
    token = r"[A-Za-z][A-Za-z0-9_]*(?:\^-?\d+)?"
    if not re.fullmatch(token + rf"(?:[*/]{token})*", unit):
        raise ValueError(f"unsupported unit expression: {unit}")
    parts = re.split(r"([*/])", unit)
    dimensions: dict[str, int] = {}
    sign = 1
    for part in parts:
        if part == "*":
            sign = 1
            continue
        if part == "/":
            sign = -1
            continue
        name, sep, power_text = part.partition("^")
        power = int(power_text) if sep else 1
        if abs(power) > 8:
            raise ValueError(f"unit exponent is out of range: {unit}")
        dimensions[name] = dimensions.get(name, 0) + sign * power
        if dimensions[name] == 0:
            del dimensions[name]
    return dimensions


def _format_unit(dimensions: dict[str, int]) -> str:
    if not dimensions:
        return "1"
    numerator: list[str] = []
    denominator: list[str] = []
    for name in sorted(dimensions):
        power = dimensions[name]
        target = numerator if power > 0 else denominator
        magnitude = abs(power)
        target.append(name if magnitude == 1 else f"{name}^{magnitude}")
    left = "*".join(numerator) if numerator else "1"
    return left if not denominator else left + "/" + "*".join(denominator)


@dataclass(frozen=True)
class _Quantity:
    value: Decimal
    dimensions: dict[str, int]


def _combine_dimensions(left: dict[str, int], right: dict[str, int], sign: int) -> dict[str, int]:
    result = dict(left)
    for name, power in right.items():
        result[name] = result.get(name, 0) + sign * power
        if result[name] == 0:
            del result[name]
    return result


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _formula_tree(formula: str) -> ast.Expression:
    if not formula or len(formula) > 512:
        raise ValueError("formula must contain 1..512 characters")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid formula syntax: {exc.msg}") from exc
    try:
        nodes = list(ast.walk(tree))
    except RecursionError as exc:
        raise ValueError("formula nesting is too deep") from exc
    if len(nodes) > 100:
        raise ValueError("formula is too complex")
    return tree


def _formula_names(formula: str) -> set[str]:
    tree = _formula_tree(formula)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _eval_quantity(formula: str, registry: dict[str, _Quantity]) -> _Quantity:
    tree = _formula_tree(formula)

    def walk(node: ast.AST, depth: int = 0) -> _Quantity:
        if depth > 20:
            raise ValueError("formula nesting is too deep")
        if isinstance(node, ast.Expression):
            return walk(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("only numeric constants are allowed in formulas")
            raw = ast.get_source_segment(formula, node)
            if raw is None:
                raise ValueError("cannot read numeric constant from formula")
            return _Quantity(_parse_decimal(raw, "formula constant"), {})
        if isinstance(node, ast.Name):
            if node.id not in registry:
                raise ValueError(f"unknown value ref in formula: {node.id}")
            return registry[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = walk(node.operand, depth + 1)
            return _Quantity(-operand.value if isinstance(node.op, ast.USub) else operand.value,
                             dict(operand.dimensions))
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left = walk(node.left, depth + 1)
            right = walk(node.right, depth + 1)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if left.dimensions != right.dimensions:
                    raise ValueError(
                        "addition/subtraction requires compatible units: "
                        f"{_format_unit(left.dimensions)} vs {_format_unit(right.dimensions)}"
                    )
                result_unit = dict(left.dimensions)
            elif isinstance(node.op, ast.Mult):
                result_unit = _combine_dimensions(left.dimensions, right.dimensions, 1)
            else:
                result_unit = _combine_dimensions(left.dimensions, right.dimensions, -1)
            try:
                value = _BIN_OPS[type(node.op)](left.value, right.value)
            except (DecimalException, ZeroDivisionError) as exc:
                raise ValueError("invalid arithmetic in formula (division by zero or decimal overflow)") from exc
            if not value.is_finite():
                raise ValueError("formula produced a non-finite value")
            _parse_decimal(str(value), "formula result")
            return _Quantity(value, result_unit)
        raise ValueError(f"unsupported formula node: {type(node).__name__}")

    with localcontext() as ctx:
        ctx.prec = 100
        return walk(tree)


def eval_formula(formula: str, registry: dict[str, Decimal]) -> Decimal:
    """Evaluate a safe scalar-only arithmetic expression (no Python execution)."""
    quantities: dict[str, _Quantity] = {}
    for key, value in registry.items():
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"formula input {key} must be a finite Decimal")
        quantities[key] = _Quantity(value, {})
    return _eval_quantity(formula, quantities).value


@dataclass(frozen=True)
class ResolvedValue:
    value: Decimal
    unit: str
    fact_ids: tuple[str, ...]
    source_urls: tuple[str, ...]
    status: str


def resolve_provenance(
    registry: dict[str, DataProvenance],
    facts: dict[str, ApprovedFact],
) -> dict[str, ResolvedValue]:
    """Resolve LLM-selected references against an externally supplied fact registry."""
    resolved: dict[str, ResolvedValue] = {}
    visiting: set[str] = set()

    def value_of(value_id: str) -> ResolvedValue:
        if value_id in resolved:
            return resolved[value_id]
        if value_id in visiting:
            raise ValueError(f"cyclic provenance dependency at {value_id}")
        provenance = registry.get(value_id)
        if provenance is None:
            raise ValueError(f"missing provenance for {value_id}")
        visiting.add(value_id)
        try:
            declared_value = _parse_decimal(provenance.value, f"value {value_id}")
            declared_dimensions = _parse_unit(provenance.unit)
            if provenance.kind == "literal":
                fact = facts.get(provenance.fact_id or "")
                if fact is None:
                    raise ValueError(f"missing approved fact {provenance.fact_id!r} for {value_id}")
                fact_value = _parse_decimal(fact.value, f"fact {fact.fact_id}")
                fact_dimensions = _parse_unit(fact.unit)
                if fact_value != declared_value or fact_dimensions != declared_dimensions:
                    raise ValueError(f"literal {value_id} does not exactly match approved fact {fact.fact_id}")
                result = ResolvedValue(
                    fact_value, _format_unit(fact_dimensions), (fact.fact_id,),
                    (fact.source_url,), fact.status,
                )
            else:
                assert provenance.formula is not None
                formula_refs = _formula_names(provenance.formula)
                if formula_refs != set(provenance.inputs):
                    raise ValueError(
                        f"derived value {value_id}: formula references {sorted(formula_refs)} "
                        f"do not match declared inputs {sorted(provenance.inputs)}"
                    )
                input_values = {ref: value_of(ref) for ref in provenance.inputs}
                quantity_registry = {
                    ref: _Quantity(item.value, _parse_unit(item.unit))
                    for ref, item in input_values.items()
                }
                quantity = _eval_quantity(provenance.formula, quantity_registry)
                if quantity.value != declared_value:
                    raise ValueError(
                        f"derived value {value_id} does not recompute exactly: "
                        f"expected {declared_value}, got {quantity.value}"
                    )
                if quantity.dimensions != declared_dimensions:
                    raise ValueError(
                        f"derived value {value_id} unit mismatch: calculated "
                        f"{_format_unit(quantity.dimensions)}, declared {provenance.unit}"
                    )
                fact_ids = tuple(sorted({fact_id for item in input_values.values()
                                         for fact_id in item.fact_ids}))
                source_urls = tuple(sorted({source_url for item in input_values.values()
                                            for source_url in item.source_urls}))
                statuses = {item.status for item in input_values.values()}
                if "review_required" in statuses:
                    status = "review_required"
                elif "editorial_estimate" in statuses:
                    status = "editorial_estimate"
                elif statuses <= {"verified", "derived_verified"}:
                    status = "derived_verified"
                else:
                    status = "derived"
                result = ResolvedValue(quantity.value, _format_unit(quantity.dimensions),
                                       fact_ids, source_urls, status)
            resolved[value_id] = result
            return result
        finally:
            visiting.remove(value_id)

    for fact_key, fact in facts.items():
        if fact_key != fact.fact_id:
            raise ValueError(f"approved fact registry key {fact_key!r} does not match fact_id {fact.fact_id!r}")
    for key, item in registry.items():
        if key != item.value_id:
            raise ValueError(f"provenance registry key {key!r} does not match value_id {item.value_id!r}")
        value_of(key)
    return resolved


class SpeechSegment(StrictModel):
    """One exact text span that was rendered as a separate TTS WAV unit.

    duration_sec and wav_sha256 must come from that actual WAV. A punctuation
    split alone is not timing evidence and cannot be scheduled in wall-clock time.
    """

    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    duration_sec: FiniteFloat | None = Field(default=None, gt=0)
    wav_sha256: str | None = None

    @model_validator(mode="after")
    def validate_span_and_measurement(self) -> SpeechSegment:
        if self.char_end <= self.char_start:
            raise ValueError("speech segment char_end must exceed char_start")
        if len(self.text) != self.char_end - self.char_start:
            raise ValueError("speech segment text length must equal its character range")
        if self.duration_sec is None and self.wav_sha256 is not None:
            raise ValueError("wav_sha256 cannot be supplied without measured duration_sec")
        if self.duration_sec is not None and not re.fullmatch(_SHA256, self.wav_sha256 or ""):
            raise ValueError("measured speech segment requires a 64-character WAV SHA-256")
        return self


def _is_sentence_boundary(text: str, punctuation_index: int) -> bool:
    char = text[punctuation_index]
    if punctuation_index + 1 < len(text) and not text[punctuation_index + 1].isspace():
        # Decimal points (e.g. 3.14) are not sentence boundaries.
        if char == "." and text[punctuation_index - 1:punctuation_index].isdigit() and text[punctuation_index + 1].isdigit():
            return False
        # Closing quotes/brackets can follow sentence punctuation.
        suffix = text[punctuation_index + 1:]
        stripped = suffix.lstrip("»”\"'’)]}")
        if suffix == stripped:
            return False
        if stripped and not stripped[0].isspace():
            return False
    if char == ".":
        prefix = text[:punctuation_index]
        match = re.search(r"([A-Za-zА-Яа-яЁё]+(?:\.[A-Za-zА-Яа-яЁё]+)*)$", prefix)
        token = match.group(1).lower() if match else ""
        non_terminal_abbreviations = {
            "т", "т.е", "т.д", "т.п", "и т.п", "г", "ул", "д", "стр",
            "рис", "см", "ср", "им", "др", "проф", "акад", "кв", "обл",
        }
        measurement_abbreviations = {"руб", "коп", "млн", "млрд", "тыс"}
        if token in non_terminal_abbreviations or (len(token) == 1 and token.isalpha()):
            return False
        if token in measurement_abbreviations:
            following = text[punctuation_index + 1:].lstrip()
            # A capitalized next word is a useful heuristic for an actual
            # sentence boundary after an amount ("120 руб. Далее ...").
            if not following or not following[0].isupper():
                return False
    return punctuation_index + 1 == len(text) or text[punctuation_index + 1].isspace() or any(
        c in "»”\"'’)]}" for c in text[punctuation_index + 1:punctuation_index + 3]
    )


def split_speech_segments(text: str) -> list[SpeechSegment]:
    """Create deterministic text spans; this does not claim measured speech timing.

    The returned segments are candidate TTS units. A later audio stage must render
    each unit separately and attach its measured WAV duration/hash before the
    action schedule can convert anchors to seconds.
    """
    if not text:
        return []
    segments: list[SpeechSegment] = []
    start = 0
    i = 0
    while i < len(text):
        if text[i] in ".!?…" and _is_sentence_boundary(text, i):
            end = i + 1
            while end < len(text) and text[end] in "»”\"'’)]}":
                end += 1
            while end < len(text) and text[end].isspace():
                end += 1
            segments.append(SpeechSegment(text=text[start:end], char_start=start, char_end=end))
            start = end
            i = end
        else:
            i += 1
    if start < len(text):
        segments.append(SpeechSegment(text=text[start:], char_start=start, char_end=len(text)))
    return segments


class SpeechAnchor(StrictModel):
    beat_id: str = Field(min_length=1, max_length=100)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> SpeechAnchor:
        if self.char_end <= self.char_start:
            raise ValueError("anchor char_end must exceed char_start")
        return self


def _validate_segment_coverage(beat_id: str, text: str,
                               segments: list[SpeechSegment]) -> None:
    if not segments:
        raise ValueError(f"beat {beat_id}: no speech timing/text segments")
    cursor = 0
    for index, segment in enumerate(segments, start=1):
        if segment.char_start != cursor:
            raise ValueError(f"beat {beat_id}: segment {index} leaves a gap or overlaps")
        if segment.char_end > len(text) or text[segment.char_start:segment.char_end] != segment.text:
            raise ValueError(f"beat {beat_id}: segment {index} does not match source text")
        cursor = segment.char_end
    if cursor != len(text):
        raise ValueError(f"beat {beat_id}: segments do not cover the entire beat text")


def snap_anchor(beat_id: str, char_start: int, char_end: int,
                segments: list[SpeechSegment]) -> SpeechAnchor:
    """Snap a rough character span outward to complete measured/text segment units."""
    if char_start < 0 or char_end <= char_start:
        raise ValueError("rough anchor must have a non-empty, non-negative range")
    first_index = next((i for i, seg in enumerate(segments)
                        if seg.char_start <= char_start < seg.char_end), None)
    last_index = next((i for i, seg in enumerate(segments)
                       if seg.char_start < char_end <= seg.char_end), None)
    if first_index is None or last_index is None or first_index > last_index:
        raise ValueError("rough anchor is outside the supplied speech segments")
    return SpeechAnchor(beat_id=beat_id,
                        char_start=segments[first_index].char_start,
                        char_end=segments[last_index].char_end)


class AppearParams(StrictModel):
    kind: Literal["appear"] = "appear"


class HighlightParams(StrictModel):
    kind: Literal["highlight"] = "highlight"
    color: str = Field(pattern=_HEX)
    scrim: bool = False


class UpdateValueParams(StrictModel):
    kind: Literal["update_value"] = "update_value"
    value_id: str = Field(pattern=_ID)


class RevealRowParams(StrictModel):
    kind: Literal["reveal_row"] = "reveal_row"
    row_index: int = Field(ge=0)


class DrawEdgeParams(StrictModel):
    kind: Literal["draw_edge"] = "draw_edge"
    from_id: str = Field(min_length=1, max_length=100)
    to_id: str = Field(min_length=1, max_length=100)


class MoveParams(StrictModel):
    kind: Literal["move"] = "move"
    zone: Zone


class FadeOutParams(StrictModel):
    kind: Literal["fade_out"] = "fade_out"


class CountUpParams(StrictModel):
    kind: Literal["count_up"] = "count_up"
    from_id: str = Field(pattern=_ID)
    to_id: str = Field(pattern=_ID)


class TransformParams(StrictModel):
    kind: Literal["transform"] = "transform"
    to_primitive: Primitive


ActionParams = Annotated[
    Union[
        AppearParams, HighlightParams, UpdateValueParams, RevealRowParams,
        DrawEdgeParams, MoveParams, FadeOutParams, CountUpParams, TransformParams,
    ],
    Field(discriminator="kind"),
]


class Action(StrictModel):
    action_id: str = Field(pattern=_ID)
    kind: Literal[
        "appear", "highlight", "update_value", "reveal_row", "draw_edge",
        "move", "fade_out", "count_up", "transform",
    ]
    target: str = Field(pattern=_ID)
    anchor: SpeechAnchor
    params: ActionParams

    @model_validator(mode="after")
    def validate_kind_match(self) -> Action:
        if self.kind != self.params.kind:
            raise ValueError("action.kind must equal params.kind")
        return self


class EdgeRef(StrictModel):
    from_id: str = Field(min_length=1, max_length=100)
    to_id: str = Field(min_length=1, max_length=100)


class ValueQualification(StrictModel):
    """Mandatory visible qualifier for values sourced from editorial estimates."""

    kind: Literal["estimate", "approximate"]
    text: str = Field(min_length=3, max_length=100)


class LayerObject(StrictModel):
    object_id: str = Field(pattern=_ID)
    motif_id: str | None = Field(default=None, pattern=_ID)
    zone: Zone
    primitive: Primitive
    z_order: int = Field(ge=0)
    overlap_policy: Literal["opaque", "scrim", "transparent"] = "opaque"
    value_ids: list[str] = Field(default_factory=list)
    value_qualifications: dict[str, ValueQualification] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list, max_length=12)
    edges: list[EdgeRef] = Field(default_factory=list)
    row_count: int | None = Field(default=None, ge=1)
    actions: list[Action] = Field(default_factory=list)

    @property
    def is_text(self) -> bool:
        return self.primitive in _TEXT_PRIMITIVES

    @model_validator(mode="after")
    def validate_layer_contract(self) -> LayerObject:
        if len(self.value_ids) != len(set(self.value_ids)):
            raise ValueError("value_ids must be unique")
        if any(not re.fullmatch(_ID, value_id) for value_id in self.value_ids):
            raise ValueError("value_ids must use valid ids")
        if not set(self.value_qualifications) <= set(self.value_ids):
            raise ValueError("value_qualifications may only reference value_ids on the same layer")
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("labels must be unique within a layer")
        if any(not label.strip() for label in self.labels):
            raise ValueError("labels cannot be blank")
        if self.edges and self.primitive != "node_graph":
            raise ValueError("edges are only valid for node_graph primitives")
        for edge in self.edges:
            if edge.from_id not in self.labels or edge.to_id not in self.labels:
                raise ValueError("edges must reference labels of the same object")
        if self.primitive == "table" and self.row_count is None and any(
            action.kind == "reveal_row" for action in self.actions
        ):
            raise ValueError("table with reveal_row actions requires row_count")
        if self.primitive != "table" and self.row_count is not None:
            raise ValueError("row_count is only valid for table primitives")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("duplicate action_id within layer")
        for action in self.actions:
            if action.target != self.object_id:
                raise ValueError("action.target must belong to its layer")
            if action.kind == "draw_edge":
                params = action.params
                assert isinstance(params, DrawEdgeParams)
                if EdgeRef(from_id=params.from_id, to_id=params.to_id) not in self.edges:
                    raise ValueError("draw_edge action must reference a declared layer edge")
            elif action.kind == "reveal_row":
                params = action.params
                assert isinstance(params, RevealRowParams)
                if self.primitive != "table" or self.row_count is None or params.row_index >= self.row_count:
                    raise ValueError("reveal_row must reference an existing table row")
            elif action.kind == "count_up" and self.primitive != "counter":
                raise ValueError("count_up action requires a counter primitive")
            elif action.kind == "update_value" and self.primitive not in {
                "counter", "formula", "bar_chart", "line_chart", "table"
            }:
                raise ValueError("update_value is incompatible with this primitive")
        return self


class Composition(StrictModel):
    composition_id: str = Field(pattern=_ID)
    scene_id: str = Field(min_length=1, max_length=100)
    develops: str | None = Field(default=None, pattern=_ID)
    relation: Relation
    layers: list[LayerObject] = Field(min_length=1)
    entry_state: dict[str, ObjectState] = Field(default_factory=dict)
    exit_state: dict[str, ObjectState] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_composition(self) -> Composition:
        if (self.relation == "introduces") != (self.develops is None):
            raise ValueError("introduces requires develops=None; other relations require an earlier composition")
        ids = [layer.object_id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate object_id in composition")
        z_orders = [layer.z_order for layer in self.layers]
        if len(z_orders) != len(set(z_orders)):
            raise ValueError("z_order must be unique within composition")
        backgrounds = [layer for layer in self.layers if layer.zone == "bg"]
        if len(backgrounds) > 1:
            raise ValueError("at most one bg layer")
        if backgrounds and (backgrounds[0].z_order != 0 or backgrounds[0].primitive not in ("image", "video")):
            raise ValueError("bg layer must be z_order 0 and image/video")
        layer_ids = set(ids)
        if not set(self.entry_state) <= layer_ids:
            raise ValueError("entry_state may only describe objects present in this composition")
        if not set(self.exit_state) <= layer_ids:
            raise ValueError("exit_state may only describe objects present in this composition")
        action_ids = [action.action_id for layer in self.layers for action in layer.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("duplicate action_id in composition")
        layers_by_id = {layer.object_id: layer for layer in self.layers}
        for text_layer in (layer for layer in self.layers if layer.is_text):
            for above in self.layers:
                if above.z_order <= text_layer.z_order or above.zone == "bg":
                    continue
                if _zones_overlap(text_layer.zone, above.zone) and above.overlap_policy == "opaque":
                    raise ValueError(
                        f"text object {text_layer.object_id} may be obscured by opaque layer {above.object_id}"
                    )
        for layer in self.layers:
            for action in layer.actions:
                if action.target not in layers_by_id:
                    raise ValueError("action target is missing from composition")
        return self


def _zone_rect(zone: Zone) -> tuple[float, float, float, float]:
    # Coarse editorial zones, not renderer coordinates. Exact collision QC belongs to Stage II.
    return {
        "bg": (0.0, 0.0, 1.0, 1.0),
        "left": (0.0, 0.0, 0.46, 1.0),
        "center": (0.27, 0.0, 0.73, 1.0),
        "right": (0.54, 0.0, 1.0, 1.0),
        "top": (0.0, 0.0, 1.0, 0.46),
        "bottom": (0.0, 0.54, 1.0, 1.0),
        "full": (0.0, 0.0, 1.0, 1.0),
    }[zone]


def _zones_overlap(left: Zone, right: Zone) -> bool:
    lx1, ly1, lx2, ly2 = _zone_rect(left)
    rx1, ry1, rx2, ry2 = _zone_rect(right)
    return min(lx2, rx2) > max(lx1, rx1) and min(ly2, ry2) > max(ly1, ry1)


class SceneDirection(StrictModel):
    """Scene-level intent produced by the whole-video directing pass."""

    scene_id: str = Field(min_length=1, max_length=100)
    narrative_role: Literal[
        "hook", "setup", "context", "development", "turn", "reveal",
        "consequence", "resolution", "transition", "call_to_action",
    ]
    purpose: str = Field(min_length=5, max_length=200)
    visual_intent: str = Field(min_length=10, max_length=500)
    relation_to_previous: Relation
    develops_scene_id: str | None = None
    motif_ids: list[str] = Field(default_factory=list)
    link_prev: str | None = None
    link_next: str | None = None

    @model_validator(mode="after")
    def validate_direction_shape(self) -> SceneDirection:
        if len(self.motif_ids) != len(set(self.motif_ids)):
            raise ValueError("scene motif_ids must be unique")
        if (self.relation_to_previous == "introduces") != (self.develops_scene_id is None):
            raise ValueError("introduces scene requires develops_scene_id=None; other relations require a prior scene")
        return self


class VisualStoryPlan(StrictModel):
    """Pass 1: whole-video directing decisions, before any scene composition plans."""

    schema_version: Literal["1.1"] = "1.1"
    visual_thesis: str = Field(min_length=20, max_length=500)
    arc_summary: str = Field(min_length=30, max_length=1200)
    style: StyleContract
    arc: list[str] = Field(min_length=1)
    motifs: list[Motif] = Field(default_factory=list)
    scenes: list[SceneDirection] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_global_structure(self) -> VisualStoryPlan:
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("duplicate scene_id")
        if len(self.arc) != len(set(self.arc)) or not set(self.arc) <= set(scene_ids):
            raise ValueError("arc must contain unique, known scene ids")
        scene_order = {scene_id: index for index, scene_id in enumerate(scene_ids)}
        arc_order = [scene_order[scene_id] for scene_id in self.arc]
        if self.arc[0] != scene_ids[0] or self.arc[-1] != scene_ids[-1] or arc_order != sorted(arc_order):
            raise ValueError("arc must be an ordered subsequence starting at the first and ending at the last scene")
        for index, scene in enumerate(self.scenes):
            expected_prev = scene_ids[index - 1] if index else None
            expected_next = scene_ids[index + 1] if index + 1 < len(scene_ids) else None
            if scene.link_prev != expected_prev:
                raise ValueError("link_prev must reference the immediately previous scene")
            if scene.link_next != expected_next:
                raise ValueError("link_next must reference the immediately next scene")
            if index == 0 and scene.relation_to_previous != "introduces":
                raise ValueError("first scene must introduce the visual story")
            if scene.develops_scene_id is not None:
                target_index = scene_order.get(scene.develops_scene_id)
                if target_index is None or target_index >= index:
                    raise ValueError("develops_scene_id must reference an earlier scene")
        motif_ids = [motif.motif_id for motif in self.motifs]
        if len(motif_ids) != len(set(motif_ids)):
            raise ValueError("duplicate motif_id")
        motif_registry = {motif.motif_id: motif for motif in self.motifs}
        for scene in self.scenes:
            unknown = [motif_id for motif_id in scene.motif_ids if motif_id not in motif_registry]
            if unknown:
                raise ValueError(f"scene {scene.scene_id} references unknown motifs: {unknown}")
        for motif in self.motifs:
            if motif.first_scene not in scene_order:
                raise ValueError(f"motif {motif.motif_id} has unknown first_scene")
            declared_scenes = [motif.first_scene, *motif.returns_in]
            if any(scene_id not in scene_order for scene_id in declared_scenes):
                raise ValueError(f"motif {motif.motif_id} references an unknown return scene")
            indexes = [scene_order[scene_id] for scene_id in declared_scenes]
            if indexes != sorted(indexes):
                raise ValueError(f"motif {motif.motif_id} returns_in must follow story order")
            actual_scenes = [scene.scene_id for scene in self.scenes if motif.motif_id in scene.motif_ids]
            if not actual_scenes or actual_scenes[0] != motif.first_scene:
                raise ValueError(f"motif {motif.motif_id} must be assigned to its first_scene")
            if actual_scenes != declared_scenes:
                raise ValueError(
                    f"motif {motif.motif_id} declared appearances {declared_scenes} "
                    f"do not match scene-direction assignments {actual_scenes}"
                )
        return self


class SceneCompositionPlan(StrictModel):
    """Pass 2: executable composition plan for one scene, informed by VisualStoryPlan."""

    schema_version: Literal["1.0"] = "1.0"
    scene_id: str = Field(min_length=1, max_length=100)
    compositions: list[Composition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scene_compositions(self) -> SceneCompositionPlan:
        ids = [composition.composition_id for composition in self.compositions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate composition_id within scene plan")
        for composition in self.compositions:
            if composition.scene_id != self.scene_id:
                raise ValueError(
                    f"composition {composition.composition_id} scene_id does not match SceneCompositionPlan"
                )
        return self


def validate_plan(
    plan: VisualStoryPlan,
    scene_plans: list[SceneCompositionPlan],
    *,
    expected_scene_ids: list[str],
    beat_texts: dict[str, str],
    beat_order: list[str],
    segments: dict[str, list[SpeechSegment]],
    scene_beat_ids: dict[str, list[str]],
    provenance: dict[str, DataProvenance],
    facts: dict[str, ApprovedFact],
    minimum_developing_share: float = 0.5,
    require_recurring_motif: bool = True,
) -> list[str]:
    """Validate pass 1 and pass 2 against authoritative storyboard/speech/fact inputs."""
    errors: list[str] = []
    source_ids = list(expected_scene_ids)
    direction_ids = [scene.scene_id for scene in plan.scenes]
    composition_scene_ids = [scene_plan.scene_id for scene_plan in scene_plans]
    if len(source_ids) != len(set(source_ids)):
        errors.append("expected_scene_ids contains duplicates")
    if direction_ids != source_ids:
        missing = [scene_id for scene_id in source_ids if scene_id not in direction_ids]
        extra = [scene_id for scene_id in direction_ids if scene_id not in source_ids]
        errors.append(
            f"whole-video scene coverage/order mismatch: expected={source_ids}, actual={direction_ids}, "
            f"missing={missing}, extra={extra}"
        )
    if composition_scene_ids != source_ids:
        missing = [scene_id for scene_id in source_ids if scene_id not in composition_scene_ids]
        extra = [scene_id for scene_id in composition_scene_ids if scene_id not in source_ids]
        errors.append(
            f"scene-composition coverage/order mismatch: expected={source_ids}, "
            f"actual={composition_scene_ids}, missing={missing}, extra={extra}"
        )
    direction_by_id = {scene.scene_id: scene for scene in plan.scenes}
    composition_by_scene = {scene_plan.scene_id: scene_plan for scene_plan in scene_plans}
    if not 0.0 <= minimum_developing_share <= 1.0 or not isfinite(minimum_developing_share):
        errors.append("minimum_developing_share must be a finite value between 0 and 1")
    if set(beat_texts) != set(beat_order) or len(beat_order) != len(set(beat_order)):
        errors.append("beat_order must contain every beat_text key exactly once")
    if set(segments) != set(beat_texts):
        errors.append("segments keys must exactly match beat_texts keys")
    for beat_id, text in beat_texts.items():
        if beat_id in segments:
            try:
                _validate_segment_coverage(beat_id, text, segments[beat_id])
            except ValueError as exc:
                errors.append(f"speech segmentation: {exc}")
    if set(scene_beat_ids) != set(source_ids):
        errors.append("scene_beat_ids must map every expected scene exactly once")
    else:
        flattened_scene_beats: list[str] = []
        for scene_id in source_ids:
            beat_ids = scene_beat_ids[scene_id]
            if not beat_ids or len(beat_ids) != len(set(beat_ids)):
                errors.append(f"scene {scene_id}: beat ids must be non-empty and unique")
            unknown = [beat_id for beat_id in beat_ids if beat_id not in beat_texts]
            if unknown:
                errors.append(f"scene {scene_id}: unknown beat ids {unknown}")
            flattened_scene_beats.extend(beat_ids)
        if flattened_scene_beats != beat_order:
            errors.append("scene_beat_ids must cover beat_order exactly in storyboard order")
    for fact_key, fact in facts.items():
        if fact_key != fact.fact_id:
            errors.append(f"approved fact registry key {fact_key!r} does not match fact_id {fact.fact_id!r}")
    try:
        resolved = resolve_provenance(provenance, facts)
    except ValueError as exc:
        errors.append(f"provenance: {exc}")
        resolved = {}
    beat_positions = {beat_id: index for index, beat_id in enumerate(beat_order)}
    compositions = [composition for scene_plan in scene_plans for composition in scene_plan.compositions]
    composition_ids = [composition.composition_id for composition in compositions]
    if len(composition_ids) != len(set(composition_ids)):
        errors.append("duplicate composition_id across scene plans")
    composition_positions = {composition_id: index for index, composition_id in enumerate(composition_ids)}
    composition_registry = {composition.composition_id: composition for composition in compositions}
    composition_scene_order = {
        composition.composition_id: source_ids.index(composition.scene_id)
        for composition in compositions if composition.scene_id in source_ids
    }
    motif_registry = {motif.motif_id: motif for motif in plan.motifs}
    for scene_plan in scene_plans:
        direction = direction_by_id.get(scene_plan.scene_id)
        if direction is None:
            continue
        actual_motifs = {
            layer.motif_id
            for composition in scene_plan.compositions
            for layer in composition.layers
            if layer.motif_id is not None
        }
        expected_motifs = set(direction.motif_ids)
        if actual_motifs != expected_motifs:
            errors.append(
                f"scene {scene_plan.scene_id}: scene composition motif usage "
                f"{sorted(actual_motifs)} does not match whole-video direction {sorted(expected_motifs)}"
            )
        for composition in scene_plan.compositions:
            for layer in composition.layers:
                if layer.motif_id is not None and layer.motif_id in motif_registry:
                    motif_kind = motif_registry[layer.motif_id].kind
                    allowed = {
                        "chart": {"bar_chart", "line_chart"},
                        "table": {"table"},
                        "graph": {"node_graph"},
                        "formula": {"formula"},
                        "counter": {"counter"},
                        "photo_region": {"image", "video"},
                        "object": {"image", "video", "bar_chart", "line_chart", "table",
                                   "node_graph", "formula", "counter", "callout", "typography"},
                    }[motif_kind]
                    if layer.primitive not in allowed:
                        errors.append(
                            f"{scene_plan.scene_id}/{layer.object_id}: motif {layer.motif_id} "
                            f"kind {motif_kind} is incompatible with primitive {layer.primitive}"
                        )
            for composition in scene_plan.compositions:
                if composition.scene_id != scene_plan.scene_id:
                    errors.append(
                        f"{composition.composition_id}: composition scene_id does not match scene plan"
                    )
    for composition in compositions:
        if composition.develops is None:
            continue
        previous = composition_registry.get(composition.develops)
        if previous is None:
            errors.append(f"{composition.composition_id}: develops references missing composition {composition.develops}")
            continue
        if composition_positions.get(composition.develops, len(compositions)) >= composition_positions.get(composition.composition_id, -1):
            errors.append(f"{composition.composition_id}: develops must reference an earlier composition")
            continue
        if (composition_scene_order.get(composition.develops, -1)
                > composition_scene_order.get(composition.composition_id, -1)):
            errors.append(f"{composition.composition_id}: develops cannot reference a later scene")
            continue
        previous_exit = previous.exit_state
        current_entry = composition.entry_state
        for object_id, state in current_entry.items():
            if object_id not in previous_exit:
                errors.append(
                    f"{composition.composition_id}: entry object {object_id} was not in developed exit_state"
                )
            elif previous_exit[object_id] != state:
                errors.append(
                    f"{composition.composition_id}: entry state for {object_id} "
                    f"does not match {composition.develops} exit_state"
                )
        if composition.relation in ("extends", "callbacks"):
            shared = set(previous_exit) & {layer.object_id for layer in composition.layers}
            if not shared:
                errors.append(
                    f"{composition.composition_id}: {composition.relation} must carry at least one prior object"
                )
            for object_id in shared:
                if composition.entry_state.get(object_id) != previous_exit[object_id]:
                    errors.append(
                        f"{composition.composition_id}: entry_state loses or changes carried object {object_id}"
                    )
    for scene_index, scene_plan in enumerate(scene_plans[1:], start=1):
        earlier_ids = {
            composition.composition_id
            for previous_scene_plan in scene_plans[:scene_index]
            for composition in previous_scene_plan.compositions
        }
        if not any(composition.develops in earlier_ids for composition in scene_plan.compositions):
            errors.append(f"scene {scene_plan.scene_id}: no composition develops an earlier scene")
    developed_count = sum(composition.develops is not None for composition in compositions)
    developing_share = developed_count / len(compositions) if compositions else 0.0
    if len(compositions) > 1 and developing_share < minimum_developing_share:
        errors.append(
            "plan is insufficiently woven: developing composition share "
            f"{developing_share:.3f} is below {minimum_developing_share:.3f}"
        )
    direction_developing = sum(scene.develops_scene_id is not None for scene in plan.scenes)
    direction_share = direction_developing / len(plan.scenes) if plan.scenes else 0.0
    if len(plan.scenes) > 1 and direction_share < minimum_developing_share:
        errors.append(
            "whole-video direction is insufficiently connected: developing scene share "
            f"{direction_share:.3f} is below {minimum_developing_share:.3f}"
        )
    if require_recurring_motif and len(plan.scenes) >= 3 and not any(motif.returns_in for motif in plan.motifs):
        errors.append("multi-scene plan requires at least one genuinely recurring motif")
    for scene in plan.scenes:
        scene_beats = set(scene_beat_ids.get(scene.scene_id, []))
        for scene_plan in [item for item in scene_plans if item.scene_id == scene.scene_id]:
            for composition in scene_plan.compositions:
                for layer in composition.layers:
                    for value_id in layer.value_ids:
                        resolved_value = resolved.get(value_id)
                        if resolved_value is None:
                            errors.append(f"{composition.composition_id}: unknown value_id {value_id}")
                            continue
                        qualification = layer.value_qualifications.get(value_id)
                        if resolved_value.status == "review_required":
                            errors.append(
                                f"{composition.composition_id}/{layer.object_id}: value {value_id} "
                                "is blocked until its source is human-reviewed"
                            )
                        elif resolved_value.status == "editorial_estimate" and qualification is None:
                            errors.append(
                                f"{composition.composition_id}/{layer.object_id}: estimated value {value_id} "
                                "requires a visible value_qualification"
                            )
                        elif resolved_value.status in {"verified", "derived_verified"} and qualification is not None:
                            errors.append(
                                f"{composition.composition_id}/{layer.object_id}: verified value {value_id} "
                                "must not be marked as an estimate"
                            )
                    for action in layer.actions:
                        params = action.params
                        referenced_values: list[str] = []
                        if isinstance(params, UpdateValueParams):
                            referenced_values.append(params.value_id)
                            if params.value_id not in layer.value_ids:
                                errors.append(
                                    f"{composition.composition_id}/{layer.object_id}: update_value "
                                    f"{params.value_id} is not declared in layer.value_ids"
                                )
                        elif isinstance(params, CountUpParams):
                            referenced_values.extend([params.from_id, params.to_id])
                            for value_id in (params.from_id, params.to_id):
                                if value_id not in layer.value_ids:
                                    errors.append(
                                        f"{composition.composition_id}/{layer.object_id}: count_up "
                                        f"value {value_id} is not declared in layer.value_ids"
                                    )
                        for value_id in referenced_values:
                            if value_id not in resolved:
                                errors.append(f"{composition.composition_id}: unknown action value_id {value_id}")
                        if isinstance(params, CountUpParams):
                            start_value = resolved.get(params.from_id)
                            end_value = resolved.get(params.to_id)
                            if start_value is not None and end_value is not None and start_value.unit != end_value.unit:
                                errors.append(
                                    f"{composition.composition_id}/{layer.object_id}: count_up values "
                                    "must use compatible units"
                                )
                        anchor = action.anchor
                        text = beat_texts.get(anchor.beat_id)
                        speech_units = segments.get(anchor.beat_id)
                        if text is None or speech_units is None:
                            errors.append(f"{composition.composition_id}: unknown beat {anchor.beat_id}")
                            continue
                        if anchor.beat_id not in scene_beats:
                            errors.append(
                                f"{composition.composition_id}: anchor beat {anchor.beat_id} "
                                f"does not belong to scene {scene.scene_id}"
                            )
                        if not (0 <= anchor.char_start < anchor.char_end <= len(text)):
                            errors.append(f"{composition.composition_id}: anchor out of range")
                            continue
                        starts = {segment.char_start for segment in speech_units}
                        ends = {segment.char_end for segment in speech_units}
                        if anchor.char_start not in starts or anchor.char_end not in ends:
                            errors.append(
                                f"{composition.composition_id}: anchor must span whole measured/text speech units"
                            )
                for layer in composition.layers:
                    action_keys = [
                        (beat_positions.get(action.anchor.beat_id, -1), action.anchor.char_start,
                         action.anchor.char_end)
                        for action in layer.actions
                    ]
                    if action_keys != sorted(action_keys):
                        errors.append(
                            f"{composition.composition_id}/{layer.object_id}: actions are not in global speech order"
                        )
    return errors


def continuity_report(
    plan: VisualStoryPlan,
    scene_plans: list[SceneCompositionPlan],
) -> dict[str, Any]:
    compositions = [composition for scene_plan in scene_plans for composition in scene_plan.compositions]
    developing_compositions = sum(composition.develops is not None for composition in compositions)
    developing_scenes = sum(scene.develops_scene_id is not None for scene in plan.scenes)
    recurring_motifs = [motif.motif_id for motif in plan.motifs if motif.returns_in]
    return {
        "scenes": len(plan.scenes),
        "scene_plans": len(scene_plans),
        "scene_developing_share": round(developing_scenes / len(plan.scenes), 3) if plan.scenes else 0.0,
        "compositions": len(compositions),
        "developing_compositions": developing_compositions,
        "developing_composition_share": round(developing_compositions / len(compositions), 3) if compositions else 0.0,
        "introduces": sum(composition.relation == "introduces" for composition in compositions),
        "motifs_declared": len(plan.motifs),
        "motifs_reused": len(recurring_motifs),
        "reused_motif_ids": recurring_motifs,
        "arc_scenes": len(plan.arc),
    }


def resolve_action_schedule(
    plan: VisualStoryPlan,
    scene_plans: list[SceneCompositionPlan],
    *,
    beat_texts: dict[str, str],
    segments: dict[str, list[SpeechSegment]],
    beat_order: list[str],
) -> list[dict[str, Any]]:
    """Build one global timeline from actual per-unit WAV measurements.

    Every scheduled unit must have a finite positive duration and a SHA-256 of
    the WAV that produced it. Punctuation-derived text spans without measured
    WAVs are intentionally rejected.
    """
    if [scene_plan.scene_id for scene_plan in scene_plans] != [scene.scene_id for scene in plan.scenes]:
        raise ValueError("scene_plans must follow VisualStoryPlan scene order exactly")
    if len(beat_order) != len(set(beat_order)) or set(beat_order) != set(segments) or set(segments) != set(beat_texts):
        raise ValueError("beat_order must contain every speech beat/text entry exactly once")
    for beat_id in beat_order:
        _validate_segment_coverage(beat_id, beat_texts[beat_id], segments[beat_id])
    beat_offsets: dict[str, float] = {}
    local_offsets: dict[str, list[float]] = {}
    cursor = 0.0
    for beat_id in beat_order:
        units = segments[beat_id]
        if not units:
            raise ValueError(f"beat {beat_id} has no measured speech units")
        offsets = [0.0]
        for unit in units:
            if unit.duration_sec is None or not unit.wav_sha256:
                raise ValueError(f"beat {beat_id} contains a speech unit without measured WAV metadata")
            duration = float(unit.duration_sec)
            if not isfinite(duration) or duration <= 0:
                raise ValueError(f"beat {beat_id} has invalid WAV duration")
            offsets.append(offsets[-1] + duration)
        beat_offsets[beat_id] = cursor
        local_offsets[beat_id] = offsets
        cursor += offsets[-1]
        if not isfinite(cursor):
            raise ValueError("global audio timeline duration is not finite")
    schedule: list[dict[str, Any]] = []
    for scene_plan in scene_plans:
        for composition in scene_plan.compositions:
            for layer in composition.layers:
                for action in layer.actions:
                    anchor = action.anchor
                    if anchor.beat_id not in segments:
                        raise ValueError(f"action {action.action_id} references unknown beat {anchor.beat_id}")
                    units = segments[anchor.beat_id]
                    start_index = next((i for i, unit in enumerate(units)
                                        if unit.char_start == anchor.char_start), None)
                    end_index = next((i for i, unit in enumerate(units)
                                      if unit.char_end == anchor.char_end), None)
                    if start_index is None or end_index is None or end_index < start_index:
                        raise ValueError(f"action {action.action_id} anchor does not align to measured WAV units")
                    offsets = local_offsets[anchor.beat_id]
                    absolute_start = beat_offsets[anchor.beat_id] + offsets[start_index]
                    absolute_end = beat_offsets[anchor.beat_id] + offsets[end_index + 1]
                    schedule.append({
                        "scene_id": scene_plan.scene_id,
                        "beat_id": anchor.beat_id,
                        "composition_id": composition.composition_id,
                        "object_id": layer.object_id,
                        "action_id": action.action_id,
                        "start_sec": round(absolute_start, 4),
                        "end_sec": round(absolute_end, 4),
                    })
    return schedule
