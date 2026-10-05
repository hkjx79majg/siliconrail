"""Verilog-2001 combinational-subset parser producing a JSON-safe circuit IR.

One source text may contain several modules. The supported subset is:

- case-sensitive identifiers (``A`` and ``a`` are distinct signals)
- decimal constants (``42``) and sized binary/hex constants
  (``4'b1010``, ``8'hFF``), underscores allowed inside digits
- line comments (``//``) and block comments (``/* */``)
- ANSI-style ``input`` / ``output`` / ``inout`` port declarations, with an
  optional ``wire`` keyword after the direction
- ``wire`` declarations and continuous ``assign`` statements
- expressions built from parentheses, bit selects, constant-range part
  selects, concatenations and the common unary/binary operators

Every parse failure raises :class:`RTLParseError` carrying a stable
machine-readable ``code`` plus a 1-based ``line``/``column``.
"""

from __future__ import annotations

import string


class RTLParseError(Exception):
    """Public parse failure.

    Attributes:
        code: machine-readable category, one of ``syntax_error``,
            ``duplicate_name``, ``undeclared_signal``, ``invalid_range`` or
            ``unsupported_construct``.
        line/column: 1-based start position of the offending construct.
        message: non-empty human-readable description.
    """

    def __init__(self, code: str, message: str, line: int, column: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.column = column


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_IDENT_START = set(string.ascii_letters) | {"_"}
_IDENT_CHAR = _IDENT_START | set(string.digits) | {"$"}

# Longest match first so ``<<<`` is not lexed as ``<<`` + ``<``.
_MULTI_OPS = (
    "<<<", ">>>",
    "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
    "~&", "~|", "~^", "^~", "**",
)
_SINGLE_PUNCT = set("()[]{},;:=+-*/%&|^~!<>#?")


class _Token:
    __slots__ = ("kind", "text", "line", "column", "number")

    def __init__(self, kind: str, text: str, line: int, column: int, number=None) -> None:
        self.kind = kind  # "ident" | "number" | "punct" | "eof"
        self.text = text
        self.line = line
        self.column = column
        # For number tokens: (integer value, declared width or None).
        self.number = number


def _describe(tok: _Token) -> str:
    if tok.kind == "eof":
        return "end of input"
    return repr(tok.text)


class _Lexer:
    """On-demand tokenizer.

    Tokens are produced lazily so the parser can classify a construct near
    the current position (e.g. an ``always`` block) before the lexer ever
    visits a potentially invalid character further into the source.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

    def _syntax_error(self, message: str, line: int, col: int) -> None:
        raise RTLParseError("syntax_error", message, line, col)

    def next_token(self) -> _Token:
        source, n = self.source, self.n
        while self.i < n:
            ch = source[self.i]
            if ch in " \t\r":
                self.i += 1
                self.col += 1
                continue
            if ch == "\n":
                self.i += 1
                self.line += 1
                self.col = 1
                continue
            if source.startswith("//", self.i):
                newline = source.find("\n", self.i)
                if newline == -1:
                    self.i = n
                    break
                self.i = newline  # leave the newline for position tracking
                continue
            if source.startswith("/*", self.i):
                start_line, start_col = self.line, self.col
                close = source.find("*/", self.i + 2)
                if close == -1:
                    self._syntax_error("unterminated block comment", start_line, start_col)
                segment = source[self.i:close + 2]
                last_newline = segment.rfind("\n")
                if last_newline == -1:
                    self.col += len(segment)
                else:
                    self.line += segment.count("\n")
                    self.col = len(segment) - last_newline
                self.i = close + 2
                continue
            if ch in _IDENT_START:
                start = self.i
                while self.i < n and source[self.i] in _IDENT_CHAR:
                    self.i += 1
                tok = _Token("ident", source[start:self.i], self.line, self.col)
                self.col += self.i - start
                return tok
            if ch.isdigit():
                return self._lex_number()
            for op in _MULTI_OPS:
                if source.startswith(op, self.i):
                    tok = _Token("punct", op, self.line, self.col)
                    self.i += len(op)
                    self.col += len(op)
                    return tok
            if ch in _SINGLE_PUNCT:
                tok = _Token("punct", ch, self.line, self.col)
                self.i += 1
                self.col += 1
                return tok
            self._syntax_error(f"unexpected character {ch!r}", self.line, self.col)
        return _Token("eof", "", self.line, self.col)

    def _lex_number(self) -> _Token:
        source, n = self.source, self.n
        start, line, col = self.i, self.line, self.col
        j = self.i
        while j < n and (source[j].isdigit() or source[j] == "_"):
            j += 1
        size_text = source[start:j].replace("_", "")

        if j < n and source[j] == "'":
            if j + 1 >= n or source[j + 1] not in "bBhH":
                self._syntax_error("sized literal must use base 'b' or 'h'", line, col)
            base = source[j + 1].lower()
            valid_digits = "01_" if base == "b" else "0123456789abcdefABCDEF_"
            k = j + 2
            while k < n and source[k] in valid_digits:
                k += 1
            digits = source[j + 2:k].replace("_", "")
            if not digits:
                self._syntax_error("sized literal has no digits", line, col)
            if k < n and (source[k].isalnum() or source[k] in "_$?"):
                self._syntax_error(f"invalid digit in {base!r}-based literal", line, col)
            value = int(digits, 2 if base == "b" else 16)
            self.i = k
            self.col += k - start
            return _Token("number", source[start:k], line, col, (value, int(size_text)))

        if j < n and (source[j].isalpha() or source[j] in "_$"):
            self._syntax_error("invalid decimal literal", line, col)
        self.i = j
        self.col += j - start
        return _Token("number", source[start:j], line, col, (int(size_text), None))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_DIRECTIONS = ("input", "output", "inout")
_SUPPORTED_HEADER = {"module", "endmodule", "wire", "assign"} | set(_DIRECTIONS)

# Keywords that introduce constructs deliberately outside this subset:
# procedural blocks, instances, parameters, tasks/functions and friends.
_UNSUPPORTED_KEYWORDS = {
    "always", "initial", "reg", "parameter", "localparam", "integer", "genvar",
    "function", "endfunction", "task", "endtask", "generate", "endgenerate",
    "for", "while", "repeat", "forever", "begin", "end", "if", "else",
    "case", "casex", "casez", "posedge", "negedge", "signed", "unsigned",
    "real", "realtime", "time", "tri", "wand", "wor", "supply0", "supply1",
    "defparam", "specify", "endspecify", "primitive", "endprimitive",
}

_BINARY_PREC = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4, "~^": 4, "^~": 4,
    "&": 5,
    "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7,
    "<<": 8, ">>": 8, "<<<": 8, ">>>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
    "**": 11,
}
_RIGHT_ASSOC = {"**"}
_UNARY_OPS = {"+", "-", "~", "!", "&", "|", "^", "~&", "~|", "~^", "^~"}


class _Parser:
    def __init__(self, source: str) -> None:
        self.lexer = _Lexer(source)
        self.current = self.lexer.next_token()

    # -- token stream ------------------------------------------------------

    def peek(self) -> _Token:
        return self.current

    def advance(self) -> _Token:
        tok = self.current
        if tok.kind != "eof":
            self.current = self.lexer.next_token()
        return tok

    def raise_error(self, code: str, message: str, tok: _Token | None = None) -> None:
        tok = tok if tok is not None else self.peek()
        raise RTLParseError(code, message, tok.line, tok.column)

    def at_punct(self, text: str) -> bool:
        tok = self.peek()
        return tok.kind == "punct" and tok.text == text

    def at_ident(self, text: str) -> bool:
        tok = self.peek()
        return tok.kind == "ident" and tok.text == text

    def expect_punct(self, text: str) -> _Token:
        tok = self.peek()
        if tok.kind != "punct" or tok.text != text:
            self.raise_error("syntax_error", f"expected {text!r} but found {_describe(tok)}")
        return self.advance()

    def expect_name(self, what: str) -> _Token:
        tok = self.peek()
        if tok.kind != "ident" or tok.text in _SUPPORTED_HEADER or tok.text in _UNSUPPORTED_KEYWORDS:
            self.raise_error("syntax_error", f"expected {what} but found {_describe(tok)}")
        return self.advance()

    # -- top level ---------------------------------------------------------

    def parse_source(self) -> dict:
        modules = []
        names: set[str] = set()
        while self.peek().kind != "eof":
            modules.append(self.parse_module(names))
        if not modules:
            self.raise_error("syntax_error", "source contains no module")
        return {"modules": modules}

    def parse_module(self, names: set[str]) -> dict:
        if not self.at_ident("module"):
            self.raise_error(
                "syntax_error",
                f"expected 'module' but found {_describe(self.peek())}",
            )
        self.advance()
        name_tok = self.expect_name("module name")
        if name_tok.text in names:
            self.raise_error("duplicate_name", f"duplicate module name {name_tok.text!r}", name_tok)
        names.add(name_tok.text)
        if self.at_punct("#"):
            self.raise_error(
                "unsupported_construct",
                "parameterized module headers are not supported",
            )
        self.expect_punct("(")

        ports: list[dict] = []
        declared: set[str] = set()
        if not self.at_punct(")"):
            self.parse_port_list(ports, declared)
        self.expect_punct(")")
        self.expect_punct(";")

        nets: list[dict] = []
        assigns: list[dict] = []
        refs: list[tuple[str, _Token]] = []
        while not self.at_ident("endmodule"):
            tok = self.peek()
            if tok.kind == "eof":
                self.raise_error("syntax_error", "unexpected end of input inside module")
            if tok.kind == "ident" and tok.text == "wire":
                self.advance()
                self.parse_net_decl(nets, declared)
            elif tok.kind == "ident" and tok.text == "assign":
                self.advance()
                assigns.append(self.parse_assign(refs))
            elif tok.kind == "ident" and tok.text in _UNSUPPORTED_KEYWORDS:
                self.raise_error(
                    "unsupported_construct",
                    f"construct {tok.text!r} is not supported",
                )
            elif tok.kind == "ident":
                self.raise_error(
                    "unsupported_construct",
                    f"module item starting with {tok.text!r} is not supported "
                    "(instances and procedural blocks are outside the subset)",
                )
            elif tok.kind == "punct" and tok.text == "#":
                self.raise_error("unsupported_construct", "parameter syntax is not supported")
            else:
                self.raise_error("syntax_error", f"unexpected {_describe(tok)} inside module")
        self.advance()  # consume endmodule

        # Declarations span the whole module body, so forward references are
        # legal; resolve every referenced name after the body is parsed.
        for ref_name, ref_tok in refs:
            if ref_name not in declared:
                self.raise_error(
                    "undeclared_signal",
                    f"signal {ref_name!r} is not declared in this module",
                    ref_tok,
                )

        return {"name": name_tok.text, "ports": ports, "nets": nets, "assigns": assigns}

    # -- declarations ------------------------------------------------------

    def add_declared(self, declared: set[str], name_tok: _Token) -> None:
        if name_tok.text in declared:
            self.raise_error(
                "duplicate_name",
                f"duplicate port/net name {name_tok.text!r}",
                name_tok,
            )
        declared.add(name_tok.text)

    def parse_port_list(self, ports: list[dict], declared: set[str]) -> None:
        while True:
            tok = self.peek()
            if tok.kind == "ident" and tok.text in _DIRECTIONS:
                direction = tok.text
                self.advance()
            elif tok.kind == "ident":
                # A bare name in the header is the non-ANSI style.
                self.raise_error(
                    "unsupported_construct",
                    "non-ANSI port declarations are not supported",
                )
            else:
                self.raise_error(
                    "syntax_error",
                    f"expected port direction but found {_describe(tok)}",
                )
            # ANSI headers may say ``input wire [..] a``; wire is the only
            # net kind in the subset, anything else (reg, signed, ...) is
            # rejected as unsupported.
            if self.at_ident("wire"):
                self.advance()
            elif self.peek().kind == "ident" and self.peek().text in _UNSUPPORTED_KEYWORDS:
                self.raise_error(
                    "unsupported_construct",
                    f"port kind {self.peek().text!r} is not supported",
                )
            width = self.parse_optional_range()
            while True:
                if self.peek().kind == "ident" and self.peek().text in _UNSUPPORTED_KEYWORDS:
                    self.raise_error(
                        "unsupported_construct",
                        f"port kind {self.peek().text!r} is not supported",
                    )
                name_tok = self.expect_name("port name")
                self.add_declared(declared, name_tok)
                ports.append({"name": name_tok.text, "direction": direction, "width": width})
                if not self.at_punct(","):
                    return
                self.advance()
                if any(self.at_ident(d) for d in _DIRECTIONS):
                    break  # next direction group

    def parse_net_decl(self, nets: list[dict], declared: set[str]) -> None:
        width = self.parse_optional_range()
        while True:
            name_tok = self.expect_name("net name")
            self.add_declared(declared, name_tok)
            nets.append({"name": name_tok.text, "width": width})
            if not self.at_punct(","):
                break
            self.advance()
        self.expect_punct(";")

    def parse_optional_range(self) -> int:
        if not self.at_punct("["):
            return 1
        self.advance()
        msb = self.parse_range_endpoint()
        self.expect_punct(":")
        lsb = self.parse_range_endpoint()
        self.expect_punct("]")
        return abs(msb - lsb) + 1

    def parse_range_endpoint(self) -> int:
        tok = self.peek()
        if tok.kind != "number" or tok.number[1] is not None:
            self.raise_error(
                "invalid_range",
                "range endpoint must be a non-negative decimal constant",
            )
        self.advance()
        return tok.number[0]

    # -- statements --------------------------------------------------------

    def parse_assign(self, refs: list[tuple[str, _Token]]) -> dict:
        target = self.parse_expr(refs)
        self.expect_punct("=")
        value = self.parse_expr(refs)
        self.expect_punct(";")
        return {"target": target, "value": value}

    # -- expressions -------------------------------------------------------

    def parse_expr(self, refs: list[tuple[str, _Token]], min_prec: int = 1) -> dict:
        left = self.parse_unary(refs)
        while True:
            tok = self.peek()
            if tok.kind != "punct":
                break
            prec = _BINARY_PREC.get(tok.text)
            if prec is None or prec < min_prec:
                break
            self.advance()
            next_min = prec if tok.text in _RIGHT_ASSOC else prec + 1
            right = self.parse_expr(refs, next_min)
            left = {"kind": "binary", "op": tok.text, "left": left, "right": right}
        return left

    def parse_unary(self, refs: list[tuple[str, _Token]]) -> dict:
        tok = self.peek()
        if tok.kind == "punct" and tok.text in _UNARY_OPS:
            self.advance()
            operand = self.parse_unary(refs)
            return {"kind": "unary", "op": tok.text, "operand": operand}
        return self.parse_postfix(refs)

    def parse_postfix(self, refs: list[tuple[str, _Token]]) -> dict:
        node = self.parse_primary(refs)
        while self.at_punct("["):
            bracket = self.advance()
            first = self.parse_expr(refs)
            if self.at_punct(":"):
                self.advance()
                second = self.parse_expr(refs)
                self.expect_punct("]")
                msb = self.require_const_endpoint(first, bracket)
                lsb = self.require_const_endpoint(second, bracket)
                node = {"kind": "range_select", "base": node, "msb": msb, "lsb": lsb}
            else:
                self.expect_punct("]")
                node = {"kind": "bit_select", "base": node, "index": first}
        return node

    def require_const_endpoint(self, node: dict, at: _Token) -> int:
        if node.get("kind") != "const" or node["width"] is not None or node["value"] < 0:
            self.raise_error(
                "invalid_range",
                "range select endpoints must be non-negative decimal constants",
                at,
            )
        return node["value"]

    def parse_primary(self, refs: list[tuple[str, _Token]]) -> dict:
        tok = self.peek()
        if tok.kind == "number":
            self.advance()
            value, width = tok.number
            return {"kind": "const", "value": value, "width": width}
        if tok.kind == "ident":
            if tok.text in _SUPPORTED_HEADER or tok.text in _UNSUPPORTED_KEYWORDS:
                self.raise_error("syntax_error", f"unexpected keyword {tok.text!r} in expression")
            self.advance()
            refs.append((tok.text, tok))
            return {"kind": "ref", "name": tok.text}
        if tok.kind == "punct" and tok.text == "(":
            self.advance()
            node = self.parse_expr(refs)
            self.expect_punct(")")
            return node
        if tok.kind == "punct" and tok.text == "{":
            self.advance()
            items = [self.parse_expr(refs)]
            while self.at_punct(","):
                self.advance()
                items.append(self.parse_expr(refs))
            self.expect_punct("}")
            return {"kind": "concat", "items": items}
        self.raise_error("syntax_error", f"expected expression but found {_describe(tok)}")


def parse(source: str) -> dict:
    """Parse one Verilog source text into a JSON-serializable circuit IR."""
    return _Parser(source).parse_source()


# ---------------------------------------------------------------------------
# Unsigned width inference
# ---------------------------------------------------------------------------

# Logical and reduction operators collapse to a single bit.
_UNARY_ONE_BIT_OPS = {"!", "&", "|", "^", "~&", "~|", "~^", "^~"}

_LOGICAL_BINARY_OPS = {"&&", "||"}
_COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">="}
_SHIFT_OPS = {"<<", ">>", "<<<", ">>>"}


def _unsigned_const_width(value: int) -> int:
    """Width of an unsized decimal constant: enough bits, at least 32."""
    return max(32, value.bit_length())


class _WidthAnalyzer:
    """Annotates a fresh deep copy of a parsed IR with expression widths.

    Signals are treated as unsigned per the supported subset. The original
    parse result is never mutated.
    """

    def __init__(self) -> None:
        self.widths: dict[str, int] = {}

    def analyze_module(self, module: dict) -> dict:
        self.widths = {p["name"]: p["width"] for p in module["ports"]}
        for net in module["nets"]:
            self.widths[net["name"]] = net["width"]
        return {
            "name": module["name"],
            "ports": module["ports"],
            "nets": module["nets"],
            "assigns": [self.analyze_assign(a) for a in module["assigns"]],
        }

    def analyze_assign(self, assign: dict) -> dict:
        target = self.analyze_expr(assign["target"])
        value = self.analyze_expr(assign["value"])
        target_width = target["width"]
        value_width = value["width"]
        if value_width == target_width:
            conversion = "exact"
        elif value_width < target_width:
            conversion = "zero_extend"
        else:
            conversion = "truncate"
        return {
            "target": target,
            "value": value,
            "target_width": target_width,
            "value_width": value_width,
            "conversion": conversion,
        }

    def analyze_expr(self, node: dict) -> dict:
        kind = node["kind"]
        if kind == "const":
            declared = node["width"]
            width = declared if declared is not None else _unsigned_const_width(node["value"])
            return {"kind": "const", "value": node["value"], "width": width}
        if kind == "ref":
            return {"kind": "ref", "name": node["name"], "width": self.widths[node["name"]]}
        if kind == "bit_select":
            return {
                "kind": "bit_select",
                "base": self.analyze_expr(node["base"]),
                "index": self.analyze_expr(node["index"]),
                "width": 1,
            }
        if kind == "range_select":
            width = abs(node["msb"] - node["lsb"]) + 1
            return {
                "kind": "range_select",
                "base": self.analyze_expr(node["base"]),
                "msb": node["msb"],
                "lsb": node["lsb"],
                "width": width,
            }
        if kind == "concat":
            items = [self.analyze_expr(item) for item in node["items"]]
            return {"kind": "concat", "items": items, "width": sum(i["width"] for i in items)}
        if kind == "unary":
            operand = self.analyze_expr(node["operand"])
            op = node["op"]
            width = 1 if op in _UNARY_ONE_BIT_OPS else operand["width"]
            return {"kind": "unary", "op": op, "operand": operand, "width": width}
        # binary
        left = self.analyze_expr(node["left"])
        right = self.analyze_expr(node["right"])
        op = node["op"]
        if op in _LOGICAL_BINARY_OPS or op in _COMPARISON_OPS:
            width = 1
        elif op in _SHIFT_OPS:
            width = left["width"]
        else:
            width = max(left["width"], right["width"])
        return {"kind": "binary", "op": op, "left": left, "right": right, "width": width}


def analyze_widths(source: str) -> dict:
    """Parse ``source`` and annotate the IR with unsigned expression widths.

    On success every expression node carries a positive integer ``width``;
    each continuous assignment additionally carries ``target_width``,
    ``value_width`` and ``conversion`` (``exact``/``zero_extend``/
    ``truncate``). The parse result itself is left untouched.
    """
    ir = parse(source)
    analyzer = _WidthAnalyzer()
    return {"modules": [analyzer.analyze_module(m) for m in ir["modules"]]}
