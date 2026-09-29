"""Parse gateway serial lines and derive sap metrics from load cell packets.

The gateway prints one JSON object per line. Packet lines carry the LoRa
payload in "raw", which is itself compact JSON from the load cell node:

    {"type":"packet","n":3,"rssi":-40.0,"snr":9.5,"len":58,"crc":true,
     "raw":"{\"id\":\"LC01\",\"k\":\"live\",\"s\":2,\"w\":3.412,\"r\":151234,\"hx\":1}"}

Everything here is pure Python so it can be unit tested without hardware.
"""

import json
import time

SAP_DENSITY_KG_PER_L = 1.01     # raw sap is ~2% sugar, barely denser than water
LITERS_PER_US_GALLON = 3.78541
DEFAULT_BUCKET_LITERS = 3 * LITERS_PER_US_GALLON   # common 3 gal sap bucket
FULL_THRESHOLD_PCT = 90.0
WEIGHT_MIN_KG = -1.0            # outside this range the reading is flagged
WEIGHT_MAX_KG = 55.0            # 50 kg load cell + a bit of headroom
SUDDEN_DROP_KG = 1.0            # a drop this big between readings is an event
SUDDEN_DROP_FRACTION = 0.5      # ...and it must lose at least half the weight


def parse_line(line, now=None):
    """Turn one serial line into an event dict.

    Always returns a dict with a "type" key: boot | status | packet | info |
    selftest | log. Lines that are not JSON come back as {"type": "log"}.
    """
    now = time.time() if now is None else now
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return {"type": "log", "text": text, "ts": now}
    if not isinstance(obj, dict) or "type" not in obj:
        return {"type": "log", "text": text, "ts": now}
    obj["ts"] = now
    if obj["type"] == "packet":
        obj["payload"] = parse_payload(obj.get("raw", ""))
    return obj


def parse_payload(raw):
    """Parse the node's LoRa payload into normalized fields.

    Unknown or foreign packets (e.g. someone else's LoRa gear on the same
    channel) come back with ours=False so the UI can show them separately.
    """
    result = {"ours": False, "valid_json": False, "flags": []}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        result["flags"].append("not_json")
        return result
    if not isinstance(data, dict):
        result["flags"].append("not_object")
        return result
    result["valid_json"] = True
    result["fields"] = data
    if "id" not in data or "w" not in data:
        result["flags"].append("foreign")
        return result

    result["ours"] = True
    result["node_id"] = str(data["id"])
    result["kind"] = data.get("k", "live")
    result["seq"] = _as_int(data.get("s"))
    result["hx711"] = bool(data.get("hx", 1))
    result["calibrated"] = not data.get("uncal", 0)
    result["raw_counts"] = _as_int(data.get("r"))
    if "i" in data:
        result["test_step"] = _as_int(data.get("i"))
        result["test_total"] = _as_int(data.get("of"))

    try:
        weight = float(data["w"])
    except (ValueError, TypeError):
        result["flags"].append("bad_weight")
        weight = None
    result["weight_kg"] = weight
    if weight is not None and not (WEIGHT_MIN_KG <= weight <= WEIGHT_MAX_KG):
        result["flags"].append("out_of_range")
    if result["kind"] == "nohx":
        result["flags"].append("no_hx711")
    return result


def _as_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class MetricsTracker:
    """Keeps running state for each node and derives the dashboard metrics:
    sap volume, bucket fill %, full alert, sudden drops (collected or knocked
    over), last collection time and lost packets (sequence gaps)."""

    def __init__(self, bucket_liters=DEFAULT_BUCKET_LITERS):
        self.bucket_liters = bucket_liters
        self.nodes = {}

    def update(self, event):
        """Add derived metrics to a packet event in place and return it."""
        p = event.get("payload") or {}
        if not p.get("ours"):
            return event
        node = self.nodes.setdefault(p["node_id"], {
            "last_weight": None, "last_seq": None, "lost": 0,
            "last_collection_ts": None, "packets": 0,
        })
        node["packets"] += 1

        seq = p.get("seq")
        if seq is not None and node["last_seq"] is not None:
            gap = seq - node["last_seq"] - 1
            if gap > 0:
                node["lost"] += gap
            elif seq <= node["last_seq"]:
                p.setdefault("events", []).append("node_restarted")
        if seq is not None:
            node["last_seq"] = seq

        w = p.get("weight_kg")
        if w is not None and "out_of_range" not in p["flags"] and p["kind"] != "nohx":
            m = derive_metrics(w, self.bucket_liters)
            p.update(m)
            prev = node["last_weight"]
            if is_sudden_drop(prev, w):
                p.setdefault("events", []).append("sudden_drop")
                node["last_collection_ts"] = event.get("ts")
            node["last_weight"] = w

        p["lost_packets"] = node["lost"]
        p["last_collection_ts"] = node["last_collection_ts"]
        return event


def derive_metrics(weight_kg, bucket_liters=DEFAULT_BUCKET_LITERS):
    liters = max(weight_kg, 0.0) / SAP_DENSITY_KG_PER_L
    fill = 100.0 * liters / bucket_liters if bucket_liters > 0 else 0.0
    return {
        "volume_l": round(liters, 3),
        "volume_gal": round(liters / LITERS_PER_US_GALLON, 3),
        "fill_pct": round(fill, 1),
        "bucket_full": fill >= FULL_THRESHOLD_PCT,
    }


def is_sudden_drop(prev_kg, new_kg):
    if prev_kg is None or new_kg is None:
        return False
    drop = prev_kg - new_kg
    return drop >= SUDDEN_DROP_KG and drop >= SUDDEN_DROP_FRACTION * prev_kg
