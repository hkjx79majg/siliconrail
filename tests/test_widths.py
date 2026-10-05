import json
import unittest

from siliconrail import RTLParseError
from siliconrail.service import Service

from test_http import HttpTestBase


def analyze(source):
    return Service().analyze_widths(source)


class AnalyzeWidthsDirectTest(unittest.TestCase):
    def test_ref_and_sized_const_use_declared_width(self):
        ir = analyze(
            "module m(input [7:0] a, output [7:0] o);\n"
            "assign o = a + 4'b1010;\n"
            "endmodule"
        )
        assign = ir["modules"][0]["assigns"][0]
        value = assign["value"]
        self.assertEqual(value["kind"], "binary")
        self.assertEqual(value["width"], 8)
        self.assertEqual(value["left"], {"kind": "ref", "name": "a", "width": 8})
        self.assertEqual(value["right"]["width"], 4)
        self.assertEqual(assign["target"]["width"], 8)
        self.assertEqual(
            (assign["target_width"], assign["value_width"], assign["conversion"]),
            (8, 8, "exact"),
        )

    def test_unsized_decimal_constants(self):
        ir = analyze(
            "module m(output [63:0] a, output [63:0] b, output [63:0] c);\n"
            "assign a = 0;\n"
            "assign b = 42;\n"
            "assign c = 4294967296;\n"  # 2**32
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual(assigns[0]["value"]["width"], 32)
        self.assertEqual(assigns[1]["value"]["width"], 32)
        self.assertEqual(assigns[2]["value"]["width"], 33)
        self.assertEqual(assigns[2]["conversion"], "zero_extend")

    def test_selects_and_concat(self):
        ir = analyze(
            "module m(input [15:0] a, output [7:0] o);\n"
            "wire [3:0] w;\n"
            "assign w = a[9:6];\n"
            "assign o = {a[3], a[11:8], 3'b101};\n"
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual(assigns[0]["value"]["width"], 4)
        self.assertEqual(assigns[0]["conversion"], "exact")
        concat = assigns[1]["value"]
        self.assertEqual(concat["kind"], "concat")
        self.assertEqual(concat["width"], 8)
        self.assertEqual([item["width"] for item in concat["items"]], [1, 4, 3])

    def test_operator_width_rules(self):
        ir = analyze(
            "module m(input [7:0] a, input [3:0] b, output [15:0] o);\n"
            "wire [7:0] w;\n"
            "assign w = a & b;\n"          # max(8, 4)
            "assign w = a << b;\n"         # left width
            "assign w = -a;\n"             # operand width
            "assign w = ~a;\n"
            "assign o[0] = !a;\n"
            "assign o[1] = &a;\n"
            "assign o[2] = a == b;\n"
            "assign o[3] = a && b;\n"
            "assign o[4] = a < b;\n"
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual(assigns[0]["value"]["width"], 8)
        self.assertEqual(assigns[1]["value"]["width"], 8)
        self.assertEqual(assigns[2]["value"]["width"], 8)
        self.assertEqual(assigns[3]["value"]["width"], 8)
        for assign in assigns[4:]:
            self.assertEqual(assign["value"]["width"], 1)
            self.assertEqual(assign["conversion"], "exact")

    def test_conversion_labels(self):
        ir = analyze(
            "module m(input [7:0] a, output [3:0] s, output [15:0] big, output [7:0] same);\n"
            "assign s = a;\n"
            "assign big = a;\n"
            "assign same = a;\n"
            "endmodule"
        )
        assigns = ir["modules"][0]["assigns"]
        self.assertEqual(assigns[0]["conversion"], "truncate")
        self.assertEqual(assigns[1]["conversion"], "zero_extend")
        self.assertEqual(assigns[2]["conversion"], "exact")

    def test_truncation_does_not_rewrite_constants(self):
        ir = analyze("module m(output [3:0] o); assign o = 8'hFF; endmodule")
        assign = ir["modules"][0]["assigns"][0]
        self.assertEqual(assign["conversion"], "truncate")
        self.assertEqual(assign["value"]["value"], 0xFF)
        self.assertEqual(assign["value"]["width"], 8)

    def test_parse_result_unchanged_and_source_order_preserved(self):
        source = (
            "module m(input [7:0] a, input [7:0] b, output [8:0] o);\n"
            "wire [8:0] s;\n"
            "assign s = a + b;\n"
            "assign o = s;\n"
            "endmodule"
        )
        service = Service()
        parsed = service.parse_rtl(source)
        analyzed = service.analyze_widths(source)
        # parse_rtl output is untouched by the width pass
        self.assertEqual(service.parse_rtl(source), parsed)
        self.assertNotIn("width", parsed["modules"][0]["assigns"][0]["target"])
        # same skeleton, in source order, with annotations added
        self.assertEqual([a["target"]["name"] for a in analyzed["modules"][0]["assigns"]], ["s", "o"])
        self.assertEqual(analyzed["modules"][0]["nets"], [{"name": "s", "width": 9}])
        # max(8, 8) for the add, then zero-extended onto the 9-bit net
        self.assertEqual(analyzed["modules"][0]["assigns"][0]["value_width"], 8)
        self.assertEqual(analyzed["modules"][0]["assigns"][0]["conversion"], "zero_extend")

    def test_json_serializable_and_deterministic(self):
        source = (
            "module m(input [3:0] a, output [7:0] o);\n"
            "assign o = {a, a[1:0], 2'b10} << 1;\n"
            "endmodule"
        )
        first = json.dumps(analyze(source), sort_keys=True)
        second = json.dumps(analyze(source), sort_keys=True)
        self.assertEqual(first, second)

    def test_non_string_raises_type_error(self):
        for bad in (None, 42, b"module m; endmodule", ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    Service().analyze_widths(bad)

    def test_parse_failures_match_parse_rtl(self):
        service = Service()
        sources = [
            "",
            "module m(input a, output o); assign o = ghost; endmodule",
            "module m(output o); assign o = 1; assign o = 2; endmodule\nmodule m; endmodule",
            "module m(output o); wire [3:0] w; assign o = w; always @* begin end endmodule",
            "module m(output o); assign o = 1'bx; endmodule",
        ]
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(RTLParseError) as direct:
                    service.analyze_widths(source)
                with self.assertRaises(RTLParseError) as baseline:
                    service.parse_rtl(source)
                for exc in (direct.exception, baseline.exception):
                    self.assertTrue(exc.message)
                self.assertEqual(
                    (direct.exception.code, direct.exception.line, direct.exception.column),
                    (baseline.exception.code, baseline.exception.line, baseline.exception.column),
                )


class AnalyzeWidthsHttpTest(HttpTestBase):
    def post_widths(self, body):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.request("POST", "/v1/rtl/widths", body=raw,
                            headers={"Content-Type": "application/json"})

    def test_success_matches_direct_entry(self):
        source = "module m(input [3:0] a, output [7:0] o); assign o = a; endmodule"
        status, payload = self.post_widths({"source": source})
        self.assertEqual(status, 200)
        self.assertEqual(payload, Service().analyze_widths(source))
        assign = payload["modules"][0]["assigns"][0]
        self.assertEqual(assign["conversion"], "zero_extend")

    def test_parse_error_maps_to_422_with_four_fields(self):
        status, payload = self.post_widths({"source": "module m(output o); assign o = nope; endmodule"})
        self.assertEqual(status, 422)
        self.assertEqual(set(payload["error"]), {"code", "line", "column", "message"})
        self.assertEqual(payload["error"]["code"], "undeclared_signal")

    def test_invalid_requests_map_to_400(self):
        for body in (json.dumps([1]), json.dumps({}), json.dumps({"source": 7}), "{not json"):
            with self.subTest(body=body):
                status, payload = self.post_widths(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_extra_fields_are_allowed(self):
        source = "module m(input a, output o); assign o = a; endmodule"
        status, payload = self.post_widths({"source": source, "extra": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["modules"][0]["assigns"][0]["conversion"], "exact")

    def test_parse_endpoint_still_unannotated(self):
        source = "module m(input a, output o); assign o = a; endmodule"
        status, payload = self.post({"source": source})
        self.assertEqual(status, 200)
        self.assertNotIn("width", payload["modules"][0]["assigns"][0]["target"])
        self.assertNotIn("conversion", payload["modules"][0]["assigns"][0])


if __name__ == "__main__":
    unittest.main()
