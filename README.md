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
decrypts the stream. The "How it works" section below summarizes the protocol.

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
python pocket_radar_client.py --no-speak # don't read speeds aloud
python pocket_radar_client.py --address <ble-address>
```

By default each speed is also spoken aloud on the default speakers (macOS `say`,
or `spd-say`/`espeak` on Linux; silently skipped if none is installed). Pass
`--no-speak` to disable. It auto-reconnects when the radar drops the link.
`Ctrl-C` to stop.

### Web dashboard

```bash
python radar_web.py                      # serve on http://127.0.0.1:8000
python radar_web.py --port 9000 --kph
python radar_web.py --demo               # fake readings, no radar needed
```

Same BLE session, but with a browser UI: a big live speed readout, a sidebar of
previous swings, and running count / average / max for the session. History is
kept on the server, so refreshing the page (or opening it on a second device
with `--host 0.0.0.0`) keeps the whole session. The **Reset** button clears it;
the **Voice** toggle speaks each speed in the browser. `--speak` additionally
speaks on the server's speakers (off by default, unlike the CLI).

## Files

| File | What it is |
|------|------------|
| `pocket_radar_client.py` | The CLI: pairs, decrypts, and prints speeds + battery/units/status. |
| `prlib.py` | Pure-Python reimplementation of the app's crypto (`encrypt`/`decrypt`/`return_key`). |
| `radar_web.py` | FastAPI/uvicorn web dashboard around the same client (WebSocket push). |
| `static/index.html` | The dashboard page (no build step, no external assets). |

## How it works (short version)

1. Enable notifications on the speed characteristic and send the pairing password.
2. The radar returns three 8-byte nonces; from them `ReturnKey()` derives the AES
   session key.
3. Send an encrypted heartbeat once a second to keep the link alive.
4. Decrypt each notification; `frame[2]==1` is a speed reading at `frame[13]`.
