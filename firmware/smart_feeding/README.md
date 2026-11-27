# Firmware ESP32 Smart Feeding & Water Station (DEV_003)

Folder ini berisi firmware Arduino / ESP32 untuk subsistem **Kelompok 3: Smart Feeding & Water Level** pada project Smart Farm MES.

## 📁 Berkas Firmware

1. **`SmartFeeding_DEV_003_Integrated.ino` (Direkomendasikan)**:
   - Firmware yang sudah diintegrasikan penuh dengan standar Smart Farm MES (Envelope MQTT 1.0.0).
   - Mendukung kontrol langsung dari Web Dashboard (`ACT_PUMP_003` dan `ACT_FEEDER_MOTOR_003`).
   - Mengirim status perangkat `ONLINE`/`READY` dan telemetri terstruktur (`SNS_ULTRASONIC_003`, `SNS_WATER_LEVEL_003`).
   - Menyediakan kompatibilitas ganda (*Dual-Mode*) dengan topik lama `smartfeeding/*`.
   - Menggunakan fail-safe otomatis (mematikan aktuator jika koneksi terputus).

2. **`SmartFeeding_ESP32_RelayPump_L298N_Conveyor_WaterLevel.ino`**:
   - Salinan kode asli yang menggunakan topik mandiri `smartfeeding/*`.

---

## 🔌 Skema Pinout ESP32 & Sambungan Hardware

| Komponen | Pin Modul | GPIO ESP32 | Keterangan |
|---|---|---|---|
| **Relay Pompa Air** | IN / Signal | **GPIO 19** | Aktif LOW (Relay 5V) |
| **LED Indikator Pompa 1** | Anoda (+) | **GPIO 15** | Mengikuti status relay |
| **LED Indikator Pompa 2** | Anoda (+) | **GPIO 2** | Mengikuti status relay / onboard LED |
| **Driver L298N Motor** | ENA | **GPIO 25** | PWM Kecepatan Motor (0 - 255) |
| **Driver L298N Motor** | IN1 | **GPIO 32** | Arah Putaran Motor |
| **Driver L298N Motor** | IN2 | **GPIO 33** | Arah Putaran Motor |
| **Sensor Ultrasonik (HC-SR04)** | TRIG | **GPIO 26** | Trigger pulse jarak pakan |
| **Sensor Ultrasonik (HC-SR04)** | ECHO | **GPIO 27** | Echo pulse (Gunakan pembagi tegangan jika 5V) |
| **Sensor Analog Water Level** | Signal (S) | **GPIO 34** | ADC Analog (Tegangan maks 3.3V) |
| **RTC DS3231 (I2C)** | SDA | **GPIO 21** | Komunikasi I2C |
| **RTC DS3231 (I2C)** | SCL | **GPIO 22** | Komunikasi I2C |

---

## ⚙️ Persiapan & Konfigurasi

Sebelum mengunggah kode ke ESP32 menggunakan **Arduino IDE**:

1. **Library yang Diperlukan**:
   - `WiFi.h` (Bawaan ESP32 Board Core)
   - `PubSubClient` oleh Nick O'Leary
   - `Wire.h` (Bawaan)
   - `RTClib` oleh Adafruit

2. **Pengaturan Wi-Fi & MQTT**:
   Buka file `SmartFeeding_DEV_003_Integrated.ino`, lalu sesuaikan baris berikut:
   ```cpp
   const char* WIFI_SSID     = "NAMA_WIFI_ANDA";
   const char* WIFI_PASSWORD = "PASSWORD_WIFI_ANDA";

   // Ganti dengan IP komputer/laptop yang menjalankan backend Smart Farm,
   // atau gunakan broker cloud publik seperti "broker.emqx.io"
   const char* MQTT_SERVER   = "192.168.1.50"; 
   const int   MQTT_PORT     = 1883;
   ```

3. **Kalibrasi Sensor**:
   - `WATER_DRY_RAW` & `WATER_FULL_RAW`: Kalibrasi nilai pembacaan sensor air saat kering dan saat basah.
   - `TINGGI_WADAH_CM`: Ukur jarak sensor ultrasonik ke dasar wadah pakan Anda (default: 30.0 cm).

---

## 🚀 Pengujian Integrasi dengan Web Dashboard

1. Jalankan backend Smart Farm:
   ```powershell
   cd backend
   .\.venv\Scripts\Activate.ps1
   python app.py
   ```
2. Buka Web Dashboard di browser: `http://localhost:5000`
3. Masuk ke tab **Smart Feeding**.
4. Saat ESP32 menyala dan terhubung ke Wi-Fi & MQTT broker:
   - Data level pakan, jarak, dan ketinggian air akan tampil secara live.
   - Tombol **PUMP 1 ON / OFF** akan menggerakkan motor konveyor L298N.
   - Tombol **PUMP 2 ON / OFF** akan menyalakan/mematikan relay pompa air minum.
