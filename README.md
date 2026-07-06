# Pocket Radar Smart Coach — BLE speed reader

A small Python tool that connects to a **Pocket Radar Smart Coach (SR1100)** over
Bluetooth Low Energy and prints the speed readings live — the same data the
official phone app receives.

```
[10:15:02]  · radar MAC xx:xx:xx:xx:xx:xx
[10:15:02]  · battery 3/4 · on battery · idle
[10:15:07]  · measuring
[10:15:08]  ⚾  71 mph
[10:15:11]  · idle
```

The radar's BLE payloads are AES-encrypted behind a challenge/response handshake,
so a plain BLE client sees nothing useful. This tool reproduces the handshake and
decrypts the stream. See [`FINDINGS.md`](FINDINGS.md) for the full protocol writeup.

## Legal / scope

This is an independent, unofficial interoperability tool for talking to a radar
**you own**. It is not affiliated with or endorsed by Pocket Radar, Inc.

It does **not** bundle any Pocket Radar binary or the decompiled app. The payload
encryption is standard AES-128, and the session-key derivation was reverse-
engineered and re-implemented in pure Python (`prlib.py`) for interoperability.
The app APK, its native library, the decompiled sources, and BLE captures are all
git-ignored and must never be committed or redistributed.

## Requirements

- Python 3.10+
- A machine with Bluetooth LE (macOS or Linux; `bleak` handles the platform)

That's it — the tool is pure Python (`bleak` + `pycryptodome`); no Android device,
native library, or emulator needed.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Pairing password

The radar uses a 9-byte pairing password. The client resolves it from, in order:
`--pwd <hex>`, the `POCKET_RADAR_PWD` env var, or a git-ignored `radar_password.txt`
next to the script. If none exists, it **generates a random one and saves it** —
put the radar in pairing mode on that first run so it registers the new password.
(Registering a new password re-pairs the radar to this tool; if you also use the
phone app, reuse the app's password instead of generating a new one.)

## Usage

```bash
# close the phone app first — the radar accepts one connection at a time
python pocket_radar_client.py            # scan, pair, stream speeds
python pocket_radar_client.py --kph      # label speeds as kph
python pocket_radar_client.py --address <ble-address>
```

It auto-reconnects when the radar drops the link. `Ctrl-C` to stop.

## Files

| File | What it is |
|------|------------|
| `pocket_radar_client.py` | The tool: pairs, decrypts, and prints speeds + battery/units/status. |
| `prlib.py` | Pure-Python reimplementation of the app's crypto (`encrypt`/`decrypt`/`return_key`). |
| `pocket_radar.py` | BLE utility: `scan` / `explore` (GATT dump) / `listen` (raw notifications). |
| `FINDINGS.md` | How the protocol and encryption were reverse-engineered. |

## How it works (short version)

1. Enable notifications on the speed characteristic and send the pairing password.
2. The radar returns three 8-byte nonces; from them `ReturnKey()` derives the AES
   session key.
3. Send an encrypted heartbeat once a second to keep the link alive.
4. Decrypt each notification; `frame[2]==1` is a speed reading at `frame[13]`.
