#!/usr/bin/env python3
"""attract_outline.py — Option 6: dashed-outline attract display.

Reads /tmp/cam_state.json (written by cam_v2.py), same as attract_v2.py.
Reuses attract_v2.py's torso-trapezoid + head-circle geometry (sized off
the tracked bbox/shoulders/hips), but instead of a solid fill, draws only
a broken/dashed outline — like a quick line sketch rather than a filled
coloring-book shape. Should read as much less "chunky" at 28x28.

Env vars (all optional, same names as attract_v2.py where applicable):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_SERIAL    default /dev/ttyS0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W reference scale for landmark pixel coordinates (default 480)
  HUSKYLENS_FRAME_H same, height (default 480)
  DASH_LEN          "on" segment length in display-units (default 1.4)
  GAP_LEN           "off" segment length in display-units (default 1.0)
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
DASH_LEN      = float(os.getenv("DASH_LEN", "1.4"))
GAP_LEN       = float(os.getenv("GAP_LEN", "1.0"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

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


STEP_LEN = 0.25  # fixed sampling step for dash rendering (display-units)


def _dashed_segment(draw: ImageDraw.ImageDraw, p0, p1, phase: float) -> float:
    """Draw a dashed line from p0 to p1 by sampling at a fixed step and
    plotting points that fall in the "on" part of the dash/gap cycle.
    `phase` is the distance already consumed into that cycle by prior
    segments (so dashes stay continuous around a multi-segment outline).
    Returns the phase to carry into the next segment.

    Sampling at a fixed step (rather than solving exact dash-boundary
    crossings) avoids float-precision edge cases where the exact-boundary
    approach could compute a near-zero step and loop effectively forever."""
    x0, y0 = p0
    x1, y1 = p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-6:
        return phase
    cycle = DASH_LEN + GAP_LEN
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    steps = max(1, int(length / STEP_LEN))
    for i in range(steps + 1):
        d = min(i * STEP_LEN, length)
        if (phase + d) % cycle < DASH_LEN:
            x, y = x0 + dx * d, y0 + dy * d
            draw.point((x, y), fill=0)
    return (phase + length) % cycle


def _draw_dashed_polygon(draw: ImageDraw.ImageDraw, points: list, phase: float = 0.0) -> float:
    for i in range(len(points)):
        p0, p1 = points[i], points[(i + 1) % len(points)]
        phase = _dashed_segment(draw, p0, p1, phase)
    return phase


def _draw_dashed_circle(draw: ImageDraw.ImageDraw, cx, cy, r, phase: float = 0.0) -> float:
    circumference = 2 * math.pi * r
    steps = max(8, int(circumference / 0.6))
    pts = [
        (cx + r * math.cos(2 * math.pi * i / steps), cy + r * math.sin(2 * math.pi * i / steps))
        for i in range(steps)
    ]
    return _draw_dashed_polygon(draw, pts, phase)


def _draw_outline(landmarks: dict, bbox: dict | None) -> Image.Image:
    """Same body geometry as attract_v2._draw_silhouette, but rendered as a
    dashed outline instead of a solid fill."""
    canvas = _blank_frame()

    lsh = _clean_point(tuple(landmarks.get("lshoulder", (0, 0))))
    rsh = _clean_point(tuple(landmarks.get("rshoulder", (0, 0))))
    if not (lsh and rsh):
        return canvas

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
    phase = _draw_dashed_polygon(
        draw,
        [
            (cx_top - torso_top_w / 2, top_y),
            (cx_top + torso_top_w / 2, top_y),
            (cx_bot + torso_bot_w / 2, bot_y),
            (cx_bot - torso_bot_w / 2, bot_y),
        ],
    )

    head_cx, head_cy = cx_top, top_y - head_r * 1.2
    phase = _draw_dashed_circle(draw, head_cx, head_cy, head_r, phase)

    for side, shoulder_c in (("l", lsh_c), ("r", rsh_c)):
        phase = _draw_extended_arm(draw, landmarks, side, shoulder_c, cx_top, torso_top_w, top_y, bot_y, phase)

    return canvas


# How far a wrist has to stray from the torso outline (in display-units)
# before we treat the arm as "extended" and draw it, rather than just
# resting at the person's side (where drawing it would just look noisy).
ARM_EXTEND_MARGIN = 1.5


def _draw_extended_arm(draw, landmarks, side, shoulder_c, cx_top, torso_top_w, top_y, bot_y, phase):
    elbow = _clean_point(tuple(landmarks.get(f"{side}elbow", (0, 0))))
    wrist = _clean_point(tuple(landmarks.get(f"{side}wrist", (0, 0))))
    if not wrist:
        return phase

    wrist_c = _to_canvas(wrist)
    outside_x = wrist_c[0] < cx_top - torso_top_w / 2 - ARM_EXTEND_MARGIN or \
        wrist_c[0] > cx_top + torso_top_w / 2 + ARM_EXTEND_MARGIN
    outside_y = wrist_c[1] < top_y - ARM_EXTEND_MARGIN or wrist_c[1] > bot_y + ARM_EXTEND_MARGIN
    if not (outside_x or outside_y):
        return phase  # arm at rest against the body — skip, avoid clutter

    points = [shoulder_c]
    if elbow:
        points.append(_to_canvas(elbow))
    points.append(wrist_c)

    for i in range(len(points) - 1):
        phase = _dashed_segment(draw, points[i], points[i + 1], phase)
    return phase


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

    log.info("attract_outline: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    # Reset to blank/white immediately so whatever the previous script left
    # on the panel (last outline frame, old QR code, etc.) doesn't linger
    # until the first person shows up.
    _send_frame(ser, _blank_frame())

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
            _send_frame(ser, _draw_outline(landmarks, bbox))
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_outline: exited cleanly")


if __name__ == "__main__":
    main()
