# Backend Smart Farm

Backend Flask untuk kontrak pada dokumen standar IoT MES Smart Cattle Barn:

- REST API `/api/v1` untuk device, telemetry, event, dashboard, konfigurasi, dan command.
- PostgreSQL sebagai penyimpanan aplikasi; aplikasi membuat dan memvalidasi data sebelum menyimpan.
- MQTT client untuk menerima telemetry/status/event/ACK dari EMQX dan menerbitkan command ke perangkat.
- Integrasi Group 2 untuk menerima sensor kandang melalui endpoint kompatibel, menjalankan Random Forest, dan menyimpan histori prediksi.
- Envelope MQTT versi `1.0.0`, topic farm/barn/zone/device, validasi identitas, dan deduplikasi `message_id`.

## Menjalankan lokal

Gunakan Python 3.10+ dan PostgreSQL. Dari folder `backend`:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Ubah `DATABASE_URL` dan kredensial EMQX di `.env`, lalu siapkan tabel dan jalankan:

```powershell
flask --app app init-db
python app.py
```

Jika EMQX belum tersedia, set `MQTT_ENABLED=false`; API tetap berjalan, tetapi command akan ditolak dan pesan MQTT tidak diterima. Jangan gunakan token contoh untuk deployment. Perubahan skema setelah pembuatan tabel memerlukan migrasi database; `init-db` tidak mengubah tabel yang sudah ada.

Untuk database PostgreSQL yang sudah dibuat dengan skema backend sebelumnya, jalankan [migrations/001_group4_postgres.sql](./migrations/001_group4_postgres.sql) satu kali sebelum menjalankan seed. Untuk database baru, cukup jalankan perintah seed di bawah.

Jika database sudah memakai migrasi Kelompok 4, jalankan [migrations/002_group2_model.sql](./migrations/002_group2_model.sql) satu kali sebelum menjalankan seed Kelompok 2.

Kelompok 1 dapat mendaftarkan perangkat dan sensor pulse analog dengan:

```powershell
flask --app app seed-dev001
```

Seed membuat atau memperbarui `DEV_001` di `FARM_001/BARN_001/ZONE_001` dan sensor `SNS_PULSE_001`. Reading yang diterima:

| Parameter | Tipe/unit | Rentang |
|---|---|---|
| `pulse_raw` | Integer `adc` | 0–4095 |
| `heart_rate_bpm` | Angka `bpm` | 0–300 |

`heart_rate_bpm` dihitung di ESP32 dari sinyal analog; backend tidak menghitung BPM dari raw ADC. Firmware Smart Collar sekarang membaca GPIO 33 (ADC1) dan menerbitkan kedua parameter melalui envelope standar ke topic DEV_001. Pembacaan ADC 0–4095 dikirim sebagai `pulse_raw`; BPM yang belum terdeteksi dikirim sebagai `0` dengan quality `UNCERTAIN`. Nilai sinyal ke ESP32 tidak boleh melebihi 3.3 V.

Firmware mempertahankan topic lama untuk payload fitur AI dan menerbitkan telemetry pulse standar secara terpisah ke `sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_001/telemetry`. Semua perangkat/backend harus memakai prefix yang sama melalui `MQTT_TOPIC_PREFIX`. Prefix ini hanya mengurangi kemungkinan bentrok; broker publik tetap dapat dibaca dan ditulisi pengguna lain. Gunakan hanya data simulasi dan jangan hubungkan aktuator nyata ke broker publik. Timestamp standar bergantung pada sinkronisasi NTP; firmware melewatkan publish pulse jika waktu belum tersedia.

## Registrasi perangkat

Perangkat harus terdaftar sebelum pesan MQTT diterima. Endpoint registrasi dilindungi `API_ADMIN_TOKEN`. DEV_004 beserta sensor dan aktuator Kelompok 4 dapat didaftarkan berulang kali secara idempoten melalui CLI:

```powershell
flask --app app seed-dev004
```

Set `SMART_GATE_STREAM_URL` di `.env` ke endpoint HTTP/HTTPS MJPEG laptop AI, misalnya `http://192.168.1.50:5001/video_feed`; URL itu akan dikembalikan pada `GET /api/v1/devices/DEV_004`. Jika stream URL berubah, jalankan seed kembali setelah mengubah `.env`.

Seed membuat:

- `DEV_004` di `FARM_001` / `BARN_001` / `ZONE_001`.
- Sensor `SNS_RFID_001` (MFRC522, termasuk telemetry state) dan `SNS_CAM_AI_001` (YOLOv8).
- Aktuator `ACT_GATE_001` dengan `OPEN_GATE`/`CLOSE_GATE`, dan `ACT_ALARM_001` dengan `ALERT`/`CLEAR`.

Untuk registrasi device lain, gunakan endpoint yang dilindungi token admin:

```powershell
$env:API_ADMIN_TOKEN = 'replace-with-a-long-random-admin-token'
curl.exe -X POST http://localhost:5000/api/v1/devices `
  -H "Authorization: Bearer $env:API_ADMIN_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{"device_id":"DEV_001","farm_id":"FARM_001","barn_id":"BARN_001","zone_id":"ZONE_001","device_type":"SENSOR_NODE"}'
```

Kelompok 2 dapat didaftarkan dengan:

```powershell
flask --app app seed-dev002
```

Perintah seed mendaftarkan `DEV_002` pada `FARM_001/BARN_001/ZONE_001` dan sensor `SNS_TEMP_002`, `SNS_HUMIDITY_002`, `SNS_SOUND_002`, serta `SNS_MQ135_002`. Model Random Forest, scaler, label encoder, dan `model_metadata.json` dibaca dari `..\backend kel 2\model` secara default; folder Kelompok 2 tidak diubah. Jika dipindahkan, atur `GROUP2_MODEL_DIR`.

API Kelompok 2 tetap menerima format yang digunakan `app_kandang.py`:

| Method | Endpoint | Fungsi |
|---|---|---|
| POST | `/api/sensors` | Terima `temperature_c`, `humidity_rh`, `sound_rms`, `mq135_raw`; kembalikan prediksi status |
| GET | `/api/sensors/latest` | Prediksi terbaru |
| GET | `/api/sensors/history` | Maksimal 50 prediksi terakhir |
| GET | `/health` | Status model dan jumlah data |
| GET | `/model/info` | Metadata model |

`temperature_c` dan `humidity_rh` wajib. Untuk kompatibilitas dengan kode Kelompok 2, `sound_rms`/`mq135_raw` yang tidak dikirim bernilai default `0` dan ditandai `UNCERTAIN`/dalam `defaulted_fields`. `recorded_at` dapat memakai ISO 8601 berzona; format lama `YYYY-MM-DD HH:MM:SS` ditafsirkan sebagai WIB. Jalankan model hanya dari artefak pickle yang dipercaya karena pemuatan file model mengeksekusi deserialisasi Python.

Kelompok 3 didaftarkan dengan:

```powershell
flask --app app seed-dev003
```

Seed membuat `DEV_003` di `FARM_001/BARN_001/ZONE_001`, sensor `SNS_ULTRASONIC_003` dan `SNS_WATER_LEVEL_003`, serta aktuator `ACT_PUMP_003` dan `ACT_FEEDER_MOTOR_003`. Parameter telemetry standar:

| Sensor | Parameter | Tipe/unit |
|---|---|---|
| `SNS_ULTRASONIC_003` | `distance_cm` | Angka 2–400 cm |
| `SNS_ULTRASONIC_003` | `feed_level_percent` | Angka 0–100 `%` |
| `SNS_ULTRASONIC_003` | `feed_status` | `LOW`, `NORMAL`, atau `ERROR` |
| `SNS_WATER_LEVEL_003` | `water_level_percent` | Angka 0–100 `%` |

Nilai `water_level_percent` perlu dikalibrasi oleh firmware sesuai sensor dan wadah yang benar-benar dipakai. Untuk kesalahan pembacaan ultrasonic, kirim `feed_status: "ERROR"` dengan quality `BAD`; jangan mengirim angka jarak palsu.

Command menggunakan `SET_STATE`: `ACT_PUMP_003` menerima `state: ON|OFF`; `ACT_FEEDER_MOTOR_003` menerima `state: ON|OFF` dan `speed_percent` opsional bernilai integer 0–100. Contoh request:

```json
{"actuator_id":"ACT_PUMP_003","command":"SET_STATE","parameters":{"state":"ON"}}
{"actuator_id":"ACT_FEEDER_MOTOR_003","command":"SET_STATE","parameters":{"state":"ON","speed_percent":60}}
```

Sketch Kelompok 3 yang diberikan masih memakai topic `smartfeeding/*`, payload tanpa envelope, dan konfigurasi output pompa lama. Firmware perlu diperbarui ke topic ber-prefix `sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_003/{telemetry|status|event|command|command/ack}` dan envelope MQTT backend. Hapus dan rotasi kredensial Wi-Fi yang tertulis langsung di sketch.

Backend subscribe pada topic masukan device dengan format `{MQTT_TOPIC_PREFIX}/farm/{farm_id}/barn/{barn_id}/zone/{zone_id}/device/{device_id}/{telemetry|status|event|command/ack}`. Segmen `zone/{zone_id}` boleh dihilangkan jika perangkat tidak memakai zona. ID topic harus cocok dengan envelope dan registri. Prefix topic bukan autentikasi atau ACL.

## Endpoint

| Method | Endpoint | Keterangan |
|---|---|---|
| GET | `/api/v1/health` | Health check |
| GET, POST | `/api/v1/devices` | Daftar; POST mendaftarkan perangkat (token admin) |
| GET | `/api/v1/devices/{device_id}` | Detail, status terakhir, dan `stream_url` opsional |
| GET | `/api/v1/telemetry?device_id=DEV_001&from=...&to=...&page=1&per_page=50` | Histori telemetry; ISO 8601 dengan zona waktu |
| GET | `/api/v1/events?severity=HIGH&page=1&per_page=50` | Event/alarm |
| GET | `/api/v1/dashboard/summary` | Ringkasan status |
| GET | `/api/v1/config/devices/{device_id}` | Konfigurasi device tersimpan |
| POST | `/api/v1/devices/{device_id}/commands` | Terbitkan command MQTT (token kontrol) |
| GET | `/api/v1/commands/{command_id}` | Status dan ACK command |

Command hanya diterbitkan ketika device berstatus `ONLINE`, menggunakan QoS 1, non-retained, serta expiry yang dibuat backend. Jika broker tidak tersedia atau publish tidak dikonfirmasi, command tercatat sebagai `FAILED` dan API mengembalikan error. `API_CONTROL_TOKEN` merupakan proteksi service-token untuk prototype, bukan pengganti autentikasi pengguna dan role-based authorization; integrasikan identity provider sebelum deployment publik.

Untuk DEV_004, endpoint hanya menerima pasangan aktuator/perintah yang terdaftar. Contoh:

```json
{"actuator_id":"ACT_GATE_001","command":"OPEN_GATE","parameters":{}}
{"actuator_id":"ACT_ALARM_001","command":"ALERT","parameters":{}}
```

Balasan perangkat dikirim ke topic `.../device/DEV_004/command/ack` dengan `command_id`, `actuator_id`, dan `result`. Status ACK dapat dipantau melalui `GET /api/v1/commands/{command_id}`.

## Kontrak MQTT

Setiap envelope menggunakan `schema_version: "1.0.0"`, UUID `message_id`, waktu ISO 8601 berzona, ID terdaftar, `message_type` yang sama dengan suffix topic, dan object `payload`. DEV_004 menerima counter integer non-negatif `cow_count`, `farmer_count`, `intruder_count`, dan `animal_anomaly_count`; `gate_state` dan `alarm_state` berupa string. Event RFID `RFID_ACCESS_GRANTED`/`RFID_ACCESS_DENIED` menyertakan `metadata.uid`; event AI yang didukung mencakup `INTRUDER_DETECTED`, `ANIMAL_ANOMALY`, dan `ALARM_CLEARED`. Telemetry disimpan per reading dengan nilai JSON bertipe, indeks device/sensor, dan waktu; event serta command disimpan terstruktur. Pesan duplikat diabaikan berdasarkan `message_id`.

Jalankan pengujian dengan `python -m unittest discover -s tests`.
