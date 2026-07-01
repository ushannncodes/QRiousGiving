#!/usr/bin/env python3
"""attract_shadow.py — full-body shadow attract display.

Reads /tmp/cam_state.json (written by cam_v2.py), same as attract_v2.py.
Unlike attract_v2.py's fixed head+torso-trapezoid icon, this draws a solid
"shadow" built from the person's actual tracked skeleton — head, torso,
both arms (shoulder-elbow-wrist), and both legs (hip-knee-ankle) — as
thick filled strokes. Because it follows the real limb positions, a raised
arm or a walking stride actually shows up in the shape, rather than always
rendering the same static torso block regardless of pose.

Env vars (all optional, same names as attract_v2.py where applicable):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480)
  HUSKYLENS_FRAME_H same, height (default 480)
  LIMB_WIDTH        stroke width for arms/legs in display-units (default 2.2)
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
LIMB_WIDTH    = float(os.getenv("LIMB_WIDTH", "2.2"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

# See attract_v2.py — HuskyLens occasionally returns a bit-corrupted
# landmark value under I2C timing pressure; treat anything past this as
# "not detected" rather than drawn.
MAX_COORD = 2000


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


def _lm(landmarks: dict, key: str):
    """Cleaned, canvas-space point for a landmark, or None if unavailable."""
    pt = _clean_point(tuple(landmarks.get(key, (0, 0))))
    return _to_canvas(pt) if pt else None


def _thick_line(draw, points, width):
    """Draw.line with round joints — PIL's line width doesn't round the
    ends/joints on its own, which leaves visible gaps/notches at elbows and
    knees; capping each segment with a filled circle keeps the limb
    reading as one continuous stroke instead of a jointed rod."""
    pts = [p for p in points if p is not None]
    if len(pts) < 2:
        return
    draw.line(pts, fill=0, width=round(width))
    r = width / 2
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=0)


def _draw_shadow(landmarks: dict, bbox: dict | None) -> Image.Image:
    canvas = _blank_frame()

    lsh_c = _lm(landmarks, "lshoulder")
    rsh_c = _lm(landmarks, "rshoulder")
    if not (lsh_c and rsh_c):
        return canvas  # not enough signal to draw anything meaningful

    mid_sh = ((lsh_c[0] + rsh_c[0]) / 2, (lsh_c[1] + rsh_c[1]) / 2)

    if bbox and bbox.get("height"):
        body_h = (bbox["height"] / FRAME_H) * DISPLAY_H
    else:
        body_h = DISPLAY_H * 0.6
    body_h = _clamp(body_h, 6.0, float(DISPLAY_H))

    lhip_c = _lm(landmarks, "lhip")
    rhip_c = _lm(landmarks, "rhip")
    if lhip_c and rhip_c:
        mid_hip = ((lhip_c[0] + rhip_c[0]) / 2, (lhip_c[1] + rhip_c[1]) / 2)
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

    # Torso — same trapezoid as attract_v2.py's icon, still the sturdiest
    # anchor for "this is a body" at 28x28.
    draw.polygon(
        [
            (cx_top - torso_top_w / 2, top_y),
            (cx_top + torso_top_w / 2, top_y),
            (cx_bot + torso_bot_w / 2, bot_y),
            (cx_bot - torso_bot_w / 2, bot_y),
        ],
        fill=0,
    )

    # Head
    head_cx, head_cy = cx_top, top_y - head_r * 1.2
    draw.ellipse(
        [head_cx - head_r, head_cy - head_r, head_cx + head_r, head_cy + head_r],
        fill=0,
    )

    # Arms and legs — drawn from real tracked joints so a raised arm or a
    # walking stride actually shows, instead of the torso always looking
    # identical regardless of pose.
    for side, shoulder_c, hip_c in (("l", lsh_c, lhip_c), ("r", rsh_c, rhip_c)):
        elbow_c = _lm(landmarks, f"{side}elbow")
        wrist_c = _lm(landmarks, f"{side}wrist")
        _thick_line(draw, [shoulder_c, elbow_c, wrist_c], LIMB_WIDTH)

        if hip_c:
            knee_c = _lm(landmarks, f"{side}knee")
            ankle_c = _lm(landmarks, f"{side}ankle")
            _thick_line(draw, [hip_c, knee_c, ankle_c], LIMB_WIDTH)

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

    log.info("attract_shadow: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
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
            _send_frame(ser, _draw_shadow(landmarks, bbox))
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_shadow: exited cleanly")


if __name__ == "__main__":
    main()
