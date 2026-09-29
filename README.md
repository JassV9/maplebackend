# Maple Backend: load cell over LoRa

```
[50 kg load cell] -> [HX711] -> [LoRa32 LOAD CELL NODE] ~~LoRa 915 MHz~~> [LoRa32 GATEWAY] -USB-> [server.py] -> browser dashboard
```

| Folder | What it is |
|---|---|
| `firmware/maple_loadcell_node/` | Node: reads the HX711, sends weight over LoRa every 5 s, OLED says **LOAD CELL NODE** |
| `firmware/maple_gateway/` | Gateway: receives LoRa, prints one JSON line per packet (with RSSI/SNR) on USB serial, OLED says **GATEWAY (RX)** |
| `frontend/` | `server.py` reads the gateway's COM port and serves a live dashboard at http://localhost:8000 |
| `flash.ps1` | Compile + upload helper |

Both boards are **Heltec WiFi LoRa 32 V3** (the ESP32-LoRa-V3 boards from the hardware list), FQBN `esp32:esp32:heltec_wifi_lora_32_V3`.
Radio settings reuse what worked in `loratesting/maple_camp`: **915.0 MHz, BW 125 kHz, SF9, CR 4/5, sync word 0x12, CRC on**. If you change them, change both sketches.

## Wiring (breadboard)

The amp we have is the purple **"Load Cell Amp"** HX711 board: 5 pads on the left (RED, BLK, WHT, GRN, YLW) and 4 on the right (VCC, DAT, CLK, GND). It has a single VCC pin that powers both the analog and logic side, so it runs at 3.3 V straight from the LoRa32. Pins 5 and 6 are free header pins; they are not used by the radio (8 to 14), OLED (17, 18, 21), Vext (36), LED (35) or the PRG button (0).

```
   LOAD CELL                  HX711 "Load Cell Amp"               Heltec LoRa32 V3
                           +---------------------------+
   red   ----------------> | RED               VCC     | <-------- 3V3
   black ----------------> | BLK               DAT     | --------> GPIO 5
   white ----------------> | WHT               CLK     | <-------- GPIO 6
   green ----------------> | GRN               GND     | <-------- GND
   (shield, if any) -----> | YLW                       |
                           +---------------------------+
```

Right side, amp to LoRa32:

| Amp pin | LoRa32 V3 pin | Notes |
|---|---|---|
| VCC | 3V3 | **3.3 V, not 5V**: DAT must stay at 3.3 V logic for the ESP32 |
| DAT | GPIO 5 | data out from the amp |
| CLK | GPIO 6 | clock from the LoRa32 |
| GND | GND | |

Left side, load cell to amp:

| Load cell wire | Amp pad | Meaning |
|---|---|---|
| Red | RED | E+ (excitation +) |
| Black | BLK | E- (excitation -) |
| White | WHT | A- (signal -) |
| Green | GRN | A+ (signal +) |
| (none / shield) | YLW | leave empty; only used if the cable has a yellow or bare shield wire |

Notes: the amp's holes are empty (no header pins), so solder a 4-pin header on the VCC/DAT/CLK/GND side to plug it into the breadboard. The load cell's thin wires can be soldered straight into the left pads, or soldered to a 5-pin header. If the load cell wire colors don't match (some cells use different colors), check its datasheet for E+/E-/S+/S-. If weight goes *down* when you press, white and green are swapped; calibration fixes the sign anyway. Leave the antenna connected on both boards before powering them (transmitting without an antenna can damage the SX1262).

## Flash the boards

Find the ports: `arduino-cli board list` (each LoRa32 shows up as a Silicon Labs CP210x COM port). Unplug one board to see which is which. Then:

```powershell
.\flash.ps1 gateway COM6
.\flash.ps1 node COM7
```

Close `server.py` or any serial monitor first, since they hold the port.

## Run the dashboard

```powershell
cd frontend
python server.py            # finds the gateway board automatically
python server.py --port COM6
python server.py --demo     # fake data, no hardware at all
```

Open http://localhost:8000. The banner at the top is the thing to watch:

- **green "Packets arriving"**: a packet from the node arrived in the last 15 s
- **amber**: gateway is connected and listening but nothing has arrived
- **red**: gateway board not connected, or its radio failed

Every packet is also appended to `frontend/logs/packets-YYYYMMDD.csv`. No extra Python packages are needed (pyserial is used if you have it; otherwise it talks to the COM port through Windows directly). Options: `--bucket-liters 11.4` sets the bucket size used for fill %, `--list` shows serial ports.

Run the tests: `cd frontend; python -m unittest discover -s tests -v`

### Sending readings to the TBD dashboard (Postgres)

`server.py` can also forward every gateway line to the TBD worker API (the `worker/` app in the MapleSugaring_TBD repo), which stores it in Postgres and drives the real dashboard:

```powershell
python server.py --forward http://localhost:4000                 # worker running on this PC (npm run dev in MapleSugaring_TBD)
python server.py --forward https://<vm-host>/api --ingest-key KEY # worker on the VM, through the web app's /api proxy
```

Lines are batched every 2 s to `<url>/ingest`. If the worker is unreachable they are saved to `frontend/logs/forward-spool.jsonl` and re-sent once it answers, so the local dashboard keeps working and nothing is lost. `--gateway-id` sets the gateway name (default `GW-<computer name>`); the forwarding status shows up in `/api/state` under `forward`.

## Using the node

On boot the OLED shows a **self-test** for 2.5 s (HX711 OK / NOT FOUND, Radio OK / FAIL); the same result is printed on serial as a `selftest` JSON line. After that it shows the live weight.

**PRG button** (the one that is not RST):

| Press | Does |
|---|---|
| short tap (< 1 s) | **Test loop**: sends 20 fake readings 1.5 s apart, ramping 0.25 to 9.25 kg then dropping to 0.15 kg (looks like a collection). Works without a load cell. |
| hold 1.5 s then release | **Tare** (zero). Take everything off the scale first. |
| hold 5 s then release | **Calibrate** with a 1.000 kg known weight on the scale (change `CAL_MASS_KG` in the sketch for a different weight). |

The OLED tells you what releasing will do while you hold it. Tare and calibration are saved in flash, so they survive power cycles.

Serial commands on the node (115200 baud, newline), e.g. `arduino-cli monitor -p COM7 -c baudrate=115200`:
`tare`, `cal 2.5` (calibrate with 2.5 kg on the scale), `scale 42000` (set counts per kg directly), `test`, `info`.

If the HX711 is missing, the node still sends `"k":"nohx"` heartbeats so you can tell the radio link works and the problem is the wiring. It re-checks for the HX711 every 5 s.

## Packet format

Node to gateway over LoRa (compact JSON, about 60 bytes):

```json
{"id":"LC01","k":"live","s":17,"w":3.412,"r":151234,"hx":1}
```
`id` node id, `k` kind (`live`, `test`, `nohx`), `s` sequence number, `w` weight in kg, `r` raw HX711 counts after tare, `hx` HX711 found, `uncal:1` if never calibrated. Test packets add `i`/`of` (step 3 of 20).

Gateway to PC over USB serial, one JSON per line: `boot`, `status` (every 5 s), and `packet`:

```json
{"type":"packet","n":12,"rssi":-45.0,"snr":9.8,"len":61,"crc":true,"raw":"{\"id\":\"LC01\",...}"}
```

## What the load cell gives the dashboard

Picked from the requirements list and user stories:

| Metric | How | Requirement |
|---|---|---|
| Weight (kg) | HX711 reading | FR-038 |
| Sap volume (L and US gal) | weight / 1.01 kg per L | FR-015, FR-030, FR-038 |
| Bucket fill % and **BUCKET FULL** at 90% | volume / bucket size (default 3 US gal) | FR-006 |
| Sudden drop alert (collected or knocked over) | weight falls by at least 1 kg and at least half | FR-008, FR-011 |
| Last collection time | time of last sudden drop | FR-011 |
| Lost packets | gaps in the sequence number | FR-046 |
| Out of range flag | weight outside -1 to 55 kg | FR-052 |
| Node id per packet | `id` field | FR-048 |

Not done yet: battery voltage (needs the board revision's ADC control pin checked first, CR-004), local buffering on the node when the gateway is offline (FR-039), and the Raspberry Pi gateway with the LoRa HAT (this LoRa32 gateway stands in for it).

## In class tomorrow

1. Plug in the **gateway** board only. `cd frontend; python server.py`, open http://localhost:8000. Banner should go **amber: "Gateway listening, no packets yet"**. The gateway OLED says `GATEWAY (RX)` and `listening...`.
2. Power the **node** (USB or battery) with nothing wired to it. Self-test shows `HX711: NOT FOUND`, `Radio: OK`. Within 5 s the banner goes **green: "Packets arriving, but node has no load cell"**. This proves the radio link.
3. Tap **PRG** on the node. The banner says **TEST LOOP**, the chart draws a purple ramp, the log fills with `test` rows, and the last step triggers the "Sudden drop" alert and a Last collection time.
4. Unplug the node, wire the HX711 and load cell per the tables above, plug it back in. Self-test should say `HX711: OK`.
5. With the scale empty, **hold PRG 1.5 s** to tare. Put a known 1 kg weight on it and **hold PRG 5 s** to calibrate. Check the reading with a second known weight.
6. Watch the orange live line on the chart track what you put on the plate.

Troubleshooting:
- Banner red "not connected": wrong board plugged in, or another program holds the COM port. Try `python server.py --list` and `--port`.
- Node sends but gateway hears nothing: both sketches must have the same frequency/SF/sync. Our two boards were not frequency-calibrated against each other; if CRC errors climb on the gateway, try `FREQ_MHZ` 915.1 on one board (the troll sketch found a ~110 kHz offset against another rig).
- Weight stuck or "HX711 SATURATED": check E+/E-/A+/A- wiring and that VDD is 3.3 V.
- Board doesn't show up as a COM port: use a data USB cable, not a charge-only one.
