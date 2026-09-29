// Maple GATEWAY (receiver) for Heltec WiFi LoRa 32 V3 (ESP32-S3 + SX1262)
// Libraries: RadioLib, U8g2 (both already installed).
//
// Listens for LoRa packets and prints one JSON object per line on USB serial
// (115200 baud) so the PC front end (frontend/server.py) can read it:
//
//   {"type":"boot","role":"gateway","radio":"ok","freq":915.00,...}
//   {"type":"packet","n":12,"rssi":-45.0,"snr":9.8,"len":61,"crc":true,"raw":"{...}"}
//   {"type":"status","uptime":30,"rx":12,"crc_err":0,"last_rx_ms":1200}
//
// "raw" is the payload exactly as received (JSON-escaped). The node sends
// compact JSON, so the front end parses "raw" a second time.

#include <RadioLib.h>
#include <U8g2lib.h>
#include <Wire.h>

// ---- Radio settings: MUST match the load cell node ----
// Same channel/SF/sync that worked in loratesting/maple_camp.
#define FREQ_MHZ       915.0
#define BANDWIDTH_KHZ  125.0
#define SPREAD_FACTOR  9
#define CODING_RATE    5      // 4/5
#define SYNC_WORD      0x12
#define TX_POWER_DBM   14
#define PREAMBLE_LEN   8

// Heltec V3 pins
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

#define STATUS_INTERVAL_MS 5000

U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, OLED_RST, OLED_SCL, OLED_SDA);
SX1262 radio = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);

volatile bool gotPacket = false;
void IRAM_ATTR onRx() { gotPacket = true; }

bool radioOk = false;
int radioErr = 0;
uint32_t rxCount = 0, crcErrCount = 0;
unsigned long lastRxMs = 0, lastStatusMs = 0;
float lastRssi = 0, lastSnr = 0;
char lastNode[12] = "-";
char lastWeight[16] = "-";
char lastKind[8] = "";

// Print s as a JSON string literal (with quotes), escaping as needed.
void printJsonString(const String& s) {
  Serial.print('"');
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '"':  Serial.print("\\\""); break;
      case '\\': Serial.print("\\\\"); break;
      case '\n': Serial.print("\\n"); break;
      case '\r': Serial.print("\\r"); break;
      case '\t': Serial.print("\\t"); break;
      default:
        if ((uint8_t)c < 0x20 || (uint8_t)c > 0x7E) Serial.printf("\\u%04x", (uint8_t)c);
        else Serial.print(c);
    }
  }
  Serial.print('"');
}

// Tiny field extractor for the OLED only: finds "key":value and copies the
// value (without quotes). The real parsing happens on the PC.
bool extractField(const String& s, const char* key, char* out, size_t outLen) {
  String k = String("\"") + key + "\":";
  int i = s.indexOf(k);
  if (i < 0) return false;
  i += k.length();
  bool quoted = (i < (int)s.length() && s[i] == '"');
  if (quoted) i++;
  size_t n = 0;
  while (i < (int)s.length() && n < outLen - 1) {
    char c = s[i];
    if (quoted ? c == '"' : (c == ',' || c == '}')) break;
    out[n++] = c; i++;
  }
  out[n] = 0;
  return n > 0;
}

void drawOled() {
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x13B_tf);
  u8g2.drawStr(0, 11, "GATEWAY (RX)");
  u8g2.setFont(u8g2_font_6x10_tf);
  char line[32];
  if (!radioOk) {
    snprintf(line, sizeof(line), "RADIO FAIL %d", radioErr);
    u8g2.drawStr(0, 26, line);
    u8g2.drawStr(0, 38, "check antenna/board");
    u8g2.sendBuffer();
    return;
  }
  snprintf(line, sizeof(line), "rx:%lu  crcErr:%lu", (unsigned long)rxCount, (unsigned long)crcErrCount);
  u8g2.drawStr(0, 24, line);
  if (rxCount == 0) {
    u8g2.drawStr(0, 38, "listening...");
    snprintf(line, sizeof(line), "%.1fMHz SF%d", FREQ_MHZ, SPREAD_FACTOR);
    u8g2.drawStr(0, 50, line);
  } else {
    snprintf(line, sizeof(line), "from %s %s", lastNode, lastKind);
    u8g2.drawStr(0, 36, line);
    snprintf(line, sizeof(line), "w: %s kg", lastWeight);
    u8g2.drawStr(0, 48, line);
    unsigned long ago = (millis() - lastRxMs) / 1000;
    snprintf(line, sizeof(line), "%.0fdBm %.1fdB %lus", lastRssi, lastSnr, ago);
    u8g2.drawStr(0, 60, line);
  }
  u8g2.sendBuffer();
}

void printStatus() {
  long lastAgo = rxCount ? (long)(millis() - lastRxMs) : -1;
  Serial.printf("{\"type\":\"status\",\"role\":\"gateway\",\"uptime\":%lu,\"radio\":\"%s\",\"rx\":%lu,\"crc_err\":%lu,\"last_rx_ms\":%ld}\n",
                millis() / 1000, radioOk ? "ok" : "fail", (unsigned long)rxCount,
                (unsigned long)crcErrCount, lastAgo);
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  delay(800);

  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, LOW);  // Vext on (powers OLED)
  delay(50);
  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x13B_tf);
  u8g2.drawStr(0, 11, "GATEWAY (RX)");
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 30, "self-test...");
  u8g2.sendBuffer();

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);
  radioErr = radio.begin(FREQ_MHZ, BANDWIDTH_KHZ, SPREAD_FACTOR, CODING_RATE,
                         SYNC_WORD, TX_POWER_DBM, PREAMBLE_LEN, 1.8);
  radioOk = (radioErr == RADIOLIB_ERR_NONE);
  if (radioOk) {
    radio.setCRC(true);
    radio.setDio1Action(onRx);
    radioErr = radio.startReceive();
    radioOk = (radioErr == RADIOLIB_ERR_NONE);
  }

  Serial.printf("{\"type\":\"boot\",\"role\":\"gateway\",\"radio\":\"%s\",\"radio_code\":%d,"
                "\"freq\":%.2f,\"bw\":%.0f,\"sf\":%d,\"sync\":\"0x%02X\"}\n",
                radioOk ? "ok" : "fail", radioErr, FREQ_MHZ, BANDWIDTH_KHZ, SPREAD_FACTOR, SYNC_WORD);
  drawOled();
}

void loop() {
  if (gotPacket) {
    gotPacket = false;
    String payload;
    int st = radio.readData(payload);
    if (payload.length() > 0) {
      bool crcOk = (st == RADIOLIB_ERR_NONE);
      lastRssi = radio.getRSSI();
      lastSnr = radio.getSNR();
      if (crcOk) {
        rxCount++;
        lastRxMs = millis();
        if (!extractField(payload, "id", lastNode, sizeof(lastNode))) strcpy(lastNode, "?");
        if (!extractField(payload, "w", lastWeight, sizeof(lastWeight))) strcpy(lastWeight, "-");
        if (!extractField(payload, "k", lastKind, sizeof(lastKind))) lastKind[0] = 0;
      } else {
        crcErrCount++;
      }
      digitalWrite(LED_PIN, HIGH);
      Serial.printf("{\"type\":\"packet\",\"n\":%lu,\"rssi\":%.1f,\"snr\":%.2f,\"len\":%u,\"crc\":%s,\"raw\":",
                    (unsigned long)rxCount, lastRssi, lastSnr, (unsigned)payload.length(),
                    crcOk ? "true" : "false");
      printJsonString(payload);
      Serial.println("}");
      digitalWrite(LED_PIN, LOW);
      drawOled();
    }
    radio.startReceive();
  }

  if (millis() - lastStatusMs >= STATUS_INTERVAL_MS) {
    lastStatusMs = millis();
    printStatus();
    drawOled();  // refresh the "seconds ago" counter
  }
  delay(2);
}
