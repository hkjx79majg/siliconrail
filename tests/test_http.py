import json
import threading
import unittest
from http.client import HTTPConnection

from siliconrail.server import Handler
from http.server import ThreadingHTTPServer


class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw.decode("utf-8"))

    def post(self, body, content_type="application/json"):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.request("POST", "/v1/rtl/parse", body=raw, headers={"Content-Type": content_type})


class RtlParseEndpointTest(HttpTestBase):
    def test_success_matches_direct_entry(self):
        source = "module m(input a, output o); assign o = ~a; endmodule"
        status, payload = self.post({"source": source})
        self.assertEqual(status, 200)
        from siliconrail.service import Service

        self.assertEqual(payload, Service().parse_rtl(source))

    def test_parse_error_maps_to_422_with_four_fields(self):
        status, payload = self.post({"source": "module m(output o);\nassign o = nope;\nendmodule"})
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(set(error), {"code", "line", "column", "message"})
        self.assertEqual(error["code"], "undeclared_signal")
        self.assertEqual(error["line"], 2)
        self.assertIsInstance(error["column"], int)
        self.assertTrue(error["message"])
        self.assertNotIn("modules", payload)

    def test_invalid_requests_map_to_400(self):
        bad_bodies = [
            json.dumps([1, 2]),  # not an object
            json.dumps("module m; endmodule"),  # not an object
            json.dumps({}),  # missing source
            json.dumps({"source": 42}),  # source not a string
            json.dumps({"source": None}),
            "{not json",  # malformed JSON
            "module m; endmodule",  # not JSON at all
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                status, payload = self.post(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_invalid_utf8_is_invalid_request(self):
        status, payload = self.post(b'{"source": "module \xff"}')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_empty_source_maps_to_422_syntax_error(self):
        status, payload = self.post({"source": ""})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "syntax_error")

    def test_unknown_post_path_is_404(self):
        status, payload = self.request("POST", "/nope", body="{}", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


class BaselineBehaviorTest(HttpTestBase):
    def test_healthz_unchanged(self):
        status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "siliconrail")
        self.assertIn("version", payload)

    def test_unknown_get_path_404_structure(self):
        status, payload = self.request("GET", "/whatever")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertIn("no route for /whatever", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
