# Moo-Nitoring Kandang Sapi

## Smart Livestock Environmental Monitoring and Early Warning System

## 1. Deskripsi Proyek

Moo-Nitoring merupakan sistem pemantauan dan analisis kondisi lingkungan
kandang sapi berbasis Internet of Things (IoT). Sistem ini dirancang
untuk membantu peternak menjaga kenyamanan lingkungan ternak serta
mendeteksi potensi risiko sejak dini.

Sistem mengintegrasikan sensor suhu, kelembapan, indikator kualitas
udara, dan suara lingkungan. Data tidak hanya ditampilkan sebagai angka
mentah, tetapi diolah menjadi **Comfort Score**, klasifikasi kondisi
lingkungan, **Comfort Recovery Time**, dan **Predictive Early Warning**.
Sistem juga dapat mengaktifkan kipas, LED, dan buzzer berdasarkan aturan
otomatis.

Moo-Nitoring berfokus pada pemantauan kenyamanan lingkungan ternak,
bukan diagnosis medis atau pengukuran stres sapi secara langsung.

## 2. Komponen yang Digunakan

  -----------------------------------------------------------------------
  Komponen                            Fungsi
  ----------------------------------- -----------------------------------
  ESP32                               Membaca sensor, melakukan
                                      pengolahan awal, komunikasi data,
                                      dan mengendalikan aktuator.

  DHT22                               Mengukur suhu dan kelembapan
                                      relatif kandang.

  INMP441                             Mikrofon digital I2S untuk
                                      menangkap suara dan menganalisis
                                      intensitas suara relatif.

  MQ-135                              Indikator perubahan kualitas udara
                                      berdasarkan respons sensor gas.

  RTC DS3231                          Menyediakan waktu untuk pencatatan
                                      data dan penjadwalan LED.

  Relay                               Mengendalikan kipas berdasarkan
                                      kondisi lingkungan.

  Mini Fan 5V                         Memberikan pendinginan lokal ketika
                                      suhu melewati batas.

  LED                                 Pencahayaan berdasarkan jadwal
                                      waktu.

  Buzzer                              Memberikan peringatan suara.

  Adaptor 5V 3A                       Menyuplai daya sesuai kebutuhan dan
                                      spesifikasi komponen.
  -----------------------------------------------------------------------

### Catatan teknis komponen

-   DHT22 dapat digunakan untuk menghitung Temperature-Humidity Index
    (THI) dengan rumus dan ambang yang sesuai untuk ternak.
-   INMP441 menghasilkan audio digital. Nilai RMS dapat menjadi
    indikator intensitas relatif, bukan dB terkalibrasi tanpa kalibrasi.
-   MQ-135 menghasilkan indikator respons sensor, bukan nilai ppm akurat
    secara langsung. Sensor memerlukan pemanasan, kalibrasi, dan
    validasi.
-   Mini Fan 5V merupakan pendinginan lokal, bukan sistem ventilasi
    untuk seluruh kandang.
-   RTC DS3231 digunakan untuk penjadwalan LED; sistem tidak menggunakan
    sensor cahaya.

## 3. Fitur Utama

### 3.1 Environmental Monitoring

Sistem membaca parameter lingkungan secara berkala: - Suhu udara. -
Kelembapan relatif. - Indikator kualitas udara. - Intensitas suara
lingkungan.

Data dikirim dan disimpan ke database agar peternak dapat melihat
kondisi terkini dan riwayat perubahan lingkungan kandang.

### 3.2 Comfort Score dan Condition Classification

Comfort Score merupakan skor gabungan yang menggambarkan kondisi
kenyamanan lingkungan berdasarkan parameter yang tersedia. Data sensor
diolah menjadi skor dan kategori agar mudah dipahami peternak.

Contoh rancangan klasifikasi awal:

  ------------------------------------------------------------------------
                          Skor Kategori              Interpretasi
  ---------------------------- --------------------- ---------------------
                       80--100 Normal                Kondisi lingkungan
                                                     relatif sesuai
                                                     rentang target.

                        50--79 Perlu Penanganan      Terdapat parameter
                                                     yang mulai menyimpang
                                                     dan perlu
                                                     diperhatikan.

                         0--49 Kritis                Kondisi lingkungan
                                                     menunjukkan risiko
                                                     tinggi dan
                                                     membutuhkan
                                                     pemeriksaan serta
                                                     tindakan segera.
  ------------------------------------------------------------------------

Rentang tersebut merupakan rancangan awal, bukan standar veteriner.
Bobot parameter dan ambang harus ditentukan serta divalidasi berdasarkan
jenis dan umur sapi, kondisi kandang, serta referensi peternakan yang
relevan.

Contoh keluaran:

  Parameter                    Hasil contoh Status
  -------------------------- -------------- ----------------------
  Suhu                                 32°C Perlu penanganan
  Kelembapan                            75% Perlu penanganan
  Indikator kualitas udara        Meningkat Waspada
  Intensitas suara relatif        Meningkat Perlu diperiksa
  **Comfort Score**              **58/100** **Perlu penanganan**

Suhu dan kelembapan dapat diolah menjadi THI. Indikator MQ-135 dan
intensitas suara dapat menjadi informasi pendukung. Comfort Score adalah
indikator kenyamanan lingkungan, bukan diagnosis stres atau kesehatan
sapi.

### 3.3 Automatic Environmental Control

Sistem menjalankan tindakan otomatis berdasarkan kondisi yang
terdeteksi: 1. Sensor mendeteksi suhu melewati batas. 2. ESP32
mengaktifkan relay. 3. Kipas pendingin menyala. 4. Sistem terus memantau
perubahan suhu. 5. Kipas dapat dimatikan ketika kondisi kembali ke
rentang target.

LED dikendalikan berdasarkan jadwal RTC DS3231. Buzzer memberikan
peringatan lokal ketika kondisi tertentu terdeteksi. Batas aktivasi dan
penonaktifan sebaiknya menggunakan histeresis atau jeda agar aktuator
tidak sering hidup-mati akibat fluktuasi kecil.

### 3.4 Comfort Recovery Time

Comfort Recovery Time mengukur waktu yang dibutuhkan parameter
lingkungan untuk kembali ke rentang target setelah tindakan penanganan
dilakukan.

Contoh skenario: 1. Suhu kandang mencapai 33°C dan berada di luar
rentang target. 2. Sistem mengaktifkan kipas. 3. Sensor terus membaca
suhu selama kipas bekerja. 4. Jika suhu kembali ke rentang target dalam
12 menit, sistem mencatat recovery time selama 12 menit. 5. Jika suhu
tidak turun secara memadai setelah batas waktu evaluasi, sistem
memberikan peringatan agar peternak melakukan pemeriksaan lebih lanjut.

Contoh keluaran:

  -----------------------------------------------------------------------
  Indikator                           Hasil
  ----------------------------------- -----------------------------------
  Suhu awal                           33°C

  Tindakan                            Kipas aktif

  Waktu evaluasi                      15 menit

  Suhu akhir                          32,8°C

  Status recovery                     Belum tercapai

  Rekomendasi                         Periksa sirkulasi udara, kipas,
                                      penempatan sensor, dan kondisi
                                      kandang.
  -----------------------------------------------------------------------

Fitur ini mengevaluasi respons lingkungan terhadap tindakan yang
dilakukan. Recovery time mengukur pemulihan parameter lingkungan, bukan
pemulihan fisiologis sapi.

### 3.5 Predictive Analysis dan Early Warning

Sistem menganalisis data historis dan tren perubahan parameter untuk
memperkirakan potensi kondisi lingkungan yang tidak ideal sebelum
mencapai kategori kritis.

Pola yang dapat dianalisis: - Suhu meningkat secara konsisten. -
Kelembapan menunjukkan tren kenaikan. - Indikator kualitas udara terus
memburuk. - Suhu tidak turun secara memadai meskipun kipas aktif.

Contoh keluaran simulasi:

> **Peringatan dini: potensi suhu tinggi meningkat** - Suhu saat ini:
> 30,5°C - Tren: meningkat - Estimasi 15 menit berikutnya: 32°C -
> Rekomendasi: periksa sirkulasi udara dan kesiapan kipas.

Pengembangan dilakukan bertahap: 1. **Rule-Based Early Warning:**
menggunakan ambang batas, tren, dan laju perubahan sensor. 2. **Machine
Learning:** menggunakan data historis untuk mempelajari pola dan
memperkirakan kondisi lingkungan berikutnya.

Prediksi numerik harus dibangun dan dievaluasi menggunakan data kandang
yang memadai. Sebelum model tervalidasi, keluaran sebaiknya disebut
estimasi atau peringatan berbasis tren, bukan prediksi yang pasti.

### 3.6 Historical Data dan Environmental Analytics

Sistem menyimpan riwayat data untuk membantu peternak mengevaluasi
kondisi kandang. Informasi yang dapat ditampilkan: - Grafik perubahan
suhu dan kelembapan. - Riwayat Comfort Score dan klasifikasi kondisi. -
Frekuensi kondisi Normal, Perlu Penanganan, dan Kritis. - Riwayat
aktivasi kipas. - Rata-rata dan distribusi Comfort Recovery Time. -
Riwayat peringatan dini dan hasil prediksi.

Fitur ini membantu mengidentifikasi pola berulang dan mengevaluasi
efektivitas tindakan.

## 4. Alur Kerja Sistem

1.  **Sensing:** DHT22, INMP441, dan MQ-135 membaca parameter
    lingkungan. RTC DS3231 menyediakan informasi waktu.
2.  **Edge Processing:** ESP32 membaca sensor, melakukan pengolahan
    awal, dan menjalankan kontrol aktuator sesuai aturan.
3.  **Data Logging:** data dikirim ke server dan disimpan dalam
    database.
4.  **Environmental Analytics:** sistem menghitung Comfort Score,
    mengklasifikasikan kondisi, dan mengevaluasi Comfort Recovery Time.
5.  **Early Warning:** tren data dianalisis untuk mengidentifikasi
    potensi kondisi lingkungan yang memburuk.
6.  **Action:** kipas, LED, dan buzzer bekerja sesuai aturan kontrol dan
    jadwal.
7.  **Evaluation:** peternak melihat kondisi, peringatan, riwayat, dan
    rekomendasi melalui dashboard.

## 5. Nilai Tambah Moo-Nitoring

  -----------------------------------------------------------------------
  Monitoring konvensional             Moo-Nitoring
  ----------------------------------- -----------------------------------
  Menampilkan angka suhu              Mengolah parameter menjadi Comfort
                                      Score.

  Menampilkan kelembapan              Mengklasifikasikan kondisi
                                      lingkungan.

  Membaca indikator kualitas udara    Menganalisis perubahan indikator
                                      kualitas udara.

  Menyalakan kipas berdasarkan ambang Mengevaluasi respons pendinginan
                                      melalui Recovery Time.

  Menampilkan data saat ini           Menyimpan dan menganalisis data
                                      historis.

  Memberi alarm saat kondisi buruk    Memberikan Early Warning
                                      berdasarkan tren dan, setelah
                                      tervalidasi, model prediksi.
  -----------------------------------------------------------------------

Tiga fitur analitik utama Moo-Nitoring adalah **Comfort Score, Comfort
Recovery Time, dan Predictive Early Warning**. Ketiganya membentuk
siklus: sistem menilai kondisi lingkungan, membantu menentukan tindakan,
mengevaluasi respons tindakan, dan mengidentifikasi potensi risiko
berikutnya.

## 6. Batasan dan Validasi

1.  **Comfort Score bukan diagnosis stres sapi.** Skor menggambarkan
    kenyamanan lingkungan. Pengukuran stres langsung memerlukan
    parameter tambahan seperti suhu tubuh, perilaku, atau indikator
    fisiologis.
2.  **MQ-135 bukan pengukur ppm akurat secara langsung.** Pembacaan
    memerlukan pemanasan, kalibrasi, dan validasi.
3.  **INMP441 menghasilkan data audio digital.** RMS merupakan indikator
    intensitas relatif, bukan dB terkalibrasi tanpa kalibrasi.
4.  **Prediksi membutuhkan data historis.** Kinerja model harus diuji
    menggunakan data representatif sebelum digunakan untuk mendukung
    keputusan.
5.  **Kipas 5V adalah pendinginan lokal.** Efektivitasnya untuk kandang
    secara keseluruhan harus diuji.
6.  **Sensor perlu diperiksa berkala.** Pembacaan tidak wajar, sensor
    rusak, atau koneksi terputus perlu ditandai agar data yang salah
    tidak menghasilkan klasifikasi dan tindakan yang keliru.

## 7. Tahapan Pengembangan

  -----------------------------------------------------------------------
  Tahap                   Pengembangan            Target
  ----------------------- ----------------------- -----------------------
  1                       Environmental           Pembacaan sensor stabil
                          Monitoring              dan pencatatan data
                                                  berjalan.

  2                       Comfort Score           Klasifikasi kondisi
                                                  lingkungan dapat
                                                  dihitung dan
                                                  divalidasi.

  3                       Automatic Control       Kipas dan alarm bekerja
                                                  sesuai aturan.

  4                       Comfort Recovery Time   Respons lingkungan
                                                  setelah tindakan dapat
                                                  dievaluasi.

  5                       Historical Analytics    Riwayat dan tren
                                                  parameter dapat
                                                  dianalisis.

  6                       Predictive Early        Peringatan berbasis
                          Warning                 tren dan model prediksi
                                                  diuji menggunakan data
                                                  historis.
  -----------------------------------------------------------------------

**Arah pengembangan:** Moo-Nitoring tidak berhenti pada pemantauan
kondisi kandang. Sistem diarahkan menjadi perangkat pendukung keputusan
berbasis data yang membantu peternak memahami kondisi lingkungan,
mengevaluasi tindakan, dan mengantisipasi potensi risiko secara lebih
dini.

---

## 8. Panduan Menjalankan Project Moo-nitoring Kandang

Berikut adalah panduan lengkap langkah demi langkah untuk menjalankan project **Moo-nitoring Mikroklimat Kandang (K2)**, mulai dari setup backend, database, machine learning, firmware ESP32, hingga antarmuka web frontend.

### 8.1 Prasyarat Sistem
1. **Perangkat Lunak:**
   - Python 3.10 atau lebih baru (disarankan Python 3.11 / 3.12 / 3.13).
   - Arduino IDE 2.x dengan board support package **ESP32 by Espressif Systems**.
   - Web browser modern (Google Chrome, Microsoft Edge, atau Firefox).
2. **Perangkat Keras:**
   - Node ESP32 Dev Module (30-pin / 38-pin).
   - Sensor Suhu & Kelembapan **DHT22** (GPIO 4).
   - Sensor Gas Kualitas Udara **MQ-135** (GPIO 34 ADC1).
   - Mikrofon Digital I2S **INMP441** (SCK: GPIO 32, WS: GPIO 25, SD: GPIO 33).
   - Modul RTC **DS3231** I2C (SDA: GPIO 21, SCL: GPIO 22).
   - Modul Relay 2-Channel (Relay 1 Kipas: GPIO 26, Relay 2 Lampu: GPIO 27).
   - Buzzer Aktif / Pasif (GPIO 14).
   - Mini Fan 5V DC, Lampu LED Sirkadian 4000K, dan Adaptor Power 5V 3A.

---

### 8.2 Langkah 1: Persiapan Environment & Backend
Buka terminal **PowerShell** di folder project:

```powershell
# 1. Pindah ke direktori backend
cd "c:\Users\lukit\Downloads\POLMAN TERBAIK\Semester 5\MES\smart-farm\backend"

# 2. Buat virtual environment (opsional jika belum dibuat)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install seluruh pustaka dependensi (Flask, Scikit-Learn, Pandas, Joblib, dll)
pip install -r requirements.txt
```

---

### 8.3 Langkah 2: Inisialisasi Database SQLite & Seeding Perangkat
Jalankan perintah Flask CLI untuk membuat tabel database SQLite dan mendaftarkan perangkat `DEV_002` (Moo-nitoring):

```powershell
# Inisialisasi tabel database (smart_farm.db)
python -m flask --app app init-db

# Mendaftarkan device DEV_002 dan 4 sensor kandang
python -m flask --app app seed-dev002
```

> **Hasil yang Diharapkan:** Terminal mengonfirmasi bahwa `DEV_002` berhasil terdaftar beserta sensor `SNS_TEMP_002`, `SNS_HUMIDITY_002`, `SNS_SOUND_002`, dan `SNS_MQ135_002`.

---

### 8.4 Langkah 3: Training Model Random Forest (Opsional)
Jika ingin melatih ulang model Machine Learning menggunakan dataset `backend/model/sensor_data.csv`:

```powershell
python train_kandang_model.py
```
Perintah ini akan membaca data sensor, menghitung THI, melatih algoritma **RandomForestClassifier**, dan mengekspor artefak model ke `backend/model/`:
- `rf_kandang.pkl`
- `scaler_kandang.pkl`
- `label_encoder.pkl`
- `model_metadata.json`

---

### 8.5 Langkah 4: Menjalankan Flask Server Backend
Jalankan server backend Flask dengan mode debug agar hot-reload aktif:

```powershell
python -m flask --app app run --host 0.0.0.0 --port 5000 --debug
```

* Host `0.0.0.0` memastikan server dapat diakses baik dari laptop (`localhost`) maupun dari perangkat ESP32 melalui IP jaringan Wi-Fi lokal.
* **Cek endpoint:** Buka browser dan verifikasi:
  - `http://localhost:5000/health` → Status model ML & data diterima.
  - `http://localhost:5000/api/v1/kandang/status` → Status telemetri kandang terkini.

---

### 8.6 Langkah 5: Konfigurasi & Upload Firmware ESP32
1. Buka Arduino IDE dan buka file sketch:
   `firmware/esp32_moo_nitoring/esp32_moo_nitoring.ino`
2. Atur konfigurasi Wi-Fi dan IP Server pada baris awal:
   ```cpp
   const char* ssid = "NAMA_WIFI_ATAU_HOTSPOT_ANDA";
   const char* password = "PASSWORD_WIFI_ANDA";
   const char* serverIp = "192.168.100.13"; // Ganti dengan IP lokal laptop Anda (cek via 'ipconfig')
   const int serverPort = 5000;
   ```
3. Pastikan Board dipilih: **ESP32 Dev Module**, dan Port COM sesuai perangkat Anda.
4. Klik **Upload**.
5. Buka **Serial Monitor** (Baud Rate: `115200`):
   - ESP32 akan membaca DHT22, MQ-135, INMP441, dan RTC DS3231.
   - ESP32 mengirimkan payload JSON ke `http://<IP-Laptop>:5000/api/sensors` setiap 5 detik.
   - Respons `201 Created` menandakan data sensor dan prediksi ML sukses tersimpan ke database.

---

### 8.7 Langkah 6: Membuka & Mengoperasikan Halaman Web Frontend
Buka browser Anda dan akses salah satu opsi berikut:

#### A. Halaman Khusus Moo-nitoring Kandang (Direkomendasikan)
Akses langsung URL:
**`http://localhost:5000/moo-nitoring`**

Fitur yang tersedia pada halaman ini:
1. **Hero Dial Comfort Score:** Indeks gabungan (0–100) berbasis suhu, kelembapan, gas, dan suara dengan indikator visual otomatis (*Normal*, *Perlu Penanganan*, *Kritis*).
2. **Kontrol Aktuator Cerdas & Otomasi:**
   - Kipas Exhaust (5V) berputar saat suhu mencapai ambang batas atau dinyalakan manual.
   - Lampu LED 4000K menyala otomatis sesuai jadwal sirkadian atau manual.
   - Buzzer peringatan bahaya dengan tombol *Uji Bunyi*.
   - Tombol **"Atur Otomasi"** untuk mengubah batas suhu nyala/mati kipas dan jam sirkadian secara fleksibel dengan validasi rentang wajar.
3. **Manajemen Sanitasi Kandang:**
   - Menampilkan diagnosis kualitas udara (Amonia ppm) dan kelembapan.
   - Tombol **"Catat Pembersihan"** di sudut kanan bawah untuk membuka modal penjadwalan dan melihat histori log pembersihan kandang tersimpan di database.
4. **Kartu Monitoring 5 Sensor:** DHT22 Suhu, Kelembapan, Indeks THI Sapi, MQ-135 Gas, dan INMP441 Suara.
5. **Grafik Real-Time Bersebelahan:**
   - **Kiri:** Riwayat telemetri 5 sensor mikroklimat.
   - **Kanan:** Grafik Comfort Score (1 garis tunggal hijau emerald, sumbu X: waktu WIB, sumbu Y: skor 0–100).

#### B. Dashboard Terpadu Multi-Tab
Akses URL:
**`http://localhost:5000/dashboard`**
Klik tab **`Moo-nitoring Kandang`** di bilah navigasi atas untuk memantau modul kandang di dalam ekosistem terpadu peternakan.

---

## 9. Struktur Direktori Project

Struktur folder project telah dirapikan dan diorganisasikan secara modular:

```text
smart-farm/
│
├── moo-nitoring.md                 # Dokumentasi arsitektur, spesifikasi, dan panduan menjalankan
├── .gitignore                      # Mengabaikan file cache, database lokal, dan lingkungan virtual
│
├── backend/                        # Backend Python Flask REST API & Machine Learning
│   ├── app.py                      # Aplikasi utama Flask (registrasi blueprint, static files, CLI)
│   ├── moo_nitoring.py             # [MODUL KHUSUS] Blueprint logika Moo-nitoring (AI, THI, Otomasi, Sanitasi)
│   ├── models.py                   # Definisi skema database SQLAlchemy (Device, Telemetry, Logs)
│   ├── schemas.py                  # Validasi input data sensor dan format timestamp ISO 8601
│   ├── group2_model.py             # Wrapper pemuatan model Random Forest Classifier
│   ├── mqtt_service.py             # Layanan komunikasi MQTT broker
│   ├── train_kandang_model.py      # Script pelatihan model ML Random Forest
│   ├── requirements.txt            # Daftar pustaka Python yang diperlukan
│   │
│   ├── model/                      # Artefak Model Machine Learning & Dataset
│   │   ├── sensor_data.csv         # Dataset mentah pembacaan sensor kandang
│   │   ├── kandang_dataset.csv     # Dataset latih yang telah diproses
│   │   ├── rf_kandang.pkl          # Model Random Forest terkompilasi
│   │   ├── scaler_kandang.pkl      # Scaler normalisasi fitur
│   │   ├── label_encoder.pkl       # Encoder label klasifikasi
│   │   └── model_metadata.json     # Metadata performa dan akurasi model
│   │
│   ├── instance/                   # Database SQLite lokal (smart_farm.db)
│   ├── migrations/                 # Skrip migrasi SQL
│   └── tests/
│       └── test_app.py             # 18 Unit Test otomatis backend
│
├── firmware/                       # Firmware Perangkat Keras ESP32
│   └── esp32_moo_nitoring/
│       └── esp32_moo_nitoring.ino  # Kode Arduino untuk ESP32 (DHT22, MQ-135, INMP441, RTC, Relay, Buzzer)
│
└── frontend/                       # Antarmuka Pengguna Web (HTML, Tailwind, ApexCharts)
    ├── moo_nitoring.html           # [HALAMAN KHUSUS] Dashboard mandiri Moo-nitoring Kandang
    ├── dashboard_smart_farm.html   # Dashboard multi-tab terpadu SmartFarm (Collar, Kandang, Feeding, Gate)
    ├── index.html                  # Portal navigasi awal & pengarah halaman
    ├── css/
    │   └── style.css               # Styling kustom tambahan
    ├── img/
    │   └── logo.png                # Aset logo SmartFarm
    └── js/
        └── api.js                  # Modul sinkronisasi real-time REST API frontend ke backend
```
