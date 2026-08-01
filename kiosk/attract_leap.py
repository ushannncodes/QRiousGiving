#!/usr/bin/env python3
"""attract_leap.py — Leap Motion presence + shadow attract display.

Replaces cam_v2.py + attract_v2.py/attract_outline.py as a single process.
Those two were split across separate scripts only because the HuskyLens's
I2C bus needed single-owner discipline (one process reading the sensor,
a second just rendering whatever that first process wrote to
/tmp/cam_state.json) — the Leap feed has no such constraint, since it
arrives over UDP from the Beelink PC, not a local bus. So this script does
both jobs: it's the sole UDP consumer during the kiosk's RUN_KIOSK state,
and it both draws the live hand-shadow to the physical panel *and* writes
/tmp/cam_state.json so run_kiosk.py's existing presence-trigger logic
(_read_cam_state(), WARMUP_SEC/TRIGGER_HOLD_SEC) keeps working unchanged.

Rendering (motion easing, fixed-size calibration bounds, filled-silhouette
palm/finger strokes) is entirely delegated to leap/flipdot_render.py's
HandRenderer — the same module leap_flipdot_preview.py and
leap_visualizer.py already use, so this stage's on-panel look matches the
already-hardware-verified standalone demo (GRID_ROTATE=180 MIRROR=1
PROJECT_AXES=x,z, per LEAP_HANDOFF.md) rather than a second, potentially
drifting reimplementation.

"Active" (for /tmp/cam_state.json) is defined the same way "not stale" is
for rendering: a hand was present in the most recently received packet,
and that packet arrived within STALE_SEC. There's no separate presence
concept — if the panel is showing a hand, run_kiosk.py sees active=True.

Idle state: rather than blanking the panel when no hand is present, this
now draws kiosk/hourglass.py's slowly draining hourglass — the kiosk's
resting animation, and what the panel shows the vast majority of the time.
The moment a hand appears the panel cuts to the live shadow, and each
return to idle restarts the glass full rather than resuming mid-drain.
The animation shares this process (rather than being a stage run_kiosk.py
switches to) because only one process can bind the Leap UDP port, so
whatever draws the idle art must also be the thing watching for hands.

Frames are only written to the panel when they differ from the last one
sent — the idle animation changes roughly every half second, far slower
than REFRESH_HZ, and there's no reason to re-send a frame the panel is
already displaying.

Env vars:
  LISTEN_PORT     UDP port the Leap data arrives on (default 5111, must
                  match beelink/leap_sender.py's RPI_PORT). Safe to reuse
                  the same port hi5_final.py listens on later — run_kiosk.py
                  guarantees this process is fully stopped before hi5_final.py
                  starts, so they never bind concurrently.
  FLIPDOT_SERIAL  serial port (default /dev/ttyS0)
  FLIPDOT_BAUD    baud rate (default 57600)
  CAM_SIGNAL_PATH presence/landmark state file (default /tmp/cam_state.json,
                  same path cam_v2.py used — run_kiosk.py's _read_cam_state()
                  needs no changes)
  WHITE_VAL       polarity, "1" or "0" (default 0 — dark hand-shadow on a
                  light panel, matching leap_flipdot_preview.py's tuned
                  look, NOT attract_v2.py/hi5_final.py's WHITE_VAL=1
                  convention; override to 1 if this stage should go back
                  to matching those instead)

  HOURGLASS_*     idle-animation knobs (cycle length, flip beat, vertical
                  flip) — see kiosk/hourglass.py's docstring.

Rendering/staleness tuning (REFRESH_HZ, STALE_SEC, EASE_FACTOR,
LINE_THICKNESS, X_RANGE_MM, Z_MIN_MM, Z_MAX_MM, Z_CENTER_EASE) all come
from leap/flipdot_render.py — see that module's docstring, or
leap/leap_flipdot_preview.py's, for full descriptions. GRID_ROTATE
(default 180) and MIRROR (default 1) are defaulted here specifically to
the hardware-verified values from LEAP_HANDOFF.md, since flipdot_render.py
itself defaults both to 0 for its other callers.
"""

import json
import os
import signal as _signal
import socket
import sys
import time

import serial

os.environ.setdefault("GRID_ROTATE", "180")
os.environ.setdefault("MIRROR", "1")

# flipdot_render.py lives in the sibling leap/ directory, not kiosk/ — add
# it to the path rather than duplicating ~300 lines of rendering logic.
_LEAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "leap")
sys.path.insert(0, _LEAP_DIR)
from flipdot_render import GRID, HandRenderer, REFRESH_HZ, STALE_SEC  # noqa: E402

from hourglass import Hourglass  # idle animation; sits next to this file

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
FLIPDOT_SERIAL = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
FLIPDOT_BAUD = int(os.getenv("FLIPDOT_BAUD", "57600"))
CAM_SIGNAL_PATH = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
WHITE_VAL = int(os.getenv("WHITE_VAL", "0"))
BLACK_VAL = 1 - WHITE_VAL
MIN_INTERVAL = 1.0 / REFRESH_HZ


def _pack_28x28_to_panels(lit28):
    panels = []
    for p in range(4):
        off = p * 7
        data = bytearray()
        for x in range(GRID):
            b = 0
            for y in range(7):
                bit = WHITE_VAL if lit28[off + y][x] else BLACK_VAL
                b |= (bit & 1) << y
            data.append(b)
        panels.append(data)
    return panels


def _send_frame(ser, lit28):
    for addr, data in zip([1, 2, 3, 4], _pack_28x28_to_panels(lit28)):
        ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
    ser.flush()


def _blank_frame():
    return [[0] * GRID for _ in range(GRID)]


def _write_cam_state(active, now):
    # Same shape run_kiosk.py's _read_cam_state() already expects; extra
    # fields cam_v2.py used to write (active_secs, bbox, landmarks) are
    # not read by run_kiosk.py's trigger logic, so they're omitted here.
    # delta_ema=0.0 is a no-op gate, same as cam_v2.py's own convention
    # ("cam_v2 always writes delta_ema=0; gate is the active flag").
    state = {"ts": now, "active": active, "delta_ema": 0.0}
    tmp = CAM_SIGNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, CAM_SIGNAL_PATH)


def main():
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    sock.settimeout(0.05)
    print(f"[attract_leap] UDP listener on :{LISTEN_PORT}")

    ser = serial.Serial(FLIPDOT_SERIAL, FLIPDOT_BAUD, timeout=0, write_timeout=0)
    print(f"[attract_leap] flipdot serial on {FLIPDOT_SERIAL} @ {FLIPDOT_BAUD}")

    renderer = HandRenderer()
    idle = Hourglass()
    last_hands = None
    last_packet_t = 0.0
    last_draw_t = 0.0
    was_active = None
    last_sent = None

    while running:
        try:
            data, _addr = sock.recvfrom(8192)
            payload = json.loads(data.decode("utf-8"))
            last_hands = payload.get("hands")
            last_packet_t = time.time()
        except socket.timeout:
            pass
        except (OSError, json.JSONDecodeError):
            pass

        now = time.time()
        active = bool(last_hands) and (now - last_packet_t) <= STALE_SEC

        # Write on every loop iteration (bounded by the socket's 50ms
        # timeout, so ~20Hz+ whenever packets are arriving) so run_kiosk.py's
        # ACTIVE_STALE_SEC freshness check always sees a recent timestamp —
        # deliberately not coupled to the slower REFRESH_HZ render throttle.
        _write_cam_state(active, now)

        if now - last_draw_t >= MIN_INTERVAL:
            last_draw_t = now

            if active != was_active:
                # Drop stale easing so a reappearing hand doesn't slide in
                # from where the last one left, and restart the idle glass
                # from a full top bulb rather than resuming mid-drain.
                renderer.reset()
                idle.reset()
                was_active = active

            frame = renderer.update(last_hands) if active else idle.frame(now)

            # The idle animation only actually changes every ~0.5s, so at
            # REFRESH_HZ most frames are identical to the one before. Sending
            # those anyway would be pure serial traffic for a panel that's
            # already showing them — and on hardware that flips physical
            # discs, "draw nothing new" should cost nothing.
            if frame != last_sent:
                _send_frame(ser, frame)
                last_sent = [row[:] for row in frame]

    print("\n[attract_leap] stopping, blanking panel")
    _send_frame(ser, _blank_frame())
    ser.close()


if __name__ == "__main__":
    main()
