#!/usr/bin/env python3
"""Web dashboard for the Pocket Radar Smart Coach.

Runs the same BLE session as pocket_radar_client.py, but instead of only
printing to the terminal it pushes every event over a WebSocket to a browser
page: a big live speed readout, a sidebar of previous readings, and running
count / average / max for the session.

Usage:
    python radar_web.py                    # serve on http://127.0.0.1:8000
    python radar_web.py --port 9000
    python radar_web.py --address ADDR --kph --speak
"""

import argparse
import asyncio
import json
import pathlib
import random
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from pocket_radar_client import PocketRadar, load_password

INDEX_HTML = pathlib.Path(__file__).with_name("static") / "index.html"

# set by main() before uvicorn starts
CONFIG = {"address": None, "pwd": None, "units": "mph", "speak": False,
          "demo": False}


class Hub:
    """Session state + fan-out to connected browsers.

    History lives here (not in the page) so a refresh or a second browser
    gets the whole session so far.
    """

    def __init__(self):
        self.history: list[dict] = []      # {"speed", "units", "ts"}
        self.status = {"state": "starting", "battery": None, "usb": None,
                       "units": None, "meas": None}
        self.clients: set[WebSocket] = set()

    def snapshot(self) -> dict:
        return {"type": "snapshot", "history": self.history, "status": self.status}

    def publish(self, event: dict):
        """PocketRadar.on_event callback — runs inside the event loop."""
        kind = event["type"]
        if kind == "speed":
            self.history.append({k: event[k] for k in ("speed", "units", "ts")})
        elif kind == "status":
            self.status.update({k: event[k] for k in
                                ("battery", "usb", "units", "meas")})
        elif kind == "connection":
            self.status["state"] = event["state"]
        self.broadcast(event)

    def reset(self):
        self.history.clear()
        self.broadcast(self.snapshot())

    def broadcast(self, event: dict):
        msg = json.dumps(event)
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws: WebSocket, msg: str):
        try:
            await ws.send_text(msg)
        except Exception:
            self.clients.discard(ws)


hub = Hub()


async def _demo_loop():
    """Fake radar for trying the page without hardware (--demo)."""
    hub.publish({"type": "connection", "state": "paired",
                 "ts": datetime.now().strftime("%H:%M:%S")})
    while True:
        await asyncio.sleep(random.uniform(2.0, 5.0))
        hub.publish({"type": "speed", "speed": random.randint(55, 78),
                     "units": CONFIG["units"],
                     "ts": datetime.now().strftime("%H:%M:%S")})


@asynccontextmanager
async def lifespan(app: FastAPI):
    if CONFIG["demo"]:
        task = asyncio.create_task(_demo_loop())
    else:
        pwd = CONFIG["pwd"] if CONFIG["pwd"] is not None else load_password(None)
        radar = PocketRadar(pwd=pwd, units=CONFIG["units"],
                            speak=CONFIG["speak"], on_event=hub.publish)
        task = asyncio.create_task(radar.run_forever(CONFIG["address"]))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_text(json.dumps(hub.snapshot()))
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("cmd") == "reset":
                hub.reset()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--address", help="BLE address (macOS: CoreBluetooth UUID)")
    ap.add_argument("--pwd", help="9-byte pairing password as hex", default=None)
    ap.add_argument("--kph", action="store_true", help="assume kph units")
    ap.add_argument("--speak", action="store_true",
                    help="also speak speeds on the server's speakers (default: off)")
    ap.add_argument("--demo", action="store_true",
                    help="fake readings instead of connecting to a radar")
    args = ap.parse_args()

    pwd = None
    if not args.demo:
        pwd = load_password(args.pwd)
        if len(pwd) != 9:
            raise SystemExit("password must be 9 bytes (18 hex chars)")

    CONFIG.update(address=args.address, pwd=pwd, demo=args.demo,
                  units="kph" if args.kph else "mph", speak=args.speak)
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
