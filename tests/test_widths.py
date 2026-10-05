import json
import unittest

from siliconrail import RTLParseError
from siliconrail.service import Service


def widths(source):
    return Service().analyze_widths(source)


def only_assign(source):
    return widths(source)["modules"][0]["assigns"][0]


def _walk(node):
    yield node
    kind = node["kind"]
    if kind in ("unary",):
        yield from _walk(node["operand"])
    elif kind == "binary":
        yield from _walk(node["left"])
        yield from _walk(node["right"])
    elif kind == "bit_select":
        yield from _walk(node["base"])
        yield from _walk(node["index"])
    elif kind == "range_select":
        yield from _walk(node["base"])
    elif kind == "concat":
        for item in node["items"]:
            yield from _walk(item)


class AnalyzeWidthsStructureTest(unittest.TestCase):
    def test_top_level_and_ir_fields_preserved(self):
        source = (
            "module m(input [7:0] a, output [3:0] b);\n"
            "wire [15:0] w;\n"
            "assign b = a[3:0];\n"
            "assign w = {8'h00, a};\n"
            "endmodule\n"
            "module second(input c, output o); assign o = c; endmodule"
        )
        ir = widths(source)
        self.assertEqual([m["name"] for m in ir["modules"]], ["m", "second"])
        mod = ir["modules"][0]
        self.assertEqual(
            mod["ports"],
            [
                {"name": "a", "direction": "input", "width": 8},
                {"name": "b", "direction": "output", "width": 4},
            ],
        )
        self.assertEqual(mod["nets"], [{"name": "w", "width": 16}])
        self.assertEqual(
            set(mod["assigns"][0]), {"target", "value", "target_width", "value_width", "conversion"}
        )
        json.dumps(ir, sort_keys=True)

    def test_every_expression_node_has_positive_width(self):
        source = (
            "module m(input [7:0] a, input b, input [2:0] i, output [15:0] o);\n"
            "assign o = ~(a[7:4] & {a[0], 3'b000}) + (b << 1) - (i ** 2) "
            "+ (a == b) && (!b || &a) ^ ~|a;\n"
            "endmodule"
        )
        ir = widths(source)
        nodes = []
        for assign in ir["modules"][0]["assigns"]:
            nodes.extend(_walk(assign["target"]))
            nodes.extend(_walk(assign["value"]))
        self.assertTrue(nodes)
        for node in nodes:
            self.assertIsInstance(node["width"], int)
            self.assertGreaterEqual(node["width"], 1, node)

    def test_parse_rtl_result_is_unchanged(self):
        source = "module m(input [7:0] a, output [3:0] b); assign b = a[3:0]; endmodule"
        service = Service()
        parsed = service.parse_rtl(source)
        service.analyze_widths(source)
        again = service.parse_rtl(source)
        self.assertEqual(parsed, again)
        target = parsed["modules"][0]["assigns"][0]["target"]
        value = parsed["modules"][0]["assigns"][0]["value"]
        self.assertEqual(target, {"kind": "ref", "name": "b"})
        self.assertEqual(
            value, {"kind": "range_select", "base": {"kind": "ref", "name": "a"}, "msb": 3, "lsb": 0}
        )
        self.assertEqual(set(parsed["modules"][0]["assigns"][0]), {"target", "value"})

    def test_const_node_keeps_value_but_replaces_none_width(self):
        assign = only_assign("module m(output [31:0] o); assign o = 42; endmodule")
        self.assertEqual(assign["value"], {"kind": "const", "value": 42, "width": 32})
        self.assertNotIn(None, [n["width"] for n in _walk(assign["value"])])

    def test_deterministic(self):
        source = "module m(input [7:0] a, output [3:0] o); assign o = a + 1; endmodule"
        self.assertEqual(widths(source), widths(source))


class WidthInferenceRuleTest(unittest.TestCase):
    def test_refs_and_sized_constants_use_declared_width(self):
        assign = only_assign(
            "module m(input [7:0] a, output [11:0] o); assign o = 12'hABC + a; endmodule"
        )
        self.assertEqual(assign["target"]["width"], 12)
        self.assertEqual(assign["value"]["left"]["width"], 12)
        self.assertEqual(assign["value"]["right"]["width"], 8)
        self.assertEqual(assign["value"]["width"], 12)

    def test_unsized_decimal_constants(self):
        for literal, expected in [("0", 32), ("1", 32), ("42", 32), ("2147483648", 32),
                                  ("4294967296", 33)]:
            assign = only_assign(f"module m(output [63:0] o); assign o = {literal}; endmodule")
            self.assertEqual(assign["value"]["width"], expected, literal)

    def test_bit_select_is_one_bit(self):
        assign = only_assign(
            "module m(input [7:0] a, input [2:0] i, output o); assign o = a[i]; endmodule"
        )
        self.assertEqual(assign["value"]["width"], 1)
        self.assertEqual(assign["value"]["index"]["width"], 3)

    def test_constant_range_select_width(self):
        assign = only_assign("module m(input [7:0] a, output [4:0] o); assign o = a[4:0]; endmodule")
        self.assertEqual(assign["value"]["width"], 5)
        assign = only_assign("module m(input [7:0] a, output [3:0] o); assign o = a[0:3]; endmodule")
        self.assertEqual(assign["value"]["width"], 4)

    def test_concat_sums_item_widths(self):
        assign = only_assign(
            "module m(input [7:0] a, input b, output [10:0] o);"
            " assign o = {1'b1, a[7:4], b, 2'b10}; endmodule"
        )
        self.assertEqual(assign["value"]["width"], 1 + 4 + 1 + 2)

    def test_logical_reduction_and_comparison_are_one_bit(self):
        cases = {
            "!a": 1,
            "&a": 1,
            "|a": 1,
            "^a": 1,
            "~&a": 1,
            "~|a": 1,
            "~^a": 1,
            "^~a": 1,
            "a && b": 1,
            "a || b": 1,
            "a == b": 1,
            "a != b": 1,
            "a < b": 1,
            "a <= b": 1,
            "a > b": 1,
            "a >= b": 1,
        }
        for expr, expected in cases.items():
            assign = only_assign(
                f"module m(input [7:0] a, input b, output o); assign o = ({expr}); endmodule"
            )
            self.assertEqual(assign["value"]["width"], expected, expr)

    def test_unary_arithmetic_and_bitwise_not_keep_operand_width(self):
        for expr in ("+a", "-a", "~a"):
            assign = only_assign(
                f"module m(input [7:0] a, output [7:0] o); assign o = {expr}; endmodule"
            )
            self.assertEqual(assign["value"]["width"], 8, expr)

    def test_shift_uses_left_operand_width(self):
        for op in ("<<", ">>", "<<<", ">>>"):
            assign = only_assign(
                f"module m(input [7:0] a, input [3:0] s, output [7:0] o);"
                f" assign o = a {op} s; endmodule"
            )
            self.assertEqual(assign["value"]["width"], 8, op)

    def test_other_binary_ops_use_wider_operand(self):
        for op in ("+", "-", "*", "/", "%", "&", "|", "^", "~^", "^~", "**"):
            assign = only_assign(
                f"module m(input [3:0] a, input [7:0] b, output [7:0] o);"
                f" assign o = a {op} b; endmodule"
            )
            self.assertEqual(assign["value"]["width"], 8, op)
            assign = only_assign(
                f"module m(input [3:0] a, input [7:0] b, output [7:0] o);"
                f" assign o = b {op} a; endmodule"
            )
            self.assertEqual(assign["value"]["width"], 8, op)

    def test_nested_parenthesized_expression(self):
        assign = only_assign(
            "module m(input [3:0] a, input [1:0] b, output [3:0] o);"
            " assign o = ((a & 4'hF) | {2'b00, b}); endmodule"
        )
        self.assertEqual(assign["value"]["width"], 4)


class ConversionTest(unittest.TestCase):
    def test_exact(self):
        assign = only_assign("module m(input [7:0] a, output [7:0] o); assign o = a; endmodule")
        self.assertEqual(assign["target_width"], 8)
        self.assertEqual(assign["value_width"], 8)
        self.assertEqual(assign["conversion"], "exact")

    def test_zero_extend(self):
        assign = only_assign("module m(input [3:0] a, output [7:0] o); assign o = a; endmodule")
        self.assertEqual(assign["target_width"], 8)
        self.assertEqual(assign["value_width"], 4)
        self.assertEqual(assign["conversion"], "zero_extend")

    def test_truncate_keeps_low_target_width_bits_semantics(self):
        assign = only_assign("module m(input [7:0] a, output [3:0] o); assign o = a; endmodule")
        self.assertEqual(assign["target_width"], 4)
        self.assertEqual(assign["value_width"], 8)
        self.assertEqual(assign["conversion"], "truncate")

    def test_bit_targets(self):
        assign = only_assign(
            "module m(input [7:0] a, output o); assign o = a[0] == a[1]; endmodule"
        )
        self.assertEqual(assign["target_width"], 1)
        self.assertEqual(assign["conversion"], "exact")

    def test_multiple_assigns_order_and_conversions(self):
        ir = widths(
            "module m(input [7:0] a, output [3:0] lo, output [15:0] hi);\n"
            "assign lo = a;\n"
            "assign hi = a;\n"
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual([a["target"]["name"] for a in assigns], ["lo", "hi"])
        self.assertEqual(
            [(a["target_width"], a["value_width"], a["conversion"]) for a in assigns],
            [(4, 8, "truncate"), (16, 8, "zero_extend")],
        )

    def test_forward_referenced_wire_width(self):
        ir = widths(
            "module m(input a, output [3:0] o);\n"
            "assign o = w;\n"
            "wire [1:0] w;\n"
            "assign w = a;\n"
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual(assigns[0]["value"]["width"], 2)
        self.assertEqual(assigns[0]["conversion"], "zero_extend")
        self.assertEqual(assigns[1]["conversion"], "zero_extend")


class AnalyzeWidthsErrorTest(unittest.TestCase):
    def assert_error(self, source, code):
        with self.assertRaises(RTLParseError) as ctx:
            widths(source)
        exc = ctx.exception
        self.assertEqual(exc.code, code, source)
        self.assertGreaterEqual(exc.line, 1)
        self.assertGreaterEqual(exc.column, 1)
        self.assertTrue(exc.message)
        return exc

    def test_type_error_for_non_string(self):
        for bad in (None, 42, b"module m; endmodule", ["x"], {"source": "x"}):
            with self.assertRaises(TypeError):
                Service().analyze_widths(bad)

    def test_same_failures_as_parse(self):
        self.assert_error("", "syntax_error")
        self.assert_error("module m(input a); assign a = @; endmodule", "syntax_error")
        self.assert_error("module m(input a, output a); endmodule", "duplicate_name")
        self.assert_error("module m(output o); assign o = ghost; endmodule", "undeclared_signal")
        self.assert_error(
            "module m(input [7:0] a, output o); assign o = a[a:0]; endmodule", "invalid_range"
        )
        self.assert_error(
            "module m(input c, output reg o); endmodule", "unsupported_construct"
        )
        self.assert_error(
            "module m(input a, output o); always @* o = a; endmodule", "unsupported_construct"
        )
        self.assert_error(
            "module m #(parameter N = 1)(output o); assign o = 0; endmodule",
            "unsupported_construct",
        )
        self.assert_error(
            "module m(input a, output o); assign o = $signed(a); endmodule", "syntax_error"
        )


if __name__ == "__main__":
    unittest.main()
