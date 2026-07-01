#!/usr/bin/env python3
"""attract_ripple.py — Option 2: ripple/wave attract display for QRiousGiving.

Reads /tmp/cam_state.json (written by cam_v2.py), same as attract_v2.py.
Instead of drawing a silhouette, spawns concentric rings that expand outward
from the person's tracked position — no attempt at a human shape, just a
motion-based "presence" reaction that should read as elegant even at 28x28.

A new ripple spawns on first detection, then again every RIPPLE_INTERVAL
seconds while the person stays in frame (following their live x/y), so
someone walking past sees a trail of rings drifting with them. Rings expire
once they grow past the display's diagonal.

Env vars (all optional, same names as attract_v2.py where applicable):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480)
  HUSKYLENS_FRAME_H same, height (default 480)
  RIPPLE_SPEED      ring growth, display-units/second (default 14.0)
  RIPPLE_INTERVAL   seconds between new ripples while active (default 0.6)
  RIPPLE_WIDTH      ring thickness in display-units (default 0.9)
"""

import json
import os
import time
import logging
import signal as _signal

import serial
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH   = os.getenv("CAM_SIGNAL_PATH",  "/tmp/cam_state.json")
SERIAL_PORT   = os.getenv("FLIPDOT_SERIAL",    "/dev/ttyS0")
BAUD_RATE     = int(os.getenv("FLIPDOT_BAUD",  "57600"))
STALE_THRESH  = float(os.getenv("CAM_STALE_SECS", "2.0"))
POLL_INTERVAL = float(os.getenv("ATTRACT_POLL", "0.1"))
FRAME_W       = int(os.getenv("HUSKYLENS_FRAME_W", "480"))
FRAME_H       = int(os.getenv("HUSKYLENS_FRAME_H", "480"))

RIPPLE_SPEED    = float(os.getenv("RIPPLE_SPEED", "14.0"))
RIPPLE_INTERVAL = float(os.getenv("RIPPLE_INTERVAL", "0.6"))
RIPPLE_WIDTH    = float(os.getenv("RIPPLE_WIDTH", "0.9"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

MAX_COORD = 2000
MAX_RADIUS = (DISPLAY_W ** 2 + DISPLAY_H ** 2) ** 0.5


def _blank_frame() -> Image.Image:
    return Image.new("L", (DISPLAY_W, DISPLAY_H), 255)


def _clean_point(pt):
    if not pt:
        return None
    x, y = pt
    if x == 0 and y == 0:
        return None
    if not (0 <= x <= MAX_COORD and 0 <= y <= MAX_COORD):
        return None
    return (x, y)


def _to_canvas(pt):
    x, y = pt
    return (x / FRAME_W) * DISPLAY_W, (y / FRAME_H) * DISPLAY_H


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _person_point(landmarks: dict, bbox: dict | None):
    """Best-effort (x, y) canvas position for the person, preferring
    shoulder midpoint (steadier for "where they are") and falling back to
    the bbox center if shoulders aren't tracked this frame."""
    lsh = _clean_point(tuple(landmarks.get("lshoulder", (0, 0)))) if landmarks else None
    rsh = _clean_point(tuple(landmarks.get("rshoulder", (0, 0)))) if landmarks else None
    if lsh and rsh:
        lsh_c, rsh_c = _to_canvas(lsh), _to_canvas(rsh)
        return ((lsh_c[0] + rsh_c[0]) / 2, (lsh_c[1] + rsh_c[1]) / 2)

    if bbox and bbox.get("x") is not None and bbox.get("y") is not None:
        cx = bbox["x"] + bbox.get("width", 0) / 2
        cy = bbox["y"] + bbox.get("height", 0) / 2
        pt = _clean_point((cx, cy))
        if pt:
            return _to_canvas(pt)

    return None


def _draw_ripples(ripples: list[dict], now: float) -> Image.Image:
    canvas = _blank_frame()
    draw = ImageDraw.Draw(canvas)
    for r in ripples:
        radius = (now - r["born"]) * RIPPLE_SPEED
        if radius <= 0 or radius > MAX_RADIUS:
            continue
        cx, cy = r["x"], r["y"]
        draw.ellipse(
            [cx - radius - RIPPLE_WIDTH / 2, cy - radius - RIPPLE_WIDTH / 2,
             cx + radius + RIPPLE_WIDTH / 2, cy + radius + RIPPLE_WIDTH / 2],
            outline=0, width=max(1, round(RIPPLE_WIDTH)),
        )
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

    log.info("attract_ripple: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    ripples: list[dict] = []
    was_active = False
    last_spawn = 0.0

    while running:
        t0 = time.time()

        state     = _read_state()
        active    = False
        landmarks = None
        bbox      = None

        if state:
            age = time.time() - state.get("ts", 0)
            if age < STALE_THRESH and state.get("active", False):
                active    = True
                landmarks = state.get("landmarks")
                bbox      = state.get("bbox")

        if active:
            point = _person_point(landmarks or {}, bbox)
            if point and (not was_active or t0 - last_spawn >= RIPPLE_INTERVAL):
                ripples.append({"x": point[0], "y": point[1], "born": t0})
                last_spawn = t0
            was_active = True
        else:
            was_active = False

        ripples = [r for r in ripples if (t0 - r["born"]) * RIPPLE_SPEED <= MAX_RADIUS]

        if ripples:
            _send_frame(ser, _draw_ripples(ripples, t0))
        elif was_active is False and ripples == []:
            # nothing left to animate; blank once and idle
            pass

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_ripple: exited cleanly")


if __name__ == "__main__":
    main()
