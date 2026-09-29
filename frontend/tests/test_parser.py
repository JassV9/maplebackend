import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from packet_parser import (MetricsTracker, derive_metrics, is_sudden_drop,  # noqa: E402
                           parse_line, parse_payload)


def packet_line(payload, crc=True, rssi=-50.0, snr=9.0):
    raw = json.dumps(payload, separators=(",", ":")) if isinstance(payload, dict) else payload
    return json.dumps({"type": "packet", "n": 1, "rssi": rssi, "snr": snr,
                       "len": len(raw), "crc": crc, "raw": raw})


class ParseLineTests(unittest.TestCase):
    def test_blank_line_is_ignored(self):
        self.assertIsNone(parse_line("   \r"))

    def test_plain_text_becomes_log(self):
        ev = parse_line("ESP-ROM:esp32s3-20210327", now=5)
        self.assertEqual(ev, {"type": "log", "text": "ESP-ROM:esp32s3-20210327", "ts": 5})

    def test_status_line(self):
        ev = parse_line('{"type":"status","role":"gateway","uptime":10,"radio":"ok","rx":2}')
        self.assertEqual(ev["type"], "status")
        self.assertEqual(ev["rx"], 2)

    def test_live_packet_is_parsed(self):
        ev = parse_line(packet_line({"id": "LC01", "k": "live", "s": 7, "w": 3.412, "r": 151234, "hx": 1}))
        p = ev["payload"]
        self.assertTrue(p["ours"])
        self.assertEqual((p["node_id"], p["kind"], p["seq"]), ("LC01", "live", 7))
        self.assertAlmostEqual(p["weight_kg"], 3.412)
        self.assertTrue(p["calibrated"])
        self.assertEqual(p["flags"], [])

    def test_gateway_escaping_round_trips(self):
        # Exactly what the gateway firmware prints for a node packet.
        line = '{"type":"packet","n":1,"rssi":-41.0,"snr":9.75,"len":44,"crc":true,' \
               '"raw":"{\\"id\\":\\"LC01\\",\\"k\\":\\"test\\",\\"s\\":0,\\"w\\":0.250,\\"i\\":1,\\"of\\":20}"}'
        p = parse_line(line)["payload"]
        self.assertEqual(p["kind"], "test")
        self.assertEqual((p["test_step"], p["test_total"]), (1, 20))

    def test_uncalibrated_flag(self):
        p = parse_payload('{"id":"LC01","k":"live","s":1,"w":0.1,"r":10,"hx":1,"uncal":1}')
        self.assertFalse(p["calibrated"])

    def test_foreign_packet(self):
        p = parse_payload('{"Node_Code":"X","Weight":1}')
        self.assertFalse(p["ours"])
        self.assertIn("foreign", p["flags"])

    def test_garbage_payload(self):
        p = parse_payload("\x01\x02garbage")
        self.assertFalse(p["ours"])
        self.assertIn("not_json", p["flags"])

    def test_out_of_range_weight_flagged(self):
        p = parse_payload('{"id":"LC01","k":"live","s":1,"w":999.0}')
        self.assertIn("out_of_range", p["flags"])

    def test_no_hx711(self):
        p = parse_payload('{"id":"LC01","k":"nohx","s":1,"w":0.000,"r":0,"hx":0}')
        self.assertFalse(p["hx711"])
        self.assertIn("no_hx711", p["flags"])


class MetricsTests(unittest.TestCase):
    def test_volume_and_fill(self):
        m = derive_metrics(10.1, bucket_liters=20.0)
        self.assertAlmostEqual(m["volume_l"], 10.0, places=2)
        self.assertAlmostEqual(m["fill_pct"], 50.0, places=1)
        self.assertFalse(m["bucket_full"])

    def test_full_bucket(self):
        self.assertTrue(derive_metrics(19.0, bucket_liters=20.0)["bucket_full"])

    def test_negative_weight_is_zero_volume(self):
        self.assertEqual(derive_metrics(-0.2)["volume_l"], 0.0)

    def test_sudden_drop(self):
        self.assertTrue(is_sudden_drop(9.0, 0.2))
        self.assertFalse(is_sudden_drop(9.0, 8.5))     # normal noise
        self.assertFalse(is_sudden_drop(0.8, 0.0))     # too small to matter
        self.assertFalse(is_sudden_drop(None, 1.0))

    def test_tracker_counts_lost_packets_and_collection(self):
        t = MetricsTracker(bucket_liters=20.0)
        for seq, w, ts in [(0, 5.0, 1), (1, 6.0, 2), (4, 9.0, 3), (5, 0.1, 4)]:
            ev = parse_line(packet_line({"id": "LC01", "k": "live", "s": seq, "w": w}), now=ts)
            t.update(ev)
        p = ev["payload"]
        self.assertEqual(p["lost_packets"], 2)
        self.assertIn("sudden_drop", p["events"])
        self.assertEqual(p["last_collection_ts"], 4)

    def test_tracker_ignores_foreign(self):
        t = MetricsTracker()
        ev = t.update(parse_line(packet_line({"Node_Code": "X"})))
        self.assertNotIn("fill_pct", ev["payload"])
        self.assertEqual(t.nodes, {})


if __name__ == "__main__":
    unittest.main()
