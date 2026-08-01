#!/usr/bin/env python3
"""synthetic_leap_udp_sender.py — fake Leap Motion UDP feed, no hardware
required.

Sends the *current* wire schema (matching beelink/leap_sender.py, NOT the
older /tmp/leap_state.json schema leap/synthetic_hand_test.py writes —
that schema predates this pipeline and is incompatible with it, see
LEAP_HANDOFF.md) over UDP to a Pi-side listener, so kiosk/attract_leap.py
and kiosk/hi5_final.py can be exercised end-to-end without the real
Beelink PC or Leap Motion Controller.

Wire schema sent, matching flipdot_render.py's HandRenderer.update() input
exactly:
{
  "ts": <float unix time>,
  "hands": [
    {"hand_type": "left" | "right",
     "landmarks": [[x_mm, z_mm], ...]}   # 21 points, wrist-first:
                                          #   0 wrist
                                          #   1-4 thumb (CMC,MCP,IP,TIP)
                                          #   5-8/9-12/13-16/17-20
                                          #     index/middle/ring/pinky
                                          #     (MCP,PIP,DIP,TIP)
  ]
}

Usage (in a terminal alongside the simulator, e.g. per the verification
steps in LEAP_HANDOFF.md):
    python3 simulator/flipdot_simulator.py
    export FLIPDOT_SERIAL=/tmp/flipdot_vserial
    python3 leap/synthetic_leap_udp_sender.py &
    python3 kiosk/attract_leap.py         # or kiosk/hi5_final.py, or kiosk/run_kiosk.py

Env vars (all optional):
  RPI_HOST      default 127.0.0.1
  RPI_PORT      default 5111
  SEND_HZ       default 30 (matches beelink/leap_sender.py's default)
  SYNTH_MODE    "animate" (default) — palm drifts in a slow circle, curl
                  oscillates open<->closed, good for exercising
                  attract_leap.py's rendering/easing.
                "open" — a held-still, fully open palm (3+ fingers
                  extended), for testing hi5_final.py's hold-to-fill
                  timer without the gesture flickering shut mid-hold.
                "closed" — a held-still fist, for testing the "not open"
                  / idle-abort path.
  SYNTH_HAND    "right" (default) or "left" — hand_type to send.
"""

import json
import math
import os
import socket
import time
import signal as _signal

RPI_HOST   = os.getenv("RPI_HOST", "127.0.0.1")
RPI_PORT   = int(os.getenv("RPI_PORT", "5111"))
SEND_HZ    = float(os.getenv("SEND_HZ", "30"))
SYNTH_MODE = os.getenv("SYNTH_MODE", "animate")
SYNTH_HAND = os.getenv("SYNTH_HAND", "right")

# Same rough per-finger fan (degrees from straight up) and length (mm) as
# leap/synthetic_hand_test.py, just re-targeted at the 21-point wrist-first
# schema (4 joints/finger: MCP/PIP/DIP/TIP, thumb's MCP-analog is CMC) with
# an added wrist point, instead of that file's 5-joints/finger, no-wrist
# schema.
FINGERS = [
    ("thumb",  -55, 55),
    ("index",  -20, 70),
    ("middle",   0, 78),
    ("ring",    18, 72),
    ("pinky",   38, 58),
]
MCP_OFFSET = 14.0   # mm from palm center to each finger's base joint
WRIST_OFFSET = 60.0  # mm from palm center to the wrist, opposite the fingers
N_SEGMENTS = 3        # -> 4 joints per finger (base + 3 segment ends)
CURL_DEG = 70.0        # total inward bend at full curl


def _rotate(vx, vy, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (vx * c - vy * s, vx * s + vy * c)


def _finger_chain(palm, angle_deg, length, curl):
    dx, dy = math.sin(math.radians(angle_deg)), math.cos(math.radians(angle_deg))
    base = (palm[0] + dx * MCP_OFFSET, palm[1] + dy * MCP_OFFSET)
    seg_len = length / N_SEGMENTS
    bend_per_seg = curl * CURL_DEG / N_SEGMENTS
    pts = [base]
    cur = base
    cdx, cdy = dx, dy
    for _ in range(N_SEGMENTS):
        cdx, cdy = _rotate(cdx, cdy, -bend_per_seg)
        cur = (cur[0] + cdx * seg_len, cur[1] + cdy * seg_len)
        pts.append(cur)
    return [list(p) for p in pts]


def _hand_landmarks(t, mirror):
    sign = -1.0 if mirror else 1.0

    if SYNTH_MODE == "animate":
        cx = sign * 70.0 + math.cos(t * 0.6) * 35.0
        cy = 190.0 + math.sin(t * 0.6) * 30.0
        curl = (math.sin(t * 1.3) + 1.0) / 2.0  # 0..1
    elif SYNTH_MODE == "open":
        cx, cy = sign * 70.0, 190.0
        curl = 0.0
    elif SYNTH_MODE == "closed":
        cx, cy = sign * 70.0, 190.0
        curl = 1.0
    else:
        raise ValueError(f"SYNTH_MODE must be animate/open/closed, got {SYNTH_MODE!r}")

    palm = (cx, cy)
    wrist = (palm[0], palm[1] - WRIST_OFFSET)

    landmarks = [list(wrist)]
    for name, angle, length in FINGERS:
        a = angle * sign
        landmarks.extend(_finger_chain(palm, a, length, curl))
    return landmarks


def main():
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"[synthetic_leap_udp_sender] sending to {RPI_HOST}:{RPI_PORT} "
          f"at {SEND_HZ:.0f}Hz, mode={SYNTH_MODE}, hand={SYNTH_HAND}, ctrl-C to stop")

    t0 = time.time()
    interval = 1.0 / SEND_HZ
    while running:
        loop_start = time.time()
        t = loop_start - t0

        landmarks = _hand_landmarks(t, mirror=(SYNTH_HAND == "left"))
        payload = {
            "ts": loop_start,
            "hands": [{"hand_type": SYNTH_HAND, "landmarks": landmarks}],
        }
        sock.sendto(json.dumps(payload).encode("utf-8"), (RPI_HOST, RPI_PORT))
        time.sleep(max(0, interval - (time.time() - loop_start)))

    print("[synthetic_leap_udp_sender] stopped sending (no more packets — "
          "consumers should go stale/blank per their STALE_SEC)")


if __name__ == "__main__":
    main()
