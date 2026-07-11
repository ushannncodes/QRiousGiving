#!/usr/bin/env python3
"""leap_sender.py â runs on the Beelink mini PC (Windows/Linux/Mac).

Reads hand tracking data from a Leap Motion Controller via Ultraleap's
Gemini tracking service + LeapC Python bindings, reshapes it into the same
21-point wrist-first landmark layout QRiousGiving already uses for
HuskyLens hand recognition (see kiosk/hi5_final.py's `_LM` /
`is_open_palm()`), and streams it over UDP to the Raspberry Pi as JSON.

Landmark order (matches MediaPipe / HuskyLens ALGORITHM_HAND_RECOGNITION):
  0  wrist
  1-4   thumb   (CMC, MCP, IP, TIP)
  5-8   index   (MCP, PIP, DIP, TIP)
  9-12  middle  (MCP, PIP, DIP, TIP)
  13-16 ring    (MCP, PIP, DIP, TIP)
  17-20 pinky   (MCP, PIP, DIP, TIP)

2D projection note:
  Which two of Leap's native (x, y, z) axes get sent as each landmark's
  2D [a, b] pair depends entirely on how the controller is physically
  mounted, and is controlled by PROJECT_AXES (see below) rather than
  hardcoded, since this has already changed once (desk-flat, lens-up ->
  mounted vertically on the panel's right edge, lens facing outward) and
  will probably change again. Leap's native axes, controller at rest:
    x = along the controller's long edge
    y = perpendicular to the lensed face (the "up" direction when lying
        flat lens-up)
    z = along the controller's short edge
  When you change the physical mount, don't guess the new axis pairing
  from geometry alone — run this with a few PROJECT_AXES candidates and
  watch leap_visualizer.py's raw-capture pane on the Pi while moving your
  hand through the gestures you actually care about (up/down, toward/away
  from the sensor); pick whichever pairing makes the plotted shape spread
  out like an actual hand instead of compressing into a blob.

Setup (once, on the Beelink):
  1. Install Ultraleap Gemini V5 tracking software for Windows
     (https://leapmotion.ultraleap.com/, "Leap Motion Controller" download,
     not "Leap Motion Controller 2"). Plug in the controller and confirm
     it tracks in the bundled Ultraleap Control Panel / Visualizer.
  2. pip install --break-system-packages leapc-python-api
     (or: git clone https://github.com/ultraleap/leapc-python-bindings,
     then pip install -e leapc-python-api, if the prebuilt wheel doesn't
     match your Python version â see that repo's README).
  3. Run this script: python leap_sender.py
     Env vars:
       RPI_HOST        IP of the Raspberry Pi on your LAN (required)
       RPI_PORT        UDP port on the Pi (default 5111)
       SEND_HZ         max send rate, throttled from Leap's own ~90-120Hz
                        frame rate (default 30)
       PREFERRED_HAND  "right", "left", or "any" (default "any" â sends
                        whichever hand is tracked; if both are visible,
                        sends the most recently updated one)
       PROJECT_AXES    which two Leap axes become each landmark's [a, b],
                        comma-separated, each optionally "-"-prefixed to
                        flip its sign (default "x,z" â the original
                        desk-flat, lens-up mount). E.g. "x,y" or "-z,y".
"""

import json
import os
import socket
import time

import leap

RPI_HOST = os.environ["RPI_HOST"]  # no sane default â must point somewhere
RPI_PORT = int(os.getenv("RPI_PORT", "5111"))
SEND_HZ = float(os.getenv("SEND_HZ", "30"))
PREFERRED_HAND = os.getenv("PREFERRED_HAND", "any").lower()
MIN_INTERVAL = 1.0 / SEND_HZ

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _parse_axis(spec):
    sign = -1.0 if spec.startswith("-") else 1.0
    axis = spec.lstrip("-")
    if axis not in ("x", "y", "z"):
        raise ValueError(f"invalid axis {spec!r} in PROJECT_AXES — expected x, y, or z, optionally '-'-prefixed")
    return axis, sign


_axis_specs = [s.strip() for s in os.getenv("PROJECT_AXES", "x,z").split(",")]
if len(_axis_specs) != 2:
    raise ValueError(f"PROJECT_AXES must be two comma-separated axes, got {os.getenv('PROJECT_AXES')!r}")
_AXIS_A, _SIGN_A = _parse_axis(_axis_specs[0])
_AXIS_B, _SIGN_B = _parse_axis(_axis_specs[1])


def _pt(vec):
    """Project a Leap Vector(x, y, z) to the 2D plane we track finger
    geometry in, per PROJECT_AXES — see the module docstring."""
    return [getattr(vec, _AXIS_A) * _SIGN_A, getattr(vec, _AXIS_B) * _SIGN_B]


def hand_to_landmarks(hand):
    lm = [None] * 21
    lm[0] = _pt(hand.arm.next_joint)  # wrist

    finger_bases = {
        "thumb": 1,
        "index": 5,
        "middle": 9,
        "ring": 13,
        "pinky": 17,
    }
    for name, base in finger_bases.items():
        digit = getattr(hand, name)
        lm[base + 0] = _pt(digit.metacarpal.next_joint)   # MCP/CMC
        lm[base + 1] = _pt(digit.proximal.next_joint)     # PIP/MCP
        lm[base + 2] = _pt(digit.intermediate.next_joint) # DIP/IP
        lm[base + 3] = _pt(digit.distal.next_joint)       # TIP
    return lm


class SenderListener(leap.Listener):
    def __init__(self):
        self._last_sent = 0.0

    def on_connection_event(self, event):
        print("[leap_sender] connected to tracking service")

    def on_device_event(self, event):
        try:
            with event.device.open():
                info = event.device.get_info()
        except leap.LeapCannotOpenDeviceError:
            info = event.device.get_info()
        print(f"[leap_sender] device: {info.serial}")

    def on_tracking_event(self, event):
        now = time.time()
        if now - self._last_sent < MIN_INTERVAL:
            return  # throttle to SEND_HZ

        hands = list(event.hands)
        if not hands:
            return

        chosen = hands[0]
        if PREFERRED_HAND in ("left", "right") and len(hands) > 1:
            wanted = "HandType.Left" if PREFERRED_HAND == "left" else "HandType.Right"
            for h in hands:
                if str(h.type) == wanted:
                    chosen = h
                    break

        payload = {
            "ts": now,
            "hand_type": "left" if str(chosen.type) == "HandType.Left" else "right",
            "landmarks": hand_to_landmarks(chosen),
        }
        msg = json.dumps(payload).encode("utf-8")
        sock.sendto(msg, (RPI_HOST, RPI_PORT))
        self._last_sent = now


def main():
    print(f"[leap_sender] sending to {RPI_HOST}:{RPI_PORT} at up to {SEND_HZ}Hz, PROJECT_AXES={_axis_specs}")
    listener = SenderListener()
    connection = leap.Connection()
    connection.add_listener(listener)

    with connection.open():
        connection.set_tracking_mode(leap.TrackingMode.Desktop)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[leap_sender] stopping")


if __name__ == "__main__":
    main()
