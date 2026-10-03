# Sisi A — Roblox Auto-Rejoin Termux (WebSocket)

Transport utama ke Sisi B sekarang **WebSocket**: `ws://host:port/ws` untuk deployment NuraHost tanpa TLS. `/pair/claim` dan `/health` tetap HTTP.

## Instalasi

```bash
bash setup-termux.sh
```

atau:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --setup
```

## Pairing

1. Jalankan `/pair generate` di Discord.
2. Termux → `[1] Pairing device`.
3. Masukkan `http://nano-1.nura.host:5127` saat diminta.
4. Masukkan token 8 karakter.
5. `device_token` + `device_id` disimpan di `config.json` permission `0600`.
6. Jalankan daemon dengan `[7]` atau `python main.py --daemon`.
7. Client otomatis membuat koneksi `ws://nano-1.nura.host:5127/ws`.

## Test koneksi

Menu `[5] Test koneksi ke server` sekarang melakukan WS connect, autentikasi handshake, hello/ready, lalu probe ping/pong.

## Reconnect

`5s → 10s → 30s → 60s → 120s → 300s`. Server restart atau 4G drop akan memicu reconnect otomatis. Setelah connect, client kirim hello + ready lagi dan server mengirim queued command.

## Ping / heartbeat

Server mengirim application ping tiap 30 detik. Client membalas pong. Kalau dua ping berturut tidak mendapat pong dalam 5 detik, server menutup koneksi. Client akan reconnect. Heartbeat device dikirim setiap 30 detik melalui frame `heartbeat`. Kalau server ping tidak terdengar 60 detik, client memutus koneksi dan reconnect.

## Command

Server mengirim:

```json
{
  "type": "command",
  "id": "cmd_xxx",
  "payload": {"action": "rejoin", "package": "com.example.roblox"}
}
```

`CommandHandler` tetap memakai action lama: `rejoin`, `start`, `stop`, `scan`, `log`, `ping`, `package_config`. ACK dikirim dalam frame `ack`. ID command disimpan di LRU cache maksimum 1000 entry agar reconnect/retry tidak mengeksekusi command yang sama dua kali.

## HTTP vs WS

HTTP diterima untuk `/pair/claim` dan `/health`. WS memakai skema yang sesuai dengan server: `http://` → `ws://`, `https://` → `wss://`. Untuk NuraHost port allocation plaintext, gunakan `http://...` di config sehingga client memakai `ws://.../ws`. HMAC, Authorization, timestamp, dan nonce tetap dipakai pada handshake. Plain HTTP/WS tidak mengenkripsi traffic.
