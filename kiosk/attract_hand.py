#!/usr/bin/env python3
"""attract_hand.py — hand-silhouette variant of attract_v2.py (experimental).

Standalone test script: instead of reading pose landmarks from
/tmp/cam_state.json (written by cam_v2.py), this connects to the HuskyLens
directly with ALGORITHM_HAND_RECOGNITION and draws a live silhouette from
the 21 hand landmarks (MediaPipe wrist-first order, same as hi5_final.py)
— a palm blob plus finger sticks for whichever fingers are extended —
instead of the head+torso pose silhouette.

This does NOT replace attract_v2.py; run it standalone to compare:

    export FLIPDOT_SERIAL=/tmp/flipdot_vserial
    python3 kiosk/attract_hand.py

Env vars (all optional):
  FLIPDOT_SERIAL       default /dev/ttyS0
  FLIPDOT_BAUD         default 57600
  HUSKYLENS_I2C_BUS    default 1
  HUSKYLENS_I2C_ADDR   default 0x50
  HAND_POLL            loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W    reference scale for landmark pixel coords (default 480)
  HUSKYLENS_FRAME_H    same, for y (default 480)
"""

import math
import os
import time
import logging
import signal as _signal

import serial
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SERIAL_PORT     = os.getenv("FLIPDOT_SERIAL",     "/dev/ttyS0")
BAUD_RATE       = int(os.getenv("FLIPDOT_BAUD",   "57600"))
I2C_BUS         = int(os.getenv("HUSKYLENS_I2C_BUS",  "1"))
I2C_ADDR        = int(os.getenv("HUSKYLENS_I2C_ADDR", "0x50"), 16)
POLL_INTERVAL   = float(os.getenv("HAND_POLL", "0.1"))
FRAME_W         = int(os.getenv("HUSKYLENS_FRAME_W", "480"))
FRAME_H         = int(os.getenv("HUSKYLENS_FRAME_H", "480"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7
PANEL_ADDRS = [0x01, 0x02, 0x03, 0x04]

# MediaPipe-order hand landmark indices (see DFRobot_HuskyLens.py _HandBlock).
WRIST = 0
MCPS  = [1, 5, 9, 13, 17]          # thumb cmc, then index/middle/ring/pinky mcp
FINGERS = [
    (5, 6, 7, 8),      # index: mcp, pip, dip, tip
    (9, 10, 11, 12),   # middle
    (13, 14, 15, 16),  # ring
    (17, 18, 19, 20),  # pinky
]
THUMB = (1, 2, 3, 4)  # cmc, mcp, ip, tip

# Same tuned thresholds/logic as hi5_final.py's is_open_palm() — applied in
# raw HuskyLens pixel space, *not* the 28x28 canvas space, since the joint
# angle is scale-invariant but a fixed distance margin isn't once you've
# already squashed everything down to a 28-unit canvas.
ANGLE_PIP_THRESH_DEG = float(os.getenv("ANGLE_PIP_THRESH_DEG", "130"))
ANGLE_DIP_THRESH_DEG = float(os.getenv("ANGLE_DIP_THRESH_DEG", "118"))
DIST_MARGIN          = float(os.getenv("DIST_MARGIN", "3"))


def _blank_frame() -> Image.Image:
    return Image.new("L", (DISPLAY_W, DISPLAY_H), 255)


def _to_canvas(pt):
    x, y = pt
    return (x / FRAME_W) * DISPLAY_W, (y / FRAME_H) * DISPLAY_H


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle_deg(a, b, c):
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    num = bax * bcx + bay * bcy
    den = math.hypot(bax, bay) * math.hypot(bcx, bcy) + 1e-9
    val = max(-1.0, min(1.0, num / den))
    return math.degrees(math.acos(val))


def _extended_finger(raw, mcp_i, pip_i, dip_i, tip_i) -> bool:
    wrist = raw[WRIST]
    mcp, pip, dip, tip = raw[mcp_i], raw[pip_i], raw[dip_i], raw[tip_i]
    dist_ok = _dist(tip, wrist) > _dist(pip, wrist) + DIST_MARGIN
    angle_ok = (_angle_deg(mcp, pip, dip) >= ANGLE_PIP_THRESH_DEG
                and _angle_deg(pip, dip, tip) >= ANGLE_DIP_THRESH_DEG)
    return dist_ok and angle_ok


def _extended_thumb(raw) -> bool:
    wrist = raw[WRIST]
    mcp, ip, tip = raw[THUMB[1]], raw[THUMB[2]], raw[THUMB[3]]
    dist_ok = _dist(tip, wrist) > _dist(ip, wrist) + (DIST_MARGIN * 0.6)
    return dist_ok and _angle_deg(mcp, ip, tip) >= (ANGLE_PIP_THRESH_DEG - 10)


def _draw_hand_silhouette(landmarks) -> Image.Image:
    """Palm blob (wrist + finger MCPs) plus a stick per extended finger.
    `landmarks` is the raw 21-point list of (x, y) pixel tuples HuskyLens
    reports for the largest/first hand, in its own raw pixel space —
    extension is tested there, and only the final shapes get scaled to the
    28x28 canvas for drawing."""
    canvas = _blank_frame()
    if not landmarks or len(landmarks) < 21:
        return canvas

    pts = [_to_canvas(p) for p in landmarks]
    draw = ImageDraw.Draw(canvas)

    # Palm: polygon through wrist + the four finger MCPs (thumb cmc pulls
    # the outline out toward the thumb side), filled solid.
    palm_poly = [pts[WRIST]] + [pts[i] for i in MCPS]
    draw.polygon(palm_poly, fill=0)

    for mcp_i, pip_i, dip_i, tip_i in FINGERS:
        if _extended_finger(landmarks, mcp_i, pip_i, dip_i, tip_i):
            draw.line([pts[mcp_i], pts[tip_i]], fill=0, width=2)

    if _extended_thumb(landmarks):
        draw.line([pts[THUMB[1]], pts[THUMB[3]]], fill=0, width=2)

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
                if pixels[x, y + y_off] == 255:  # white -> bit 1
                    col_byte |= (1 << y)
            data[x] = col_byte
        panels.append(data)
    return panels


def _send_frame(ser: serial.Serial, img: Image.Image) -> None:
    for addr, data in zip(PANEL_ADDRS, _image_to_panels(img)):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()


def main() -> None:
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    from DFRobot_HuskyLens import DFRobot_HuskyLens_I2C, ALGORITHM_HAND_RECOGNITION

    log.info("attract_hand: connecting to HuskyLens (I2C bus %d addr 0x%02x)", I2C_BUS, I2C_ADDR)
    hl = DFRobot_HuskyLens_I2C(bus=I2C_BUS, addr=I2C_ADDR)
    for attempt in range(10):
        if hl.begin():
            break
        log.warning("HuskyLens connect attempt %d/10 failed, retrying…", attempt + 1)
        time.sleep(1)
    else:
        log.error("Could not connect to HuskyLens")
        return
    hl.write_algo(ALGORITHM_HAND_RECOGNITION)

    log.info("attract_hand: opening %s @ %d baud", SERIAL_PORT, BAUD_RATE)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(0.1)

    was_active = False

    while running:
        t0 = time.time()

        try:
            got = hl.request()
        except Exception as e:
            log.warning("HuskyLens read error: %s", e)
            got = False

        if got and hl.count_blocks() > 0:
            block = hl.blocks()[0]  # largest/first hand only
            _send_frame(ser, _draw_hand_silhouette(block.landmarks))
            was_active = True
        elif was_active:
            _send_frame(ser, _blank_frame())
            was_active = False

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_hand: exited cleanly")


if __name__ == "__main__":
    main()
