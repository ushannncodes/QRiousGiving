#!/usr/bin/env python3
"""leap_sender.py — runs on the PC/Mac with the Leap Motion attached, NOT
on the Pi.

Why this exists: Leap Motion Gen 1's official tracking service (the old
"Leap Motion"/v2 app, or Orion) was only ever shipped for x86/x86_64
Windows, macOS, and Ubuntu — Ultraleap never released an ARM/Raspberry Pi
build. So the Pi can't run hand tracking itself; instead, plug the Leap
into a regular PC/Mac that CAN run the real tracking software, and this
script forwards a compact hand/finger JSON over UDP to the Pi, which just
draws it (leap_receiver.py -> attract_leap_shadow.py).

Prerequisites on THIS machine (the PC/Mac, not the Pi):
  - Leap Motion tracking software installed and running (tray icon showing
    the sensor is actively tracking), sensor plugged in.
  - `pip install websocket-client` (see requirements-sender.txt in this dir).

*** UNVERIFIED PROTOCOL WARNING ***
This connects to the tracking service's legacy WebSocket "Skeletal Tracking
Model" API (documented at ws://127.0.0.1:6437/v6.json — this is the same
interface the old Leap.js browser SDK used, so no native/compiled Leap SDK
bindings are needed). The frame JSON shape assumed below (`hands[].type`,
`hands[].palmPosition`, `pointables[].handId`, `pointables[].type`,
`pointables[].bones[].prevJoint/nextJoint`) is based on that documented
protocol, but has NOT been tested against a live service from here. Run
with --dump-raw FIRST to print real frames from your machine and confirm
the field names actually match before trusting the drawn output — if they
don't, adjust `_extract_frame()` below to match what you actually see.

Usage:
    python3 leap_sender.py --dump-raw                 # inspect real JSON first
    python3 leap_sender.py --pi-host 192.168.1.42      # then start streaming

Env vars (all optional, CLI flags below take precedence):
  LEAP_WS_URL     default ws://127.0.0.1:6437/v6.json
  LEAP_PI_HOST    Pi's IP on the LAN (required, no default)
  LEAP_PI_PORT    default 5566
"""

import argparse
import json
import os
import socket
import time

import websocket  # pip install websocket-client

FINGER_TYPES = {0: "thumb", 1: "index", 2: "middle", 3: "ring", 4: "pinky"}


def _extract_frame(msg: dict) -> dict | None:
    """Compact a raw Leap frame message down to the schema
    attract_leap_shadow.py expects. Returns None for messages with no
    usable hand data (e.g. the initial version/handshake message)."""
    raw_hands = msg.get("hands")
    if not raw_hands:
        return None

    pointables_by_hand: dict = {}
    for p in msg.get("pointables", []):
        pointables_by_hand.setdefault(p.get("handId"), []).append(p)

    hands_out = []
    for h in raw_hands:
        palm = h.get("palmPosition")
        htype = h.get("type")
        if not palm or htype not in ("left", "right"):
            continue

        fingers = {}
        for p in pointables_by_hand.get(h.get("id"), []):
            fname = FINGER_TYPES.get(p.get("type"))
            bones = p.get("bones")
            if fname is None or not bones or len(bones) < 4:
                continue
            joints = [bones[0]["prevJoint"][:2]]
            for b in bones:
                joints.append(b["nextJoint"][:2])
            fingers[fname] = {"joints": joints}

        if len(fingers) < 3:
            # Not enough tracked fingers to draw a legible hand this frame.
            continue

        hands_out.append({"type": htype, "palm": palm[:2], "fingers": fingers})

    return {"hands": hands_out} if hands_out else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leap-ws", default=os.getenv("LEAP_WS_URL", "ws://127.0.0.1:6437/v6.json"))
    ap.add_argument("--pi-host", default=os.getenv("LEAP_PI_HOST"))
    ap.add_argument("--pi-port", type=int, default=int(os.getenv("LEAP_PI_PORT", "5566")))
    ap.add_argument("--dump-raw", action="store_true",
                     help="print raw frame JSON from the Leap service and exit — use this first")
    args = ap.parse_args()

    if not args.dump_raw and not args.pi_host:
        ap.error("--pi-host (or LEAP_PI_HOST) is required unless using --dump-raw")

    print(f"leap_sender: connecting to {args.leap_ws} ...")
    ws = websocket.create_connection(args.leap_ws, timeout=5)

    # Ask the service to keep streaming even though this script has no
    # window/focus of its own. Documented legacy protocol behavior — if
    # your service ignores/ doesn't need this, it's a harmless extra message.
    try:
        ws.send(json.dumps({"background": True, "optimizeHMD": False}))
    except Exception as e:
        print(f"leap_sender: warm-up message failed (continuing anyway): {e}")

    sock = None
    if not args.dump_raw:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(f"leap_sender: streaming to {args.pi_host}:{args.pi_port}")

    frame_count = 0
    last_stats = time.time()

    try:
        while True:
            raw = ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)

            if args.dump_raw:
                print(json.dumps(msg, indent=2))
                continue

            frame = _extract_frame(msg)
            if frame is None:
                continue

            sock.sendto(json.dumps(frame).encode(), (args.pi_host, args.pi_port))
            frame_count += 1

            if time.time() - last_stats >= 5.0:
                print(f"leap_sender: {frame_count / 5.0:.1f} frames/sec sent")
                frame_count = 0
                last_stats = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        ws.close()
        if sock:
            sock.close()
        print("leap_sender: exited cleanly")


if __name__ == "__main__":
    main()
