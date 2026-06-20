#!/usr/bin/env python3
"""cam_v2.py — HuskyLens 2 face-detection presence sensor.

Replaces cam_final.py. Writes /tmp/cam_state.json in a format
backward-compatible with the existing orchestrator/run_kiosk, plus
face bounding box fields used by attract_v2.py for silhouette scaling.

Env vars (all optional):
  CAM_SIGNAL_PATH       default /tmp/cam_state.json
  HUSKYLENS_PORT        default /dev/serial0     (UART mode)
  HUSKYLENS_BAUD        default 9600
  HUSKYLENS_I2C         "1" = I2C (default), "0" = UART
  HUSKYLENS_I2C_BUS     default 1
  HUSKYLENS_I2C_ADDR    default 0x32
  HUSKYLENS_FRAME_W     default 320  (sensor frame width in px)
  HUSKYLENS_FRAME_H     default 240
  POLL_INTERVAL         default 0.1 s
  ACTIVE_FRAMES         consecutive detections before going active (default 3)
"""

import json
import os
import sys
import time
import logging
import signal as _signal

from DFRobot_HuskyLens import (
    DFRobot_HuskyLens_UART,
    DFRobot_HuskyLens_I2C,
    ALGORITHM_FACE_RECOGNITION,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH   = os.getenv("CAM_SIGNAL_PATH",    "/tmp/cam_state.json")
PORT          = os.getenv("HUSKYLENS_PORT",      "/dev/serial0")
BAUD          = int(os.getenv("HUSKYLENS_BAUD",  "9600"))
USE_I2C       = os.getenv("HUSKYLENS_I2C",       "1") == "1"
I2C_BUS       = int(os.getenv("HUSKYLENS_I2C_BUS",  "1"))
I2C_ADDR      = int(os.getenv("HUSKYLENS_I2C_ADDR", "0x32"), 16)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.1"))
ACTIVE_FRAMES = int(os.getenv("ACTIVE_FRAMES",   "3"))


def connect():
    if USE_I2C:
        hl = DFRobot_HuskyLens_I2C(bus=I2C_BUS, addr=I2C_ADDR)
    else:
        hl = DFRobot_HuskyLens_UART(baud=BAUD, uart_addr=PORT)

    for attempt in range(10):
        if hl.begin():
            log.info("HuskyLens 2 connected (%s)", "I2C" if USE_I2C else f"UART {PORT}")
            hl.write_algo(ALGORITHM_FACE_RECOGNITION)
            return hl
        log.warning("Connection attempt %d/10 failed, retrying…", attempt + 1)
        time.sleep(1)

    log.error("Could not connect to HuskyLens 2")
    sys.exit(1)


def largest_block(blocks):
    return max(blocks, key=lambda b: b.width * b.height) if blocks else None


def write_state(active, active_secs, face=None):
    state = {
        # Orchestrator reads: ts, active
        "ts":           time.time(),
        "active":       active,
        "active_secs":  active_secs,
        # Legacy fields (orchestrator may read these)
        "active_pixels": 1 if active else 0,
        "ema":           1.0 if active else 0.0,
        "delta":         0,
        "delta_ema":     0.0,
        # attract_v2.py reads these for silhouette scaling
        "face_x": face.x      if face else 0,
        "face_y": face.y      if face else 0,
        "face_w": face.width  if face else 0,
        "face_h": face.height if face else 0,
    }
    tmp = SIGNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SIGNAL_PATH)


def main():
    hl = connect()

    consecutive = 0
    active       = False
    active_start = None
    running      = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    while running:
        t0 = time.time()

        try:
            got_data  = hl.request()
            has_faces = got_data and hl.count_blocks() > 0
        except Exception as e:
            log.warning("HuskyLens read error: %s", e)
            has_faces = False
            time.sleep(1.0)
            continue

        if has_faces:
            consecutive = min(consecutive + 1, ACTIVE_FRAMES + 1)
            face = largest_block(hl.blocks())
        else:
            consecutive = max(consecutive - 1, 0)
            face = None

        # State transitions
        if not active and consecutive >= ACTIVE_FRAMES:
            active       = True
            active_start = time.time()
            log.info("Presence: active")
        elif active and consecutive == 0:
            active       = False
            active_start = None
            log.info("Presence: idle")

        active_secs = (time.time() - active_start) if active_start else 0.0
        write_state(active, active_secs, face if active else None)

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    log.info("cam_v2: exiting")


if __name__ == "__main__":
    main()
