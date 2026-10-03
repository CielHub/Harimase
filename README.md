# Roblox Auto-Rejoin + Discord Universal Control Bot — Sisi A

Client ini berjalan di Android 10 + Termux + root/Magisk. Client melakukan polling ke Sisi B, memonitor package secara independen, dan hanya boleh menghentikan package target yang sudah diverifikasi.

## Struktur

```text
termux-client/
├── main.py
├── config.json                  # dibuat saat setup, chmod 600
├── config.example.json
├── requirements.txt
├── api/
│   ├── server.py                # local debug API opsional
│   └── client.py                # remote HTTP client
├── core/
│   ├── package_scanner.py
│   ├── process_monitor.py
│   ├── crash_detector.py
│   ├── killer.py
│   ├── rejoiner.py
│   ├── heartbeat.py
│   └── command_handler.py
├── utils/
│   ├── logger.py
│   ├── shell.py
│   └── crypto.py
├── menus/setup_menu.py
├── logs/
└── scripts/boot.sh
```

## Instalasi dari HP kosong

1. Install Termux dan Termux:Boot.
2. Pastikan Magisk root aktif dan `su -c id` menghasilkan `uid=0`.
3. Salin folder `termux-client` ke penyimpanan Termux.
4. Dari root project jalankan:

```bash
bash setup-termux.sh
```

Atau langsung dari folder client:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --setup
```

## Pairing

Di Discord jalankan:

```text
/pair generate
```

Token berlaku 5 menit dan hanya bisa dipakai sekali. Di Termux:

```text
[1] Pairing device
```

Isi server URL saat pertama kali diminta dan masukkan pairing token. Server mengembalikan `device_token` dan `device_id`. Token raw ditulis ke `config.json` dengan permission `0600`.

### Kenapa claim tidak memakai device token?

Karena `device_token` belum ada sebelum `/pair/claim` berhasil. Endpoint claim memakai pairing token sebagai credential sementara + HMAC key. Setelah pairing, semua request normal menggunakan device token. Signature mencakup timestamp dan nonce agar request yang tertangkap tidak dapat direplay dengan mengganti header tanpa membuat signature baru.

## Auto-discovery

Scanner menjalankan tiga sumber informasi lalu mendeduplikasi hasil:

1. `pm list packages -3`
2. Heuristik data directory, metadata package/activity, keyword executor/Roblox, dan overlay permission
3. Package yang ditambahkan manual melalui setup menu

Package yang ditemukan otomatis ditandai `status=detected` dan `enabled=false`, sehingga tidak langsung aktif tanpa konfigurasi.

Client juga menjalankan scan pada setiap startup. Dengan begitu package baru tetap bisa muncul setelah aplikasi Roblox/executor berubah package name.

## Monitoring crash

Setiap siklus monitor melakukan:

- `pidof <pkg>`
- verifikasi `/proc/<pid>/cmdline`
- verifikasi `/proc/<pid>/status` UID
- `dumpsys package <pkg>` untuk target UID
- `dumpsys activity processes` untuk state
- `logcat -b crash -d` dan logcat umum untuk crash/ANR baru
- pemeriksaan log internal package bila tersedia

PID terakhir yang berhasil diverifikasi disimpan agar ketika PID hilang, keputusan tidak bergantung pada `/proc/<pid>` yang sudah lenyap.

**Tidak ada `pkill`, `killall`, pattern kill, atau `kill -9` tanpa verifikasi.** Urutan stop adalah:

```text
am force-stop <pkg>
↓
verifikasi target masih hidup
↓
kill -9 <PID> hanya bila cmdline + UID + process start-time masih cocok
```

## Rejoin

Mode `deep_link` menjalankan:

```text
am start -W -a android.intent.action.VIEW -d 'roblox://experiences/start?placeId=...&gameInstanceId=...'
```

Mode `executor_only` menyelesaikan launchable activity package executor dan membuka activity tersebut tanpa membuat deep link Roblox.

Per package:

- cooldown default 25 detik
- maksimal 5 retry per 30 menit
- retry dihitung saat attempt mulai, termasuk attempt yang gagal
- bila batas tercapai, event `rejoin_failed` dikirim ke Sisi B

## Polling dan backoff

Request memakai `X-Timestamp` + `X-Nonce` + HMAC signature. Polling command memakai satu command per request dengan lease server 120 detik, sehingga command tidak dieksekusi dua kali hanya karena polling cepat atau dua proses daemon yang tidak sengaja aktif. File lock daemon mencegah dua instance client berjalan bersamaan.

Command dipoll setiap 5 detik. Heartbeat dikirim setiap 30 detik.

Ketika Sisi B tidak dapat dihubungi, client memakai backoff:

```text
5s → 10s → 30s → 60s → 120s → 300s
```

401 membuat client menandai `needs_repair` dan menulis instruksi re-pair ke log. Setelah token direvoke di Discord, pairing ulang dilakukan melalui setup menu.

## Menu

```text
=== Roblox Auto-Rejoin Setup ===
[1] Pairing device
[2] Scan package otomatis
[3] Tambah package manual
[4] Edit config package
[5] Test koneksi ke server
[6] Lihat status semua package
[7] Start daemon di background
[8] Stop daemon
[9] Keluar
```

## Auto-start setelah reboot

`setup-termux.sh` membuat:

```text
~/.termux/boot/roblox-auto-rejoin.sh
```

Script memanggil `termux-wake-lock` dan menjalankan daemon dari virtual environment project bila tersedia.

## Logging

Format:

```text
[YYYY-MM-DD HH:MM:SS] [LEVEL] [pkg] message
```

File utama:

```text
logs/client.log
```

Rotasi pada 5 MB, menyimpan 5 arsip gzip.

## Konfigurasi package

Contoh:

```json
{
  "package": "com.delta.lite",
  "alias": "Delta Lite",
  "enabled": true,
  "status": "active",
  "place_id": "1234567890",
  "job_id": "abcdef-1234-...",
  "mode": "deep_link",
  "cooldown_sec": 25,
  "max_retry_per_30min": 5,
  "detect_methods": ["pid", "logcat_crash", "dumpsys"]
}
```

## Local debug API opsional

Jalankan:

```bash
.venv/bin/python main.py --daemon --local-api
```

Lalu dari device buka:

```text
http://127.0.0.1:8787/health
http://127.0.0.1:8787/status
```

API lokal sengaja hanya bind ke localhost.
