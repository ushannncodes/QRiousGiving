#!/usr/bin/env python3
"""attract_v2.py — Silhouette attract display for QRiousGiving v2.

Reads /tmp/cam_state.json (written by cam_v2.py). While a person is
detected, draws a live silhouette from their HuskyLens pose landmarks
(nose, shoulders, hips) — head + torso trapezoid, sized and positioned
from their actual tracked body each frame — instead of a fixed icon.

Runs as a long-lived subprocess managed by run_kiosk / orchestrator.
Exits cleanly on SIGINT / SIGTERM and blanks the display.

Env vars (all optional):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480) —
  HUSKYLENS_FRAME_H HuskyLens doesn't report its working resolution, these are an
                     empirical approximation used only for proportions, not a hard
                     calibration; retune if the silhouette looks mis-scaled.
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

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7   # rows per panel
# Panel addresses — must match your hardware. Same order as qr_works.py.
PANEL_ADDRS = [0x00, 0x01, 0x02, 0x03]

# Max plausible raw landmark coordinate. HuskyLens occasionally returns a
# bit-corrupted value under I2C timing pressure (same transport issue
# already worked around for Face/Hand results in DFRobot_HuskyLens.py) —
# seen live as e.g. nose=(33194, 178) instead of ~(440, 178). Anything past
# this is treated as "not detected" rather than drawn.
MAX_COORD = 2000


def _blank_frame() -> Image.Image:
    return Image.new("L", (DISPLAY_W, DISPLAY_H), 255)


def _clean_point(pt):
    """(0, 0) means HuskyLens didn't report this landmark; reject
    out-of-range values too (transport glitch, see MAX_COORD)."""
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


def _draw_silhouette(landmarks: dict) -> Image.Image:
    """Head + torso trapezoid from pose landmarks, scaled/positioned from
    the person's actual tracked body. Falls back gracefully as landmarks
    drop out: extrapolates hips below the shoulders if missing, extrapolates
    the head above the shoulder midpoint if the nose isn't visible."""
    canvas = _blank_frame()

    lsh = _clean_point(tuple(landmarks.get("lshoulder", (0, 0))))
    rsh = _clean_point(tuple(landmarks.get("rshoulder", (0, 0))))
    if not (lsh and rsh):
        return canvas  # not enough signal to draw anything meaningful

    lsh_c, rsh_c = _to_canvas(lsh), _to_canvas(rsh)
    shoulder_w = math.hypot(rsh_c[0] - lsh_c[0], rsh_c[1] - lsh_c[1])
    mid_sh = ((lsh_c[0] + rsh_c[0]) / 2, (lsh_c[1] + rsh_c[1]) / 2)

    lhip = _clean_point(tuple(landmarks.get("lhip", (0, 0))))
    rhip = _clean_point(tuple(landmarks.get("rhip", (0, 0))))
    if lhip and rhip:
        lhip_c, rhip_c = _to_canvas(lhip), _to_canvas(rhip)
    else:
        torso_h = shoulder_w * 1.4
        lhip_c = (lsh_c[0], lsh_c[1] + torso_h)
        rhip_c = (rsh_c[0], rsh_c[1] + torso_h)

    draw = ImageDraw.Draw(canvas)
    draw.polygon([lsh_c, rsh_c, rhip_c, lhip_c], fill=0)

    nose = _clean_point(tuple(landmarks.get("nose", (0, 0))))
    head_c = _to_canvas(nose) if nose else (mid_sh[0], mid_sh[1] - shoulder_w * 0.6)
    head_r = max(1.0, shoulder_w * 0.32)
    draw.ellipse(
        [head_c[0] - head_r, head_c[1] - head_r, head_c[0] + head_r, head_c[1] + head_r],
        fill=0,
    )

    return canvas


def _image_to_panels(img: Image.Image) -> list[bytearray]:
    """Convert 28×28 PIL image to 4 panel byte arrays (28 bytes each, LSB = top row)."""
    pixels = img.load()
    panels = []
    for panel in range(NUM_PANELS):
        y_off = panel * PANEL_H
        data = bytearray(DISPLAY_W)
        for x in range(DISPLAY_W):
            col_byte = 0
            for y in range(PANEL_H):
                if pixels[x, y + y_off] == 255:  # white → bit 1
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
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    log.info("attract_v2: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
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
            # Continuously redraw — the shape varies every frame as the
            # person moves, unlike the old fixed-size icons.
            _send_frame(ser, _draw_silhouette(landmarks))
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_v2: exited cleanly")


if __name__ == "__main__":
    main()
