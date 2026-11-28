/**
 * ============================================================================
 * SMART FARM MES - FIRMWARE TERINTEGRASI ESP32 DEV_003 (SMART FEEDING & WATER)
 * ============================================================================
 * 
 * Perangkat : DEV_003 (FARM_001 / BARN_001 / ZONE_001)
 * Komponen  :
 *   1. Pompa Air Minum (Relay 5V Aktif LOW) -> ACT_PUMP_003
 *   2. Konveyor Pakan Silase (Driver L298N DC Motor) -> ACT_FEEDER_MOTOR_003
 *   3. Sensor Level Pakan (Ultrasonik HC-SR04) -> SNS_ULTRASONIC_003
 *   4. Sensor Ketinggian Air (Analog Water Level) -> SNS_WATER_LEVEL_003
 *   5. Master Clock Hardware (RTC DS3231 I2C) + NTP Synchronization
 * 
 * Standar Kontrak: Envelope MQTT 1.0.0 Smart Farm MES
 * Mendukung DUAL-MODE:
 *   - Mode Standar MES: sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_003/...
 *   - Mode Kompatibilitas Legacy: smartfeeding/*
 * ============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include "RTClib.h"
#include <time.h>

// =====================================================
// KONFIGURASI WIFI & MQTT BROKER
// =====================================================
const char* WIFI_SSID     = "Samsung A17";
const char* WIFI_PASSWORD = "Fyreablaze";

// Arahkan ke IP Laptop/Server Backend Smart Farm (misal: "192.168.1.50")
// atau gunakan broker publik seperti "broker.emqx.io"
const char* MQTT_SERVER   = "broker.emqx.io";
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "";  // Kosongkan jika tanpa auth
const char* MQTT_PASS     = "";

// Identitas Topologi Smart Farm
const char* TOPIC_PREFIX  = "sf-kandang-sapih";
const char* FARM_ID       = "FARM_001";
const char* BARN_ID       = "BARN_001";
const char* ZONE_ID       = "ZONE_001";
const char* DEVICE_ID     = "DEV_003";

// Topik Standar Smart Farm (Otomatis Dibentuk)
char TOPIC_TELEMETRY[160];
char TOPIC_STATUS[160];
char TOPIC_COMMAND[160];
char TOPIC_COMMAND_ACK[160];

// Topik Kompatibilitas Legacy (smartfeeding/*)
const char* TOPIC_LEG_RTC_DATA       = "smartfeeding/rtc/data";
const char* TOPIC_LEG_RTC_LOG        = "smartfeeding/rtc/log";
const char* TOPIC_LEG_FEED_LEVEL     = "smartfeeding/feed/level";
const char* TOPIC_LEG_WATER_LEVEL    = "smartfeeding/water/level";
const char* TOPIC_LEG_PUMP_STATUS    = "smartfeeding/pump/status";
const char* TOPIC_LEG_PUMP_COMMAND   = "smartfeeding/pump/command";
const char* TOPIC_LEG_CONV_STATUS    = "smartfeeding/conveyor/status";
const char* TOPIC_LEG_CONV_COMMAND   = "smartfeeding/conveyor/command";
const char* TOPIC_LEG_CONV_SPEED     = "smartfeeding/conveyor/speed";

// =====================================================
// PIN HARDWARE ESP32
// =====================================================
#define PUMP_RELAY_PIN    19  // Relay Pompa 5V (Aktif LOW)
#define LED_MOTOR1_PIN    15  // Indikator LED Pompa 1
#define LED_MOTOR2_PIN    2   // Indikator LED Pompa 2

#define CONVEYOR_ENA_PIN  25  // L298N Speed (PWM)
#define CONVEYOR_IN1_PIN  32  // L298N Arah 1
#define CONVEYOR_IN2_PIN  33  // L298N Arah 2

#define WATER_SENSOR_PIN  34  // Analog Water Level ADC (Maks 3.3V)
#define TRIG_PIN          26  // HC-SR04 Trigger
#define ECHO_PIN          27  // HC-SR04 Echo

const uint8_t RELAY_ON  = LOW;
const uint8_t RELAY_OFF = HIGH;

const uint32_t CONVEYOR_PWM_FREQ       = 1000;
const uint8_t  CONVEYOR_PWM_RESOLUTION = 8;

// =====================================================
// KALIBRASI SENSOR
// =====================================================
const int   WATER_DRY_RAW      = 0;      // Nilai ADC saat sensor kering
const int   WATER_FULL_RAW     = 4095;   // Nilai ADC saat sensor terendam penuh
const float BATAS_AIR_RENDAH   = 20.0;   // %

const float TINGGI_WADAH_CM    = 30.0;   // Jarak dasar wadah ke sensor HC-SR04
const float BATAS_PAKAN_RENDAH = 20.0;   // %

// =====================================================
// JADWAL PAKAN RTC (06:00, 12:00, 15:15, 18:00)
// =====================================================
const int jadwalJam[]   = {6, 12, 15, 18};
const int jadwalMenit[] = {0, 0, 15, 0};
const int JUMLAH_JADWAL = 4;
bool jadwalSudahJalan[JUMLAH_JADWAL] = {false, false, false, false};

int hariTerakhir  = -1;
int bulanTerakhir = -1;
int tahunTerakhir = -1;

// =====================================================
// STATE SISTEM & PROTOKOL
// =====================================================
RTC_DS3231 rtc;
WiFiClient espClient;
PubSubClient mqtt(espClient);

bool pumpActive       = false;
bool conveyorActive   = false;
int  conveyorSpeedPwm = 200;  // 0 - 255
int  conveyorPercent  = 60;   // 0 - 100%

unsigned long waktuKirimTerakhir    = 0;
const unsigned long INTERVAL_KIRIM  = 2000; // Kirim telemetri setiap 2 detik

unsigned long waktuCobaWiFiTerakhir = 0;
unsigned long waktuCobaMQTTTerakhir = 0;
bool wifiSebelumnyaTerhubung        = false;
uint32_t messageCounter             = 0;

// =====================================================
// FUNGSI KONTROL AKTUATOR FISIK
// =====================================================
void terapkanStatusPompa() {
  digitalWrite(PUMP_RELAY_PIN, pumpActive ? RELAY_ON : RELAY_OFF);
  digitalWrite(LED_MOTOR1_PIN, pumpActive ? HIGH : LOW);
  digitalWrite(LED_MOTOR2_PIN, pumpActive ? HIGH : LOW);
}

void terapkanStatusKonveyor() {
  if (conveyorActive && conveyorSpeedPwm > 0) {
    digitalWrite(CONVEYOR_IN1_PIN, HIGH);
    digitalWrite(CONVEYOR_IN2_PIN, LOW);
    ledcWrite(CONVEYOR_ENA_PIN, conveyorSpeedPwm);
  } else {
    ledcWrite(CONVEYOR_ENA_PIN, 0);
    digitalWrite(CONVEYOR_IN1_PIN, LOW);
    digitalWrite(CONVEYOR_IN2_PIN, LOW);
  }
}

void matikanSemuaPerangkat() {
  pumpActive     = false;
  conveyorActive = false;
  terapkanStatusPompa();
  terapkanStatusKonveyor();
}

// =====================================================
// GENERATOR ISO 8601 UTC TIMESTAMP & MESSAGE ID
// =====================================================
String getISOTimestamp(const DateTime& dt) {
  // ISO 8601 UTC format: YYYY-MM-DDTHH:MM:SSZ
  char buf[30];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ",
           dt.year(), dt.month(), dt.day(),
           dt.hour(), dt.minute(), dt.second());
  return String(buf);
}

String generateMessageId() {
  // Pseudo-UUID v4 generator sederhana untuk payload envelope
  char buf[40];
  snprintf(buf, sizeof(buf), "%08x-%04x-4%03x-%04x-%08x%04x",
           (uint32_t)ESP.getEfuseMac(),
           (uint16_t)random(0, 0xFFFF),
           (uint16_t)random(0, 0xFFF),
           (uint16_t)random(0x8000, 0xBFFF),
           (uint32_t)millis(),
           (uint16_t)(++messageCounter));
  return String(buf);
}

// =====================================================
// PENGIRIMAN STATUS KONEKSI (STANDAR MES)
// =====================================================
void publikasikanStatusOnline() {
  if (!mqtt.connected()) return;
  DateTime now = rtc.now();
  String ts = getISOTimestamp(now);
  String msgId = generateMessageId();

  char payload[380];
  snprintf(payload, sizeof(payload),
    "{\"schema_version\":\"1.0.0\","
    "\"message_id\":\"%s\","
    "\"message_type\":\"status\","
    "\"timestamp\":\"%s\","
    "\"farm_id\":\"%s\","
    "\"barn_id\":\"%s\","
    "\"zone_id\":\"%s\","
    "\"device_id\":\"%s\","
    "\"payload\":{"
      "\"connection_state\":\"ONLINE\","
      "\"operational_state\":\"READY\","
      "\"firmware_version\":\"1.0.0\","
      "\"uptime_seconds\":%lu"
    "}}",
    msgId.c_str(), ts.c_str(), FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID,
    (unsigned long)(millis() / 1000)
  );

  mqtt.publish(TOPIC_STATUS, payload, true);
  Serial.println("[MQTT] Status ONLINE terkirim ke backend.");
}

// =====================================================
// PENGIRIMAN ACK PERINTAH (STANDAR MES)
// =====================================================
void kirimCommandAck(const String& cmdId, const String& actId, const String& result,
                     const String& state, int speedPercent, const String& msg) {
  if (!mqtt.connected()) return;
  DateTime now = rtc.now();
  String ts = getISOTimestamp(now);
  String msgId = generateMessageId();

  char payload[512];
  snprintf(payload, sizeof(payload),
    "{\"schema_version\":\"1.0.0\","
    "\"message_id\":\"%s\","
    "\"message_type\":\"command_ack\","
    "\"timestamp\":\"%s\","
    "\"farm_id\":\"%s\","
    "\"barn_id\":\"%s\","
    "\"zone_id\":\"%s\","
    "\"device_id\":\"%s\","
    "\"payload\":{"
      "\"command_id\":\"%s\","
      "\"actuator_id\":\"%s\","
      "\"result\":\"%s\","
      "\"actual_state\":{\"state\":\"%s\",\"speed_percent\":%d},"
      "\"message\":\"%s\""
    "}}",
    msgId.c_str(), ts.c_str(), FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID,
    cmdId.c_str(), actId.c_str(), result.c_str(), state.c_str(), speedPercent, msg.c_str()
  );

  mqtt.publish(TOPIC_COMMAND_ACK, payload, 1);
  Serial.print("[MQTT] ACK terkirim untuk Command ID: ");
  Serial.println(cmdId);
}

// =====================================================
// PEMBACAAN SENSOR
// =====================================================
float bacaJarakUltrasonik() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(3);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  unsigned long durasi = pulseIn(ECHO_PIN, HIGH, 30000UL);
  if (durasi == 0) return -1.0;
  float jarak = durasi * 0.0343 / 2.0;
  if (jarak < 2.0 || jarak > 400.0) return -1.0;
  return jarak;
}

float hitungLevelPakan(float jarak) {
  if (jarak < 0) return -1.0;
  float level = ((TINGGI_WADAH_CM - jarak) / TINGGI_WADAH_CM) * 100.0;
  return constrain(level, 0.0, 100.0);
}

String hitungStatusPakan(float level) {
  if (level < 0) return "ERROR";
  if (level <= BATAS_PAKAN_RENDAH) return "LOW";
  return "NORMAL";
}

int bacaWaterRaw() {
  uint32_t total = 0;
  for (int i = 0; i < 10; i++) {
    total += analogRead(WATER_SENSOR_PIN);
    delay(2);
  }
  return total / 10;
}

float hitungLevelAir(int raw) {
  if (WATER_DRY_RAW == WATER_FULL_RAW) return -1.0;
  float level = ((float)(raw - WATER_DRY_RAW) / (float)(WATER_FULL_RAW - WATER_DRY_RAW)) * 100.0;
  return constrain(level, 0.0, 100.0);
}

// =====================================================
// PENGIRIMAN TELEMETRI
// =====================================================
void kirimTelemetri(float jarak, float feedLevel, const String& feedStatus,
                    float waterLevel, int waterRaw, const DateTime& now) {
  if (!mqtt.connected()) return;

  String ts = getISOTimestamp(now);
  String msgId = generateMessageId();

  // 1. Publikasikan ke Topik Standar Smart Farm MES Envelope 1.0.0
  char payload[750];
  snprintf(payload, sizeof(payload),
    "{\"schema_version\":\"1.0.0\","
    "\"message_id\":\"%s\","
    "\"message_type\":\"telemetry\","
    "\"timestamp\":\"%s\","
    "\"farm_id\":\"%s\","
    "\"barn_id\":\"%s\","
    "\"zone_id\":\"%s\","
    "\"device_id\":\"%s\","
    "\"payload\":{"
      "\"readings\":["
        "{\"sensor_id\":\"SNS_ULTRASONIC_003\",\"parameter\":\"distance_cm\",\"value\":%.2f,\"unit\":\"cm\",\"quality\":\"%s\"},"
        "{\"sensor_id\":\"SNS_ULTRASONIC_003\",\"parameter\":\"feed_level_percent\",\"value\":%.1f,\"unit\":\"%%\",\"quality\":\"%s\"},"
        "{\"sensor_id\":\"SNS_ULTRASONIC_003\",\"parameter\":\"feed_status\",\"value\":\"%s\",\"unit\":\"state\",\"quality\":\"%s\"},"
        "{\"sensor_id\":\"SNS_WATER_LEVEL_003\",\"parameter\":\"water_level_percent\",\"value\":%.1f,\"unit\":\"%%\",\"quality\":\"%s\"}"
      "]"
    "}}",
    msgId.c_str(), ts.c_str(), FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID,
    jarak >= 0 ? jarak : 0.0, jarak >= 0 ? "GOOD" : "BAD",
    feedLevel >= 0 ? feedLevel : 0.0, feedLevel >= 0 ? "GOOD" : "BAD",
    feedStatus.c_str(), feedStatus != "ERROR" ? "GOOD" : "BAD",
    waterLevel >= 0 ? waterLevel : 0.0, waterLevel >= 0 ? "GOOD" : "BAD"
  );

  mqtt.publish(TOPIC_TELEMETRY, payload, 1);

  // 2. Publikasikan juga ke Topik Legacy (smartfeeding/*)
  char legPayload[300];
  snprintf(legPayload, sizeof(legPayload),
    "{\"jarak_cm\":%.2f,\"feed_level\":%.1f,\"feed_status\":\"%s\","
    "\"water_raw\":%d,\"water_level\":%.1f,\"pump_status\":\"%s\","
    "\"conveyor_status\":\"%s\",\"conveyor_speed\":%d}",
    jarak, feedLevel, feedStatus.c_str(), waterRaw, waterLevel,
    pumpActive ? "ON" : "OFF", conveyorActive ? "ON" : "OFF", conveyorSpeedPwm
  );
  mqtt.publish(TOPIC_LEG_RTC_DATA, legPayload);
  mqtt.publish(TOPIC_LEG_FEED_LEVEL, legPayload);

  Serial.println("[TELEMETRY] Standar MES & Legacy published successfully.");
}

// =====================================================
// PEMROSESAN PERINTAH DARI MQTT (CALLBACK)
// =====================================================
void mqttCallback(char* topic, byte* payloadBytes, unsigned int length) {
  String payloadStr;
  payloadStr.reserve(length + 1);
  for (unsigned int i = 0; i < length; i++) {
    payloadStr += (char)payloadBytes[i];
  }
  payloadStr.trim();

  Serial.print("[MQTT Masuk] Topik: ");
  Serial.print(topic);
  Serial.print(" | Payload: ");
  Serial.println(payloadStr);

  // A. Perintah Standar Smart Farm MES (/command)
  if (strcmp(topic, TOPIC_COMMAND) == 0) {
    // Ekstraksi nilai JSON sederhana tanpa library berat
    int actIdx = payloadStr.indexOf("\"actuator_id\"");
    int cmdIdx = payloadStr.indexOf("\"command\"");
    int idIdx  = payloadStr.indexOf("\"command_id\"");

    String cmdId = "";
    if (idIdx != -1) {
      int startVal = payloadStr.indexOf("\"", idIdx + 13) + 1;
      int endVal   = payloadStr.indexOf("\"", startVal);
      if (startVal != -1 && endVal != -1) cmdId = payloadStr.substring(startVal, endVal);
    }

    String actId = "";
    if (actIdx != -1) {
      int startVal = payloadStr.indexOf("\"", actIdx + 14) + 1;
      int endVal   = payloadStr.indexOf("\"", startVal);
      if (startVal != -1 && endVal != -1) actId = payloadStr.substring(startVal, endVal);
    }

    bool isTurnOn = payloadStr.indexOf("\"state\":\"ON\"") != -1 || payloadStr.indexOf("\"state\": \"ON\"") != -1;
    bool isTurnOff = payloadStr.indexOf("\"state\":\"OFF\"") != -1 || payloadStr.indexOf("\"state\": \"OFF\"") != -1;

    // Periksa speed_percent jika ada
    int speedPercent = 60;
    int spdIdx = payloadStr.indexOf("\"speed_percent\"");
    if (spdIdx != -1) {
      int colonIdx = payloadStr.indexOf(":", spdIdx);
      int commaIdx = payloadStr.indexOfAny(",}", colonIdx);
      if (colonIdx != -1 && commaIdx != -1) {
        String numStr = payloadStr.substring(colonIdx + 1, commaIdx);
        numStr.trim();
        speedPercent = constrain(numStr.toInt(), 0, 100);
      }
    }

    if (actId == "ACT_PUMP_003") {
      if (isTurnOn) {
        pumpActive = true;
        terapkanStatusPompa();
        kirimCommandAck(cmdId, actId, "SUCCESS", "ON", 0, "Pompa air aktif");
      } else if (isTurnOff) {
        pumpActive = false;
        terapkanStatusPompa();
        kirimCommandAck(cmdId, actId, "SUCCESS", "OFF", 0, "Pompa air nonaktif");
      }
    } else if (actId == "ACT_FEEDER_MOTOR_003") {
      if (isTurnOn) {
        conveyorActive = true;
        conveyorPercent = speedPercent;
        conveyorSpeedPwm = (int)(speedPercent * 255.0 / 100.0);
        terapkanStatusKonveyor();
        kirimCommandAck(cmdId, actId, "SUCCESS", "ON", speedPercent, "Konveyor motor aktif");
      } else if (isTurnOff) {
        conveyorActive = false;
        conveyorPercent = 0;
        terapkanStatusKonveyor();
        kirimCommandAck(cmdId, actId, "SUCCESS", "OFF", 0, "Konveyor motor nonaktif");
      }
    }
    return;
  }

  // B. Perintah Mode Legacy (smartfeeding/*)
  payloadStr.toUpperCase();
  if (strcmp(topic, TOPIC_LEG_PUMP_COMMAND) == 0) {
    if (payloadStr == "ON" || payloadStr == "PUMP ON") {
      pumpActive = true;
    } else if (payloadStr == "OFF" || payloadStr == "PUMP OFF") {
      pumpActive = false;
    }
    terapkanStatusPompa();
    mqtt.publish(TOPIC_LEG_PUMP_STATUS, pumpActive ? "ON" : "OFF", true);
    return;
  }

  if (strcmp(topic, TOPIC_LEG_CONV_COMMAND) == 0) {
    if (payloadStr == "ON" || payloadStr == "CONVEYOR ON") {
      conveyorActive = true;
    } else if (payloadStr == "OFF" || payloadStr == "CONVEYOR OFF") {
      conveyorActive = false;
    }
    terapkanStatusKonveyor();
    mqtt.publish(TOPIC_LEG_CONV_STATUS, conveyorActive ? "ON" : "OFF", true);
    return;
  }

  if (strcmp(topic, TOPIC_LEG_CONV_SPEED) == 0) {
    int spd = payloadStr.toInt();
    if (spd >= 0 && spd <= 255) {
      conveyorSpeedPwm = spd;
      if (conveyorSpeedPwm == 0) conveyorActive = false;
      terapkanStatusKonveyor();
    }
  }
}

// =====================================================
// KONEKSI & SETUP
// =====================================================
void hubungkanMQTT() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  if (millis() - waktuCobaMQTTTerakhir < 5000) return;
  waktuCobaMQTTTerakhir = millis();

  Serial.print("[MQTT] Menghubungkan ke ");
  Serial.print(MQTT_SERVER);
  Serial.print("...");

  String clientId = "DEV_003-";
  clientId += String((uint32_t)ESP.getEfuseMac(), HEX);

  bool connected = false;
  if (strlen(MQTT_USER) > 0) {
    connected = mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS);
  } else {
    connected = mqtt.connect(clientId.c_str());
  }

  if (connected) {
    Serial.println(" Berhasil terhubung!");
    // Subscribe ke Topik Perintah Standar MES
    mqtt.subscribe(TOPIC_COMMAND, 1);

    // Subscribe ke Topik Perintah Legacy
    mqtt.subscribe(TOPIC_LEG_PUMP_COMMAND, 1);
    mqtt.subscribe(TOPIC_LEG_CONV_COMMAND, 1);
    mqtt.subscribe(TOPIC_LEG_CONV_SPEED, 1);

    // Publikasikan status ONLINE ke backend
    publikasikanStatusOnline();
    matikanSemuaPerangkat();
  } else {
    Serial.print(" Gagal, kode error: ");
    Serial.println(mqtt.state());
  }
}

void sinkronisasiNTP() {
  if (WiFi.status() != WL_CONNECTED) return;
  Serial.println("[NTP] Sinkronisasi waktu...");
  configTzTime("WIB-7", "pool.ntp.org", "time.google.com");
  struct tm ti;
  if (getLocalTime(&ti, 10000)) {
    rtc.adjust(DateTime(ti.tm_year + 1900, ti.tm_mon + 1, ti.tm_mday,
                        ti.tm_hour, ti.tm_min, ti.tm_sec));
    Serial.println("[NTP] Sinkronisasi RTC DS3231 berhasil.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  // Bentuk Nama Topik Standar Smart Farm MES
  snprintf(TOPIC_TELEMETRY, sizeof(TOPIC_TELEMETRY),
           "%s/farm/%s/barn/%s/zone/%s/device/%s/telemetry",
           TOPIC_PREFIX, FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID);
  snprintf(TOPIC_STATUS, sizeof(TOPIC_STATUS),
           "%s/farm/%s/barn/%s/zone/%s/device/%s/status",
           TOPIC_PREFIX, FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID);
  snprintf(TOPIC_COMMAND, sizeof(TOPIC_COMMAND),
           "%s/farm/%s/barn/%s/zone/%s/device/%s/command",
           TOPIC_PREFIX, FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID);
  snprintf(TOPIC_COMMAND_ACK, sizeof(TOPIC_COMMAND_ACK),
           "%s/farm/%s/barn/%s/zone/%s/device/%s/command/ack",
           TOPIC_PREFIX, FARM_ID, BARN_ID, ZONE_ID, DEVICE_ID);

  Wire.begin(21, 22);

  // Inisialisasi GPIO Aktuator
  pinMode(PUMP_RELAY_PIN, OUTPUT);
  pinMode(LED_MOTOR1_PIN, OUTPUT);
  pinMode(LED_MOTOR2_PIN, OUTPUT);
  matikanSemuaPerangkat();

  pinMode(CONVEYOR_IN1_PIN, OUTPUT);
  pinMode(CONVEYOR_IN2_PIN, OUTPUT);
  ledcAttach(CONVEYOR_ENA_PIN, CONVEYOR_PWM_FREQ, CONVEYOR_PWM_RESOLUTION);
  ledcWrite(CONVEYOR_ENA_PIN, 0);

  // Sensor
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
  analogReadResolution(12);
  analogSetPinAttenuation(WATER_SENSOR_PIN, ADC_11db);

  // RTC DS3231
  if (!rtc.begin()) {
    Serial.println("[RTC] DS3231 tidak terdeteksi!");
  }

  // Inisialisasi Wi-Fi & MQTT
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(1024);

  Serial.println("==================================================");
  Serial.println("ESP32 SMART FEEDING (DEV_003) SIAP DIJALANKAN");
  Serial.println("==================================================");
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiSebelumnyaTerhubung) {
      wifiSebelumnyaTerhubung = true;
      Serial.println("[WiFi] Terhubung!");
      sinkronisasiNTP();
    }
    hubungkanMQTT();
    if (mqtt.connected()) {
      mqtt.loop();
    }
  } else {
    if (wifiSebelumnyaTerhubung) {
      wifiSebelumnyaTerhubung = false;
      Serial.println("[WiFi] Terputus. Menjalankan fail-safe (matikan aktuator).");
      matikanSemuaPerangkat();
    }
  }

  // Kirim Telemetri Berkala
  if (millis() - waktuKirimTerakhir >= INTERVAL_KIRIM) {
    waktuKirimTerakhir = millis();

    float jarak = bacaJarakUltrasonik();
    float feedLevel = hitungLevelPakan(jarak);
    String feedStatus = hitungStatusPakan(feedLevel);

    int waterRaw = bacaWaterRaw();
    float waterLevel = hitungLevelAir(waterRaw);

    DateTime now = rtc.now();
    kirimTelemetri(jarak, feedLevel, feedStatus, waterLevel, waterRaw, now);
  }

  delay(20);
}
