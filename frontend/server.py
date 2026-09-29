"""Maple gateway front end: reads the GATEWAY board's serial port and serves a
live dashboard at http://localhost:8000

    python server.py                 # auto-find the gateway COM port
    python server.py --port COM6     # or name it
    python server.py --demo          # fake gateway data, no hardware needed
    python server.py --forward http://localhost:4000   # also send readings to the TBD worker API

No third-party packages required (pyserial is used if installed).
"""

import argparse
import csv
import json
import os
import queue
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from forwarder import Forwarder
from packet_parser import DEFAULT_BUCKET_LITERS, MetricsTracker, parse_line
import serial_io

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
LOG_DIR = os.path.join(HERE, "logs")
HISTORY = 500
SILENCE_RECONNECT_S = 15


class Hub:
    """Shared state between the serial reader thread and HTTP clients."""

    def __init__(self, bucket_liters, forwarder=None):
        self.lock = threading.Lock()
        self.forwarder = forwarder   # optional: sends lines on to the worker API
        self.tracker = MetricsTracker(bucket_liters)
        self.history = []            # recent events for newly opened pages
        self.clients = []            # one queue per SSE connection
        self.serial = {"port": None, "connected": False, "error": None}
        self.gateway = {"last_status": None, "last_boot": None}
        self.last_packet_ts = None
        self.counts = {"packets": 0, "ours": 0, "foreign": 0, "crc_err": 0}
        os.makedirs(LOG_DIR, exist_ok=True)
        self.csv_path = os.path.join(LOG_DIR, time.strftime("packets-%Y%m%d.csv"))
        new = not os.path.exists(self.csv_path)
        self.csv_file = open(self.csv_path, "a", newline="")
        self.csv = csv.writer(self.csv_file)
        if new:
            self.csv.writerow(["time", "node", "kind", "seq", "weight_kg", "volume_l",
                               "fill_pct", "rssi", "snr", "crc", "flags", "raw"])

    def publish(self, event):
        with self.lock:
            t = event["type"]
            if t == "packet":
                self.tracker.update(event)
                p = event["payload"]
                self.counts["packets"] += 1
                if not event.get("crc", True):
                    self.counts["crc_err"] += 1
                elif p.get("ours"):
                    self.counts["ours"] += 1
                    self.last_packet_ts = event["ts"]
                else:
                    self.counts["foreign"] += 1
                self._log_csv(event)
            elif t == "status":
                self.gateway["last_status"] = event
            elif t == "boot":
                self.gateway["last_boot"] = event
            if self.forwarder:
                self.forwarder.submit(event)
            self.history.append(event)
            del self.history[:-HISTORY]
            msg = json.dumps({"event": event, "summary": self._summary()})
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def set_serial(self, **kw):
        with self.lock:
            self.serial.update(kw)
            msg = json.dumps({"event": {"type": "serial", "ts": time.time(), **self.serial},
                              "summary": self._summary()})
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def _summary(self):
        return {"serial": self.serial, "gateway": self.gateway, "counts": self.counts,
                "last_packet_ts": self.last_packet_ts, "now": time.time(),
                "bucket_liters": self.tracker.bucket_liters,
                "forward": dict(self.forwarder.stats, url=self.forwarder.url) if self.forwarder else None}

    def snapshot(self):
        with self.lock:
            return {"history": list(self.history), "summary": self._summary()}

    def _log_csv(self, e):
        p = e["payload"]
        self.csv.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])),
            p.get("node_id", ""), p.get("kind", ""), p.get("seq", ""), p.get("weight_kg", ""),
            p.get("volume_l", ""), p.get("fill_pct", ""), e.get("rssi"), e.get("snr"),
            e.get("crc"), " ".join(p.get("flags", [])), e.get("raw", "")])
        self.csv_file.flush()


def serial_loop(hub, port, stop):
    """Read the gateway forever, reconnecting if the board is unplugged."""
    while not stop.is_set():
        target = port
        if not target:
            hub.set_serial(connected=False, error="searching for gateway board...")
            print("Searching for the gateway board:")
            target = serial_io.find_gateway(log=print)
            if not target:
                hub.set_serial(connected=False, error="gateway board not found (plugged in? flashed?)")
                stop.wait(3)
                continue
        try:
            s = serial_io.open_port(target)
        except OSError as e:
            hub.set_serial(port=target, connected=False, error=str(e))
            print("Serial:", e)
            stop.wait(3)
            continue
        print("Reading gateway on", target)
        hub.set_serial(port=target, connected=True, error=None)
        last_line = time.time()
        try:
            while not stop.is_set():
                line = s.readline()
                if line is None:
                    # The gateway prints status every 5 s. Silence means the
                    # handle went stale (board replugged, port renumbered).
                    if time.time() - last_line > SILENCE_RECONNECT_S:
                        raise OSError("no data from %s for %d s, reconnecting" % (target, SILENCE_RECONNECT_S))
                    continue
                last_line = time.time()
                ev = parse_line(line)
                if ev:
                    hub.publish(ev)
        except OSError as e:
            hub.set_serial(connected=False, error=str(e))
            print("Serial:", e)
        finally:
            s.close()
        stop.wait(2)


def demo_loop(hub, stop):
    """Pretend to be a gateway: live readings every 5 s, with a slow fill,
    occasional foreign packet and a collection (drop to ~0) now and then."""
    hub.set_serial(port="DEMO", connected=True, error=None)
    hub.publish(parse_line('{"type":"boot","role":"gateway","radio":"ok","freq":915.00,"bw":125,"sf":9,"sync":"0x12"}'))
    seq, w, n, t0 = 0, 0.3, 0, time.time()
    while not stop.is_set():
        n += 1
        w = w + random.uniform(0.05, 0.25)
        if w > 11:
            w = 0.2
        payload = json.dumps({"id": "LC01", "k": "live", "s": seq, "w": round(w, 3),
                              "r": int(w * 42000), "hx": 1}, separators=(",", ":"))
        seq += 1
        if random.random() < 0.08:
            seq += 1  # simulate a lost packet
        line = json.dumps({"type": "packet", "n": n, "rssi": round(random.uniform(-80, -35), 1),
                           "snr": round(random.uniform(5, 11), 2), "len": len(payload),
                           "crc": True, "raw": payload})
        hub.publish(parse_line(line))
        if random.random() < 0.05:
            hub.publish(parse_line(json.dumps({"type": "packet", "n": n, "rssi": -110.0, "snr": -4.0,
                                               "len": 20, "crc": True, "raw": "{\"Node_Code\":\"X\"}"})))
        hub.publish(parse_line(json.dumps({"type": "status", "role": "gateway", "uptime": int(time.time() - t0),
                                           "radio": "ok", "rx": n, "crc_err": 0, "last_rx_ms": 0})))
        stop.wait(2)


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(hub.snapshot()).encode(), "application/json")
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = queue.Queue(maxsize=1000)
                hub.clients.append(q)
                try:
                    while True:
                        try:
                            msg = q.get(timeout=5)
                            self.wfile.write(("data: %s\n\n" % msg).encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")   # keeps the page's clock honest
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    hub.clients.remove(q)
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="gateway COM port, e.g. COM6 (default: auto-detect)")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--bucket-liters", type=float, default=DEFAULT_BUCKET_LITERS,
                    help="bucket capacity used for fill %% (default 3 US gal)")
    ap.add_argument("--demo", action="store_true", help="fake data, no board needed")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--forward", metavar="URL",
                    help="also POST every gateway line to the TBD worker, e.g. http://localhost:4000")
    ap.add_argument("--ingest-key", default=os.environ.get("INGEST_KEY"),
                    help="X-Ingest-Key for the worker (default: INGEST_KEY env var)")
    ap.add_argument("--gateway-id", default="GW-" + socket.gethostname().upper()[:40],
                    help="name this gateway reports as (default: GW-<computer name>)")
    args = ap.parse_args(argv)

    if args.list:
        for p, d in serial_io.list_ports():
            print(p, "-", d)
        return

    forwarder = None
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.forward:
        forwarder = Forwarder(args.forward, args.gateway_id, args.ingest_key,
                              spool_path=os.path.join(LOG_DIR, "forward-spool.jsonl")).start()
        print("Forwarding to %s as %s" % (forwarder.url, args.gateway_id))
    hub = Hub(args.bucket_liters, forwarder)
    stop = threading.Event()
    target = demo_loop if args.demo else serial_loop
    targs = (hub, stop) if args.demo else (hub, args.port, stop)
    threading.Thread(target=target, args=targs, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(hub))
    httpd.daemon_threads = True
    print("Dashboard: http://localhost:%d   (Ctrl+C to stop)" % args.http_port)
    print("Packet log:", hub.csv_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if forwarder:
            forwarder.stop()


if __name__ == "__main__":
    main()
