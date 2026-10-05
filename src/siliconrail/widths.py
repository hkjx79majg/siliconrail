"""Unsigned bit-width inference over the parsed circuit IR.

The pass re-parses the source with :func:`siliconrail.rtl.parse` (so all
lexical, syntactic and semantic failures surface as the same
:class:`siliconrail.rtl.RTLParseError` with unchanged ``code``/``line``/
``column``/``message``) and then annotates the resulting IR in place:

- every expression node inside an assignment target or value gains a
  ``width`` field,
- every assignment gains ``target_width``, ``value_width`` and
  ``conversion`` (one of ``exact``, ``zero_extend``, ``truncate``).

Signals are treated as unsigned, matching the current subset. The
expression tree itself is never rewritten and constant values are left
untouched; ``truncate`` only labels the conversion.
"""

from __future__ import annotations

from .rtl import parse

# Binary operators whose result is a single bit.
_BINARY_ONE_BIT = {"&&", "||", "==", "!=", "<", "<=", ">", ">="}
# Binary operators whose result takes the left operand's width.
_BINARY_LEFT_WIDTH = {"<<", ">>", "<<<", ">>>"}
# Unary operators whose result is a single bit (logical not + reductions).
_UNARY_ONE_BIT = {"!", "&", "|", "^", "~&", "~|", "~^", "^~"}


def _const_width(value: int, declared: int | None) -> int:
    if declared is not None:
        return declared
    # Unsized decimal constants: wide enough to hold the value, at least
    # 32 bits; zero is therefore 32 bits as well.
    return max(value.bit_length(), 32)


def _expr_width(node: dict, declared_widths: dict[str, int]) -> int:
    kind = node["kind"]
    if kind == "const":
        width = _const_width(node["value"], node["width"])
    elif kind == "ref":
        width = declared_widths[node["name"]]
    elif kind == "bit_select":
        _expr_width(node["base"], declared_widths)
        _expr_width(node["index"], declared_widths)
        width = 1
    elif kind == "range_select":
        _expr_width(node["base"], declared_widths)
        width = abs(node["msb"] - node["lsb"]) + 1
    elif kind == "concat":
        width = sum(_expr_width(item, declared_widths) for item in node["items"])
    elif kind == "unary":
        operand = _expr_width(node["operand"], declared_widths)
        width = 1 if node["op"] in _UNARY_ONE_BIT else operand
    elif kind == "binary":
        left = _expr_width(node["left"], declared_widths)
        right = _expr_width(node["right"], declared_widths)
        op = node["op"]
        if op in _BINARY_ONE_BIT:
            width = 1
        elif op in _BINARY_LEFT_WIDTH:
            width = left
        else:
            width = max(left, right)
    else:  # pragma: no cover - the parser only emits the kinds above
        raise AssertionError(f"unknown expression kind {kind!r}")
    node["width"] = width
    return width


def _conversion(target_width: int, value_width: int) -> str:
    if value_width < target_width:
        return "zero_extend"
    if value_width > target_width:
        return "truncate"
    return "exact"


def analyze(source: str) -> dict:
    """Parse ``source`` and annotate the IR with unsigned bit widths."""
    ir = parse(source)
    for module in ir["modules"]:
        declared_widths = {port["name"]: port["width"] for port in module["ports"]}
        declared_widths.update({net["name"]: net["width"] for net in module["nets"]})
        for assign in module["assigns"]:
            target_width = _expr_width(assign["target"], declared_widths)
            value_width = _expr_width(assign["value"], declared_widths)
            assign["target_width"] = target_width
            assign["value_width"] = value_width
            assign["conversion"] = _conversion(target_width, value_width)
    return ir
