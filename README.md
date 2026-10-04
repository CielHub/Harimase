# Sisi A - Roblox Auto-Rejoin Termux (WebSocket + TUI)

Transport utama ke Sisi B menggunakan WebSocket plaintext `ws://nano-1.nura.host:5127/ws`.
HTTP hanya dipakai untuk pairing/health bila diperlukan oleh flow server.

## Startup

```bash
python main.py
```

Mode otomatis:

- paired + TTY -> dashboard TUI `rich.Live` refresh 1 detik
- paired + non-TTY -> headless daemon + log
- first-time config belum paired + TTY -> setup menu
- config sudah ada tetapi partial/broken -> **tidak pernah auto-drop ke setup**; daemon headless melaporkan error dan berhenti aman

Explicit:

```bash
python main.py --setup
python main.py --daemon
```

`--setup` hanya untuk first-time config creation. Bukan repair flow. `--daemon` selalu headless.

## Endpoint

Server deployment sudah ditetapkan:

```text
http://nano-1.nura.host:5127
ws://nano-1.nura.host:5127/ws
```

Tidak ada prompt URL server pada setup menu.

## TUI

Prioritas: **stability > observability > aesthetics**.

Dashboard membaca snapshot `AgentState` yang diupdate daemon. TUI tidak melakukan polling ke `PackageManager`, `ProcessMonitor`, atau Android shell.

Hotkey lokal:

```text
q  quit daemon
r  reconnect WebSocket
l  toggle log panel
s  stop semua package enabled secara selective
```

Jika terminal tidak interaktif atau Rich tidak tersedia, aplikasi otomatis memakai fallback log biasa.

## Pairing

1. Generate pairing token dari Discord.
2. Jalankan `python main.py --setup` saat pertama kali.
3. Pilih `[1] Pair device`.
4. Masukkan pairing token 8 karakter.
5. Device token + device ID disimpan ke `config.json` permission `0600`.
6. Jalankan `python main.py` atau `python main.py --daemon`.

## WebSocket

Reconnect memakai exponential backoff `5s -> 10s -> 30s -> 60s -> 120s -> 300s`.
Session aktif hanya satu untuk device. Command idempotency tetap digunakan agar reconnect tidak mengeksekusi command dua kali.
