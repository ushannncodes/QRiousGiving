#!/usr/bin/env python3
"""attract_leap_shadow.py — full hand-outline shadow display, driven by Leap
Motion hand/finger tracking.

Reads /tmp/leap_state.json (written either by leap_receiver.py from a real
Leap Motion attached to another PC/Mac, or by synthetic_hand_test.py for a
hardware-free smoke test) and draws each present hand as a solid shadow —
a filled palm plus every finger as a thick stroke through its real bent/
straight joint positions — same shape language as attract_shadow.py's
full-body silhouette, but for hands.

Raw tracking data is jittery frame-to-frame (dropped joints, sensor noise,
lossy UDP over the network path); this applies exponential smoothing to
every joint position *before* projecting to canvas space, which is what
makes the on-panel motion read as smooth hand motion instead of a shaky
blob. Lower LEAP_SMOOTH_ALPHA = smoother but laggier; higher = snappier but
twitchier.

Usage against the simulator:
    python3 simulator/flipdot_simulator.py
    export FLIPDOT_SERIAL=/tmp/flipdot_vserial
    python3 leap/synthetic_hand_test.py &      # or leap_receiver.py for real data
    python3 leap/attract_leap_shadow.py

Env vars (all optional):
  LEAP_SIGNAL_PATH    default /tmp/leap_state.json
  FLIPDOT_SERIAL      default /dev/ttyS0
  FLIPDOT_BAUD        default 57600
  LEAP_STALE_SECS     max age of the state file before treating as no data (default 0.35)
  LEAP_MISS_GRACE_SEC keep last known hand across brief per-hand dropouts (default 0.15)
  ATTRACT_POLL        loop interval in seconds (default 0.05 = 20Hz; the flipdot
                      serial link has headroom well above this, see README)
  LEAP_SMOOTH_ALPHA   EMA blend factor per joint, 0-1 (default 0.4)
  LEAP_X_RANGE_MM     maps [-X, +X] mm to canvas [0, 28] (default 140)
  LEAP_Y_MIN_MM       mm height mapped to canvas bottom (default 60)
  LEAP_Y_MAX_MM       mm height mapped to canvas top (default 320)
  FINGER_WIDTH        stroke width for fingers in display-units (default 1.6)
"""

from __future__ import annotations

import json
import os
import time
import logging
import signal as _signal

import serial
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH    = os.getenv("LEAP_SIGNAL_PATH", "/tmp/leap_state.json")
SERIAL_PORT    = os.getenv("FLIPDOT_SERIAL",   "/dev/ttyS0")
BAUD_RATE      = int(os.getenv("FLIPDOT_BAUD", "57600"))
STALE_THRESH   = float(os.getenv("LEAP_STALE_SECS", "0.35"))
MISS_GRACE_SEC = float(os.getenv("LEAP_MISS_GRACE_SEC", "0.15"))
POLL_INTERVAL  = float(os.getenv("ATTRACT_POLL", "0.05"))
SMOOTH_ALPHA   = float(os.getenv("LEAP_SMOOTH_ALPHA", "0.4"))
X_RANGE_MM     = float(os.getenv("LEAP_X_RANGE_MM", "140"))
Y_MIN_MM       = float(os.getenv("LEAP_Y_MIN_MM", "60"))
Y_MAX_MM       = float(os.getenv("LEAP_Y_MAX_MM", "320"))
FINGER_WIDTH   = float(os.getenv("FINGER_WIDTH", "1.6"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

FINGER_NAMES = ["thumb", "index", "middle", "ring", "pinky"]


def _blank_frame() -> Image.Image:
    return Image.new("L", (DISPLAY_W, DISPLAY_H), 255)


def _to_canvas(pt):
    x, y = pt
    cx = ((x + X_RANGE_MM) / (2 * X_RANGE_MM)) * DISPLAY_W
    cy = ((Y_MAX_MM - y) / (Y_MAX_MM - Y_MIN_MM)) * DISPLAY_H
    return (cx, cy)


def _thick_line(draw, points, width):
    """Draw.line with round joints — PIL's line width doesn't round the
    ends/joints on its own, which leaves visible gaps/notches at knuckles;
    capping each segment with a filled circle keeps the finger reading as
    one continuous stroke instead of a jointed rod. Same trick as
    attract_shadow.py's limb drawing."""
    pts = [p for p in points if p is not None]
    if len(pts) < 2:
        return
    draw.line(pts, fill=0, width=round(width))
    r = width / 2
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=0)


class _HandSmoother:
    """Exponential smoothing over a single hand's palm + finger joints in
    mm-space (before canvas projection, so the alpha means the same thing
    regardless of canvas scale). A hand's first appearance snaps straight to
    the raw position instead of blending from zero, so it doesn't visibly
    slide in from a corner the moment it's detected."""

    def __init__(self):
        self.palm = None
        self.fingers = None  # {name: [pt, pt, pt, pt, pt]}
        self.last_seen = 0.0

    def update(self, raw_hand, now):
        palm = raw_hand["palm"]
        fingers = raw_hand["fingers"]

        if self.palm is None:
            self.palm = list(palm)
            self.fingers = {n: [list(p) for p in fingers[n]["joints"]] for n in FINGER_NAMES if n in fingers}
        else:
            a = SMOOTH_ALPHA
            self.palm[0] += (palm[0] - self.palm[0]) * a
            self.palm[1] += (palm[1] - self.palm[1]) * a
            for name in FINGER_NAMES:
                if name not in fingers:
                    continue
                raw_joints = fingers[name]["joints"]
                if name not in self.fingers or len(self.fingers[name]) != len(raw_joints):
                    self.fingers[name] = [list(p) for p in raw_joints]
                    continue
                for j, (rx, ry) in enumerate(raw_joints):
                    sj = self.fingers[name][j]
                    sj[0] += (rx - sj[0]) * a
                    sj[1] += (ry - sj[1]) * a

        self.last_seen = now

    def stale(self, now):
        return (now - self.last_seen) > MISS_GRACE_SEC


_smoothers: dict[str, _HandSmoother] = {}


def _update_smoothers(hands, now):
    seen_types = set()
    for h in hands:
        htype = h.get("type")
        if not htype or not h.get("palm") or not h.get("fingers"):
            continue
        seen_types.add(htype)
        _smoothers.setdefault(htype, _HandSmoother()).update(h, now)

    # Drop smoothers for hands that have been gone longer than the grace
    # window, so a hand that walked away doesn't linger forever.
    for htype in list(_smoothers.keys()):
        if htype not in seen_types and _smoothers[htype].stale(now):
            del _smoothers[htype]


def _draw_hands() -> Image.Image:
    canvas = _blank_frame()
    draw = ImageDraw.Draw(canvas)

    for smoother in _smoothers.values():
        if not smoother.fingers:
            continue
        palm_c = _to_canvas(smoother.palm)

        # Palm: a fan polygon from the palm center out to each finger's base
        # joint, in finger order — reads as a filled palm at 28x28 without
        # needing a wrist/arm point we can't reliably get from the tracker.
        bases = [_to_canvas(smoother.fingers[n][0]) for n in FINGER_NAMES if n in smoother.fingers]
        if len(bases) >= 3:
            draw.polygon([palm_c] + bases, fill=0)

        for name in FINGER_NAMES:
            joints = smoother.fingers.get(name)
            if not joints:
                continue
            _thick_line(draw, [_to_canvas(p) for p in joints], FINGER_WIDTH)

    return canvas


def _image_to_panels(img: Image.Image) -> list[bytearray]:
    pixels = img.load()
    panels = []
    for panel in range(NUM_PANELS):
        y_off = panel * PANEL_H
        data = bytearray(DISPLAY_W)
        for x in range(DISPLAY_W):
            col_byte = 0
            for y in range(PANEL_H):
                if pixels[x, y + y_off] == 255:
                    col_byte |= (1 << y)
            data[x] = col_byte
        panels.append(data)
    return panels


def _send_frame(ser: serial.Serial, img: Image.Image) -> None:
    for addr, data in zip(PANEL_ADDRS, _image_to_panels(img)):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()


def _read_state() -> dict | None:
    try:
        with open(SIGNAL_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def main() -> None:
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    log.info("attract_leap_shadow: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    was_active = False

    while running:
        t0 = time.time()

        state = _read_state()
        hands = []
        if state:
            age = t0 - state.get("ts", 0)
            if age < STALE_THRESH:
                hands = state.get("hands", [])

        _update_smoothers(hands, t0)

        if _smoothers:
            _send_frame(ser, _draw_hands())
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_leap_shadow: exited cleanly")


if __name__ == "__main__":
    main()
