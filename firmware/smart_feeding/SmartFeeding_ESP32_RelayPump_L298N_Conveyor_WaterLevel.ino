#include <WiFi.h>
#include <PubSubClient.h>

// =====================================================
// KONFIGURASI WIFI
// =====================================================
const char* WIFI_SSID = "Samsung A17";
const char* WIFI_PASSWORD = "Fyreablaze";

// =====================================================
// KONFIGURASI MQTT
// =====================================================
const char* MQTT_SERVER = "broker.emqx.io";
const int MQTT_PORT = 1883;

// Topic RTC dari kelompok lain (Subscribe)
const char* TOPIC_RTC_SUBSCRIBE = "kelompoklain/rtc/data";

// Topic sistem Smart Feeding & Water (Publish / Subscribe)
const char* TOPIC_DATA_SISTEM   = "smartfeeding/rtc/data";
const char* TOPIC_RTC_LOG       = "smartfeeding/rtc/log";
const char* TOPIC_PUMP_STATUS   = "smartfeeding/pump/status";
const char* TOPIC_PUMP_COMMAND  = "smartfeeding/pump/command";
const char* TOPIC_MOTOR_STATUS  = "smartfeeding/conveyor/status";
const char* TOPIC_MOTOR_COMMAND = "smartfeeding/conveyor/command";
const char* TOPIC_SCHEDULE      = "smartfeeding/rtc/schedule";
const char* TOPIC_FEED_LEVEL    = "smartfeeding/feed/level";
const char* TOPIC_WATER_LEVEL   = "smartfeeding/water/level";

// =====================================================
// PIN ESP32 (POMPA AIR PIN 19, MOTOR PIN 18)
// =====================================================
#define PUMP_PIN         19
#define MOTOR_PIN        18
#define LED_MOTOR1_PIN   15
#define LED_MOTOR2_PIN   2

// HC-SR04 Silo Pakan (Pin tetap 26 & 27)
#define TRIG_SILO_PIN    26
#define ECHO_SILO_PIN    27

// HC-SR04 Tangki Air / Water
#define TRIG_WATER_PIN   32
#define ECHO_WATER_PIN   33

// Relay aktif LOW
const uint8_t RELAY_ON = LOW;
const uint8_t RELAY_OFF = HIGH;

WiFiClient espClient;
PubSubClient mqtt(espClient);

// =====================================================
// STRUKTUR PENYIMPANAN WAKTU DARI MQTT
// =====================================================
struct WaktuMQTT {
  int hari = 0;
  int bulan = 0;
  int tahun = 0;
  int jam = 0;
  int menit = 0;
  int detik = 0;
  bool valid = false;
};

WaktuMQTT waktuSekarang;

// =====================================================
// PENGATURAN DIMENSI SILO PAKAN & TANGKI AIR (CM)
// Sesuaikan tinggi wadah dengan ukuran fisik sebenarnya.
// =====================================================
const float TINGGI_SILO_CM        = 30.0;
const float BATAS_PAKAN_RENDAH    = 20.0; // persen (%)

const float TINGGI_TANGKI_AIR_CM  = 30.0;
const float BATAS_AIR_RENDAH      = 20.0; // persen (%)

// =====================================================
// JADWAL PAKAN
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
bool statusPumpSebelumnya = false;
bool mqttMotorOn = false;
bool statusMotorSebelumnya = false;
bool wifiSebelumnyaTerhubung = false;

unsigned long waktuKirimTerakhir = 0;
const unsigned long INTERVAL_KIRIM = 1000;

unsigned long waktuCobaWiFiTerakhir = 0;
const unsigned long INTERVAL_COBA_WIFI = 10000;

unsigned long waktuCobaMQTTTerakhir = 0;
const unsigned long INTERVAL_COBA_MQTT = 5000;

// =====================================================
// FUNGSI KONTROL OUTPUT POMPA AIR & MOTOR
// =====================================================
void terapkanStatusPompa() {
  digitalWrite(PUMP_PIN, mqttPumpOn ? RELAY_ON : RELAY_OFF);
  digitalWrite(MOTOR_PIN, mqttMotorOn ? RELAY_ON : RELAY_OFF);

  digitalWrite(LED_MOTOR1_PIN, mqttPumpOn ? HIGH : LOW);
  digitalWrite(LED_MOTOR2_PIN, mqttMotorOn ? HIGH : LOW);
}

void matikanPompa() {
  mqttPumpOn = false;
  mqttMotorOn = false;
  terapkanStatusPompa();
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
  Serial.println("Wi-Fi belum terhubung. Menunggu koneksi ulang...");
  return false;
}

// =====================================================
// PARSING WAKTU DARI PAYLOAD MQTT KELOMPOK LAIN
// =====================================================
void parsingWaktuRTC(String payload) {
  int h = 0, b = 0, t = 0, jm = 0, mn = 0, dt = 0;
  bool berhasil = false;

  int idxTgl = payload.indexOf("\"tanggal\"");
  int idxWkt = payload.indexOf("\"waktu\"");

  if (idxTgl != -1 && idxWkt != -1) {
    int mulaiTgl = payload.indexOf(":", idxTgl) + 1;
    int akhirTgl = payload.indexOf(",", mulaiTgl);
    if (akhirTgl == -1) akhirTgl = payload.indexOf("}", mulaiTgl);
    String strTgl = payload.substring(mulaiTgl, akhirTgl);
    strTgl.replace("\"", "");
    strTgl.trim();

    int mulaiWkt = payload.indexOf(":", idxWkt) + 1;
    int akhirWkt = payload.indexOf(",", mulaiWkt);
    if (akhirWkt == -1) akhirWkt = payload.indexOf("}", mulaiWkt);
    String strWkt = payload.substring(mulaiWkt, akhirWkt);
    strWkt.replace("\"", "");
    strWkt.trim();

    if (sscanf(strTgl.c_str(), "%d/%d/%d", &h, &b, &t) == 3 ||
        sscanf(strTgl.c_str(), "%d-%d-%d", &t, &b, &h) == 3) {
      if (sscanf(strWkt.c_str(), "%d:%d:%d", &jm, &mn, &dt) >= 2) {
        berhasil = true;
      }
    }
  } 
  else if (sscanf(payload.c_str(), "%d/%d/%d %d:%d:%d", &h, &b, &t, &jm, &mn, &dt) >= 5) {
    berhasil = true;
  }
  else if (sscanf(payload.c_str(), "%d-%d-%d %d:%d:%d", &t, &b, &h, &jm, &mn, &dt) >= 5) {
    berhasil = true;
  }

  if (berhasil) {
    waktuSekarang.hari = h;
    waktuSekarang.bulan = b;
    waktuSekarang.tahun = t;
    waktuSekarang.jam = jm;
    waktuSekarang.menit = mn;
    waktuSekarang.detik = dt;
    waktuSekarang.valid = true;
  } else {
    Serial.println("Format waktu RTC dari MQTT tidak dikenali!");
  }
}

// =====================================================
// MQTT CALLBACK
// =====================================================
void callback(char* topic, byte* payload, unsigned int length) {
  String pesan;
  pesan.reserve(length);

  for (unsigned int i = 0; i < length; i++) {
    pesan += (char)payload[i];
  }

  pesan.trim();

  Serial.print("MQTT diterima [");
  Serial.print(topic);
  Serial.print("]: ");
  Serial.println(pesan);

  if (strcmp(topic, TOPIC_RTC_SUBSCRIBE) == 0) {
    parsingWaktuRTC(pesan);
    return;
  }

  if (strcmp(topic, TOPIC_PUMP_COMMAND) == 0) {
    pesan.toUpperCase();

    if (pesan == "ON" || pesan == "PUMP ON" || pesan == "PUMP_ON") {
      mqttPumpOn = true;
      Serial.println("Perintah website: POMPA AIR ON (GPIO 19)");
    }
    else if (pesan == "OFF" || pesan == "PUMP OFF" || pesan == "PUMP_OFF") {
      mqttPumpOn = false;
      Serial.println("Perintah website: POMPA AIR OFF (GPIO 19)");
    }
    else {
      Serial.println("Perintah pompa tidak dikenali.");
    }

    terapkanStatusPompa();
    return;
  }

  if (strcmp(topic, TOPIC_MOTOR_COMMAND) == 0) {
    pesan.toUpperCase();

    if (pesan == "ON" || pesan == "MOTOR ON" || pesan == "MOTOR_ON") {
      mqttMotorOn = true;
      Serial.println("Perintah website: MOTOR ON (GPIO 18)");
    }
    else if (pesan == "OFF" || pesan == "MOTOR OFF" || pesan == "MOTOR_OFF") {
      mqttMotorOn = false;
      Serial.println("Perintah website: MOTOR OFF (GPIO 18)");
    }
    else {
      Serial.println("Perintah motor tidak dikenali.");
    }

    terapkanStatusPompa();
  }
}

// =====================================================
// INISIALISASI & KONEKSI MQTT
// =====================================================
void inisialisasiMQTT() {
  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(callback);
  mqtt.setBufferSize(768);
  mqtt.setKeepAlive(30);
}

void cobaKoneksiMQTT() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) {
    return;
  }

  if (millis() - waktuCobaMQTTTerakhir < INTERVAL_COBA_MQTT) {
    return;
  }

  waktuCobaMQTTTerakhir = millis();

  Serial.print("Mencoba menghubungkan MQTT... ");

  String clientId = "SmartFeedingWater-";
  clientId += String((uint32_t)ESP.getEfuseMac(), HEX);

  if (mqtt.connect(clientId.c_str())) {
    Serial.println("Terhubung!");

    mqtt.subscribe(TOPIC_PUMP_COMMAND);
    mqtt.subscribe(TOPIC_MOTOR_COMMAND);
    mqtt.subscribe(TOPIC_RTC_SUBSCRIBE);

    Serial.print("Subscribed ke RTC: ");
    Serial.println(TOPIC_RTC_SUBSCRIBE);

    matikanPompa();
    statusPumpSebelumnya = false;
    statusMotorSebelumnya = false;

    mqtt.publish(TOPIC_PUMP_STATUS, "OFF", true);
    mqtt.publish(TOPIC_MOTOR_STATUS, "OFF", true);
  } else {
    Serial.print("Gagal, kode MQTT: ");
    Serial.println(mqtt.state());
  }
}

// =====================================================
// BACA SENSOR HC-SR04 (UMUM UNTUK SILO PAKAN & AIR)
// =====================================================
float bacaJarakUltrasonik(uint8_t trigPin, uint8_t echoPin) {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(3);

  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  unsigned long durasi = pulseIn(echoPin, HIGH, 30000UL);

  if (durasi == 0) {
    return -1.0;
  }

  float jarak = durasi * 0.0343 / 2.0;

  if (jarak < 2.0 || jarak > 400.0) {
    return -1.0;
  }

  return jarak;
}

float hitungPersentaseLevel(float jarak, float tinggiWadah) {
  if (jarak < 0 || tinggiWadah <= 0) {
    return -1.0;
  }

  float level = ((tinggiWadah - jarak) / tinggiWadah) * 100.0;
  return constrain(level, 0.0, 100.0);
}

String evaluasiStatusLevel(float level, float batasRendah) {
  if (level < 0) {
    return "ERROR";
  }

  if (level <= batasRendah) {
    return "LOW";
  }

  return "NORMAL";
}

// =====================================================
// KONTROL DAN PUBLIKASI STATUS POMPA
// =====================================================
void kontrolPompa() {
  if (!mqtt.connected()) {
    mqttPumpOn = false;
    mqttMotorOn = false;
  }

  terapkanStatusPompa();

  if (mqtt.connected() && mqttPumpOn != statusPumpSebelumnya) {
    mqtt.publish(TOPIC_PUMP_STATUS, mqttPumpOn ? "ON" : "OFF", true);
    statusPumpSebelumnya = mqttPumpOn;

    Serial.print("Status pompa air: ");
    Serial.println(mqttPumpOn ? "ON" : "OFF");
  }

  if (mqtt.connected() && mqttMotorOn != statusMotorSebelumnya) {
    mqtt.publish(TOPIC_MOTOR_STATUS, mqttMotorOn ? "ON" : "OFF", true);
    statusMotorSebelumnya = mqttMotorOn;

    Serial.print("Status motor: ");
    Serial.println(mqttMotorOn ? "ON" : "OFF");
  }

  if (!mqttPumpOn) {
    statusPumpSebelumnya = false;
  }
  if (!mqttMotorOn) {
    statusMotorSebelumnya = false;
  }
}

// =====================================================
// CEK PERUBAHAN TANGGAL & JADWAL PAKAN
// =====================================================
void periksaPergantianTanggal(const WaktuMQTT& sekarang) {
  if (!sekarang.valid) return;

  if (sekarang.hari != hariTerakhir ||
      sekarang.bulan != bulanTerakhir ||
      sekarang.tahun != tahunTerakhir) {

    hariTerakhir = sekarang.hari;
    bulanTerakhir = sekarang.bulan;
    tahunTerakhir = sekarang.tahun;

    for (int i = 0; i < JUMLAH_JADWAL; i++) {
      jadwalSudahJalan[i] = false;
    }

    Serial.println("Tanggal berubah. Status jadwal direset.");
  }
}

void periksaJadwal(const WaktuMQTT& sekarang) {
  if (!sekarang.valid) return;

  for (int i = 0; i < JUMLAH_JADWAL; i++) {
    if (sekarang.jam == jadwalJam[i] &&
        sekarang.menit == jadwalMenit[i] &&
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
// KIRIM DATA SILO PAKAN & AIR KE MQTT
// =====================================================
void kirimData(float jarakSilo, float levelSilo, const String& statusSilo,
               float jarakWater, float levelWater, const String& statusWater,
               const WaktuMQTT& sekarang) {
  if (!mqtt.connected()) {
    return;
  }

  char tanggal[16];
  char waktu[16];

  if (sekarang.valid) {
    snprintf(tanggal, sizeof(tanggal), "%02d/%02d/%04d", sekarang.hari, sekarang.bulan, sekarang.tahun);
    snprintf(waktu, sizeof(waktu), "%02d:%02d:%02d", sekarang.jam, sekarang.menit, sekarang.detik);
  } else {
    snprintf(tanggal, sizeof(tanggal), "MENUNGGU_RTC");
    snprintf(waktu, sizeof(waktu), "00:00:00");
  }

  // Format angka atau null untuk JSON
  String strJarakSilo  = (levelSilo >= 0)  ? String(jarakSilo, 2)  : "null";
  String strLevelSilo  = (levelSilo >= 0)  ? String(levelSilo, 1)  : "null";
  String strJarakWater = (levelWater >= 0) ? String(jarakWater, 2) : "null";
  String strLevelWater = (levelWater >= 0) ? String(levelWater, 1) : "null";

  // 1. Payload khusus Silo Pakan (kompatibel dengan format sebelumnya)
  char payloadFeed[256];
  snprintf(
    payloadFeed, sizeof(payloadFeed),
    "{\"tanggal\":\"%s\",\"waktu\":\"%s\",\"jarak_cm\":%s,\"feed_level\":%s,\"feed_status\":\"%s\",\"pump_status\":\"%s\"}",
    tanggal, waktu,
    strJarakSilo.c_str(), strLevelSilo.c_str(), statusSilo.c_str(),
    mqttPumpOn ? "ON" : "OFF"
  );
  mqtt.publish(TOPIC_FEED_LEVEL, payloadFeed);

  // 2. Payload khusus Tangki Air (Water)
  char payloadWater[256];
  snprintf(
    payloadWater, sizeof(payloadWater),
    "{\"tanggal\":\"%s\",\"waktu\":\"%s\",\"jarak_water_cm\":%s,\"water_level\":%s,\"water_status\":\"%s\",\"pump_status\":\"%s\"}",
    tanggal, waktu,
    strJarakWater.c_str(), strLevelWater.c_str(), statusWater.c_str(),
    mqttPumpOn ? "ON" : "OFF"
  );
  mqtt.publish(TOPIC_WATER_LEVEL, payloadWater);

  // 3. Payload gabungan keseluruhan sistem
  char payloadGabungan[450];
  snprintf(
    payloadGabungan, sizeof(payloadGabungan),
    "{\"tanggal\":\"%s\",\"waktu\":\"%s\","
    "\"silo_jarak_cm\":%s,\"feed_level\":%s,\"feed_status\":\"%s\","
    "\"water_jarak_cm\":%s,\"water_level\":%s,\"water_status\":\"%s\","
    "\"pump_status\":\"%s\"}",
    tanggal, waktu,
    strJarakSilo.c_str(), strLevelSilo.c_str(), statusSilo.c_str(),
    strJarakWater.c_str(), strLevelWater.c_str(), statusWater.c_str(),
    mqttPumpOn ? "ON" : "OFF"
  );
  mqtt.publish(TOPIC_DATA_SISTEM, payloadGabungan);

  // Peringatan log jika salah satu rendah
  if (statusSilo == "LOW") {
    mqtt.publish(TOPIC_RTC_LOG, "PERINGATAN: LEVEL SILO PAKAN RENDAH");
  }
  if (statusWater == "LOW") {
    mqtt.publish(TOPIC_RTC_LOG, "PERINGATAN: LEVEL AIR RENDAH");
  }

  // Output Serial Monitor
  Serial.println("=================================");
  Serial.printf("Waktu (MQTT)  : %s %s\n", tanggal, waktu);
  Serial.printf("Silo Pakan    : %s cm | %s%% (%s)\n", strJarakSilo.c_str(), strLevelSilo.c_str(), statusSilo.c_str());
  Serial.printf("Tangki Air    : %s cm | %s%% (%s)\n", strJarakWater.c_str(), strLevelWater.c_str(), statusWater.c_str());
  Serial.printf("Status Pompa  : %s\n", mqttPumpOn ? "ON" : "OFF");
  Serial.println("=================================");
}

// =====================================================
// SETUP
// =====================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  // Pin Pompa Air (19), Motor (18) & Indikator
  pinMode(PUMP_PIN, OUTPUT);
  pinMode(MOTOR_PIN, OUTPUT);
  pinMode(LED_MOTOR1_PIN, OUTPUT);
  pinMode(LED_MOTOR2_PIN, OUTPUT);

  mqttPumpOn = false;
  mqttMotorOn = false;
  terapkanStatusPompa();

  // Pin HC-SR04 Silo Pakan (Tetap 26 & 27)
  pinMode(TRIG_SILO_PIN, OUTPUT);
  pinMode(ECHO_SILO_PIN, INPUT);
  digitalWrite(TRIG_SILO_PIN, LOW);

  // Pin HC-SR04 Water (32 & 33)
  pinMode(TRIG_WATER_PIN, OUTPUT);
  pinMode(ECHO_WATER_PIN, INPUT);
  digitalWrite(TRIG_WATER_PIN, LOW);

  Serial.println();
  Serial.println("=== SMART FEEDING (SILO) & WATER SYSTEM ===");

  mulaiKoneksiWiFi();
  wifiSebelumnyaTerhubung = tungguWiFi(15000);

  inisialisasiMQTT();
}

// =====================================================
// LOOP
// =====================================================
void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiSebelumnyaTerhubung) {
      Serial.println("Wi-Fi terputus. Pompa dimatikan demi keamanan.");
      wifiSebelumnyaTerhubung = false;
      matikanPompa();
      mqtt.disconnect();
    }

    if (millis() - waktuCobaWiFiTerakhir >= INTERVAL_COBA_WIFI) {
      mulaiKoneksiWiFi();
    }
  } else if (!wifiSebelumnyaTerhubung) {
    wifiSebelumnyaTerhubung = true;
    Serial.println("Wi-Fi tersambung kembali.");
  }

  if (WiFi.status() == WL_CONNECTED) {
    cobaKoneksiMQTT();

    if (mqtt.connected()) {
      mqtt.loop();
    }
  }

  kontrolPompa();

  periksaPergantianTanggal(waktuSekarang);
  periksaJadwal(waktuSekarang);

  // Baca sensor Silo Pakan dan Water setiap 1 detik
  if (millis() - waktuKirimTerakhir >= INTERVAL_KIRIM) {
    waktuKirimTerakhir = millis();

    // 1. Baca HC-SR04 Silo Pakan (jeda singkat 30ms agar gelombang ultrasonik tidak saling bertabrakan)
    float jarakSilo = bacaJarakUltrasonik(TRIG_SILO_PIN, ECHO_SILO_PIN);
    float levelSilo = hitungPersentaseLevel(jarakSilo, TINGGI_SILO_CM);
    String statusSilo = evaluasiStatusLevel(levelSilo, BATAS_PAKAN_RENDAH);

    delay(30);

    // 2. Baca HC-SR04 Water
    float jarakWater = bacaJarakUltrasonik(TRIG_WATER_PIN, ECHO_WATER_PIN);
    float levelWater = hitungPersentaseLevel(jarakWater, TINGGI_TANGKI_AIR_CM);
    String statusWater = evaluasiStatusLevel(levelWater, BATAS_AIR_RENDAH);

    kirimData(jarakSilo, levelSilo, statusSilo, jarakWater, levelWater, statusWater, waktuSekarang);
  }

  delay(20);
}
