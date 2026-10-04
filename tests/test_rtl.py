import json
import unittest

from siliconrail import RTLParseError
from siliconrail.service import Service


def parse(source):
    return Service().parse_rtl(source)


class ParseRtlStructureTest(unittest.TestCase):
    def test_single_module_ir(self):
        ir = parse("module m(input a, output b); assign b = a; endmodule")
        self.assertEqual([m["name"] for m in ir["modules"]], ["m"])
        mod = ir["modules"][0]
        self.assertEqual(
            mod["ports"],
            [
                {"name": "a", "direction": "input", "width": 1},
                {"name": "b", "direction": "output", "width": 1},
            ],
        )
        self.assertEqual(mod["nets"], [])
        self.assertEqual(len(mod["assigns"]), 1)
        self.assertEqual(mod["assigns"][0]["target"], {"kind": "ref", "name": "b"})
        self.assertEqual(mod["assigns"][0]["value"], {"kind": "ref", "name": "a"})
        json.dumps(ir, sort_keys=True)

    def test_multiple_modules_in_source_order(self):
        ir = parse(
            "module first(output o); assign o = 1; endmodule\n"
            "module second(output o); assign o = 0; endmodule"
        )
        self.assertEqual([m["name"] for m in ir["modules"]], ["first", "second"])

    def test_widths_normalized(self):
        ir = parse(
            "module m(input [7:0] a, output [0:3] b, inout c);\n"
            "wire [15:0] w;\n"
            "wire s;\n"
            "assign b = a[3:0];\n"
            "endmodule"
        )
        mod = ir["modules"][0]
        self.assertEqual({p["name"]: p["width"] for p in mod["ports"]}, {"a": 8, "b": 4, "c": 1})
        self.assertEqual(mod["nets"], [{"name": "w", "width": 16}, {"name": "s", "width": 1}])

    def test_direction_groups_and_ansi_wire_keyword(self):
        ir = parse("module m(input wire [3:0] a, b, output c, inout d); assign c = a; endmodule")
        self.assertEqual(
            [(p["name"], p["direction"], p["width"]) for p in ir["modules"][0]["ports"]],
            [("a", "input", 4), ("b", "input", 4), ("c", "output", 1), ("d", "inout", 1)],
        )

    def test_comments_and_case_sensitivity(self):
        ir = parse(
            "// line comment\n"
            "/* block\n comment */\n"
            "module m(input A, input a, output o); assign o = A ^ a; endmodule"
        )
        self.assertEqual([p["name"] for p in ir["modules"][0]["ports"]], ["A", "a", "o"])

    def test_constants(self):
        value = parse("module m(output [7:0] o); assign o = 8'hFF; endmodule")["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value, {"kind": "const", "value": 255, "width": 8})
        value = parse("module m(output o); assign o = 4'b1010; endmodule")["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value, {"kind": "const", "value": 10, "width": 4})
        value = parse("module m(output [7:0] o); assign o = 42; endmodule")["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value, {"kind": "const", "value": 42, "width": None})
        value = parse("module m(output [11:0] o); assign o = 12'hAB_C; endmodule")["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value, {"kind": "const", "value": 0xABC, "width": 12})

    def test_selects_concat_and_ops(self):
        ir = parse(
            "module m(input [7:0] a, input b, output [3:0] o);\n"
            "assign o = ~(a[7:4] & {a[0], 3'b000}) + (b << 1);\n"
            "endmodule"
        )
        value = ir["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value["kind"], "binary")
        self.assertEqual(value["op"], "+")
        inner = value["left"]["operand"]
        self.assertEqual(
            inner["left"],
            {"kind": "range_select", "base": {"kind": "ref", "name": "a"}, "msb": 7, "lsb": 4},
        )
        concat = inner["right"]
        self.assertEqual(concat["kind"], "concat")
        self.assertEqual(
            concat["items"][0],
            {"kind": "bit_select", "base": {"kind": "ref", "name": "a"},
             "index": {"kind": "const", "value": 0, "width": None}},
        )
        self.assertEqual(concat["items"][1], {"kind": "const", "value": 0, "width": 3})

    def test_unary_reduction_and_logical_ops(self):
        ir = parse("module m(input [3:0] a, input b, output o); assign o = &a || !b; endmodule")
        value = ir["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value["op"], "||")
        self.assertEqual(value["left"], {"kind": "unary", "op": "&",
                                         "operand": {"kind": "ref", "name": "a"}})

    def test_operator_precedence_and_determinism(self):
        source = "module m(input a, input b, input c, output o); assign o = a | b & c; endmodule"
        first = parse(source)
        second = parse(source)
        self.assertEqual(first, second)
        value = first["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value["op"], "|")
        self.assertEqual(value["right"]["op"], "&")

    def test_assigns_nets_and_ports_keep_declaration_order(self):
        ir = parse(
            "module m(input a, output x, output y);\n"
            "wire w1;\n"
            "assign x = a;\n"
            "wire w2;\n"
            "assign y = ~a;\n"
            "endmodule"
        )
        mod = ir["modules"][0]
        self.assertEqual([a["target"]["name"] for a in mod["assigns"]], ["x", "y"])
        self.assertEqual([n["name"] for n in mod["nets"]], ["w1", "w2"])

    def test_forward_reference_to_wire_declared_later(self):
        ir = parse(
            "module m(input a, output o);\n"
            "assign o = w;\n"
            "wire w;\n"
            "assign w = a;\n"
            "endmodule"
        )
        self.assertEqual([n["name"] for n in ir["modules"][0]["nets"]], ["w"])

    def test_bit_select_accepts_expression_index(self):
        ir = parse("module m(input [7:0] a, input [2:0] i, output o); assign o = a[i]; endmodule")
        value = ir["modules"][0]["assigns"][0]["value"]
        self.assertEqual(value, {"kind": "bit_select", "base": {"kind": "ref", "name": "a"},
                                 "index": {"kind": "ref", "name": "i"}})


class ParseRtlErrorTest(unittest.TestCase):
    def assert_error(self, source, code):
        with self.assertRaises(RTLParseError) as ctx:
            parse(source)
        exc = ctx.exception
        self.assertEqual(exc.code, code, source)
        self.assertGreaterEqual(exc.line, 1)
        self.assertGreaterEqual(exc.column, 1)
        self.assertIsInstance(exc.message, str)
        self.assertTrue(exc.message)
        self.assertEqual(str(exc), exc.message)
        return exc

    def test_type_error_for_non_string(self):
        for bad in (None, 42, b"module m; endmodule", ["x"], {"source": "x"}):
            with self.assertRaises(TypeError):
                Service().parse_rtl(bad)

    def test_empty_source_is_syntax_error(self):
        self.assert_error("", "syntax_error")
        self.assert_error("   \n // nothing\n", "syntax_error")

    def test_lexical_errors(self):
        self.assert_error("module m(input a); assign a = @; endmodule", "syntax_error")
        self.assert_error("module m(input a); /* unterminated", "syntax_error")
        self.assert_error("module m(output o); assign o = 4'bx; endmodule", "syntax_error")
        self.assert_error("module m(output o); assign o = 4'o7; endmodule", "syntax_error")

    def test_incomplete_constructs(self):
        self.assert_error("module m(input a;", "syntax_error")
        self.assert_error("module m(input a); assign a = (a & a; endmodule", "syntax_error")
        self.assert_error("module m(input a); assign a = a", "syntax_error")
        self.assert_error("module m(input a);", "syntax_error")
        self.assert_error("module m(input a); assign a = a;", "syntax_error")

    def test_duplicate_names(self):
        exc = self.assert_error(
            "module m(output o); assign o = 0; endmodule\n"
            "module m(output o); assign o = 1; endmodule",
            "duplicate_name",
        )
        self.assertEqual(exc.line, 2)
        self.assert_error("module m(input a, output a); endmodule", "duplicate_name")
        self.assert_error("module m(input a); wire a; endmodule", "duplicate_name")
        self.assert_error("module m(output o); wire w, w; assign o = w; endmodule", "duplicate_name")

    def test_undeclared_signal(self):
        exc = self.assert_error(
            "module m(input a, output o);\nassign o = missing;\nendmodule",
            "undeclared_signal",
        )
        self.assertEqual(exc.line, 2)
        self.assert_error("module m(output o); assign ghost = 1; endmodule", "undeclared_signal")

    def test_invalid_range(self):
        self.assert_error("module m(input [7:0] a, output o); assign o = a[-1:0]; endmodule", "invalid_range")
        self.assert_error("module m(input [7:0] a, output o); assign o = a[a:0]; endmodule", "invalid_range")
        self.assert_error("module m(input [3:0] a, output o); wire [a:0] w; assign o = w; endmodule", "invalid_range")
        self.assert_error("module m(input [3:0] a, output o); wire [2'b10:0] w; assign o = w; endmodule", "invalid_range")

    def test_unsupported_constructs(self):
        self.assert_error("module m(input c, output reg o); endmodule", "unsupported_construct")
        self.assert_error("module m(input a, output o); always @* o = a; endmodule", "unsupported_construct")
        self.assert_error("module m(output o); initial o = 0; endmodule", "unsupported_construct")
        self.assert_error("module m(output o); sub u(.o(o)); endmodule", "unsupported_construct")
        self.assert_error("module m #(parameter N = 1)(output o); assign o = 0; endmodule", "unsupported_construct")
        self.assert_error("module m(output o); parameter N = 1; assign o = N; endmodule", "unsupported_construct")
        self.assert_error("module m(a); endmodule", "unsupported_construct")


if __name__ == "__main__":
    unittest.main()
