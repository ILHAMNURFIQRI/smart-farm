/**
 * Moo-nitoring Kandang Sapi - Node DEV_002 ESP32 Firmware
 * Smart Farm IoT & Edge Intelligence Subsystem
 *
 * Hardware Components & Pinout:
 * - DHT22 (Temperature & Relative Humidity): GPIO 4 (10k Pull-up to 3.3V)
 * - INMP441 (I2S Omnidirectional Digital Microphone):
 *     WS  -> GPIO 25
 *     SCK -> GPIO 33
 *     SD  -> GPIO 32
 *     L/R -> GND (Left Channel)
 * - MQ-135 (Air Quality Sensor - NH3, CO2, Benzene, Smoke):
 *     AOUT -> GPIO 34 (ADC1_CH6, input only, no Wi-Fi conflict)
 * - DS3231 (RTC I2C Module):
 *     SDA -> GPIO 21
 *     SCL -> GPIO 22
 * - Relay 1 (Mini Exhaust Fan 5V): GPIO 26 (Active LOW/HIGH)
 * - Relay 2 (Lampu LED Sirkadian 4000K): GPIO 27
 * - Active Buzzer: GPIO 14
 *
 * Protocols:
 * - MQTT Envelope 1.0.0 (sf-kandang-sapih/...)
 * - HTTP Fallback (/api/sensors)
 * - Local Edge Hysteresis: Fan ON > 31.5 C, OFF < 30.0 C
 * - Local Circadian Lighting: 18:00 - 06:00
 */

#include <WiFi.h>
#include <WiFiClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <RTClib.h>
#include <esp_idf_version.h>
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
#include <driver/i2s_std.h>
#else
#include <driver/i2s.h>
#endif
#include <HTTPClient.h>

// ==================== CONFIGURATION ====================
// Wi-Fi Credentials
const char* WIFI_SSID     = "Kost Dago";
const char* WIFI_PASSWORD = "44343329";

// MQTT Broker
const char* MQTT_BROKER   = "192.168.1.100"; // Ganti dengan IP host/broker
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "";
const char* MQTT_PASS     = "";

// Backend HTTP URL (Fallback & ML Ingestion)
const char* BACKEND_HTTP_URL = "http://192.168.100.13:5000/api/sensors";

// Farm & Device Metadata (Envelope 1.0.0)
const char* SCHEMA_VERSION = "1.0.0";
const char* FARM_ID        = "FARM_001";
const char* BARN_ID        = "BARN_001";
const char* ZONE_ID        = "ZONE_001";
const char* DEVICE_ID      = "DEV_002";

// MQTT Topics
const char* TOPIC_TELEMETRY = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_002/telemetry";
const char* TOPIC_STATUS    = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_002/status";
const char* TOPIC_COMMAND   = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_002/command";
const char* TOPIC_ACK       = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_002/command/ack";

// ==================== PIN DEFINITIONS ====================
#define PIN_DHT22     4
#define PIN_MQ135     34
#define PIN_RELAY_FAN 26
#define PIN_RELAY_LED 27
#define PIN_BUZZER    14

// I2S INMP441 Microphone Pins
#define I2S_WS        25
#define I2S_SD        32
#define I2S_SCK       33
#define I2S_PORT      I2S_NUM_0
#define I2S_SAMPLE_RATE 16000
#define I2S_BUFFER_LEN  512

// Relay Active State (Common Active LOW relay modules)
#define RELAY_ON      LOW
#define RELAY_OFF     HIGH

// Hysteresis Thresholds (Dynamic & can be updated from Web Automation Config)
float tempFanOn   = 30.5f;
float tempFanOff  = 28.5f;
#define TEMP_ALERT    34.0f
#define MQ_ALERT_RAW  850.0f

// ==================== OBJECTS & VARIABLES ====================
DHT dht(PIN_DHT22, DHT22);
RTC_DS3231 rtc;
WiFiClient espClient;
PubSubClient mqttClient(espClient);

// Actuator States
bool fanState = false;
bool ledState = false;
bool buzzerState = false;
bool autoFanMode = true;
bool autoLedMode = true;

// Sampling & Telemetry Timers
unsigned long lastTelemetryTime = 0;
const unsigned long TELEMETRY_INTERVAL_MS = 5000; // Kirim setiap 5 detik
unsigned long lastHeartbeatTime = 0;
const unsigned long HEARTBEAT_INTERVAL_MS = 30000; // Status setiap 30 detik

// Sensor Readings Cache
float currentTemperature = 28.0f;
float currentHumidity = 65.0f;
float currentThi = 72.0f;
float currentSoundRms = 0.05f;
float currentMqRaw = 450.0f;

// ==================== I2S SETUP & READING ====================
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
static i2s_chan_handle_t rx_handle = NULL;

void setupI2S() {
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &rx_handle);
    if (err != ESP_OK) {
        Serial.printf("[I2S] Channel create failed: %s\n", esp_err_to_name(err));
        return;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(I2S_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)I2S_SCK,
            .ws   = (gpio_num_t)I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din  = (gpio_num_t)I2S_SD,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };

    err = i2s_channel_init_std_mode(rx_handle, &std_cfg);
    if (err != ESP_OK) {
        Serial.printf("[I2S] Init std mode failed: %s\n", esp_err_to_name(err));
        return;
    }

    err = i2s_channel_enable(rx_handle);
    if (err != ESP_OK) {
        Serial.printf("[I2S] Enable channel failed: %s\n", esp_err_to_name(err));
        return;
    }

    Serial.println("[I2S] INMP441 Microphone Initialized (Modern Driver v5).");
}

float readSoundRMS() {
    if (!rx_handle) return 0.0f;

    int32_t samples[I2S_BUFFER_LEN];
    size_t bytesRead = 0;
    esp_err_t result = i2s_channel_read(rx_handle, samples, sizeof(samples), &bytesRead, pdMS_TO_TICKS(100));

    if (result != ESP_OK || bytesRead == 0) {
        return 0.0f;
    }

    int sampleCount = bytesRead / sizeof(int32_t);
    double sumSquares = 0.0;

    for (int i = 0; i < sampleCount; i++) {
        int32_t sample = samples[i] >> 8;
        double normalized = (double)sample / 8388608.0; // 2^23
        sumSquares += (normalized * normalized);
    }

    float rms = sqrt(sumSquares / sampleCount);
    return rms;
}
#else
// Legacy Driver for ESP-IDF v4 / Arduino-ESP32 v2.x
void setupI2S() {
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = I2S_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 4,
        .dma_buf_len = I2S_BUFFER_LEN,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num = I2S_SCK,
        .ws_io_num = I2S_WS,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = I2S_SD
    };

    esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("[I2S] Driver install failed: %d\n", err);
        return;
    }

    err = i2s_set_pin(I2S_PORT, &pin_config);
    if (err != ESP_OK) {
        Serial.printf("[I2S] Set pin failed: %d\n", err);
        return;
    }

    i2s_zero_dma_buffer(I2S_PORT);
    Serial.println("[I2S] INMP441 Microphone Initialized (Legacy Driver v4).");
}

float readSoundRMS() {
    int32_t samples[I2S_BUFFER_LEN];
    size_t bytesRead = 0;
    esp_err_t result = i2s_read(I2S_PORT, (char*)samples, sizeof(samples), &bytesRead, pdMS_TO_TICKS(100));

    if (result != ESP_OK || bytesRead == 0) {
        return 0.0f;
    }

    int sampleCount = bytesRead / sizeof(int32_t);
    double sumSquares = 0.0;

    for (int i = 0; i < sampleCount; i++) {
        int32_t sample = samples[i] >> 8;
        double normalized = (double)sample / 8388608.0;
        sumSquares += (normalized * normalized);
    }

    float rms = sqrt(sumSquares / sampleCount);
    return rms;
}
#endif

// ==================== HELPER FORMULAS ====================
// Rumus Temperature-Humidity Index (THI) Peternakan Sapi
float calculateTHI(float tempC, float humidityRh) {
    return (0.8f * tempC) + ((humidityRh / 100.0f) * (tempC - 14.4f)) + 46.4f;
}

// Generate UUID v4 format
String generateUUID() {
    uint32_t r1 = esp_random();
    uint32_t r2 = esp_random();
    uint32_t r3 = esp_random();
    uint32_t r4 = esp_random();

    char uuid[37];
    snprintf(uuid, sizeof(uuid), "%08x-%04x-4%03x-%04x-%04x%08x",
             r1, (uint16_t)(r2 >> 16), (uint16_t)(r2 & 0x0FFF),
             (uint16_t)((r3 >> 16) & 0x3FFF) | 0x8000,
             (uint16_t)(r3 & 0xFFFF), r4);
    return String(uuid);
}

// Get ISO8601 Timestamp from DS3231 RTC (WIB / UTC+7)
String getISO8601Timestamp() {
    if (rtc.lostPower()) {
        rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    }
    DateTime now = rtc.now();
    char buffer[32];
    snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02d+07:00",
             now.year(), now.month(), now.day(),
             now.hour(), now.minute(), now.second());
    return String(buffer);
}

// ==================== ACTUATOR CONTROL ====================
void setFan(bool on) {
    fanState = on;
    digitalWrite(PIN_RELAY_FAN, on ? RELAY_ON : RELAY_OFF);
    Serial.printf("[ACTUATOR] Exhaust Fan: %s\n", on ? "ON" : "OFF");
}

void setLED(bool on) {
    ledState = on;
    digitalWrite(PIN_RELAY_LED, on ? RELAY_ON : RELAY_OFF);
    Serial.printf("[ACTUATOR] Circadian LED: %s\n", on ? "ON" : "OFF");
}

void setBuzzer(bool on) {
    buzzerState = on;
    digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
    Serial.printf("[ACTUATOR] Buzzer Alert: %s\n", on ? "ALERT" : "SILENT");
}

// ==================== LOCAL AUTOMATION (EDGE LOGIC) ====================
void runLocalEdgeAutomation() {
    // 1. Histeresis Suhu Kipas Exhaust
    if (autoFanMode) {
        if (!fanState && currentTemperature >= tempFanOn) {
            Serial.printf("[AUTO] Suhu %.1fC >= %.1fC -> Menyalakan Kipas Otomatis\n", currentTemperature, tempFanOn);
            setFan(true);
        } else if (fanState && currentTemperature <= tempFanOff) {
            Serial.printf("[AUTO] Suhu %.1fC <= %.1fC -> Mematikan Kipas Otomatis\n", currentTemperature, tempFanOff);
            setFan(false);
        }
    }

    // 2. Pencahayaan Sirkadian berbasis DS3231 RTC (18:00 - 06:00 ON)
    if (autoLedMode) {
        DateTime now = rtc.now();
        int hour = now.hour();
        bool shouldLightOn = (hour >= 18 || hour < 6);
        if (ledState != shouldLightOn) {
            Serial.printf("[AUTO] Jam RTC %02d:00 -> Lampu Sirkadian %s\n", hour, shouldLightOn ? "ON" : "OFF");
            setLED(shouldLightOn);
        }
    }

    // 3. Peringatan Dini Lokal Buzzer (Kondisi Ekstrem)
    if (currentTemperature >= TEMP_ALERT || currentMqRaw >= MQ_ALERT_RAW) {
        if (!buzzerState) {
            Serial.println("[AUTO CRITICAL] Parameter ekstrem terdeteksi! Mengaktifkan Buzzer Lokal.");
            setBuzzer(true);
        }
    } else if (buzzerState && autoFanMode) {
        // Kembalikan ke hening jika parameter normal dan tidak dikunci manual
        setBuzzer(false);
    }
}

// ==================== MQTT CALLBACK & HANDLER ====================
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
    Serial.printf("[MQTT] Message arrived on [%s]\n", topic);

    StaticJsonDocument<1024> doc;
    DeserializationError err = deserializeJson(doc, payload, length);
    if (err) {
        Serial.printf("[MQTT] Deserialization failed: %s\n", err.c_str());
        return;
    }

    const char* messageType = doc["message_type"] | "";
    if (strcmp(messageType, "command") != 0) return;

    const char* commandId = doc["message_id"] | "";
    JsonObject payloadObj = doc["payload"];
    const char* actuatorId = payloadObj["actuator_id"] | "";
    const char* command = payloadObj["command"] | "";
    JsonObject params = payloadObj["parameters"];

    Serial.printf("[COMMAND] Actuator: %s | Cmd: %s\n", actuatorId, command);

    bool success = false;
    String ackStatus = "COMPLETED";
    String errorMsg = "";

    if (strcmp(actuatorId, "ACT_FAN_002") == 0) {
        if (strcmp(command, "SET_STATE") == 0) {
            const char* state = params["state"] | "OFF";
            bool turnOn = (strcmp(state, "ON") == 0);
            autoFanMode = false; // Override manual
            setFan(turnOn);
            success = true;
        }
    } else if (strcmp(actuatorId, "ACT_LED_002") == 0) {
        if (strcmp(command, "SET_STATE") == 0) {
            const char* state = params["state"] | "OFF";
            bool turnOn = (strcmp(state, "ON") == 0);
            autoLedMode = false; // Override manual
            setLED(turnOn);
            success = true;
        }
    } else if (strcmp(actuatorId, "ACT_BUZZER_002") == 0) {
        if (strcmp(command, "ALERT") == 0) {
            setBuzzer(true);
            success = true;
        } else if (strcmp(command, "CLEAR") == 0) {
            setBuzzer(false);
            success = true;
        }
    }

    if (!success) {
        ackStatus = "FAILED";
        errorMsg = "Perintah aktuator tidak didukung atau parameter tidak valid.";
    }

    // Publish Command ACK (Envelope 1.0.0)
    StaticJsonDocument<512> ackDoc;
    ackDoc["schema_version"] = SCHEMA_VERSION;
    ackDoc["message_id"]     = generateUUID();
    ackDoc["message_type"]   = "command_ack";
    ackDoc["timestamp"]      = getISO8601Timestamp();
    ackDoc["farm_id"]        = FARM_ID;
    ackDoc["barn_id"]        = BARN_ID;
    ackDoc["zone_id"]        = ZONE_ID;
    ackDoc["device_id"]      = DEVICE_ID;

    JsonObject ackPayload = ackDoc.createNestedObject("payload");
    ackPayload["command_id"]  = commandId;
    ackPayload["actuator_id"] = actuatorId;
    ackPayload["status"]      = ackStatus;
    if (errorMsg.length() > 0) {
        ackPayload["error_message"] = errorMsg;
    }

    char ackBuffer[512];
    serializeJson(ackDoc, ackBuffer);
    mqttClient.publish(TOPIC_ACK, ackBuffer, 1);
    Serial.printf("[MQTT] Sent ACK status: %s\n", ackStatus.c_str());
}

// ==================== TELEMETRY PUBLISH ====================
void publishMqttTelemetry() {
    StaticJsonDocument<1024> doc;
    doc["schema_version"] = SCHEMA_VERSION;
    doc["message_id"]     = generateUUID();
    doc["message_type"]   = "telemetry";
    doc["timestamp"]      = getISO8601Timestamp();
    doc["farm_id"]        = FARM_ID;
    doc["barn_id"]        = BARN_ID;
    doc["zone_id"]        = ZONE_ID;
    doc["device_id"]      = DEVICE_ID;

    JsonObject payload = doc.createNestedObject("payload");
    JsonArray readings = payload.createNestedArray("readings");

    // Sensor 1: Suhu Udara DHT22
    JsonObject rTemp = readings.createNestedObject();
    rTemp["sensor_id"] = "SNS_DHT22_002";
    rTemp["parameter"] = "temperature";
    rTemp["value"]     = currentTemperature;
    rTemp["unit"]      = "C";
    rTemp["quality"]   = isnan(currentTemperature) ? "BAD" : "GOOD";

    // Sensor 2: Kelembapan Udara DHT22
    JsonObject rHum = readings.createNestedObject();
    rHum["sensor_id"] = "SNS_DHT22_002";
    rHum["parameter"] = "humidity";
    rHum["value"]     = currentHumidity;
    rHum["unit"]      = "%";
    rHum["quality"]   = isnan(currentHumidity) ? "BAD" : "GOOD";

    // Sensor 3: Sound RMS INMP441
    JsonObject rSound = readings.createNestedObject();
    rSound["sensor_id"] = "SNS_INMP441_002";
    rSound["parameter"] = "sound_rms";
    rSound["value"]     = currentSoundRms;
    rSound["unit"]      = "rms";
    rSound["quality"]   = "GOOD";

    // Sensor 4: Air Quality MQ-135 Raw ADC
    JsonObject rMq = readings.createNestedObject();
    rMq["sensor_id"] = "SNS_MQ135_002";
    rMq["parameter"] = "mq135_raw";
    rMq["value"]     = currentMqRaw;
    rMq["unit"]      = "raw";
    rMq["quality"]   = "GOOD";

    char buffer[1024];
    serializeJson(doc, buffer);
    mqttClient.publish(TOPIC_TELEMETRY, buffer, 1);
    Serial.printf("[MQTT TELEMETRY] T:%.1fC | H:%.1f%% | THI:%.1f | Sound:%.3f | MQ:%.0f\n",
                  currentTemperature, currentHumidity, currentThi, currentSoundRms, currentMqRaw);
}

// Fallback HTTP Post to /api/sensors
void postHttpTelemetry() {
    if (WiFi.status() != WL_CONNECTED) return;

    HTTPClient http;
    http.begin(BACKEND_HTTP_URL);
    http.addHeader("Content-Type", "application/json");

    StaticJsonDocument<256> doc;
    doc["recorded_at"]   = getISO8601Timestamp();
    doc["temperature_c"] = currentTemperature;
    doc["humidity_rh"]   = currentHumidity;
    doc["sound_rms"]     = currentSoundRms;
    doc["mq135_raw"]     = currentMqRaw;

    String jsonStr;
    serializeJson(doc, jsonStr);
    int httpResponseCode = http.POST(jsonStr);

    if (httpResponseCode > 0) {
        Serial.printf("[HTTP TELEMETRY] T:%.1fC | H:%.1f%% | THI:%.1f | Sound:%.3f | MQ:%.0f -> Response: %d\n",
                      currentTemperature, currentHumidity, currentThi, currentSoundRms, currentMqRaw, httpResponseCode);

        // Sync Automation & Directives from Backend Response
        if (httpResponseCode == 200 || httpResponseCode == 201) {
            String resp = http.getString();
            StaticJsonDocument<768> respDoc;
            DeserializationError err = deserializeJson(respDoc, resp);
            if (!err && respDoc.containsKey("automation")) {
                JsonObject autoObj = respDoc["automation"];
                if (autoObj.containsKey("config")) {
                    JsonObject cfg = autoObj["config"];
                    if (cfg.containsKey("fan_temp_on"))  tempFanOn  = cfg["fan_temp_on"].as<float>();
                    if (cfg.containsKey("fan_temp_off")) tempFanOff = cfg["fan_temp_off"].as<float>();
                    if (cfg.containsKey("fan_auto"))     autoFanMode = cfg["fan_auto"].as<bool>();
                    if (cfg.containsKey("led_auto"))     autoLedMode = cfg["led_auto"].as<bool>();
                }
                if (autoObj.containsKey("fan_target_state")) {
                    const char* fTarget = autoObj["fan_target_state"];
                    if (fTarget != nullptr && strlen(fTarget) > 0) {
                        bool targetOn = (strcmp(fTarget, "ON") == 0);
                        if (fanState != targetOn) setFan(targetOn);
                    }
                }
                if (autoObj.containsKey("led_target_state")) {
                    const char* lTarget = autoObj["led_target_state"];
                    if (lTarget != nullptr && strlen(lTarget) > 0) {
                        bool targetOn = (strcmp(lTarget, "ON") == 0);
                        if (ledState != targetOn) setLED(targetOn);
                    }
                }
            }
        }
    } else {
        Serial.printf("[HTTP INGESTION] Error: %s\n", http.errorToString(httpResponseCode).c_str());
    }
    http.end();
}

// Status Heartbeat Publish
void publishStatus(const char* status) {
    StaticJsonDocument<512> doc;
    doc["schema_version"] = SCHEMA_VERSION;
    doc["message_id"]     = generateUUID();
    doc["message_type"]   = "status";
    doc["timestamp"]      = getISO8601Timestamp();
    doc["farm_id"]        = FARM_ID;
    doc["barn_id"]        = BARN_ID;
    doc["zone_id"]        = ZONE_ID;
    doc["device_id"]      = DEVICE_ID;

    JsonObject payload = doc.createNestedObject("payload");
    payload["connection_status"] = status;
    payload["firmware_version"]  = "2.1.0-moonitoring";
    payload["uptime_seconds"]    = millis() / 1000;
    payload["operational_state"] = fanState ? "FAN_ACTIVE" : "STANDBY";

    char buffer[512];
    serializeJson(doc, buffer);
    mqttClient.publish(TOPIC_STATUS, buffer, 1);
}

// ==================== SETUP & LOOP ====================
void connectWiFi() {
    Serial.printf("[WIFI] Connecting to %s", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 20) {
        delay(500);
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WIFI] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("\n[WIFI] Failed to connect! Continuing in offline edge mode.");
    }
}

void reconnectMqtt() {
    if (WiFi.status() != WL_CONNECTED) return;
    if (mqttClient.connected()) return;

    Serial.print("[MQTT] Attempting MQTT connection...");
    String clientId = "ESP32_DEV_002_" + String(random(0xffff), HEX);

    // Will topic: auto publish OFFLINE if disconnected abruptly
    StaticJsonDocument<256> willDoc;
    willDoc["schema_version"] = SCHEMA_VERSION;
    willDoc["message_id"]     = generateUUID();
    willDoc["message_type"]   = "status";
    willDoc["timestamp"]      = getISO8601Timestamp();
    willDoc["farm_id"]        = FARM_ID;
    willDoc["barn_id"]        = BARN_ID;
    willDoc["zone_id"]        = ZONE_ID;
    willDoc["device_id"]      = DEVICE_ID;
    willDoc["payload"]["connection_status"] = "OFFLINE";

    char willBuffer[256];
    serializeJson(willDoc, willBuffer);

    if (mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASS, TOPIC_STATUS, 1, true, willBuffer)) {
        Serial.println(" Connected to broker!");
        mqttClient.subscribe(TOPIC_COMMAND);
        publishStatus("ONLINE");
    } else {
        Serial.printf(" Failed, rc=%d. Will retry next cycle.\n", mqttClient.state());
    }
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== MOO-NITORING KANDANG SAPI - DEV_002 FIRMWARE ===");

    // Pin Modes
    pinMode(PIN_RELAY_FAN, OUTPUT);
    pinMode(PIN_RELAY_LED, OUTPUT);
    pinMode(PIN_BUZZER, OUTPUT);

    // Initial Actuator State (Off)
    digitalWrite(PIN_RELAY_FAN, RELAY_OFF);
    digitalWrite(PIN_RELAY_LED, RELAY_OFF);
    digitalWrite(PIN_BUZZER, LOW);

    // Initialize I2C and RTC DS3231
    Wire.begin(21, 22);
    if (!rtc.begin()) {
        Serial.println("[RTC] Could not find DS3231 module! Check SDA/SCL wiring.");
    } else {
        Serial.println("[RTC] DS3231 Initialized successfully.");
    }

    // Initialize DHT22
    dht.begin();
    Serial.println("[DHT22] Initialized on GPIO 4.");

    // Initialize I2S for INMP441 Microphone
    setupI2S();

    // ADC Resolution for MQ-135 on GPIO 34 (12-bit: 0 - 4095)
    analogReadResolution(12);

    // Connect Network
    connectWiFi();

    // Setup MQTT
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setCallback(onMqttMessage);
}

void loop() {
    // Keep Network Alive
    if (WiFi.status() == WL_CONNECTED) {
        if (!mqttClient.connected()) {
            reconnectMqtt();
        } else {
            mqttClient.loop();
        }
    }

    unsigned long now = millis();

    // Read Sensors & Run Local Edge Automation every 1 second
    static unsigned long lastSensorRead = 0;
    if (now - lastSensorRead >= 1000) {
        lastSensorRead = now;

        // 1. Read DHT22
        float t = dht.readTemperature();
        float h = dht.readHumidity();
        if (!isnan(t) && !isnan(h)) {
            currentTemperature = t;
            currentHumidity = h;
            currentThi = calculateTHI(currentTemperature, currentHumidity);
        }

        // 2. Read INMP441 Sound RMS via I2S DMA
        currentSoundRms = readSoundRMS();

        // 3. Read MQ-135 Gas Raw ADC (0 - 4095)
        currentMqRaw = analogRead(PIN_MQ135);

        // 4. Edge Hysteresis & Biological Circadian Scheduling
        runLocalEdgeAutomation();
    }

    // Telemetry Publishing (every 5 seconds)
    if (now - lastTelemetryTime >= TELEMETRY_INTERVAL_MS) {
        lastTelemetryTime = now;

        if (mqttClient.connected()) {
            publishMqttTelemetry();
        } else {
            // Fallback ke HTTP endpoint jika MQTT offline
            postHttpTelemetry();
        }
    }

    // Heartbeat Status (every 30 seconds)
    if (now - lastHeartbeatTime >= HEARTBEAT_INTERVAL_MS) {
        lastHeartbeatTime = now;
        if (mqttClient.connected()) {
            publishStatus("ONLINE");
        }
    }
}