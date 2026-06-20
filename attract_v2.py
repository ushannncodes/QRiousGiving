#!/usr/bin/env python3
"""attract_v2.py — Silhouette attract display for QRiousGiving v2.

Reads /tmp/cam_state.json (written by cam_v2.py). When a face is detected
the flipdot shows a person silhouette scaled to their apparent distance:
  far  (face < 15 % of frame width) → 14×14 px centred on the 28×28 display
  mid  (15–35 %)                    → 20×20 px
  close (> 35 %)                    → full 28×28 px

Runs as a long-lived subprocess managed by run_kiosk / orchestrator.
Exits cleanly on SIGINT / SIGTERM and blanks the display.

Env vars (all optional):
  CAM_SIGNAL_PATH   default /tmp/cam_state.json
  FLIPDOT_PORT      default /dev/ttyUSB0
  FLIPDOT_BAUD      default 57600
  CAM_STALE_SECS    max age of signal before treating as "no one there" (default 2.0)
  ATTRACT_POLL      loop interval in seconds (default 0.1)
  HUSKYLENS_FRAME_W HuskyLens sensor frame width in px (default 320)
"""

import json
import os
import time
import logging
import signal as _signal

import serial
import numpy as np
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH   = os.getenv("CAM_SIGNAL_PATH",  "/tmp/cam_state.json")
SERIAL_PORT   = os.getenv("FLIPDOT_PORT",      "/dev/ttyUSB0")
BAUD_RATE     = int(os.getenv("FLIPDOT_BAUD",  "57600"))
STALE_THRESH  = float(os.getenv("CAM_STALE_SECS", "2.0"))
POLL_INTERVAL = float(os.getenv("ATTRACT_POLL", "0.1"))
FRAME_W       = int(os.getenv("HUSKYLENS_FRAME_W", "320"))

DISPLAY_W   = 28
DISPLAY_H   = 28
NUM_PANELS  = 4
PANEL_H     = 7   # rows per panel
# Panel addresses — must match your hardware. Same order as qr_works.py.
PANEL_ADDRS = [0x00, 0x01, 0x02, 0x03]

# ── Silhouette bitmap ────────────────────────────────────────────────────────
# 28 × 28, row-major.  1 = person pixel (renders as dark/flipped dot),
#                       0 = background  (renders as white/unflipped dot).
SILHOUETTE = [
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],  #  0 top pad
    [0,0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0],  #  1 head
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0],  #  2
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0],  #  3
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0],  #  4
    [0,0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0],  #  5
    [0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0],  #  6 neck
    [0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0],  #  7
    [0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0],  #  8 shoulders
    [0,0,0,1,1,1,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,1,1,1,0,0,0],  #  9 arms+torso
    [0,0,0,1,1,1,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,1,1,1,0,0,0],  # 10
    [0,0,0,0,1,1,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,1,1,0,0,0,0],  # 11
    [0,0,0,0,1,1,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,1,1,0,0,0,0],  # 12
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 13 torso
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 14
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 15
    [0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 16
    [0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0],  # 17 hips
    [0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0],  # 18
    [0,0,0,0,0,0,0,0,1,1,1,1,1,0,0,1,1,1,1,1,0,0,0,0,0,0,0,0],  # 19 legs
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 20
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 21
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 22
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 23
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 24
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 25
    [0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0],  # 26
    [0,0,0,0,0,0,0,1,1,1,1,1,1,0,0,1,1,1,1,1,1,0,0,0,0,0,0,0],  # 27 feet
]

# Precompute PIL image from bitmap (28×28, L mode).
# 1 (person) → pixel 0 (black) → bit 0 → dark dot on display.
# 0 (background) → pixel 255 (white) → bit 1 → white dot on display.
_BASE_SILHOUETTE_IMG = Image.fromarray(
    np.array([[0 if v else 255 for v in row] for row in SILHOUETTE], dtype=np.uint8),
    mode="L",
)


def _make_frame(target_px: int) -> Image.Image:
    """Scale the silhouette to target_px × target_px, centred on a white 28×28 canvas."""
    scaled = _BASE_SILHOUETTE_IMG.resize((target_px, target_px), Image.NEAREST)
    canvas = Image.new("L", (DISPLAY_W, DISPLAY_H), 255)
    off = (DISPLAY_W - target_px) // 2
    canvas.paste(scaled, (off, off))
    return canvas


def _blank_frame() -> Image.Image:
    return Image.new("L", (DISPLAY_W, DISPLAY_H), 255)


# Precompute the three silhouette sizes to avoid runtime resizing.
_FRAMES = {
    0:  _blank_frame(),
    14: _make_frame(14),
    20: _make_frame(20),
    28: _make_frame(28),
}


def _target_px(face_w: int) -> int:
    ratio = face_w / FRAME_W
    if ratio < 0.15:
        return 14
    if ratio < 0.35:
        return 20
    return 28


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

    last_target = -1  # force first write

    while running:
        t0 = time.time()

        state      = _read_state()
        target_px  = 0  # blank

        if state:
            age = time.time() - state.get("ts", 0)
            if age < STALE_THRESH and state.get("active", False):
                face_w    = state.get("face_w", 0)
                target_px = _target_px(face_w) if face_w > 0 else 28

        if target_px != last_target:
            _send_frame(ser, _FRAMES[target_px])
            last_target = target_px
            log.debug("silhouette size → %d px", target_px)

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    _send_frame(ser, _blank_frame())
    ser.close()
    log.info("attract_v2: exited cleanly")


if __name__ == "__main__":
    main()
