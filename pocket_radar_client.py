#!/usr/bin/env python3
"""Pocket Radar Smart Coach (SR1100) live speed reader.

Connects to the radar over BLE, performs the same encrypted handshake the
official app does, and prints every speed reading the radar broadcasts.

How it works (reverse-engineered from the Android app's libprlib.so + a
PacketLogger capture):

  1. Enable notifications on the Speed-Measurement characteristic.
  2. Send the 9-byte pairing password (plaintext) to the Radar-Command char.
  3. The radar replies with three 8-byte nonces (frames type=7, counter 0/1/2).
     After each of the first two we send encr_one / encr_two (plaintext).
  4. From the 24 nonce bytes, the native ReturnKey() derives the AES session
     key. We send encr_three (encrypted) to finish the handshake.
  5. Steady state: send an encrypted status_req heartbeat every second, and
     AES-decrypt each notification. A frame with plaintext[2]==1 is a speed;
     the value is plaintext[13] (in the radar's current units).

Requires prlib.py (pure-Python reimplementation of the app's crypto).

Usage:
    python pocket_radar_client.py                 # auto-scan for the radar
    python pocket_radar_client.py --address ADDR  # connect to a known address
    python pocket_radar_client.py --kph           # label speeds as kph
"""

import argparse
import asyncio
import os
import pathlib
import secrets
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from prlib import PrLib

# --- BLE identifiers (from SmartCoachProfile) --------------------------------
SERVICE_UUID = "6e0ffff0-19c8-039b-b046-b395e3a2a3b4"
SPEED_MEAS_UUID = "6e0ffff1-19c8-039b-b046-b395e3a2a3b4"   # notify
RADAR_CMD_UUID = "6e0ffff2-19c8-039b-b046-b395e3a2a3b4"    # write

# --- command frames (from PocketRadarService) --------------------------------
STATUS_REQ  = bytes([0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
REQUEST_MAC = bytes([0, 0, 0x1C, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0])
ENCR_ONE   = bytes([0, 0, 0x0E, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
ENCR_TWO   = bytes([0, 0, 0x0E, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
ENCR_THREE = bytes([0, 0, 0x0E, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
RADAR_ON   = bytes([0, 0, 3, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
SEND_PWD_TEMPLATE = bytearray([0, 0, 0x25, 0, 0,  0,0,0,0,0,0,0,0,0,  0, 0])

# The 9-byte pairing password is per-radar and is a secret, so it is NOT stored
# in this file. load_password() reads it from --pwd, then $POCKET_RADAR_PWD, then
# the gitignored file radar_password.txt next to this script. If none is found a
# new random one is generated and saved there — put the radar in pairing mode on
# that first run so it registers the new password.
PWD_FILE = pathlib.Path(__file__).with_name("radar_password.txt")


def load_password(cli_hex: str | None) -> bytes:
    if cli_hex:
        return bytes.fromhex(cli_hex)
    if os.environ.get("POCKET_RADAR_PWD"):
        return bytes.fromhex(os.environ["POCKET_RADAR_PWD"])
    if PWD_FILE.exists():
        return bytes.fromhex(PWD_FILE.read_text().split("#")[0].strip())
    pwd = secrets.token_bytes(9)
    PWD_FILE.write_text(pwd.hex() + "\n")
    print(f"Generated a new pairing password and saved it to {PWD_FILE.name}.")
    print("Put the radar in pairing mode on this first run so it registers it.")
    return pwd

# message types (plaintext[2]) in the encrypted stream
MSG_SPEED, MSG_STATUS, MSG_KEYX = 1, 3, 7
MSG_ACK, MSG_PUSH, MSG_MAC, MSG_TUNNEL = 8, 9, 28, 43

NAME_HINTS = ("sc-", "smart coach", "sr1100", "pocket")


def ts():
    return datetime.now().strftime("%H:%M:%S")


class PocketRadar:
    def __init__(self, pwd, units="mph"):
        self.lib = PrLib()
        self.pwd = pwd
        self.units = units
        self.client: BleakClient | None = None
        self._reset()

    def _reset(self):
        self.cipher = bytearray(24)
        self.session_key = None
        self.state = 0
        self.ready = asyncio.Event()
        self.queue: asyncio.Queue = asyncio.Queue()
        # radar state, reported when it changes (None = not yet known)
        self.battery = self.usb = self.meas = self.mac = None

    # -- notification plumbing: hand raw bytes to the async processor ---------
    def _on_notify(self, _char, data: bytearray):
        self.queue.put_nowait(bytes(data))

    async def _write(self, payload: bytes, encrypt: bool):
        out = self.lib.encrypt(payload, self.session_key) if encrypt else payload
        await self.client.write_gatt_char(RADAR_CMD_UUID, out, response=True)

    async def run_forever(self, address):
        """Connect, pair, and stream — reconnecting whenever the radar drops."""
        while True:
            try:
                device = await find_device(address)
                await self._session(device)
            except BleakError as e:
                print(f"[{ts()}] disconnected ({e}); reconnecting…")
            except SystemExit as e:
                print(e)
            await asyncio.sleep(2.0)

    async def _session(self, device):
        self._reset()
        disconnected = asyncio.Event()
        async with BleakClient(
            device, disconnected_callback=lambda _c: disconnected.set()
        ) as client:
            self.client = client
            print(f"[{ts()}] connected to {device.address}")
            await client.start_notify(SPEED_MEAS_UUID, self._on_notify)

            # kick off the handshake: send the pairing password (plaintext)
            pwd_frame = bytearray(SEND_PWD_TEMPLATE)
            pwd_frame[5:14] = self.pwd
            await self._write(bytes(pwd_frame), encrypt=False)

            proc = asyncio.create_task(self._process_loop())
            beat = asyncio.create_task(self._heartbeat_loop())
            await disconnected.wait()
            for task in (proc, beat):
                task.cancel()

    async def _heartbeat_loop(self):
        await self.ready.wait()
        # keep the link alive; without this the radar drops us after ~2.5 s
        try:
            await self._write(REQUEST_MAC, encrypt=True)   # ask for the MAC once
            while self.client and self.client.is_connected:
                await self._write(STATUS_REQ, encrypt=True)
                await asyncio.sleep(1.0)
        except (BleakError, asyncio.CancelledError):
            pass  # disconnect is handled by the session's reconnect loop

    async def _process_loop(self):
        try:
            while True:
                data = await self.queue.get()

                if self.state < 3:             # handshake frames are plaintext
                    await self._handle_handshake(data)
                    continue

                # steady state: everything is AES-encrypted
                self._handle_frame(self.lib.decrypt(data, self.session_key))
        except (BleakError, asyncio.CancelledError):
            pass  # disconnect is handled by the session's reconnect loop

    async def _handle_handshake(self, data: bytes):
        if data[2] == MSG_KEYX and data[3] == self.state:
            self.cipher[self.state * 8:self.state * 8 + 8] = data[6:14]
            if self.state == 0:
                await self._write(ENCR_ONE, encrypt=False)
            elif self.state == 1:
                await self._write(ENCR_TWO, encrypt=False)
            elif self.state == 2:
                rc, self.session_key = self.lib.return_key(bytes(self.cipher))
                await self._write(ENCR_THREE, encrypt=True)
                print(f"[{ts()}] paired — session key {self.session_key.hex()}")
                print(f"[{ts()}] streaming speeds (throw now)…\n")
                self.ready.set()
            self.state += 1
        elif data[2] == 12:
            print(f"[{ts()}] radar rejected the password (put it in pairing mode)")

    def _handle_frame(self, pt: bytes):
        kind = pt[2]
        if kind == MSG_SPEED and pt[3] == 0:
            print(f"[{ts()}]  ⚾  {pt[13] & 0xFF} {self.units}")

        elif kind == MSG_STATUS and pt[3] == 0:
            # [10]=battery(1-4) [11]=usb(0/1) [12]=units(0 mph/1 kph) [13]=meas(1)/idle(2)
            self._update(battery=pt[10], usb=pt[11], units=pt[12], meas=pt[13])

        elif kind == MSG_PUSH:
            batt, event = pt[12], pt[13]      # event: state that just changed
            fields = {1: {"meas": 1}, 2: {"meas": 2}, 3: {"units": 0},
                      4: {"units": 1}, 5: {"usb": 1}, 6: {"usb": 0}}.get(event, {})
            self._update(battery=batt, **fields)

        elif kind in (MSG_MAC, MSG_TUNNEL):
            mac = ":".join(f"{b & 0xFF:02x}" for b in pt[8:14])
            if mac != self.mac and any(pt[8:14]):
                self.mac = mac
                print(f"[{ts()}]  · radar MAC {mac}")

    def _update(self, battery=None, usb=None, units=None, meas=None):
        """Update cached radar state; print a line for whatever changed."""
        changed = []
        if battery is not None and battery != self.battery:
            self.battery = battery
            changed.append(f"battery {battery}/4")
        if usb is not None and usb != self.usb:
            self.usb = usb
            changed.append("USB power connected" if usb == 1 else "on battery")
        if units is not None:
            label = "kph" if units == 1 else "mph"
            if label != self.units:
                self.units = label
                changed.append(f"units {label}")
        if meas is not None and meas != self.meas:
            self.meas = meas
            changed.append("measuring" if meas == 1 else "idle")
        if changed:
            print(f"[{ts()}]  · {' · '.join(changed)}")


async def find_device(address):
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=15.0)
        if not dev:
            raise SystemExit(f"No BLE device at {address} (is the radar on?)")
        return dev
    print("Scanning for a Pocket Radar (put it in pairing mode)…")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: (d.name and any(h in d.name.lower() for h in NAME_HINTS))
        or SERVICE_UUID.lower() in [u.lower() for u in (adv.service_uuids or [])],
        timeout=20.0,
    )
    if not dev:
        raise SystemExit("No Pocket Radar found. Turn it on / put it in pairing mode.")
    print(f"Found {dev.name} [{dev.address}]")
    return dev


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", help="BLE address (macOS: CoreBluetooth UUID)")
    ap.add_argument("--pwd", help="9-byte pairing password as hex", default=None)
    ap.add_argument("--kph", action="store_true", help="assume kph units")
    args = ap.parse_args()

    pwd = load_password(args.pwd)
    if len(pwd) != 9:
        raise SystemExit("password must be 9 bytes (18 hex chars)")

    radar = PocketRadar(pwd=pwd, units="kph" if args.kph else "mph")
    await radar.run_forever(args.address)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
