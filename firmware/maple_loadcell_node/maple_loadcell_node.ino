// Maple LOAD CELL NODE (transmitter) for Heltec WiFi LoRa 32 V3 (ESP32-S3 + SX1262)
// Libraries: RadioLib, U8g2 (both already installed). The HX711 driver is
// built in below, so no extra library is needed.
//
// Reads a 50 kg single point load cell through a SparkFun HX711 amplifier and
// sends compact JSON over LoRa every SEND_INTERVAL_MS:
//
//   {"id":"LC01","k":"live","s":17,"w":3.412,"r":151234,"hx":1}
//     id = node id, k = kind (live | nohx), s = sequence number,
//     w = weight in kg, r = raw HX711 counts (tare removed), hx = HX711 found
//
// PRG button (GPIO0):
//   press and release     -> TARE (zero the scale; take everything off first)
//   hold 5 s, release     -> CALIBRATE using CAL_MASS_KG sitting on the scale
//
// Serial commands (115200 baud, newline): tare | cal <kg> | scale <counts/kg>
//                                          | info

#include <RadioLib.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <Preferences.h>

#define NODE_ID "LC01"

// ---- Radio settings: MUST match the gateway ----
#define FREQ_MHZ       915.0
#define BANDWIDTH_KHZ  125.0
#define SPREAD_FACTOR  9
#define CODING_RATE    5
#define SYNC_WORD      0x12
#define TX_POWER_DBM   14
#define PREAMBLE_LEN   8

#define SEND_INTERVAL_MS   5000   // live reading every 5 s
#define HX_SAMPLES         8      // averaged per reading (~0.8 s at 10 SPS)
#define CAL_MASS_KG        1.000  // known weight used by the 5 s button hold
#define DEFAULT_SCALE      42000.0f  // counts per kg, rough guess until calibrated

// HX711 wiring (see README). Free header pins, not used by radio/OLED.
#define HX_DOUT  5   // HX711 DAT
#define HX_SCK   6   // HX711 CLK

// Our load cell's counts go DOWN as weight goes on, so flip them. If weight
// ever reads negative again (cell remounted or WHT/GRN rewired), set this to 1.
#define LOAD_SIGN  -1

// Heltec V3 pins
#define BUTTON_PIN 0
#define OLED_SDA  17
#define OLED_SCL  18
#define OLED_RST  21
#define VEXT_PIN  36
#define LED_PIN   35
#define LORA_NSS   8
#define LORA_SCK   9
#define LORA_MOSI 10
#define LORA_MISO 11
#define LORA_RST  12
#define LORA_BUSY 13
#define LORA_DIO1 14

U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, OLED_RST, OLED_SCL, OLED_SDA);
SX1262 radio = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);
Preferences prefs;

bool radioOk = false;
int radioErr = 0;
bool hxOk = false;
bool hxSaturated = false;
long tareOffset = 0;
float scaleCountsPerKg = DEFAULT_SCALE;
bool calibrated = false;

float weightKg = 0;
long rawNet = 0;
uint32_t seq = 0, txOk = 0, txFail = 0;
unsigned long lastSendMs = 0, lastHxRetryMs = 0;
char statusMsg[24] = "";
unsigned long statusUntil = 0;

// ---------------------------------------------------------------- HX711
portMUX_TYPE hxMux = portMUX_INITIALIZER_UNLOCKED;

bool hxWaitReady(unsigned long timeoutMs) {
  unsigned long t0 = millis();
  while (digitalRead(HX_DOUT) == HIGH) {
    if (millis() - t0 > timeoutMs) return false;
    delay(1);
  }
  return true;
}

// One 24-bit conversion, channel A gain 128. Returns false if not ready.
bool hxReadOnce(long& value) {
  if (!hxWaitReady(200)) return false;
  uint32_t v = 0;
  portENTER_CRITICAL(&hxMux);  // keep SCK high pulses under 60 us
  for (int i = 0; i < 24; i++) {
    digitalWrite(HX_SCK, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | digitalRead(HX_DOUT);
    digitalWrite(HX_SCK, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(HX_SCK, HIGH);  // 25th pulse -> next read is channel A, gain 128
  delayMicroseconds(1);
  digitalWrite(HX_SCK, LOW);
  portEXIT_CRITICAL(&hxMux);
  hxSaturated = (v == 0x7FFFFF || v == 0x800000);
  if (v & 0x800000) v |= 0xFF000000;  // sign extend
  value = (long)(int32_t)v;
  return true;
}

bool hxReadAvg(int n, long& avg) {
  long long sum = 0;
  int got = 0;
  for (int i = 0; i < n; i++) {
    long v;
    if (hxReadOnce(v)) { sum += v; got++; }
  }
  if (got == 0) return false;
  avg = (long)(sum / got);
  return true;
}

bool hxDetect() {
  pinMode(HX_SCK, OUTPUT);
  digitalWrite(HX_SCK, LOW);        // SCK low = HX711 powered up
  pinMode(HX_DOUT, INPUT_PULLUP);   // unplugged DAT floats high -> "not found"
  if (!hxWaitReady(1000)) return false;
  long v;
  return hxReadOnce(v);
}

// Counts above the tare point, positive when weight is added.
long hxNet(long avg) {
  return LOAD_SIGN * (avg - tareOffset);
}

// ---------------------------------------------------------------- helpers
void flash(const char* msg, unsigned long ms = 2500) {
  strncpy(statusMsg, msg, sizeof(statusMsg) - 1);
  statusMsg[sizeof(statusMsg) - 1] = 0;
  statusUntil = millis() + ms;
}

void loadSettings() {
  prefs.begin("maple", true);
  tareOffset = prefs.getLong("off", 0);
  // The sign lives in LOAD_SIGN now. A calibration saved by older firmware
  // could be negative (that was how it fixed the sign), so drop its sign.
  scaleCountsPerKg = fabsf(prefs.getFloat("scale", DEFAULT_SCALE));
  calibrated = prefs.getBool("cal", false);
  prefs.end();
}

void saveSettings() {
  prefs.begin("maple", false);
  prefs.putLong("off", tareOffset);
  prefs.putFloat("scale", scaleCountsPerKg);
  prefs.putBool("cal", calibrated);
  prefs.end();
}

void doTare() {
  long avg;
  if (!hxOk || !hxReadAvg(16, avg)) { Serial.println("TARE failed: HX711 not responding"); flash("TARE FAILED"); return; }
  tareOffset = avg;
  saveSettings();
  Serial.printf("TARE ok, offset=%ld\n", tareOffset);
  flash("TARED (zero)");
}

void doCalibrate(float knownKg) {
  long avg;
  if (knownKg <= 0) { Serial.println("CAL needs a positive weight in kg"); return; }
  if (!hxOk || !hxReadAvg(16, avg)) { Serial.println("CAL failed: HX711 not responding"); flash("CAL FAILED"); return; }
  long net = hxNet(avg);
  if (labs(net) < 100) { Serial.println("CAL failed: no load detected. Tare empty, then add the weight."); flash("CAL: NO LOAD"); return; }
  if (net < 0) { Serial.println("CAL failed: weight reads negative. Flip LOAD_SIGN in the sketch and reflash."); flash("CAL: NEGATIVE"); return; }
  scaleCountsPerKg = net / knownKg;
  calibrated = true;
  saveSettings();
  Serial.printf("CAL ok: %.3f kg -> %ld counts, scale=%.2f counts/kg\n", knownKg, net, scaleCountsPerKg);
  flash("CALIBRATED");
}

void printInfo() {
  Serial.printf("{\"type\":\"info\",\"id\":\"%s\",\"radio\":\"%s\",\"hx711\":%s,\"offset\":%ld,"
                "\"scale\":%.2f,\"calibrated\":%s,\"w\":%.3f,\"tx_ok\":%lu,\"tx_fail\":%lu}\n",
                NODE_ID, radioOk ? "ok" : "fail", hxOk ? "true" : "false", tareOffset,
                scaleCountsPerKg, calibrated ? "true" : "false", weightKg,
                (unsigned long)txOk, (unsigned long)txFail);
}

bool sendPacket(const char* kind, float w, long raw) {
  char buf[128];
  snprintf(buf, sizeof(buf), "{\"id\":\"%s\",\"k\":\"%s\",\"s\":%lu,\"w\":%.3f,\"r\":%ld,\"hx\":%d%s}",
           NODE_ID, kind, (unsigned long)seq, w, raw, hxOk ? 1 : 0, calibrated ? "" : ",\"uncal\":1");
  seq++;
  if (!radioOk) { txFail++; Serial.printf("[TX skipped, radio down] %s\n", buf); return false; }
  digitalWrite(LED_PIN, HIGH);
  int st = radio.transmit(buf);
  digitalWrite(LED_PIN, LOW);
  if (st == RADIOLIB_ERR_NONE) { txOk++; Serial.printf("[TX ok] %s\n", buf); return true; }
  txFail++;
  Serial.printf("[TX fail %d] %s\n", st, buf);
  return false;
}

void drawOled(long holdMs) {
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x13B_tf);
  u8g2.drawStr(0, 11, "LOAD CELL NODE");
  u8g2.setFont(u8g2_font_6x10_tf);
  char line[32];

  if (holdMs > 0) {  // button being held: say what release will do
    u8g2.setFont(u8g2_font_7x13B_tf);
    if (holdMs < 5000) u8g2.drawStr(0, 32, "release: TARE");
    else               u8g2.drawStr(0, 32, "release: CAL");
    u8g2.setFont(u8g2_font_6x10_tf);
    snprintf(line, sizeof(line), "held %.1fs", holdMs / 1000.0);
    u8g2.drawStr(0, 48, line);
    if (holdMs >= 5000) { snprintf(line, sizeof(line), "with %.3f kg on", CAL_MASS_KG); u8g2.drawStr(0, 60, line); }
    u8g2.sendBuffer();
    return;
  }

  if (hxOk) {
    u8g2.setFont(u8g2_font_logisoso16_tf);
    snprintf(line, sizeof(line), "%.3f kg", weightKg);
    u8g2.drawStr(0, 34, line);
    u8g2.setFont(u8g2_font_6x10_tf);
    if (hxSaturated) u8g2.drawStr(0, 46, "HX711 SATURATED!");
    else if (!calibrated) u8g2.drawStr(0, 46, "not calibrated");
  } else {
    u8g2.setFont(u8g2_font_7x13B_tf);
    u8g2.drawStr(0, 32, "NO HX711");
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(0, 46, "check wiring");
  }

  if (millis() < statusUntil) {
    u8g2.drawStr(0, 60, statusMsg);
  } else {
    snprintf(line, sizeof(line), "%s sent:%lu fail:%lu", radioOk ? "TX" : "RADIO!", (unsigned long)txOk, (unsigned long)txFail);
    u8g2.drawStr(0, 60, line);
  }
  u8g2.sendBuffer();
}

void selfTest() {
  // HX711
  hxOk = hxDetect();
  // Radio
  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);
  radioErr = radio.begin(FREQ_MHZ, BANDWIDTH_KHZ, SPREAD_FACTOR, CODING_RATE,
                         SYNC_WORD, TX_POWER_DBM, PREAMBLE_LEN, 1.8);
  radioOk = (radioErr == RADIOLIB_ERR_NONE);
  if (radioOk) radio.setCRC(true);

  Serial.printf("{\"type\":\"selftest\",\"role\":\"node\",\"id\":\"%s\",\"hx711\":\"%s\",\"radio\":\"%s\","
                "\"radio_code\":%d,\"calibrated\":%s,\"scale\":%.2f}\n",
                NODE_ID, hxOk ? "ok" : "not found", radioOk ? "ok" : "fail", radioErr,
                calibrated ? "true" : "false", scaleCountsPerKg);

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x13B_tf);
  u8g2.drawStr(0, 11, "LOAD CELL NODE");
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 24, "SELF-TEST");
  u8g2.drawStr(0, 36, hxOk ? "HX711: OK" : "HX711: NOT FOUND");
  char line[32];
  if (radioOk) snprintf(line, sizeof(line), "Radio: OK %.0fMHz", FREQ_MHZ);
  else snprintf(line, sizeof(line), "Radio: FAIL %d", radioErr);
  u8g2.drawStr(0, 48, line);
  u8g2.drawStr(0, 60, calibrated ? "Cal: saved" : "Cal: default");
  u8g2.sendBuffer();
  delay(2500);
}

// ---------------------------------------------------------------- serial
String serialLine;

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd == "tare") doTare();
  else if (cmd.startsWith("cal ")) doCalibrate(cmd.substring(4).toFloat());
  else if (cmd.startsWith("scale ")) {
    float s = cmd.substring(6).toFloat();
    if (s != 0) { scaleCountsPerKg = fabsf(s); calibrated = true; saveSettings(); Serial.printf("scale set to %.2f\n", scaleCountsPerKg); }
  }
  else if (cmd == "info") printInfo();
  else Serial.println("commands: tare | cal <kg> | scale <counts/kg> | info");
}

// ---------------------------------------------------------------- main
unsigned long pressStart = 0;
bool pressed = false;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  delay(800);

  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, LOW);
  delay(50);
  u8g2.begin();

  loadSettings();
  selfTest();
  printInfo();
  Serial.println("commands: tare | cal <kg> | scale <counts/kg> | info");
}

void loop() {
  // Serial commands
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { if (serialLine.length()) handleCommand(serialLine); serialLine = ""; }
    else serialLine += c;
  }

  // Button: act on release, based on how long it was held
  bool down = digitalRead(BUTTON_PIN) == LOW;
  if (down && !pressed) { pressed = true; pressStart = millis(); }
  if (pressed) {
    long held = millis() - pressStart;
    if (down) { drawOled(held); delay(20); return; }
    pressed = false;
    if (held < 30) {}                                  // bounce
    else if (held >= 5000) doCalibrate(CAL_MASS_KG);
    else doTare();
  }

  // HX711 plugged in after boot? retry detection every 5 s
  if (!hxOk && millis() - lastHxRetryMs > 5000) {
    lastHxRetryMs = millis();
    hxOk = hxDetect();
    if (hxOk) { Serial.println("HX711 detected"); flash("HX711 FOUND"); }
  }

  long avg;
  if (hxOk) {
    if (hxReadAvg(HX_SAMPLES, avg)) {
      rawNet = hxNet(avg);
      weightKg = rawNet / scaleCountsPerKg;
    } else {
      hxOk = false;
      Serial.println("HX711 stopped responding");
      flash("HX711 LOST");
    }
  }
  drawOled(0);

  if (millis() - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = millis();
    sendPacket(hxOk ? "live" : "nohx", hxOk ? weightKg : 0, hxOk ? rawNet : 0);
  }
  if (!hxOk) delay(50);
}
