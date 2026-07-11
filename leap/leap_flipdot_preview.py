#!/usr/bin/env python3
"""leap_flipdot_preview.py — runs on the Raspberry Pi, standalone.

Renders the live Leap Motion hand skeleton straight to the physical
flipdot panel, so you can see how the hand interaction actually looks on
real hardware. Deliberately kept separate from hi5_final.py / run_kiosk.py
— no palm-fill game, no hi-5 hold timer, no state machine, just "here's
the hand, drawn on the dots, right now." Doesn't import or modify any
existing QRiousGiving file.

Uses the same flipdot wire protocol as the rest of the repo (see
kiosk/hi5_final.py's _pack_28x28_to_panels / _send_one_frame_fast):
4 stacked 28x7 panels = 28x28, one packet per panel:
[0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F]

The landmark -> 28x28 grid rendering (motion easing, auto-expanding
calibration bounds, line dilation) lives in flipdot_render.py, shared
with leap_visualizer.py's browser preview so both show exactly the same
thing this script would put on the real panel.

Env vars:
  LISTEN_PORT     UDP port the Leap data arrives on (default 5111, must
                  match leap_sender.py's RPI_PORT)
  FLIPDOT_SERIAL  serial port (default /dev/ttyS0, same as rest of repo)
  FLIPDOT_BAUD    baud rate (default 57600, same as rest of repo)
  WHITE_VAL       polarity, "1" or "0" (default 1, same as rest of repo)
  REFRESH_HZ      max panel redraw rate (default 6 — flipdots are
                  mechanical, physically flipping each dot, so redrawing
                  as fast as the UDP stream arrives (~30Hz) would hammer
                  the mechanism for no visual benefit at this resolution;
                  6Hz is plenty to read as "live" while being kind to the
                  hardware)
  STALE_SEC       blank the panel if no packet arrives within this window
                  (default 0.5)
  EASE_FACTOR     0-1, how far the displayed hand moves toward the latest
                  raw position each redraw tick (default 0.35 — lower is
                  smoother/laggier, higher is snappier/jerkier; 1.0
                  disables easing entirely). Resets instantly (no easing
                  in) whenever the hand reappears after being stale, so
                  it doesn't glide in from a stale old position.
  LINE_THICKNESS  pixel-dilation passes applied to the skeleton so
                  fingers read as strokes instead of hairlines (default
                  1; 0 = original 1px lines)
  GRID_ROTATE     0/90/180/270 — rotates the final grid counter-clockwise
                  before it's sent to the panel (default 0). Use this to
                  fix display orientation once PROJECT_AXES (on the
                  Beelink sender) already gives a correctly *shaped*
                  hand — rotation can't fix a bad axis choice, only
                  reorient a good one.
  MIRROR          "1" to horizontally flip the grid, applied after
                  rotation (default off). Rotation alone can never fix a
                  left/right-swapped hand (e.g. thumb on the wrong side)
                  since rotation preserves handedness — that needs MIRROR.

Calibration (the Leap's real-world mm coordinates -> 28x28 grid mapping):
  X_RANGE_MM      initial left-right span mapped across the 28 columns
                  (default 300, i.e. -150..+150mm)
  Z_MIN_MM        initial nearest-to-sensor depth mapped to the grid
                  (default 80)
  Z_MAX_MM        initial farthest depth mapped to the grid (default 380)
  RANGE_MARGIN_MM padding added when the observed range expands past the
                  above (default 20)

  These four are only a starting floor, not a hard calibration — actual
  observed x/z coordinates auto-expand the mapped range as they arrive
  (never shrinks), so a bad initial guess just means the hand fills the
  panel gradually over the first few seconds of movement instead of
  being stuck in whatever narrow band the guessed range happened to
  cover. Left/right (x) is still absolute, not centered on the hand, so
  panel position reflects real position in front of the sensor.
"""

import json
import os
import socket
import time

import serial

from flipdot_render import GRID, HandRenderer, REFRESH_HZ, STALE_SEC

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
FLIPDOT_SERIAL = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
FLIPDOT_BAUD = int(os.getenv("FLIPDOT_BAUD", "57600"))
WHITE_VAL = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL = 1 - WHITE_VAL
MIN_INTERVAL = 1.0 / REFRESH_HZ


def pack_28x28_to_panels(lit28):
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


def send_frame(ser, lit28):
    for addr, data in zip([1, 2, 3, 4], pack_28x28_to_panels(lit28)):
        ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
    ser.flush()


def blank_frame():
    return [[0] * GRID for _ in range(GRID)]


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    sock.settimeout(0.05)
    print(f"[leap_flipdot_preview] UDP listener on :{LISTEN_PORT}")

    ser = serial.Serial(FLIPDOT_SERIAL, FLIPDOT_BAUD, timeout=0, write_timeout=0)
    print(f"[leap_flipdot_preview] flipdot serial on {FLIPDOT_SERIAL} @ {FLIPDOT_BAUD}")

    renderer = HandRenderer()
    last_landmarks = None
    last_packet_t = 0.0
    last_draw_t = 0.0

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(8192)
                payload = json.loads(data.decode("utf-8"))
                last_landmarks = payload.get("landmarks")
                last_packet_t = time.time()
            except socket.timeout:
                pass
            except (OSError, json.JSONDecodeError):
                pass

            now = time.time()
            if now - last_draw_t < MIN_INTERVAL:
                continue
            last_draw_t = now

            stale = (now - last_packet_t) > STALE_SEC if last_packet_t else True
            if stale or not last_landmarks:
                renderer.reset()
                frame = blank_frame()
            else:
                frame = renderer.update(last_landmarks)
            send_frame(ser, frame)

    except KeyboardInterrupt:
        print("\n[leap_flipdot_preview] stopping, blanking panel")
        send_frame(ser, blank_frame())
        ser.close()


if __name__ == "__main__":
    main()
