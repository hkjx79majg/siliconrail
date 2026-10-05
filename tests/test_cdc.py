import copy
import json
import unittest

from siliconrail.service import Service


def dff(name, clk, d, q, **kw):
    cell = {"kind": "dff", "name": name, "clk": clk, "d": d, "q": q}
    cell.update(kw)
    return cell


def logic(name, inputs, output, op="buf"):
    return {"kind": "logic", "name": name, "op": op, "inputs": inputs, "output": output}


def design(cells=(), clocks=(), ports=(), nets=(), top="top", **extra):
    result = {
        "top": top,
        "elaborated": True,
        "clocks": list(clocks),
        "ports": list(ports),
        "nets": list(nets),
        "cells": list(cells),
    }
    result.update(extra)
    return result


def two_clock_crossing(dest_cells, extra_cells=(), **kw):
    """Source register in clk_a feeding whatever ``dest_cells`` need."""
    cells = [
        dff("u_tx/req", "clk_a", "u_tx/req_d", "u_tx/req_q",
            source={"module": "tx", "line": 3, "column": 2}),
        *extra_cells,
        *dest_cells,
    ]
    return design(
        cells,
        clocks=[{"name": "clk_a"}, {"name": "clk_b"}],
        **kw,
    )


class CdcDirectTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def check(self, design_dict, constraints=None):
        return self.service.check_cdc(design_dict, constraints)

    # -- trivial designs -----------------------------------------------------

    def test_empty_design_returns_zero_diagnostics(self):
        for empty in ({"elaborated": True}, design()):
            report = self.check(empty)
            self.assertEqual(report["diagnostics"], [])
            self.assertEqual(
                report["summary"], {"total": 0, "error": 0, "warning": 0, "info": 0}
            )
            self.assertEqual(report["domains"], [])
            self.assertEqual(report["clock_relations"], [])
            json.dumps(report, sort_keys=True)

    def test_pure_combinational_design_returns_zero_diagnostics(self):
        report = self.check(
            design(
                cells=[logic("g0", ["a"], "n0"), logic("g1", ["n0"], "o")],
                ports=[
                    {"name": "a", "direction": "input"},
                    {"name": "o", "direction": "output"},
                ],
            )
        )
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(report["summary"]["total"], 0)

    def test_same_domain_paths_produce_no_diagnostics(self):
        report = self.check(
            design(
                cells=[
                    dff("r1", "clk", "d1", "q1"),
                    logic("g", ["q1"], "n"),
                    dff("r2", "clk", "n", "q2"),
                ],
                clocks=[{"name": "clk"}],
            )
        )
        self.assertEqual(report["diagnostics"], [])

    def test_same_clock_opposite_edges_are_synchronous(self):
        report = self.check(
            design(
                cells=[
                    dff("r1", "clk", "d1", "q1"),
                    dff("r2", "clk", "q1", "q2", edge="negedge"),
                ],
                clocks=[{"name": "clk"}],
            )
        )
        self.assertEqual(report["diagnostics"], [])

    def test_constant_nets_produce_no_diagnostics(self):
        report = self.check(
            two_clock_crossing(
                [dff("u_rx/cfg", "clk_b", "tie0", "u_rx/cfg_q")],
                nets=[{"name": "tie0", "const": 0}],
            )
        )
        self.assertEqual(report["diagnostics"], [])

    # -- basic crossing categories -------------------------------------------

    def test_unsynchronized_single_flop_capture(self):
        report = self.check(two_clock_crossing([dff("u_rx/req", "clk_b", "u_tx/req_q", "u_rx/req_q")]))
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "state_element")
        self.assertEqual(diag["severity"], "error")
        self.assertEqual(diag["source_domain"], "clk_a")
        self.assertEqual(diag["dest_domain"], "clk_b")
        self.assertEqual(diag["source"], "u_tx/req")
        self.assertEqual(diag["dest"], "u_rx/req")
        self.assertEqual(diag["width"], 1)
        self.assertEqual(diag["source_info"], {"module": "tx", "line": 3, "column": 2})
        self.assertTrue(diag["message"])
        self.assertEqual(report["summary"]["error"], 1)

    def test_crossing_through_combinational_logic(self):
        report = self.check(
            two_clock_crossing(
                [
                    logic("u_rx/g0", ["u_tx/req_q"], "u_rx/n0", op="and"),
                    dff("u_rx/req", "clk_b", "u_rx/n0", "u_rx/req_q"),
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "combinational")
        self.assertEqual(diag["severity"], "error")

    def test_two_stage_synchronizer_is_marked_synchronized(self):
        report = self.check(
            two_clock_crossing(
                [
                    dff("u_rx/sync1", "clk_b", "u_tx/req_q", "u_rx/s1"),
                    dff("u_rx/sync2", "clk_b", "u_rx/s1", "u_rx/s2"),
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "synchronized")
        self.assertEqual(diag["severity"], "info")
        self.assertEqual(report["summary"]["error"], 0)
        self.assertEqual(report["summary"]["info"], 1)

    def test_synchronizer_first_stage_fanout_is_unsafe(self):
        report = self.check(
            two_clock_crossing(
                [
                    dff("u_rx/sync1", "clk_b", "u_tx/req_q", "u_rx/s1"),
                    dff("u_rx/sync2", "clk_b", "u_rx/s1", "u_rx/s2"),
                    logic("u_rx/g0", ["u_rx/s1"], "u_rx/n0"),
                    dff("u_rx/other", "clk_b", "u_rx/n0", "u_rx/oq"),
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "sync_chain_fanout")
        self.assertEqual(diag["severity"], "error")

    def test_combinational_logic_before_chain_is_not_synchronized(self):
        report = self.check(
            two_clock_crossing(
                [
                    logic("u_rx/g0", ["u_tx/req_q"], "u_rx/n0"),
                    dff("u_rx/sync1", "clk_b", "u_rx/n0", "u_rx/s1"),
                    dff("u_rx/sync2", "clk_b", "u_rx/s1", "u_rx/s2"),
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["category"], "combinational")

    # -- multi-bit and gray code ----------------------------------------------

    def _bus_crossing(self, gray=False):
        src = dff("u_tx/ptr", "clk_a", "u_tx/ptr_d", "u_tx/ptr_q", width=4)
        if gray:
            src["gray_code"] = True
        return design(
            cells=[
                src,
                dff("u_rx/sync1", "clk_b", "u_tx/ptr_q", "u_rx/p1", width=4),
                dff("u_rx/sync2", "clk_b", "u_rx/p1", "u_rx/p2", width=4),
            ],
            clocks=[{"name": "clk_a"}, {"name": "clk_b"}],
        )

    def test_multi_bit_per_bit_synchronizers_still_flag_coherence(self):
        report = self.check(self._bus_crossing())
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "multi_bit_coherence")
        self.assertEqual(diag["severity"], "warning")
        self.assertEqual(diag["width"], 4)

    def test_gray_code_pointer_is_recognized(self):
        report = self.check(self._bus_crossing(gray=True))
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "gray_code")
        self.assertEqual(diag["severity"], "info")

    def test_multi_bit_without_synchronizer_is_unsafe(self):
        report = self.check(
            design(
                cells=[
                    dff("u_tx/ptr", "clk_a", "u_tx/ptr_d", "u_tx/ptr_q", width=4),
                    dff("u_rx/ptr", "clk_b", "u_tx/ptr_q", "u_rx/ptr_q", width=4),
                ],
                clocks=[{"name": "clk_a"}, {"name": "clk_b"}],
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "state_element")
        self.assertEqual(diag["severity"], "error")
        self.assertEqual(diag["width"], 4)

    # -- memory and reset -------------------------------------------------------

    def test_memory_write_control_crossing(self):
        report = self.check(
            two_clock_crossing(
                [
                    {
                        "kind": "memory",
                        "name": "u_mem/ram",
                        "write_clock": "clk_b",
                        "write_enable": "u_tx/req_q",
                        "write_data": "u_mem/wdata",
                    }
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "memory_write_control")
        self.assertEqual(diag["severity"], "error")
        self.assertEqual(diag["dest"], "u_mem/ram")

    def test_async_reset_release_crossing(self):
        report = self.check(
            two_clock_crossing(
                [dff("u_rx/rst_ff", "clk_b", "u_rx/d", "u_rx/q", async_reset="u_tx/req_q")]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "async_reset_release")
        self.assertEqual(diag["severity"], "error")

    def test_declared_reset_downgrades_release_crossing(self):
        target = two_clock_crossing(
            [dff("u_rx/rst_ff", "clk_b", "u_rx/d", "u_rx/q", async_reset="u_tx/req_q")]
        )
        report = self.check(target, {"resets": [{"name": "u_tx/req_q", "clock": "clk_b"}]})
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["category"], "async_reset_release")
        self.assertEqual(diag["severity"], "info")

    # -- quasi-static and external sources ---------------------------------------

    def test_external_input_crossing_reported_unless_quasi_static(self):
        target = design(
            cells=[dff("u_rx/cfg", "clk_b", "cfg_mode", "u_rx/cfg_q")],
            clocks=[{"name": "clk_b"}],
            ports=[{"name": "cfg_mode", "direction": "input", "width": 2}],
        )
        report = self.check(target)
        self.assertEqual(len(report["diagnostics"]), 1)
        diag = report["diagnostics"][0]
        self.assertEqual(diag["source_domain"], "external")
        self.assertEqual(diag["severity"], "error")
        self.assertEqual(diag["width"], 2)

        quiet = self.check(target, {"quasi_static": ["cfg_mode"]})
        self.assertEqual(quiet["diagnostics"], [])

    def test_quasi_static_register_source_is_suppressed(self):
        target = two_clock_crossing([dff("u_rx/req", "clk_b", "u_tx/req_q", "u_rx/req_q")])
        report = self.check(target, {"quasi_static": ["u_tx/req"]})
        self.assertEqual(report["diagnostics"], [])

    # -- convergence and determinism ----------------------------------------------

    def test_reconvergent_paths_collapse_to_one_diagnostic(self):
        report = self.check(
            two_clock_crossing(
                [
                    logic("u_rx/g0", ["u_tx/req_q"], "u_rx/n0"),
                    logic("u_rx/g1", ["u_tx/req_q"], "u_rx/n1"),
                    logic("u_rx/g2", ["u_rx/n0", "u_rx/n1"], "u_rx/n2", op="or"),
                    dff("u_rx/req", "clk_b", "u_rx/n2", "u_rx/req_q"),
                ]
            )
        )
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["category"], "combinational")

    def test_diagnostics_sorted_by_hierarchical_endpoints(self):
        report = self.check(
            design(
                cells=[
                    dff("b/src2", "clk_a", "d", "b/src2_q"),
                    dff("a/src1", "clk_a", "d", "a/src1_q"),
                    dff("z/dst2", "clk_b", "b/src2_q", "z/dst2_q"),
                    dff("y/dst1", "clk_b", "a/src1_q", "y/dst1_q"),
                    dff("x/dst0", "clk_b", "a/src1_q", "x/dst0_q"),
                ],
                clocks=[{"name": "clk_a"}, {"name": "clk_b"}],
            )
        )
        keys = [(d["source"], d["dest"]) for d in report["diagnostics"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            keys,
            [("a/src1", "x/dst0"), ("a/src1", "y/dst1"), ("b/src2", "z/dst2")],
        )

    def test_report_is_deterministic_and_serializable(self):
        target = two_clock_crossing([dff("u_rx/req", "clk_b", "u_tx/req_q", "u_rx/req_q")])
        first = self.check(target)
        second = self.check(target)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

    def test_check_does_not_mutate_design_or_constraints(self):
        target = two_clock_crossing([dff("u_rx/req", "clk_b", "u_tx/req_q", "u_rx/req_q")])
        constraints = {"async_clock_groups": [["clk_a"], ["clk_b"]]}
        snapshot = (copy.deepcopy(target), copy.deepcopy(constraints))
        self.check(target, constraints)
        self.assertEqual((target, constraints), snapshot)

    # -- clock relations -----------------------------------------------------------

    def test_default_relation_between_roots_is_potentially_asynchronous(self):
        report = self.check(two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")]))
        relations = {
            (r["clock_a"], r["clock_b"]): r["relation"] for r in report["clock_relations"]
        }
        self.assertEqual(relations[("clk_a", "clk_b")], "potentially_asynchronous")
        self.assertEqual(len(report["diagnostics"]), 1)

    def test_integer_divided_clock_is_synchronous(self):
        report = self.check(
            design(
                cells=[
                    dff("r1", "clk", "d1", "q1"),
                    dff("r2", "clk_div2", "q1", "q2"),
                ],
                clocks=[{"name": "clk"}, {"name": "clk_div2", "derived_from": "clk", "divide_by": 2}],
            )
        )
        self.assertEqual(report["diagnostics"], [])
        relations = {
            (r["clock_a"], r["clock_b"]): r["relation"] for r in report["clock_relations"]
        }
        self.assertEqual(relations[("clk", "clk_div2")], "synchronous")
        domains = {d["name"]: d for d in report["domains"]}
        self.assertEqual(domains["clk_div2"]["root"], "clk")
        self.assertEqual(domains["clk_div2"]["divide_by"], 2)

    def test_async_clock_groups_declare_relation(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        report = self.check(target, {"async_clock_groups": [["clk_a"], ["clk_b"]]})
        relations = {
            (r["clock_a"], r["clock_b"]): r["relation"] for r in report["clock_relations"]
        }
        self.assertEqual(relations[("clk_a", "clk_b")], "asynchronous")
        self.assertEqual(len(report["diagnostics"]), 1)

    def test_clocks_in_one_async_group_are_synchronous(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        report = self.check(target, {"async_clock_groups": [["clk_a", "clk_b"]]})
        self.assertEqual(report["diagnostics"], [])

    def test_synchronous_clocks_constraint_suppresses_crossing(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        report = self.check(target, {"synchronous_clocks": [["clk_a", "clk_b"]]})
        self.assertEqual(report["diagnostics"], [])

    # -- failure semantics -----------------------------------------------------------

    def test_type_errors(self):
        with self.assertRaises(TypeError):
            self.check("not a dict")
        with self.assertRaises(TypeError):
            self.check(design(), constraints=["not a dict"])

    def test_unelaborated_designs_raise_value_error(self):
        bad_designs = [
            {"elaborated": False, "cells": []},
            {"modules": [{"name": "m", "ports": [], "nets": [], "assigns": []}]},
            design(instances=[{"module": "sub", "name": "u_sub"}]),
            design(parameters={"WIDTH": 8}),
        ]
        for bad in bad_designs:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.check(bad)

    def test_unresolvable_clock_connection_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.check(
                design(
                    cells=[
                        logic("g", ["a"], "n"),
                        dff("r", "n", "d", "q"),
                    ]
                )
            )

    def test_malformed_derived_clocks_raise_value_error(self):
        base = [dff("r", "clk_div", "d", "q")]
        with self.assertRaises(ValueError):
            self.check(
                design(base, clocks=[{"name": "clk_div", "derived_from": "ghost", "divide_by": 2}])
            )
        with self.assertRaises(ValueError):
            self.check(
                design(
                    base,
                    clocks=[{"name": "clk"}, {"name": "clk_div", "derived_from": "clk", "divide_by": 2.5}],
                )
            )
        with self.assertRaises(ValueError):
            self.check(
                design(
                    base,
                    clocks=[
                        {"name": "clk_div", "derived_from": "clk_div2", "divide_by": 2},
                        {"name": "clk_div2", "derived_from": "clk_div", "divide_by": 2},
                    ],
                )
            )

    def test_conflicting_sync_async_constraints_raise_value_error(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        with self.assertRaises(ValueError):
            self.check(
                target,
                {
                    "async_clock_groups": [["clk_a"], ["clk_b"]],
                    "synchronous_clocks": [["clk_a", "clk_b"]],
                },
            )

    def test_constraints_referencing_unknown_objects_raise_key_error(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        bad_constraints = [
            {"quasi_static": ["u_ghost/sig"]},
            {"async_clock_groups": [["clk_a", "clk_ghost"]]},
            {"synchronous_clocks": [["clk_ghost"]]},
            {"resets": [{"name": "rst_ghost"}]},
            {"resets": [{"name": "u_tx/req_q", "clock": "clk_ghost"}]},
        ]
        for constraints in bad_constraints:
            with self.subTest(constraints=constraints):
                with self.assertRaises(KeyError):
                    self.check(target, constraints)

    def test_unsafe_crossings_are_results_not_failures(self):
        report = self.check(two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")]))
        self.assertEqual(report["summary"]["error"], 1)


class CdcHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from siliconrail.server import Handler

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, body):
        import json as jsonlib
        from http.client import HTTPConnection

        raw = body if isinstance(body, (bytes, str)) else jsonlib.dumps(body)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/v1/cdc/check", body=raw,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = jsonlib.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_success_matches_direct_entry(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        status, payload = self.post({"design": target})
        self.assertEqual(status, 200)
        self.assertEqual(payload, Service().check_cdc(target))

    def test_constraints_are_accepted(self):
        target = two_clock_crossing([dff("u_rx/r", "clk_b", "u_tx/req_q", "u_rx/q")])
        status, payload = self.post(
            {"design": target, "constraints": {"synchronous_clocks": [["clk_a", "clk_b"]]}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["diagnostics"], [])

    def test_invalid_requests_map_to_400(self):
        for body in ("{not json", json.dumps([1]), json.dumps({}),
                     json.dumps({"design": "x"}),
                     json.dumps({"design": {}, "constraints": []})):
            with self.subTest(body=body):
                status, payload = self.post(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_value_error_maps_to_422_invalid_design(self):
        status, payload = self.post({"design": {"elaborated": False}})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "invalid_design")
        self.assertTrue(payload["error"]["message"])

    def test_key_error_maps_to_422_unknown_object(self):
        status, payload = self.post(
            {"design": design(), "constraints": {"quasi_static": ["ghost"]}}
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "unknown_object")
        self.assertTrue(payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
