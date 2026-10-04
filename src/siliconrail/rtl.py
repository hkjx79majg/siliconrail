"""Verilog-2001 combinational-subset parser producing a JSON-safe circuit IR.

Supported subset (per source text, multiple modules allowed):

- case-sensitive identifiers, line (``//``) and block (``/* */``) comments
- decimal constants and sized binary/hex constants (``42``, ``4'b1010``, ``8'hFF``)
- ANSI-style ``input``/``output``/``inout`` port declarations
- ``wire`` declarations and continuous ``assign`` statements
- expressions: parentheses, bit-select, constant-range part-select,
  concatenation, and common unary/binary operators

All parse failures raise :class:`RTLParseError` with a stable ``code`` and a
1-based ``line``/``column``.
"""

from __future__ import annotations

import string


class RTLParseError(Exception):
    """Public parse failure.

    Carries ``code`` (machine-readable category), 1-based ``line`` and
    ``column``, and a non-empty human-readable ``message``.
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

_MULTI_OPS = (
    "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "~&", "~|", "~^", "^~", "**",
)
_SINGLE_PUNCT = set("()[]{},;:=+-*/%&|^~!<>#")


class _Token:
    __slots__ = ("kind", "text", "line", "column", "number")

    def __init__(self, kind: str, text: str, line: int, column: int, number=None) -> None:
        self.kind = kind  # "ident" | "number" | "punct" | "eof"
        self.text = text
        self.line = line
        self.column = column
        self.number = number  # (value, width) for number tokens, else None


def _describe(tok: _Token) -> str:
    if tok.kind == "eof":
        return "end of input"
    return repr(tok.text)


class _Lexer:
    """Lazy tokenizer: tokens are produced on demand so the parser can
    classify an earlier construct (e.g. ``always``) before the lexer ever
    reaches a later illegal character."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.i = 0
        self.line = 1
        self.col = 1

    def _error(self, message: str, line: int, col: int) -> None:
        raise RTLParseError("syntax_error", message, line, col)

    def next_token(self) -> _Token:
        source, n = self.source, len(self.source)
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
                j = source.find("\n", self.i)
                if j == -1:
                    self.i = n
                    break
                self.i = j
                continue
            if source.startswith("/*", self.i):
                start_line, start_col = self.line, self.col
                j = source.find("*/", self.i + 2)
                if j == -1:
                    self._error("unterminated block comment", start_line, start_col)
                segment = source[self.i:j + 2]
                self.line += segment.count("\n")
                if "\n" in segment:
                    self.col = len(segment) - segment.rindex("\n")
                else:
                    self.col += len(segment)
                self.i = j + 2
                continue
            if ch in _IDENT_START:
                j = self.i
                while j < n and source[j] in _IDENT_CHAR:
                    j += 1
                tok = _Token("ident", source[self.i:j], self.line, self.col)
                self.col += j - self.i
                self.i = j
                return tok
            if ch.isdigit():
                return self._lex_number()
            two = source[self.i:self.i + 2]
            if two in _MULTI_OPS:
                tok = _Token("punct", two, self.line, self.col)
                self.i += 2
                self.col += 2
                return tok
            if ch in _SINGLE_PUNCT:
                tok = _Token("punct", ch, self.line, self.col)
                self.i += 1
                self.col += 1
                return tok
            self._error(f"unexpected character {ch!r}", self.line, self.col)
        return _Token("eof", "", self.line, self.col)

    def _lex_number(self) -> _Token:
        source, n = self.source, len(self.source)
        i, line, col = self.i, self.line, self.col
        j = i
        while j < n and (source[j].isdigit() or source[j] == "_"):
            j += 1
        size_text = source[i:j].replace("_", "")

        if j < n and source[j] == "'":
            if j + 1 >= n or source[j + 1] not in "bBhH":
                self._error("sized literal must use base 'b' or 'h'", line, col)
            base_ch = source[j + 1].lower()
            valid = "01_" if base_ch == "b" else "0123456789abcdefABCDEF_"
            k = j + 2
            while k < n and source[k] in valid:
                k += 1
            digit_text = source[j + 2:k].replace("_", "")
            if not digit_text:
                self._error("sized literal has no digits", line, col)
            if k < n and (source[k].isalnum() or source[k] in "_$?"):
                self._error(f"invalid digit in {base_ch!r}-based literal", line, col)
            width = int(size_text)
            value = int(digit_text, 2 if base_ch == "b" else 16)
            self.i = k
            self.col += k - i
            return _Token("number", source[i:k], line, col, (value, width))

        if j < n and (source[j].isalpha() or source[j] in "_$"):
            self._error("invalid decimal literal", line, col)
        self.i = j
        self.col += j - i
        return _Token("number", source[i:j], line, col, (int(size_text), None))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_DIRECTIONS = ("input", "output", "inout")
_CORE_KEYWORDS = {"module", "endmodule", "wire", "assign"}
_UNSUPPORTED_KEYWORDS = {
    "always", "initial", "reg", "parameter", "localparam", "integer", "genvar",
    "function", "endfunction", "task", "endtask", "generate", "endgenerate",
    "for", "while", "repeat", "forever", "begin", "end", "if", "else",
    "case", "casex", "casez", "posedge", "negedge", "signed", "unsigned",
    "real", "realtime", "time", "tri", "wand", "wor", "supply0", "supply1",
    "defparam", "specify", "endspecify", "primitive", "endprimitive",
}
_ALL_KEYWORDS = _CORE_KEYWORDS | _UNSUPPORTED_KEYWORDS | set(_DIRECTIONS)

_BINARY_PREC = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4, "~^": 4, "^~": 4,
    "&": 5,
    "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
    "**": 11,
}
_RIGHT_ASSOC = {"**"}
_UNARY_OPS = {"+", "-", "~", "!", "&", "|", "^", "~&", "~|", "~^", "^~"}


class _Parser:
    def __init__(self, lexer: _Lexer) -> None:
        self.lexer = lexer
        self.current = lexer.next_token()

    # -- token helpers -----------------------------------------------------

    def peek(self) -> _Token:
        return self.current

    def advance(self) -> _Token:
        tok = self.current
        if tok.kind != "eof":
            self.current = self.lexer.next_token()
        return tok

    def error(self, code: str, message: str, tok: _Token | None = None) -> None:
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
            self.error("syntax_error", f"expected {text!r} but found {_describe(tok)}")
        return self.advance()

    def expect_name(self, what: str) -> _Token:
        tok = self.peek()
        if tok.kind != "ident" or tok.text in _ALL_KEYWORDS:
            self.error("syntax_error", f"expected {what} but found {_describe(tok)}")
        return self.advance()

    # -- top level ---------------------------------------------------------

    def parse(self) -> dict:
        modules = []
        module_names: set[str] = set()
        while self.peek().kind != "eof":
            modules.append(self.parse_module(module_names))
        if not modules:
            self.error("syntax_error", "source contains no module")
        return {"modules": modules}

    def parse_module(self, module_names: set[str]) -> dict:
        if not self.at_ident("module"):
            self.error("syntax_error", f"expected 'module' but found {_describe(self.peek())}")
        self.advance()
        name_tok = self.expect_name("module name")
        if name_tok.text in module_names:
            self.error("duplicate_name", f"duplicate module name {name_tok.text!r}", name_tok)
        module_names.add(name_tok.text)
        if self.at_punct("#"):
            self.error("unsupported_construct", "parameterized module headers are not supported")
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
                self.error("syntax_error", "unexpected end of input inside module")
            if tok.kind == "ident" and tok.text == "wire":
                self.advance()
                self.parse_net_decl(nets, declared)
            elif tok.kind == "ident" and tok.text == "assign":
                self.advance()
                assigns.append(self.parse_assign(refs))
            elif tok.kind == "ident" and tok.text in _UNSUPPORTED_KEYWORDS:
                self.error("unsupported_construct", f"construct {tok.text!r} is not supported")
            elif tok.kind == "ident":
                self.error(
                    "unsupported_construct",
                    f"module item starting with {tok.text!r} is not supported "
                    "(instances and procedural blocks are outside the subset)",
                )
            elif tok.kind == "punct" and tok.text == "#":
                self.error("unsupported_construct", "parameter syntax is not supported")
            else:
                self.error("syntax_error", f"unexpected {_describe(tok)} inside module")
        self.advance()  # consume 'endmodule'

        for name, tok in refs:
            if name not in declared:
                self.error("undeclared_signal", f"signal {name!r} is not declared in this module", tok)

        return {"name": name_tok.text, "ports": ports, "nets": nets, "assigns": assigns}

    # -- declarations --------------------------------------------------------

    def _declare(self, declared: set[str], name_tok: _Token) -> None:
        if name_tok.text in declared:
            self.error("duplicate_name", f"duplicate port/net name {name_tok.text!r}", name_tok)
        declared.add(name_tok.text)

    def parse_port_list(self, ports: list[dict], declared: set[str]) -> None:
        while True:
            tok = self.peek()
            if tok.kind == "ident" and tok.text in _DIRECTIONS:
                direction = tok.text
                self.advance()
            elif tok.kind == "ident":
                self.error("unsupported_construct", "non-ANSI port declarations are not supported")
            else:
                self.error("syntax_error", f"expected port direction but found {_describe(tok)}")
            width = self.parse_optional_range()
            while True:
                if self.peek().kind == "ident" and self.peek().text in _UNSUPPORTED_KEYWORDS:
                    self.error("unsupported_construct", f"port kind {self.peek().text!r} is not supported")
                name_tok = self.expect_name("port name")
                self._declare(declared, name_tok)
                ports.append({"name": name_tok.text, "direction": direction, "width": width})
                if not self.at_punct(","):
                    return
                self.advance()
                if self.at_ident(_DIRECTIONS[0]) or self.at_ident(_DIRECTIONS[1]) or self.at_ident(_DIRECTIONS[2]):
                    break  # a new direction group follows

    def parse_net_decl(self, nets: list[dict], declared: set[str]) -> None:
        width = self.parse_optional_range()
        while True:
            name_tok = self.expect_name("net name")
            self._declare(declared, name_tok)
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
            self.error("invalid_range", "range endpoint must be a non-negative decimal constant")
        self.advance()
        return tok.number[0]

    # -- statements ----------------------------------------------------------

    def parse_assign(self, refs: list[tuple[str, _Token]]) -> dict:
        target = self.parse_expr(refs)
        self.expect_punct("=")
        value = self.parse_expr(refs)
        self.expect_punct(";")
        return {"target": target, "value": value}

    # -- expressions ---------------------------------------------------------

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
            start = self.advance()
            first = self.parse_expr(refs)
            if self.at_punct(":"):
                self.advance()
                second = self.parse_expr(refs)
                self.expect_punct("]")
                msb = self._const_endpoint(first, start)
                lsb = self._const_endpoint(second, start)
                node = {"kind": "range_select", "base": node, "msb": msb, "lsb": lsb}
            else:
                self.expect_punct("]")
                node = {"kind": "bit_select", "base": node, "index": first}
        return node

    def _const_endpoint(self, node: dict, tok: _Token) -> int:
        if node["kind"] != "const" or node["width"] is not None or node["value"] < 0:
            self.error("invalid_range", "range select endpoints must be non-negative decimal constants", tok)
        return node["value"]

    def parse_primary(self, refs: list[tuple[str, _Token]]) -> dict:
        tok = self.peek()
        if tok.kind == "number":
            self.advance()
            value, width = tok.number
            return {"kind": "const", "value": value, "width": width}
        if tok.kind == "ident":
            if tok.text in _ALL_KEYWORDS:
                self.error("syntax_error", f"unexpected keyword {tok.text!r} in expression")
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
        self.error("syntax_error", f"expected expression but found {_describe(tok)}")


def parse(source: str) -> dict:
    """Parse Verilog source text into a JSON-serializable circuit IR."""
    return _Parser(_Lexer(source)).parse()
