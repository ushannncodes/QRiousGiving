#!/usr/bin/env python3
"""cam_v2.py — HuskyLens 2 pose-presence sensor.

Replaces cam_final.py. Writes /tmp/cam_state.json in a format
backward-compatible with the existing orchestrator/run_kiosk, plus body
landmarks (nose, shoulders, hips, etc.) used by attract_v2.py to draw a
live-tracking silhouette.

Uses ALGORITHM_POSE_RECOGNITION rather than face recognition so presence
triggers on a person's body generally (doesn't need a clear forward-facing
face) — closer to how cam_final.py's old motion+pose silhouette detected
"anything in front of the camera".

Env vars (all optional):
  CAM_SIGNAL_PATH       default /tmp/cam_state.json
  HUSKYLENS_PORT        default /dev/serial0     (UART mode)
  HUSKYLENS_BAUD        default 9600
  HUSKYLENS_I2C         "1" = I2C (default), "0" = UART
  HUSKYLENS_I2C_BUS     default 1
  HUSKYLENS_I2C_ADDR    default 0x50 (confirmed via i2cdetect on this unit)
  POLL_INTERVAL         default 0.1 s
  ACTIVE_FRAMES         consecutive detections before going active (default 3)
  POSE_MISS_GRACE_SEC   keep last known landmarks across brief detection
                         dropouts, same flicker as hand recognition showed
                         (default 0.5)
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
    ALGORITHM_POSE_RECOGNITION,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH    = os.getenv("CAM_SIGNAL_PATH",    "/tmp/cam_state.json")
PORT           = os.getenv("HUSKYLENS_PORT",      "/dev/serial0")
BAUD           = int(os.getenv("HUSKYLENS_BAUD",  "9600"))
USE_I2C        = os.getenv("HUSKYLENS_I2C",       "1") == "1"
I2C_BUS        = int(os.getenv("HUSKYLENS_I2C_BUS",  "1"))
I2C_ADDR       = int(os.getenv("HUSKYLENS_I2C_ADDR", "0x50"), 16)
POLL_INTERVAL  = float(os.getenv("POLL_INTERVAL", "0.1"))
ACTIVE_FRAMES  = int(os.getenv("ACTIVE_FRAMES",   "3"))
MISS_GRACE_SEC = float(os.getenv("POSE_MISS_GRACE_SEC", "0.5"))


def connect():
    if USE_I2C:
        hl = DFRobot_HuskyLens_I2C(bus=I2C_BUS, addr=I2C_ADDR)
    else:
        hl = DFRobot_HuskyLens_UART(baud=BAUD, uart_addr=PORT)

    for attempt in range(10):
        try:
            ok = hl.begin()
        except Exception as e:
            ok = False
            log.warning("HuskyLens connection error: %s", e)
        if ok:
            log.info("HuskyLens 2 connected (%s)", "I2C" if USE_I2C else f"UART {PORT}")
            if not hl.write_algo(ALGORITHM_POSE_RECOGNITION):
                # write_algo() already retried internally and verified against
                # the device's actual reported algorithm — if it still says no,
                # silently continuing would poll for pose landmarks the sensor
                # was never actually producing (observed live: HuskyLens screen
                # stuck showing the previous session's algorithm). Exit instead
                # so run_kiosk.py's RUN_KIOSK loop respawns us for a fresh try.
                log.error("HuskyLens never confirmed switching to pose recognition.")
                sys.exit(1)
            return hl
        log.warning("Connection attempt %d/10 failed, retrying…", attempt + 1)
        time.sleep(1)

    log.error("Could not connect to HuskyLens 2")
    sys.exit(1)


def largest_block(blocks):
    return max(blocks, key=lambda b: b.width * b.height) if blocks else None


def write_state(active, active_secs, pose=None):
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
        # attract_v2.py reads these to draw a live-tracking silhouette
        "bbox": (
            {"x": pose.x, "y": pose.y, "width": pose.width, "height": pose.height}
            if pose else None
        ),
        "landmarks": pose.landmarks if pose else None,
    }
    tmp = SIGNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SIGNAL_PATH)


def main():
    hl = connect()

    consecutive  = 0
    active       = False
    active_start = None
    running      = True
    last_pose    = None
    last_seen    = 0.0

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    while running:
        t0 = time.time()

        try:
            got_data  = hl.request()
            has_poses = got_data and hl.count_blocks() > 0
        except Exception as e:
            log.warning("HuskyLens read error: %s", e)
            has_poses = False
            time.sleep(1.0)
            continue

        if has_poses:
            consecutive = min(consecutive + 1, ACTIVE_FRAMES + 1)
            last_pose = largest_block(hl.blocks())
            last_seen = t0
        else:
            consecutive = max(consecutive - 1, 0)

        # Smooth over brief detection dropouts (pose recognition flickers
        # frame-to-frame even with a person steady in view) so the
        # silhouette doesn't blank out between misses.
        pose = last_pose if (t0 - last_seen) <= MISS_GRACE_SEC else None

        # State transitions
        if not active and consecutive >= ACTIVE_FRAMES:
            active       = True
            active_start = time.time()
            log.info("Presence: active")
        elif active and consecutive == 0 and pose is None:
            active       = False
            active_start = None
            log.info("Presence: idle")

        active_secs = (time.time() - active_start) if active_start else 0.0
        write_state(active, active_secs, pose if active else None)

        time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

    log.info("cam_v2: exiting")


if __name__ == "__main__":
    main()
