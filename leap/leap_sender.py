#!/usr/bin/env python3
"""leap_sender.py — runs on the PC/Mac with the Leap Motion attached, NOT
on the Pi.

Why this exists: Leap Motion Gen 1's tracking software was only ever
shipped for x86/x86_64 Windows, macOS, and Linux — Ultraleap never
released an ARM/Raspberry Pi build. So the Pi can't run hand tracking
itself; instead, plug the Leap into a regular PC/Mac running Ultraleap's
official tracking service (the "Ultraleap Hand Tracking"/Gemini app —
confirmed working with Gen 1 hardware even on Apple Silicon), and this
script forwards a compact hand/finger JSON over UDP to the Pi, which just
draws it (leap_receiver.py -> attract_leap_shadow.py).

*** Why this isn't a plain websocket client ***
An earlier version of this script connected to the tracking service's
legacy WebSocket API (ws://127.0.0.1:6437) — that was Leap Motion Orion
(V4)'s interface, used by the old Leap.js browser SDK. Gemini (V5, what
"Ultraleap Hand Tracking"/Ultraleap Control Panel installs) REMOVED that
websocket entirely, which is why connecting to port 6437 fails with
"Connection refused" on a Gemini install — confirmed via Ultraleap's own
docs/changelog, not a bug on your end. Gemini's supported way to read
tracking data from Python is the official `leapc-python-bindings` package
(https://github.com/ultraleap/leapc-python-bindings), which this script
now uses. The class/attribute names below (`leap.Connection`,
`leap.Listener`, `hand.palm.position`, `hand.thumb/index/middle/ring/pinky`,
`digit.bones` -> `.metacarpal/.proximal/.intermediate/.distal`,
`bone.prev_joint/.next_joint`) are taken directly from that package's own
example (`examples/tracking_event_example.py`) and source
(`leapc-python-api/src/leap/datatypes.py`), not guessed.

Prerequisites on THIS machine (the PC/Mac, not the Pi):
  1. Ultraleap Hand Tracking service installed and running (Ultraleap
     Control Panel showing "Tracking: Active" with the sensor's IR feed
     visible), sensor plugged in.
  2. Python 3.8+ — leapc-python-bindings ships a pre-compiled module for
     3.8; older/newer interpreters may need the "custom compilation" path
     in its README (needs a C compiler + the Gemini SDK already installed).
     Check with `python3 --version`; if it's older than 3.8 (e.g. the
     system python3.7 on older macOS installs), install a newer Python
     (e.g. `brew install python@3.11`) and use that instead.
  3. Clone and install the bindings (NOT a plain `pip install` package):
       git clone https://github.com/ultraleap/leapc-python-bindings.git
       cd leapc-python-bindings
       python3 -m venv venv && source venv/bin/activate
       pip install -r requirements.txt
       pip install -e leapc-python-api
     The default SDK path it looks for is
     "/Applications/Ultraleap Hand Tracking.app/Contents/LeapSDK" on
     macOS — if yours differs, set LEAPSDK_INSTALL_LOCATION.
  4. Sanity-check with THEIR example first (simplest possible test):
       python3 leapc-python-bindings/examples/tracking_event_example.py
     Move your hand over the sensor and confirm palm positions print.
     Only once that works, copy/point this script's venv at the same
     environment and run it instead.

Usage:
    python3 leap_sender.py --dump-raw                 # inspect real data first
    python3 leap_sender.py --pi-host 192.168.1.42      # then start streaming

Env vars (all optional, CLI flags below take precedence):
  LEAP_PI_HOST    Pi's IP on the LAN (required, no default)
  LEAP_PI_PORT    default 5566
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time

import leap

FINGER_NAMES = ["thumb", "index", "middle", "ring", "pinky"]
STATS_INTERVAL = 5.0


def _hand_type(hand) -> str:
    return "left" if str(hand.type) == "HandType.Left" else "right"


def _extract_hand(hand) -> dict:
    fingers = {}
    for name in FINGER_NAMES:
        digit = getattr(hand, name)
        bones = digit.bones  # [metacarpal, proximal, intermediate, distal]
        joints = [[bones[0].prev_joint.x, bones[0].prev_joint.y]]
        for b in bones:
            joints.append([b.next_joint.x, b.next_joint.y])
        fingers[name] = {"joints": joints}
    palm = hand.palm.position
    return {"type": _hand_type(hand), "palm": [palm.x, palm.y], "fingers": fingers}


class _ForwardingListener(leap.Listener):
    """Turns each tracking frame into the compact JSON schema
    attract_leap_shadow.py expects and either prints it (--dump-raw) or
    UDP-sends it to the Pi. Runs on the bindings' own event-callback
    thread, not the main thread."""

    def __init__(self, sock, pi_addr, dump_raw):
        super().__init__()
        self.sock = sock
        self.pi_addr = pi_addr
        self.dump_raw = dump_raw
        self.frame_count = 0
        self.last_stats = time.time()

    def on_connection_event(self, event):
        print("leap_sender: connected to tracking service")

    def on_device_event(self, event):
        try:
            with event.device.open():
                info = event.device.get_info()
        except leap.LeapCannotOpenDeviceError:
            info = event.device.get_info()
        print(f"leap_sender: found device {info.serial}")

    def on_tracking_event(self, event):
        hands = [_extract_hand(h) for h in event.hands]
        frame = {"hands": hands}

        if self.dump_raw:
            print(json.dumps(frame, indent=2))
            return

        if not hands:
            return
        self.sock.sendto(json.dumps(frame).encode(), self.pi_addr)
        self.frame_count += 1

        now = time.time()
        if now - self.last_stats >= STATS_INTERVAL:
            print(f"leap_sender: {self.frame_count / STATS_INTERVAL:.1f} frames/sec sent")
            self.frame_count = 0
            self.last_stats = now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pi-host", default=os.getenv("LEAP_PI_HOST"))
    ap.add_argument("--pi-port", type=int, default=int(os.getenv("LEAP_PI_PORT", "5566")))
    ap.add_argument("--dump-raw", action="store_true",
                     help="print extracted hand/finger data instead of sending it — use this first")
    args = ap.parse_args()

    if not args.dump_raw and not args.pi_host:
        ap.error("--pi-host (or LEAP_PI_HOST) is required unless using --dump-raw")

    sock = None
    pi_addr = None
    if not args.dump_raw:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pi_addr = (args.pi_host, args.pi_port)
        print(f"leap_sender: streaming to {args.pi_host}:{args.pi_port}")

    listener = _ForwardingListener(sock, pi_addr, args.dump_raw)
    connection = leap.Connection()
    connection.add_listener(listener)

    print("leap_sender: opening connection to Ultraleap tracking service...")
    try:
        with connection.open():
            connection.set_tracking_mode(leap.TrackingMode.Desktop)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if sock:
            sock.close()
        print("leap_sender: exited cleanly")


if __name__ == "__main__":
    main()
