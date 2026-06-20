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


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _draw_silhouette(landmarks: dict, bbox: dict | None) -> Image.Image:
    """Stylized head + torso trapezoid, positioned from the person's
    tracked shoulders/hips but *sized* off the overall detection bbox
    height rather than literal shoulder width.

    Real shoulder-width-to-height proportions are too narrow to read as
    "a person" at 28x28 — e.g. a real ~4%-of-frame shoulder span renders
    as a ~1px-wide sliver. bbox height is a much more stable reference
    (one box vs. two individually-noisy landmarks), and the proportions
    below are deliberately exaggerated (like the old static icon was) so
    the shape stays legible at any distance, while still tracking the
    person's real position/size live rather than snapping between presets.
    """
    canvas = _blank_frame()

    lsh = _clean_point(tuple(landmarks.get("lshoulder", (0, 0))))
    rsh = _clean_point(tuple(landmarks.get("rshoulder", (0, 0))))
    if not (lsh and rsh):
        return canvas  # not enough signal to draw anything meaningful

    lsh_c, rsh_c = _to_canvas(lsh), _to_canvas(rsh)
    mid_sh = ((lsh_c[0] + rsh_c[0]) / 2, (lsh_c[1] + rsh_c[1]) / 2)

    if bbox and bbox.get("height"):
        body_h = (bbox["height"] / FRAME_H) * DISPLAY_H
    else:
        body_h = DISPLAY_H * 0.6
    body_h = _clamp(body_h, 6.0, float(DISPLAY_H))

    lhip = _clean_point(tuple(landmarks.get("lhip", (0, 0))))
    rhip = _clean_point(tuple(landmarks.get("rhip", (0, 0))))
    if lhip and rhip:
        mid_hip = _to_canvas(lhip)
        rhip_c = _to_canvas(rhip)
        mid_hip = ((mid_hip[0] + rhip_c[0]) / 2, (mid_hip[1] + rhip_c[1]) / 2)
    else:
        mid_hip = (mid_sh[0], mid_sh[1] + body_h * 0.55)

    top_y, bot_y = mid_sh[1], mid_hip[1]
    if bot_y - top_y < 3.0:
        bot_y = top_y + max(3.0, body_h * 0.5)
    cx_top, cx_bot = mid_sh[0], mid_hip[0]

    torso_top_w = _clamp(body_h * 0.42, 5.0, 16.0)
    torso_bot_w = torso_top_w * 0.85
    head_r = _clamp(body_h * 0.20, 2.2, 6.0)

    draw = ImageDraw.Draw(canvas)
    draw.polygon(
        [
            (cx_top - torso_top_w / 2, top_y),
            (cx_top + torso_top_w / 2, top_y),
            (cx_bot + torso_bot_w / 2, bot_y),
            (cx_bot - torso_bot_w / 2, bot_y),
        ],
        fill=0,
    )

    head_cx, head_cy = cx_top, top_y - head_r * 1.2
    draw.ellipse(
        [head_cx - head_r, head_cy - head_r, head_cx + head_r, head_cy + head_r],
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
        bbox      = None

        if state:
            age = time.time() - state.get("ts", 0)
            if age < STALE_THRESH and state.get("active", False):
                active    = True
                landmarks = state.get("landmarks")
                bbox      = state.get("bbox")

        if active and landmarks:
            # Continuously redraw — the shape varies every frame as the
            # person moves, unlike the old fixed-size icons.
            _send_frame(ser, _draw_silhouette(landmarks, bbox))
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
