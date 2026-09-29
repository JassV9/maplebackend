"""Smoke test: start the dashboard in --demo mode and check the page, the
state API and the live event stream all work."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.join(os.path.dirname(__file__), "..")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DemoServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, "server.py", "--demo", "--http-port", str(cls.port)],
                                    cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = "http://127.0.0.1:%d" % cls.port
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/state", timeout=1)
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def test_page_served(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        self.assertIn("Maple Gateway", html)

    def test_state_has_packets(self):
        time.sleep(2.5)
        state = json.load(urllib.request.urlopen(self.base + "/api/state", timeout=3))
        self.assertTrue(state["summary"]["serial"]["connected"])
        packets = [e for e in state["history"] if e["type"] == "packet" and e["payload"]["ours"]]
        self.assertTrue(packets)
        self.assertIn("fill_pct", packets[-1]["payload"])

    def test_event_stream(self):
        r = urllib.request.urlopen(self.base + "/events", timeout=10)
        line = b""
        while not line.startswith(b"data: "):
            line = r.readline()
        msg = json.loads(line[6:])
        self.assertIn("event", msg)
        self.assertIn("summary", msg)
        r.close()


if __name__ == "__main__":
    unittest.main()
