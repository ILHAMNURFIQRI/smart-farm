#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include "RTClib.h"
#include <time.h>

// =====================================================
// KONFIGURASI WIFI
// Isi kembali SSID dan password Wi-Fi Anda.
// =====================================================
const char* WIFI_SSID = "NAMA_WIFI_ANDA";
const char* WIFI_PASSWORD = "PASSWORD_WIFI_ANDA";

// =====================================================
// KONFIGURASI MQTT
// =====================================================
const char* MQTT_SERVER = "broker.emqx.io";
const int MQTT_PORT = 1883;

const char* TOPIC_RTC_DATA = "smartfeeding/rtc/data";
const char* TOPIC_RTC_LOG = "smartfeeding/rtc/log";
const char* TOPIC_PUMP_STATUS = "smartfeeding/pump/status";
const char* TOPIC_PUMP_COMMAND = "smartfeeding/pump/command";
const char* TOPIC_SCHEDULE = "smartfeeding/rtc/schedule";
const char* TOPIC_FEED_LEVEL = "smartfeeding/feed/level";
const char* TOPIC_WATER_LEVEL = "smartfeeding/water/level";

const char* TOPIC_CONVEYOR_COMMAND = "smartfeeding/conveyor/command";
const char* TOPIC_CONVEYOR_STATUS = "smartfeeding/conveyor/status";
const char* TOPIC_CONVEYOR_SPEED = "smartfeeding/conveyor/speed";

// =====================================================
// PIN ESP32
// =====================================================
// Pompa 5V menggunakan relay (aktif LOW).
#define PUMP_RELAY_PIN  19

// Dua LED lama tetap digunakan sebagai indikator pompa.
#define LED_MOTOR1_PIN  15
#define LED_MOTOR2_PIN  2

// L298N khusus untuk motor DC konveyor.
#define CONVEYOR_ENA_PIN 25
#define CONVEYOR_IN1_PIN 32
#define CONVEYOR_IN2_PIN 33

// Sensor analog water level.
#define WATER_SENSOR_PIN 34

// HC-SR04 untuk level pakan.
#define TRIG_PIN 26
#define ECHO_PIN 27

// RTC DS3231 menggunakan SDA 21 dan SCL 22.
const uint8_t RELAY_ON = LOW;
const uint8_t RELAY_OFF = HIGH;

// PWM L298N. Berlaku untuk Arduino-ESP32 core 3.x.
const uint32_t CONVEYOR_PWM_FREQ = 1000;
const uint8_t CONVEYOR_PWM_RESOLUTION = 8;

// =====================================================
// KALIBRASI SENSOR WATER LEVEL
// Ganti nilai ini berdasarkan pembacaan Serial Monitor.
// WATER_DRY_RAW  = nilai ADC saat sensor menunjukkan kosong/kering.
// WATER_FULL_RAW = nilai ADC saat sensor menunjukkan penuh.
// Nilainya bisa terbalik, tergantung jenis sensor.
// =====================================================
const int WATER_DRY_RAW = 0;       // WAJIB kalibrasi
const int WATER_FULL_RAW = 4095;   // WAJIB kalibrasi
const float BATAS_AIR_RENDAH = 20.0;

// =====================================================
// OBJEK RTC DAN MQTT
// =====================================================
RTC_DS3231 rtc;
WiFiClient espClient;
PubSubClient mqtt(espClient);

// =====================================================
// PENGATURAN WADAH PAKAN
// Asumsi jarak sensor ke dasar wadah = 30 cm.
// Sesuaikan dengan tinggi wadah sebenarnya.
// =====================================================
const float TINGGI_WADAH_CM = 30.0;
const float BATAS_PAKAN_RENDAH = 20.0;

// =====================================================
// JADWAL PAKAN
// Jadwal hanya mengirim notifikasi MQTT.
// Jadwal TIDAK menyalakan pompa atau konveyor otomatis.
// =====================================================
const int jadwalJam[] = {6, 12, 15, 18};
const int jadwalMenit[] = {0, 0, 15, 0};
const int JUMLAH_JADWAL = sizeof(jadwalJam) / sizeof(jadwalJam[0]);
bool jadwalSudahJalan[JUMLAH_JADWAL] = {false, false, false, false};

int hariTerakhir = -1;
int bulanTerakhir = -1;
int tahunTerakhir = -1;

// =====================================================
// STATUS SISTEM
// =====================================================
bool mqttPumpOn = false;
bool mqttConveyorOn = false;
int conveyorSpeed = 200;

bool statusPumpSebelumnya = false;
bool statusConveyorSebelumnya = false;
int speedSebelumnya = -1;
bool wifiSebelumnyaTerhubung = false;

unsigned long waktuKirimTerakhir = 0;
const unsigned long INTERVAL_KIRIM = 1000;

unsigned long waktuCobaWiFiTerakhir = 0;
const unsigned long INTERVAL_COBA_WIFI = 10000;

unsigned long waktuCobaMQTTTerakhir = 0;
const unsigned long INTERVAL_COBA_MQTT = 5000;

// =====================================================
// FUNGSI OUTPUT POMPA DAN KONVEYOR
// =====================================================
void terapkanStatusPompa() {
  digitalWrite(PUMP_RELAY_PIN, mqttPumpOn ? RELAY_ON : RELAY_OFF);

  // LED lama mengikuti status pompa.
  digitalWrite(LED_MOTOR1_PIN, mqttPumpOn ? HIGH : LOW);
  digitalWrite(LED_MOTOR2_PIN, mqttPumpOn ? HIGH : LOW);
}

void terapkanStatusKonveyor() {
  if (mqttConveyorOn) {
    digitalWrite(CONVEYOR_IN1_PIN, HIGH);
    digitalWrite(CONVEYOR_IN2_PIN, LOW);
    ledcWrite(CONVEYOR_ENA_PIN, conveyorSpeed);
  } else {
    ledcWrite(CONVEYOR_ENA_PIN, 0);
    digitalWrite(CONVEYOR_IN1_PIN, LOW);
    digitalWrite(CONVEYOR_IN2_PIN, LOW);
  }
}

void matikanSemuaPerangkat() {
  mqttPumpOn = false;
  mqttConveyorOn = false;
  terapkanStatusPompa();
  terapkanStatusKonveyor();
}

// =====================================================
// WIFI
// =====================================================
void mulaiKoneksiWiFi() {
  Serial.println();
  Serial.print("Mencoba menghubungkan Wi-Fi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  waktuCobaWiFiTerakhir = millis();
}

bool tungguWiFi(unsigned long batasWaktu) {
  unsigned long mulai = millis();

  while (WiFi.status() != WL_CONNECTED &&
         millis() - mulai < batasWaktu) {
    delay(250);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.println("Wi-Fi terhubung!");
    Serial.print("IP ESP32: ");
    Serial.println(WiFi.localIP());
    return true;
  }

  Serial.println();
  Serial.println("Wi-Fi belum terhubung. Sistem tetap menggunakan RTC.");
  return false;
}

// =====================================================
// SINKRONISASI RTC DENGAN NTP
// =====================================================
bool sinkronisasiRTC() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("NTP dilewati karena Wi-Fi tidak terhubung.");
    return false;
  }

  Serial.println();
  Serial.println("Mengambil waktu dari server NTP...");
  configTzTime("WIB-7", "pool.ntp.org", "time.google.com");

  struct tm waktuNTP;
  if (getLocalTime(&waktuNTP, 20000)) {
    rtc.adjust(DateTime(
      waktuNTP.tm_year + 1900,
      waktuNTP.tm_mon + 1,
      waktuNTP.tm_mday,
      waktuNTP.tm_hour,
      waktuNTP.tm_min,
      waktuNTP.tm_sec
    ));

    Serial.println("Sinkronisasi RTC berhasil.");
    return true;
  }

  Serial.println("Sinkronisasi NTP gagal. Menggunakan waktu RTC.");
  return false;
}

void perbaruiTanggalTerakhir() {
  DateTime sekarang = rtc.now();

  hariTerakhir = sekarang.day();
  bulanTerakhir = sekarang.month();
  tahunTerakhir = sekarang.year();

  for (int i = 0; i < JUMLAH_JADWAL; i++) {
    jadwalSudahJalan[i] = false;
  }
}

// =====================================================
// PUBLIKASI STATUS MQTT
// =====================================================
void publikasikanStatusPerangkat(bool paksa = false) {
  if (!mqtt.connected()) return;

  if (paksa || mqttPumpOn != statusPumpSebelumnya) {
    mqtt.publish(TOPIC_PUMP_STATUS, mqttPumpOn ? "ON" : "OFF", true);
    statusPumpSebelumnya = mqttPumpOn;
  }

  if (paksa || mqttConveyorOn != statusConveyorSebelumnya) {
    mqtt.publish(TOPIC_CONVEYOR_STATUS, mqttConveyorOn ? "ON" : "OFF", true);
    statusConveyorSebelumnya = mqttConveyorOn;
  }

  if (paksa || conveyorSpeed != speedSebelumnya) {
    char nilaiSpeed[8];
    snprintf(nilaiSpeed, sizeof(nilaiSpeed), "%d", conveyorSpeed);
    mqtt.publish(TOPIC_CONVEYOR_SPEED, nilaiSpeed, true);
    speedSebelumnya = conveyorSpeed;
  }
}

// =====================================================
// MQTT CALLBACK: PERINTAH DARI WEBSITE
// Pompa: smartfeeding/pump/command -> ON / OFF
// Konveyor: smartfeeding/conveyor/command -> ON / OFF
// Kecepatan: smartfeeding/conveyor/speed -> angka 0-255
// =====================================================
void callback(char* topic, byte* payload, unsigned int length) {
  String pesan;
  pesan.reserve(length);

  for (unsigned int i = 0; i < length; i++) {
    pesan += (char)payload[i];
  }

  pesan.trim();
  pesan.toUpperCase();

  Serial.print("MQTT diterima [");
  Serial.print(topic);
  Serial.print("]: ");
  Serial.println(pesan);

  if (strcmp(topic, TOPIC_PUMP_COMMAND) == 0) {
    if (pesan == "ON" || pesan == "PUMP ON" || pesan == "PUMP_ON") {
      mqttPumpOn = true;
      Serial.println("Perintah website: POMPA ON");
    } else if (pesan == "OFF" || pesan == "PUMP OFF" || pesan == "PUMP_OFF") {
      mqttPumpOn = false;
      Serial.println("Perintah website: POMPA OFF");
    } else {
      Serial.println("Perintah pompa tidak dikenali.");
    }
    terapkanStatusPompa();
    publikasikanStatusPerangkat();
    return;
  }

  if (strcmp(topic, TOPIC_CONVEYOR_COMMAND) == 0) {
    if (pesan == "ON" || pesan == "CONVEYOR ON" || pesan == "CONVEYOR_ON") {
      mqttConveyorOn = true;
      Serial.println("Perintah website: KONVEYOR ON");
    } else if (pesan == "OFF" || pesan == "CONVEYOR OFF" || pesan == "CONVEYOR_OFF") {
      mqttConveyorOn = false;
      Serial.println("Perintah website: KONVEYOR OFF");
    } else {
      Serial.println("Perintah konveyor tidak dikenali.");
    }
    terapkanStatusKonveyor();
    publikasikanStatusPerangkat();
    return;
  }

  if (strcmp(topic, TOPIC_CONVEYOR_SPEED) == 0) {
    bool angkaValid = pesan.length() > 0;
    for (unsigned int i = 0; i < pesan.length(); i++) {
      if (!isDigit(pesan[i])) angkaValid = false;
    }

    if (angkaValid) {
      conveyorSpeed = constrain(pesan.toInt(), 0, 255);
      if (conveyorSpeed == 0) mqttConveyorOn = false;
      terapkanStatusKonveyor();
      publikasikanStatusPerangkat();
      Serial.print("Kecepatan PWM konveyor: ");
      Serial.println(conveyorSpeed);
    } else {
      Serial.println("Nilai speed harus berupa angka 0-255.");
    }
  }
}

// =====================================================
// INISIALISASI MQTT
// =====================================================
void inisialisasiMQTT() {
  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(callback);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);
}

void cobaKoneksiMQTT() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  if (millis() - waktuCobaMQTTTerakhir < INTERVAL_COBA_MQTT) return;

  waktuCobaMQTTTerakhir = millis();

  Serial.print("Mencoba menghubungkan MQTT... ");
  String clientId = "SmartFeeding-";
  clientId += String((uint32_t)ESP.getEfuseMac(), HEX);

  if (mqtt.connect(clientId.c_str())) {
    Serial.println("Terhubung!");

    mqtt.subscribe(TOPIC_PUMP_COMMAND);
    mqtt.subscribe(TOPIC_CONVEYOR_COMMAND);
    mqtt.subscribe(TOPIC_CONVEYOR_SPEED);

    // Fail-safe: setiap koneksi MQTT baru dimulai dengan semua output OFF.
    matikanSemuaPerangkat();
    statusPumpSebelumnya = false;
    statusConveyorSebelumnya = false;
    speedSebelumnya = -1;
    publikasikanStatusPerangkat(true);
  } else {
    Serial.print("Gagal, kode MQTT: ");
    Serial.println(mqtt.state());
  }
}

// =====================================================
// SENSOR HC-SR04
// =====================================================
float bacaJarak() {
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

// =====================================================
// HITUNG LEVEL PAKAN
// =====================================================
float hitungLevelPakan(float jarak) {
  if (jarak < 0) return -1.0;

  float level = ((TINGGI_WADAH_CM - jarak) / TINGGI_WADAH_CM) * 100.0;
  return constrain(level, 0.0, 100.0);
}

String statusPakan(float level) {
  if (level < 0) return "ERROR";
  if (level <= BATAS_PAKAN_RENDAH) return "LOW";
  return "NORMAL";
}

// =====================================================
// SENSOR WATER LEVEL ANALOG
// Pastikan tegangan output sensor ke GPIO34 tidak melebihi 3.3V.
// =====================================================
int bacaWaterRaw() {
  uint32_t total = 0;
  const int JUMLAH_SAMPLE = 10;

  for (int i = 0; i < JUMLAH_SAMPLE; i++) {
    total += analogRead(WATER_SENSOR_PIN);
    delay(2);
  }

  return total / JUMLAH_SAMPLE;
}

float hitungLevelAir(int raw) {
  if (WATER_DRY_RAW == WATER_FULL_RAW) return -1.0;

  float level = ((float)(raw - WATER_DRY_RAW) /
                 (float)(WATER_FULL_RAW - WATER_DRY_RAW)) * 100.0;
  return constrain(level, 0.0, 100.0);
}

String statusAir(float level) {
  if (level < 0) return "CALIBRATION_ERROR";
  if (level <= BATAS_AIR_RENDAH) return "LOW";
  return "NORMAL";
}

// =====================================================
// KONTROL DAN PUBLIKASI STATUS PERANGKAT
// =====================================================
void kontrolPerangkat() {
  // Jika MQTT terputus, pompa dan konveyor dipaksa OFF.
  if (!mqtt.connected()) {
    mqttPumpOn = false;
    mqttConveyorOn = false;
  }

  terapkanStatusPompa();
  terapkanStatusKonveyor();
  publikasikanStatusPerangkat();
}

// =====================================================
// CEK PERGANTIAN TANGGAL DAN RESET JADWAL
// =====================================================
void periksaPergantianTanggal(const DateTime& sekarang) {
  if (sekarang.day() != hariTerakhir ||
      sekarang.month() != bulanTerakhir ||
      sekarang.year() != tahunTerakhir) {

    hariTerakhir = sekarang.day();
    bulanTerakhir = sekarang.month();
    tahunTerakhir = sekarang.year();

    for (int i = 0; i < JUMLAH_JADWAL; i++) {
      jadwalSudahJalan[i] = false;
    }

    Serial.println("Tanggal berubah. Status jadwal direset.");
  }
}

// =====================================================
// PEMERIKSAAN JADWAL PAKAN
// Jadwal hanya mengirim notifikasi, bukan mengaktifkan perangkat.
// =====================================================
void periksaJadwal(const DateTime& sekarang) {
  for (int i = 0; i < JUMLAH_JADWAL; i++) {
    if (sekarang.hour() == jadwalJam[i] &&
        sekarang.minute() == jadwalMenit[i] &&
        !jadwalSudahJalan[i]) {

      char pesanJadwal[100];
      snprintf(
        pesanJadwal,
        sizeof(pesanJadwal),
        "JADWAL_PAKAN|%02d:%02d",
        jadwalJam[i],
        jadwalMenit[i]
      );

      Serial.println(">>> JADWAL PAKAN TERDETEKSI <<<");

      if (mqtt.connected()) {
        mqtt.publish(TOPIC_SCHEDULE, pesanJadwal);
        mqtt.publish(TOPIC_RTC_LOG, pesanJadwal);
        jadwalSudahJalan[i] = true;
      } else {
        Serial.println("MQTT offline; notifikasi jadwal belum terkirim.");
      }
    }
  }
}

// =====================================================
// KIRIM DATA RTC, LEVEL PAKAN, DAN LEVEL AIR
// =====================================================
void kirimData(float jarak, float levelPakan, const String& kondisiPakan,
               int waterRaw, float levelAir, const String& kondisiAir,
               const DateTime& sekarang) {
  if (!mqtt.connected()) return;

  char tanggal[16];
  char waktu[16];
  char payload[420];
  char payloadAir[180];

  snprintf(
    tanggal, sizeof(tanggal),
    "%02d/%02d/%04d",
    sekarang.day(), sekarang.month(), sekarang.year()
  );

  snprintf(
    waktu, sizeof(waktu),
    "%02d:%02d:%02d",
    sekarang.hour(), sekarang.minute(), sekarang.second()
  );

  if (levelPakan >= 0) {
    snprintf(
      payload, sizeof(payload),
      "{\"tanggal\":\"%s\","
      "\"waktu\":\"%s\","
      "\"jarak_cm\":%.2f,"
      "\"feed_level\":%.1f,"
      "\"feed_status\":\"%s\","
      "\"water_raw\":%d,"
      "\"water_level\":%.1f,"
      "\"water_status\":\"%s\","
      "\"pump_status\":\"%s\","
      "\"conveyor_status\":\"%s\","
      "\"conveyor_speed\":%d}",
      tanggal, waktu, jarak, levelPakan, kondisiPakan.c_str(),
      waterRaw, levelAir, kondisiAir.c_str(),
      mqttPumpOn ? "ON" : "OFF",
      mqttConveyorOn ? "ON" : "OFF",
      conveyorSpeed
    );
  } else {
    snprintf(
      payload, sizeof(payload),
      "{\"tanggal\":\"%s\","
      "\"waktu\":\"%s\","
      "\"jarak_cm\":null,"
      "\"feed_level\":null,"
      "\"feed_status\":\"ERROR\","
      "\"water_raw\":%d,"
      "\"water_level\":%.1f,"
      "\"water_status\":\"%s\","
      "\"pump_status\":\"%s\","
      "\"conveyor_status\":\"%s\","
      "\"conveyor_speed\":%d}",
      tanggal, waktu, waterRaw, levelAir, kondisiAir.c_str(),
      mqttPumpOn ? "ON" : "OFF",
      mqttConveyorOn ? "ON" : "OFF",
      conveyorSpeed
    );
  }

  snprintf(
    payloadAir, sizeof(payloadAir),
    "{\"water_raw\":%d,\"water_level\":%.1f,\"water_status\":\"%s\"}",
    waterRaw, levelAir, kondisiAir.c_str()
  );

  mqtt.publish(TOPIC_RTC_DATA, payload);
  mqtt.publish(TOPIC_FEED_LEVEL, payload);
  mqtt.publish(TOPIC_WATER_LEVEL, payloadAir);

  if (kondisiPakan == "LOW") {
    mqtt.publish(TOPIC_RTC_LOG, "PERINGATAN: LEVEL PAKAN RENDAH");
  }
  if (kondisiAir == "LOW") {
    mqtt.publish(TOPIC_RTC_LOG, "PERINGATAN: LEVEL AIR RENDAH");
  }

  Serial.println("-------------------------");
  Serial.print("Tanggal          : ");
  Serial.println(tanggal);
  Serial.print("Waktu            : ");
  Serial.println(waktu);

  Serial.print("Jarak pakan      : ");
  if (levelPakan >= 0) {
    Serial.print(jarak, 2);
    Serial.println(" cm");
  } else {
    Serial.println("Tidak terbaca");
  }

  Serial.print("Level pakan      : ");
  if (levelPakan >= 0) {
    Serial.print(levelPakan, 1);
    Serial.println("%");
  } else {
    Serial.println("ERROR");
  }

  Serial.print("Water raw        : ");
  Serial.println(waterRaw);
  Serial.print("Level air        : ");
  if (levelAir >= 0) {
    Serial.print(levelAir, 1);
    Serial.println("%");
  } else {
    Serial.println("Belum dikalibrasi");
  }

  Serial.print("Status pompa     : ");
  Serial.println(mqttPumpOn ? "ON" : "OFF");
  Serial.print("Status konveyor  : ");
  Serial.println(mqttConveyorOn ? "ON" : "OFF");
  Serial.print("Speed konveyor   : ");
  Serial.println(conveyorSpeed);
  Serial.println("-------------------------");
}

// =====================================================
// SETUP
// =====================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  Wire.begin(21, 22);

  // Pompa relay dan LED.
  pinMode(PUMP_RELAY_PIN, OUTPUT);
  pinMode(LED_MOTOR1_PIN, OUTPUT);
  pinMode(LED_MOTOR2_PIN, OUTPUT);

  // Tetapkan kondisi OFF sedini mungkin.
  mqttPumpOn = false;
  terapkanStatusPompa();

  // L298N.
  pinMode(CONVEYOR_IN1_PIN, OUTPUT);
  pinMode(CONVEYOR_IN2_PIN, OUTPUT);
  digitalWrite(CONVEYOR_IN1_PIN, LOW);
  digitalWrite(CONVEYOR_IN2_PIN, LOW);

  if (!ledcAttach(CONVEYOR_ENA_PIN, CONVEYOR_PWM_FREQ, CONVEYOR_PWM_RESOLUTION)) {
    Serial.println("ERROR: Inisialisasi PWM L298N gagal.");
  }
  ledcWrite(CONVEYOR_ENA_PIN, 0);

  // Sensor.
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);

  analogReadResolution(12);
  analogSetPinAttenuation(WATER_SENSOR_PIN, ADC_11db);

  Serial.println();
  Serial.println("=== SMART FEEDING SYSTEM ===");

  // Inisialisasi RTC.
  if (!rtc.begin()) {
    Serial.println("ERROR: RTC DS3231 tidak ditemukan. Periksa SDA, SCL, dan daya.");
    while (true) {
      matikanSemuaPerangkat();
      delay(1000);
    }
  }

  if (rtc.lostPower()) {
    Serial.println("PERINGATAN: RTC kehilangan daya atau waktu belum valid.");
    Serial.println("Waktu akan dicoba diperbaiki melalui NTP jika Wi-Fi tersedia.");
  } else {
    Serial.println("RTC ditemukan. Waktu tersimpan akan digunakan jika NTP gagal.");
  }

  mulaiKoneksiWiFi();
  wifiSebelumnyaTerhubung = tungguWiFi(15000);

  if (wifiSebelumnyaTerhubung) {
    sinkronisasiRTC();
  }

  DateTime sekarang = rtc.now();
  hariTerakhir = sekarang.day();
  bulanTerakhir = sekarang.month();
  tahunTerakhir = sekarang.year();

  Serial.printf(
    "Waktu RTC: %02d/%02d/%04d %02d:%02d:%02d\n",
    sekarang.day(), sekarang.month(), sekarang.year(),
    sekarang.hour(), sekarang.minute(), sekarang.second()
  );

  inisialisasiMQTT();

  Serial.println("Jadwal notifikasi: 06:00, 12:00, 15:15, 18:00");
  Serial.println("Pompa: relay GPIO19.");
  Serial.println("Konveyor: L298N ENA=25, IN1=32, IN2=33.");
  Serial.println("Kontrol pompa dan konveyor melalui website MQTT.");
  Serial.println("Status awal pompa dan konveyor: OFF.");
  Serial.println("Perhatian: kalibrasikan WATER_DRY_RAW dan WATER_FULL_RAW.");
}

// =====================================================
// LOOP
// =====================================================
void loop() {
  // Sambungkan kembali Wi-Fi secara berkala.
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiSebelumnyaTerhubung) {
      Serial.println("Wi-Fi terputus. Pompa dan konveyor dimatikan demi keamanan.");
      wifiSebelumnyaTerhubung = false;
      matikanSemuaPerangkat();
      mqtt.disconnect();
    }

    if (millis() - waktuCobaWiFiTerakhir >= INTERVAL_COBA_WIFI) {
      mulaiKoneksiWiFi();
    }
  } else if (!wifiSebelumnyaTerhubung) {
    wifiSebelumnyaTerhubung = true;
    Serial.println("Wi-Fi tersambung kembali.");
    sinkronisasiRTC();
    perbaruiTanggalTerakhir();
  }

  // Koneksi MQTT dan pemrosesan pesan.
  if (WiFi.status() == WL_CONNECTED) {
    cobaKoneksiMQTT();
    if (mqtt.connected()) {
      mqtt.loop();
    }
  }

  kontrolPerangkat();

  DateTime sekarang = rtc.now();
  periksaPergantianTanggal(sekarang);
  periksaJadwal(sekarang);

  // Baca sensor dan kirim data setiap satu detik.
  if (millis() - waktuKirimTerakhir >= INTERVAL_KIRIM) {
    waktuKirimTerakhir = millis();

    float jarak = bacaJarak();
    float levelPakan = hitungLevelPakan(jarak);
    String kondisiPakan = statusPakan(levelPakan);

    int waterRaw = bacaWaterRaw();
    float levelAir = hitungLevelAir(waterRaw);
    String kondisiAir = statusAir(levelAir);

    kirimData(
      jarak, levelPakan, kondisiPakan,
      waterRaw, levelAir, kondisiAir,
      sekarang
    );
  }

  delay(20);
}
