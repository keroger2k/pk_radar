# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pure-Python CLI that connects to a **Pocket Radar Smart Coach (SR1100)** over Bluetooth LE, performs the same encrypted handshake the official phone app uses, and prints live speed readings. The protocol was reverse-engineered from the Android app's native `libprlib.so` and a PacketLogger capture; `prlib.py` reimplements that native crypto in pure Python (verified byte-for-byte against the original), so no proprietary binary is needed.

## Commands

```bash
# setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run (radar accepts ONE BLE connection at a time — close the phone app first)
python pocket_radar_client.py            # scan, pair, stream speeds (speaks each speed aloud)
python pocket_radar_client.py --kph      # label speeds as kph
python pocket_radar_client.py --no-speak # don't read speeds aloud
python pocket_radar_client.py --address <ble-address>
python pocket_radar_client.py --pwd <hex>   # 9-byte pairing password as 18 hex chars

# crypto self-check (no hardware needed): AES round-trip + return_key vector
python prlib.py
```

There is no test framework, linter, or build step. `python prlib.py` is the only offline check; everything else requires the physical radar and a Bluetooth LE adapter.

## Architecture

Two modules, one clean dependency edge (`pocket_radar_client.py` → `prlib.py`):

- **`prlib.py`** — `PrLib` class, the crypto layer. `encrypt`/`decrypt` are plain AES-128-ECB; `return_key(cipher24)` derives the 16-byte AES session key from the three 8-byte nonces the radar sends during pairing (12 rounds of AES-128 *decrypt* under a fixed constant `_CONST`, carrying bytes between rounds). This mirrors the native JNI methods exactly — do not "fix" the derivation to look more conventional; it must match the device.

- **`pocket_radar_client.py`** — everything else: BLE (via `bleak`), the handshake state machine, and output formatting. `PocketRadar` is one asyncio session with three cooperating pieces:
  - `_on_notify` (bleak callback) drops raw notification bytes onto an `asyncio.Queue`.
  - `_process_loop` drains the queue. While `self.state < 3` frames are plaintext handshake (`_handle_handshake`); after pairing everything is AES-encrypted and goes through `lib.decrypt` → `_handle_frame`.
  - `_heartbeat_loop` sends an encrypted `STATUS_REQ` every second — without it the radar drops the link after ~2.5 s.

  `run_forever` wraps `_session` in a reconnect loop; a `disconnected_callback` sets an event that tears down the loops so the outer loop can rebuild the session.

### Protocol specifics that matter

- **Handshake ordering is stateful.** The radar sends three `type==7` key-exchange frames with counters 0/1/2. `self.state` must equal the frame counter. After nonce 0 → send `ENCR_ONE` (plaintext), after nonce 1 → `ENCR_TWO` (plaintext), after nonce 2 → derive the key and send `ENCR_THREE` (**encrypted**). Only after state reaches 3 is the stream encrypted.
- **Frame decoding is byte-offset based** on the decrypted 16-byte plaintext. `pt[2]` is the message type (`MSG_SPEED=1`, `MSG_STATUS=3`, `MSG_KEYX=7`, `MSG_PUSH=9`, `MSG_MAC=28`, `MSG_TUNNEL=43`); a speed reading is `pt[2]==1 && pt[3]==0` with the value at `pt[13]`. These offsets and the command frame templates (`STATUS_REQ`, `ENCR_*`, etc.) come from the device firmware — treat them as fixed constants, not tunables.
- **BLE UUIDs**: service `6e0ffff0-…`, speed-measurement notify char `…fff1`, radar-command write char `…fff2`.

## Secrets

The 9-byte pairing password is a per-radar secret and must never be committed. `load_password()` resolves it in order: `--pwd`, `$POCKET_RADAR_PWD`, then the git-ignored `radar_password.txt` next to the script. If none exists it generates a random one and saves it — which re-pairs the radar to this tool (put the radar in pairing mode on that first run). `radar_password.txt` is in `.gitignore`; keep it that way.
