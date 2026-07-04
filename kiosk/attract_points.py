#!/usr/bin/env python3
"""attract_points.py — Option 3: sparse point-cloud attract display.

Reads /tmp/cam_state.json (written by cam_v2.py), same as attract_v2.py /
attract_ripple.py. Instead of a filled silhouette, draws only a handful of
dots at the person's actual tracked landmark positions (head, shoulders,
hips) — no fill, no outline connecting them. At 28x28 a solid shape reads
as a blocky cartoon; a few well-placed points read as a gesture sketch.

Unlike attract_v2.py's silhouette, this uses landmarks at their real
tracked scale (no exaggerated proportions) since the point is negative
space, not legibility-as-a-body-shape.

Env vars (all optional, same names as attract_v2.py where applicable):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480)
  HUSKYLENS_FRAME_H same, height (default 480)
  POINT_RADIUS      dot radius in display-units (default 0.6)
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
POINT_RADIUS  = float(os.getenv("POINT_RADIUS", "0.9"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

MAX_COORD = 2000

# Which landmarks to draw as points — deliberately sparse. Order doesn't
# matter since each is drawn independently with no connecting lines.
POINT_KEYS = [
    "nose", "leye", "reye",
    "lshoulder", "rshoulder",
    "lelbow", "relbow",
    "lhip", "rhip",
    "lknee", "rknee",
]


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


def _draw_points(landmarks: dict) -> Image.Image:
    canvas = _blank_frame()
    draw = ImageDraw.Draw(canvas)
    for key in POINT_KEYS:
        pt = _clean_point(tuple(landmarks.get(key, (0, 0))))
        if not pt:
            continue
        cx, cy = _to_canvas(pt)
        draw.ellipse(
            [cx - POINT_RADIUS, cy - POINT_RADIUS, cx + POINT_RADIUS, cy + POINT_RADIUS],
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

    log.info("attract_points: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    was_active = False

    while running:
        t0 = time.time()

        state     = _read_state()
        active    = False
        landmarks = None

        if state:
            age = time.time() - state.get("ts", 0)
            if age < STALE_THRESH and state.get("active", False):
                active    = True
                landmarks = state.get("landmarks")

        if active and landmarks:
            _send_frame(ser, _draw_points(landmarks))
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_points: exited cleanly")


if __name__ == "__main__":
    main()
