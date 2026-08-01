#!/usr/bin/env python3
"""synthetic_hand_test.py — fake Leap data generator, no hardware required.

Writes /tmp/leap_state.json at a high, steady rate with two procedurally
animated hands (palms drifting in slow circles, fingers curling open/closed)
in the same schema leap_receiver.py produces from real network frames. This
lets you validate attract_leap_shadow.py's rendering, smoothing, and the
flipdot simulator end-to-end *before* the real Leap Motion + second-PC path
is working — the network/protocol side is the riskiest, least-tested part
of this pipeline, so prove the drawing side is smooth first.

Usage (three terminals):
    python3 simulator/flipdot_simulator.py
    export FLIPDOT_SERIAL=/tmp/flipdot_vserial
    python3 leap/synthetic_hand_test.py
    python3 leap/attract_leap_shadow.py

State schema written (matches leap_receiver.py's output exactly):
{
  "ts": <float unix time>,
  "hands": [
    {
      "type": "left" | "right",
      "palm": [x_mm, y_mm],
      "fingers": {
        "thumb":  {"joints": [[x,y], [x,y], [x,y], [x,y], [x,y]]},
        "index":  {"joints": [...]},
        "middle": {"joints": [...]},
        "ring":   {"joints": [...]},
        "pinky":  {"joints": [...]}
      }
    }
  ]
}
Coordinates are Leap-style real-world millimeters (x: left/right, y: height
above the device), 5 joints per finger from base (near palm) to fingertip.

Env vars (all optional):
  LEAP_SIGNAL_PATH   default /tmp/leap_state.json
  SYNTH_RATE_HZ      write rate (default 60)
  SYNTH_HANDS        "1" or "2" (default 2)
"""

import json
import math
import os
import time
import signal as _signal

SIGNAL_PATH = os.getenv("LEAP_SIGNAL_PATH", "/tmp/leap_state.json")
RATE_HZ     = float(os.getenv("SYNTH_RATE_HZ", "60"))
NUM_HANDS   = int(os.getenv("SYNTH_HANDS", "2"))

# Rough natural finger fan (degrees from straight up) and length (mm),
# thumb..pinky, mirrored in sign for the left hand.
FINGERS = [
    ("thumb",  -55, 55),
    ("index",  -20, 70),
    ("middle",   0, 78),
    ("ring",    18, 72),
    ("pinky",   38, 58),
]
MCP_OFFSET  = 14.0   # mm from palm center to each finger's base joint
N_SEGMENTS  = 4       # -> 5 joints per finger (base + 4 segment ends)
CURL_DEG    = 70.0    # total inward bend at full curl


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


def _hand(hand_type, t, mirror):
    sign = -1.0 if mirror else 1.0
    cx = sign * 70.0 + math.cos(t * 0.6 + (math.pi if mirror else 0)) * 35.0
    cy = 190.0 + math.sin(t * 0.6) * 30.0
    palm = (cx, cy)
    curl = (math.sin(t * 1.3 + (1.5 if mirror else 0)) + 1.0) / 2.0  # 0..1
    fingers = {}
    for name, angle, length in FINGERS:
        a = angle * sign
        fingers[name] = {"joints": _finger_chain(palm, a, length, curl)}
    return {"type": hand_type, "palm": list(palm), "fingers": fingers}


def _write_state(state):
    tmp = SIGNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, SIGNAL_PATH)


def main():
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    print(f"synthetic_hand_test: writing {SIGNAL_PATH} at {RATE_HZ:.0f}Hz "
          f"({NUM_HANDS} hand{'s' if NUM_HANDS != 1 else ''}), ctrl-C to stop")

    t0 = time.time()
    interval = 1.0 / RATE_HZ
    while running:
        loop_start = time.time()
        t = loop_start - t0

        hands = [_hand("right", t, mirror=False)]
        if NUM_HANDS >= 2:
            hands.append(_hand("left", t, mirror=True))

        _write_state({"ts": loop_start, "hands": hands})
        time.sleep(max(0, interval - (time.time() - loop_start)))

    _write_state({"ts": time.time(), "hands": []})
    print("synthetic_hand_test: exited cleanly")


if __name__ == "__main__":
    main()
