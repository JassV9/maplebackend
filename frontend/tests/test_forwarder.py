"""Forwarder: batches gateway lines to the worker and spools them while it is unreachable."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from forwarder import Forwarder, to_line  # noqa: E402
from packet_parser import parse_line  # noqa: E402

PACKET = '{"type":"packet","n":3,"rssi":-40.0,"snr":9.5,"len":58,"crc":true,"raw":"{\\"id\\":\\"LC01\\",\\"k\\":\\"live\\",\\"s\\":2,\\"w\\":3.412}"}'


class FakeWorker:
    def __init__(self):
        self.bodies = []
        self.headers = []
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                worker.bodies.append(body)
                worker.headers.append(dict(self.headers))
                out = json.dumps({"stored": len(body["lines"])}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class ForwarderTest(unittest.TestCase):
    def test_line_keeps_gateway_fields_only(self):
        ev = parse_line(PACKET, now=1_800_000_000)
        line = to_line(ev)
        self.assertEqual(set(line), {"type", "n", "rssi", "snr", "len", "crc", "raw", "received_at"})
        self.assertTrue(line["received_at"].startswith("2027-01-15T08:00:00"))

    def test_batches_and_sends_key(self):
        worker = FakeWorker()
        try:
            fwd = Forwarder(worker.url, "GW-TEST", ingest_key="k1", interval=0.05)
            for _ in range(3):
                fwd.submit(parse_line(PACKET))
            fwd.submit({"type": "log", "text": "ignored"})
            fwd.start()
            fwd.stop()
            lines = [line for body in worker.bodies for line in body["lines"]]
            self.assertEqual(len(lines), 3)
            self.assertEqual(worker.bodies[0]["gateway"], "GW-TEST")
            self.assertEqual(worker.headers[0].get("X-Ingest-Key"), "k1")
            self.assertEqual(fwd.stats["sent"], 3)
        finally:
            worker.close()

    def test_spools_while_down_then_resends(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = os.path.join(tmp, "spool.jsonl")
            down = Forwarder("http://127.0.0.1:9", "GW-TEST", spool_path=spool, timeout=1, log=lambda *_: None)
            down.submit(parse_line(PACKET))
            down.submit(parse_line(PACKET))
            down._drain_once()
            self.assertEqual(down.stats["spooled"], 2)
            self.assertTrue(os.path.exists(spool))

            worker = FakeWorker()
            try:
                up = Forwarder(worker.url, "GW-TEST", spool_path=spool, log=lambda *_: None)
                up.submit(parse_line(PACKET))
                up._drain_once()
                sent = sum(len(body["lines"]) for body in worker.bodies)
                self.assertEqual(sent, 3)
                self.assertFalse(os.path.exists(spool))
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
