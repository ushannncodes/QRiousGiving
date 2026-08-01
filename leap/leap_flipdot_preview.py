#!/usr/bin/env python3
"""leap_flipdot_preview.py — runs on the Raspberry Pi, standalone.

Renders the live Leap Motion hand as a filled silhouette straight to the
physical flipdot panel, so you can see how the hand interaction actually
looks on real hardware. Deliberately kept separate from hi5_final.py /
run_kiosk.py — no palm-fill game, no hi-5 hold timer, no state machine,
just "here's the hand, drawn on the dots, right now." Doesn't import or
modify any existing QRiousGiving file.

Uses the same flipdot wire protocol as the rest of the repo (see
kiosk/hi5_final.py's _pack_28x28_to_panels / _send_one_frame_fast):
4 stacked 28x7 panels = 28x28, one packet per panel:
[0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F]

The landmark -> 28x28 grid rendering (motion easing, fixed-size
calibration bounds, filled-silhouette palm/finger strokes) lives in
flipdot_render.py, shared with leap_visualizer.py's browser preview so
both show exactly the same thing this script would put on the real
panel.

Env vars:
  LISTEN_PORT     UDP port the Leap data arrives on (default 5111, must
                  match leap_sender.py's RPI_PORT)
  FLIPDOT_SERIAL  serial port (default /dev/ttyS0, same as rest of repo)
  FLIPDOT_BAUD    baud rate (default 57600, same as rest of repo)
  WHITE_VAL       polarity, "1" or "0" (default 0 for this hand-shadow
                  effect — dots physically show their dark face for the
                  lit/hand pixels and their white face for the
                  background, i.e. a dark silhouette on a light panel,
                  like a real hand shadow. This differs from the rest of
                  the repo's WHITE_VAL=1 convention deliberately; set to
                  1 to go back to a light silhouette on a dark panel.)
  REFRESH_HZ      max panel redraw rate (default 18 — the serial link
                  (4 panels x 32 bytes/frame @ 57600 baud) tops out
                  around ~45Hz, so 18Hz leaves headroom while still being
                  far more responsive than the old 6Hz default; flipdots
                  are mechanical, so there's still a ceiling above which
                  redraws outrun the dots physically flipping, but 18Hz
                  is well under that)
  STALE_SEC       blank the panel if no packet arrives within this window
                  (default 0.5)
  EASE_FACTOR     0-1, how far the displayed hand moves toward the latest
                  raw position each redraw tick (default 0.65 — lower is
                  smoother/laggier, higher is snappier/jerkier; 1.0
                  disables easing entirely). Resets instantly (no easing
                  in) whenever the hand reappears after being stale, so
                  it doesn't glide in from a stale old position.
  LINE_THICKNESS  base finger stroke radius in grid cells, at the
                  knuckle end of each finger's taper (default 1.1,
                  trimmed down from an initial 1.6 per on-panel feedback
                  — adjacent fingers like a peace sign read as fused
                  "fat fingers" until this came down; the thumb is drawn
                  at 1.15x this). Fingers narrow toward the tip — see
                  flipdot_render.py's FINGER_TIP_RATIO /
                  THUMB_RADIUS_SCALE for the exact taper. Thickness comes
                  entirely from these strokes plus the filled palm
                  polygon, not a post-hoc dilation pass.
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
  X_RANGE_MM      left-right span mapped across the 28 columns (default
                  270, i.e. -135..+135mm around 0 — sized off real
                  two-hand testing (combined span 131-314mm, median
                  224mm across both hands) so both hands fit on panel at
                  once, rather than sized to fill the grid tightly with
                  one hand. A single hand will look smaller than an
                  ideal single-hand fill as a direct, deliberate
                  consequence of prioritizing two-hand visibility.)
  Z_MIN_MM        near edge of the depth window, as an offset from the
                  hand's own position when it appears (default -110)
  Z_MAX_MM        far edge of the depth window, same offset convention
                  (default 110 — i.e. a fixed ~220mm window)

  X_RANGE_MM is a fixed, absolute window (not re-centered on the hand)
  tuned so a hand at normal hover distance fills roughly 75-85% of the
  grid — deliberately not auto-expanding, since an expand-only range
  meant one wide gesture would permanently zoom the hand out for the
  rest of the session. Left/right (x) position on the panel reflects
  real absolute position in front of the sensor.

  Z_MIN_MM/Z_MAX_MM are different: real testing showed this mount's z
  axis has a consistent *spread* (how big the hand looks) but a
  session-to-session *absolute* value that drifts by 100mm+ depending
  on exactly where someone holds their hand — a fixed absolute window
  clipped the whole hand to one edge unless they happened to hover in
  exactly the calibrated spot. So the z window's center continuously
  eases toward the hand's actual average z position (see
  flipdot_render.py's HandRenderer / Z_CENTER_EASE below) rather than
  holding one fixed absolute value — giving a consistent fill
  percentage without needing X's kind of fixed absolute mapping, which
  this axis can't reliably support.
  Z_CENTER_EASE   0-1, how fast the z window's center follows a
                  sustained change in the hand's real position (default
                  0.05 — deliberately much slower than EASE_FACTOR, so
                  one atypical frame, e.g. a hand mid-transition or a
                  left/right hand-type swap, can't skew the whole
                  session; it takes a couple seconds of sustained
                  movement to actually drag the window). Resets
                  instantly, not eased, the moment a hand reappears
                  after being stale.
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
WHITE_VAL = int(os.getenv("WHITE_VAL", "0"))
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
    last_hands = None
    last_packet_t = 0.0
    last_draw_t = 0.0

    try:
        while True:
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
            if now - last_draw_t < MIN_INTERVAL:
                continue
            last_draw_t = now

            stale = (now - last_packet_t) > STALE_SEC if last_packet_t else True
            if stale or not last_hands:
                renderer.reset()
                frame = blank_frame()
            else:
                frame = renderer.update(last_hands)
            send_frame(ser, frame)

    except KeyboardInterrupt:
        print("\n[leap_flipdot_preview] stopping, blanking panel")
        send_frame(ser, blank_frame())
        ser.close()


if __name__ == "__main__":
    main()
