#!/usr/bin/env python3
"""attract_pulse.py — Option 7: presence-pulse attract display.

Reads /tmp/cam_state.json (written by cam_v2.py), same as attract_v2.py /
attract_ripple.py. Draws a small cluster of dots centered on the person's
tracked position that gently breathes (grows/shrinks on a sine cycle) and
scales up the closer they are — pure ambient "presence" feedback, no
attempt at a body shape at all.

Env vars (all optional, same names as attract_v2.py where applicable):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480)
  HUSKYLENS_FRAME_H same, height (default 480)
  PULSE_PERIOD      breathing cycle length in seconds (default 1.8)
  PULSE_AMPLITUDE   how much the radius grows/shrinks each breath, in
                     display-units (default 1.6)
  PULSE_MIN_RADIUS  smallest cluster radius (far away / just entered) (default 2.0)
  PULSE_MAX_RADIUS  largest cluster radius (very close) (default 6.5)
  PULSE_DOT_RADIUS  size of each individual dot in the cluster (default 0.6)
"""

import json
import math
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

PULSE_PERIOD     = float(os.getenv("PULSE_PERIOD", "1.8"))
PULSE_AMPLITUDE  = float(os.getenv("PULSE_AMPLITUDE", "1.6"))
PULSE_MIN_RADIUS = float(os.getenv("PULSE_MIN_RADIUS", "2.0"))
PULSE_MAX_RADIUS = float(os.getenv("PULSE_MAX_RADIUS", "6.5"))
PULSE_DOT_RADIUS = float(os.getenv("PULSE_DOT_RADIUS", "0.6"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

MAX_COORD = 2000

# Fixed relative offsets (unit circle, roughly even spread) for the dot
# cluster — scaled by the live pulse radius each frame. Kept static so the
# cluster's "shape" doesn't jitter, only its size breathes/reacts.
_N_DOTS = 12
_CLUSTER_OFFSETS = [
    (math.cos(2 * math.pi * i / _N_DOTS + 0.4), math.sin(2 * math.pi * i / _N_DOTS + 0.4))
    for i in range(_N_DOTS)
]
# A few inner points so the cluster reads as a soft blob, not a hollow ring.
_CLUSTER_OFFSETS += [(0.0, 0.0), (0.35, 0.2), (-0.3, -0.25), (-0.15, 0.4)]


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


def _proximity_radius(bbox: dict | None) -> float:
    """Bigger tracked bbox height ~= closer to camera. Map that to a base
    cluster radius between PULSE_MIN_RADIUS (far/just arrived) and
    PULSE_MAX_RADIUS (close), before the breathing oscillation is applied."""
    if not bbox or not bbox.get("height"):
        return PULSE_MIN_RADIUS
    frac = _clamp(bbox["height"] / FRAME_H, 0.0, 1.0)
    return PULSE_MIN_RADIUS + frac * (PULSE_MAX_RADIUS - PULSE_MIN_RADIUS)


def _draw_pulse(center, base_radius: float, t: float) -> Image.Image:
    canvas = _blank_frame()
    draw = ImageDraw.Draw(canvas)

    breath = math.sin(2 * math.pi * t / PULSE_PERIOD)
    radius = max(0.5, base_radius + breath * PULSE_AMPLITUDE)

    cx, cy = center
    for ox, oy in _CLUSTER_OFFSETS:
        px, py = cx + ox * radius, cy + oy * radius
        draw.ellipse(
            [px - PULSE_DOT_RADIUS, py - PULSE_DOT_RADIUS, px + PULSE_DOT_RADIUS, py + PULSE_DOT_RADIUS],
            fill=0,
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

    log.info("attract_pulse: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    was_active = False
    t_start = time.time()

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
            if point:
                base_radius = _proximity_radius(bbox)
                _send_frame(ser, _draw_pulse(point, base_radius, t0 - t_start))
                was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_pulse: exited cleanly")


if __name__ == "__main__":
    main()
