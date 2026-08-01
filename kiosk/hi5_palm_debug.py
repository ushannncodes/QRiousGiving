#!/usr/bin/env python3
"""hi5_palm_debug.py — HI-5 palm-fill logic only, no intro text.

Standalone debug harness for the palm-open → fill-progress state machine in
hi5_final.py. Skips the "HI" bitmap flash, the MESSAGES scroller, and the
NEXT_SCRIPT → qr_works.py chain on success — those add ~2-4s of dead time
per test cycle in hi5_final.py and aren't relevant to debugging detection
sensitivity. This script goes straight to the palm outline + Leap UDP
listener, and on success just logs it and loops back to outline instead of
exiting, so you can retry gestures back-to-back without a relaunch.

Detection: Leap-native, NOT the 2D landmark geometry hi5_final.py uses
-----------------------------------------------------------------------
hi5_final.py inherited its open-palm test from the HuskyLens/MediaPipe era:
re-derive finger extension from the 21 2D landmarks, with a bounding-box
area fallback. Both parts break on a Leap feed:

  * the landmarks are a 2D *projection* (PROJECT_AXES drops one of Leap's
    three axes), so much of the finger-curl information is simply gone
    before the Pi ever sees it;
  * the bbox fallback's MIN_HAND_AREA was tuned in HuskyLens sensor pixels,
    but Leap landmarks are real-world millimetres. 5000 "pixels" became
    5000 mm² ≈ a 70×70mm box — smaller than a closed fist — so the fallback
    fired for *any* hand in view and the fill ran to 100% regardless of
    hand shape.

So this script ignores the landmarks entirely and uses the pose signals
Gemini's tracking model already computes, added to the wire schema by
hand_to_pose() in beelink/leap_sender.py:

  grab      0.0 = flat open hand, 1.0 = closed fist   (primary signal)
  extended  per-digit [thumb, index, middle, ring, pinky] booleans
  pinch     0.0 = not pinching, 1.0 = full pinch      (rejects pinch/OK signs)
  palm_normal / palm_dir, confidence                  (optional gates)

An open high-five is "grab below MAX_GRAB_STRENGTH, at least
MIN_EXTENDED_FINGERS digits extended, not pinching". A fist, a peace sign,
or a hand just drifting through the volume all fail it.

>>> REQUIRES THE UPDATED SENDER. <<< The Beelink runs a hand-copied
leap_sender.py from C:\\Users\\Creative Machine 02\\Desktop\\
leapc-python-bindings-main\\ — editing the repo copy changes nothing until
you copy it over and restart it. If packets arrive without a "pose" block,
this script says so loudly and exits rather than silently falling back to
"any hand counts", which is the bug it exists to fix.

Sub-tests whose field the bindings don't expose (pose value None) are
skipped with a one-time warning, so an older binding degrades to a looser
test rather than to a broken one.

Releasing the gesture
---------------------
A pose only counts as a detection on the packet it arrived in — it is never
re-evaluated on later loop passes. The sender also emits `{"hands": []}`
when the volume is empty instead of falling silent, so "the hand left" is
positive evidence that arrives within a frame, and it cancels the grace
window outright rather than letting it coast. Together those stop the fill
essentially the moment you pull your hand away.

Both matter, because previously they stacked: the Pi held the last
open-palm pose and kept re-detecting it for UDP_STALE_SEC, and only then
did MISS_GRACE_SEC start counting — ~0.85s of fill after the hand was
physically gone, enough that showing a palm briefly and yanking it away
still reached 100%. MISS_GRACE_SEC is now the only dropout bridge.

Env vars:
  LISTEN_PORT ("5111")
  UDP_STALE_SEC ("0.5") — no packets at all for this long means the sender
    or network is down (not that a hand left; that arrives as an explicit
    empty-hands packet), and also cancels the grace window
  FLIPDOT_SERIAL ("/dev/ttyS0"), FLIPDOT_BAUD ("57600"), WHITE_VAL ("1")
  HOLD_REQUIRED_SEC ("1.5")
  MISS_GRACE_SEC ("0.35") — bridges a brief tracking dropout mid-hold. Must
    stay well BELOW HOLD_REQUIRED_SEC: it keeps the fill counting after the
    last open frame, so at 1.5s (hi5_final.py's value, == its hold) a single
    detected frame was enough to complete the whole gesture on its own.
  HYST_ALPHA ("0.5"), HYST_THRESH ("0.5")
  MAX_GRAB_STRENGTH ("0.20")   — above this = too closed
  MAX_PINCH_STRENGTH ("0.40")  — above this = pinching, not a flat palm
  MIN_EXTENDED_FINGERS ("4")   — of 5; 4 tolerates a tucked thumb
  MIN_CONFIDENCE ("0.0")       — Leap's own tracking confidence
  PALM_FACING_AXIS ("")        — off by default. Set to a Leap axis
    ("y", "-y", "z", "-z", …) to also require the palm to face that way,
    i.e. an actual high-five toward the panel rather than an open hand held
    edge-on. Mount-dependent (the controller is currently vertical on the
    panel's right edge, lens outward), so tune it by watching the palm_n=
    values this logs rather than reasoning it out geometrically.
  PALM_FACING_MIN_DOT ("0.5")  — how strictly, once PALM_FACING_AXIS is set
  REARM_REQUIRES_RELEASE ("1") — after a success, require one non-open frame
    before the next fill can start, so holding your hand up doesn't
    immediately refill
  PALM_JSON (default: assets/palm_combo.json relative to kiosk/)
  LOOP_SLEEP_SEC ("0.03"), DEBUG_LOG ("1"), LOG_EVERY_MS ("350")
  SUCCESS_HOLD_SEC ("1.0") — how long the full frame stays up before
    resetting to outline for the next attempt (debug-only knob, no
    equivalent in hi5_final.py)
  NO_POSE_GRACE_SEC ("5.0") — how long to tolerate pose-less packets (i.e.
    an old sender still running on the Beelink) before giving up
"""

import json
import os
import socket
import sys
import time
from typing import List, Optional

WIDTH, HEIGHT = 28, 28
WHITE_VAL = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL = 1 - WHITE_VAL

SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE = int(os.getenv("FLIPDOT_BAUD", "57600"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

HOLD_REQUIRED_SEC = float(os.getenv("HOLD_REQUIRED_SEC", "1.5"))
MISS_GRACE_SEC = float(os.getenv("MISS_GRACE_SEC", "0.35"))
SUCCESS_HOLD_SEC = float(os.getenv("SUCCESS_HOLD_SEC", "1.0"))

MAX_GRAB_STRENGTH = float(os.getenv("MAX_GRAB_STRENGTH", "0.20"))
MAX_PINCH_STRENGTH = float(os.getenv("MAX_PINCH_STRENGTH", "0.40"))
MIN_EXTENDED_FINGERS = int(os.getenv("MIN_EXTENDED_FINGERS", "4"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.0"))

PALM_FACING_AXIS = os.getenv("PALM_FACING_AXIS", "").strip()
PALM_FACING_MIN_DOT = float(os.getenv("PALM_FACING_MIN_DOT", "0.5"))

REARM_REQUIRES_RELEASE = os.getenv("REARM_REQUIRES_RELEASE", "1") == "1"

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
UDP_STALE_SEC = float(os.getenv("UDP_STALE_SEC", "0.5"))
NO_POSE_GRACE_SEC = float(os.getenv("NO_POSE_GRACE_SEC", "5.0"))

LOOP_SLEEP_SEC = float(os.getenv("LOOP_SLEEP_SEC", "0.03"))
HYST_ALPHA = float(os.getenv("HYST_ALPHA", "0.5"))
HYST_THRESH = float(os.getenv("HYST_THRESH", "0.5"))

PALM_JSON = os.getenv("PALM_JSON", os.path.join(SCRIPT_DIR, "..", "assets", "palm_combo.json"))

DEBUG_LOG = os.getenv("DEBUG_LOG", "1") == "1"
LOG_EVERY_MS = int(os.getenv("LOG_EVERY_MS", "350"))


def fatal(msg: str):
    print(msg)
    sys.exit(1)


try:
    import serial
except Exception:
    fatal("ERROR: pyserial missing.")

_ser = None


def _ensure_serial():
    global _ser
    if _ser is None or not _ser.is_open:
        _ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    return _ser


def _pack_28x28_to_panels(bw28):
    panels = []
    for p in range(4):
        off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            b = 0
            for y in range(7):
                b |= (int(bw28[off + y][x]) & 1) << y
            data.append(b)
        panels.append(data)
    return panels


def send_frame_to_flipdot(frame28: List[List[int]]):
    s = _ensure_serial()
    for addr, data in zip([1, 2, 3, 4], _pack_28x28_to_panels(frame28)):
        s.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
    s.flush()


def maybe_close_serial():
    global _ser
    try:
        if _ser and _ser.is_open:
            _ser.close()
    except Exception:
        pass


def load_palm_from_json(path: str):
    if not os.path.exists(path):
        fatal(f"ERROR: JSON not found: {path}")
    with open(path, "r") as f:
        data = json.load(f)

    def parse(key):
        rows = data.get(key)
        if not isinstance(rows, list) or len(rows) != HEIGHT:
            fatal(f"ERROR: '{key}' must be a list of 28 strings.")
        mask = []
        for i, row in enumerate(rows):
            if not isinstance(row, str) or len(row) != WIDTH or any(c not in "01" for c in row):
                fatal(f"ERROR in {key} row {i}: must be 28 chars of 0/1")
            mask.append([1 if c == "1" else 0 for c in row])
        return mask

    return parse("palm_outline"), parse("palm_filled")


def compose_outline_frame(outline):
    frame = [[WHITE_VAL] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if outline[y][x] == 1:
                frame[y][x] = BLACK_VAL
    return frame


def compose_fill_frame_from_filled(outline, filled, cutoff_row: int):
    frame = [[WHITE_VAL] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if outline[y][x] == 1:
                frame[y][x] = BLACK_VAL
        if y >= cutoff_row:
            for x in range(WIDTH):
                if filled[y][x] == 1:
                    frame[y][x] = BLACK_VAL
    return frame


# ---------------------------------------------------------------- detection

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_warned = set()


def _warn_once(key: str, msg: str):
    if key not in _warned:
        _warned.add(key)
        sys.stderr.write(f"[HI5-DEBUG] {msg}\n")
        sys.stderr.flush()


def _parse_facing_axis(spec: str):
    if not spec:
        return None
    sign = -1.0 if spec.startswith("-") else 1.0
    axis = spec.lstrip("-").lower()
    if axis not in _AXIS_INDEX:
        fatal(f"ERROR: PALM_FACING_AXIS={spec!r} — expected x, y or z, optionally '-'-prefixed")
    return _AXIS_INDEX[axis], sign


_FACING = _parse_facing_axis(PALM_FACING_AXIS)


def _num(pose, key) -> Optional[float]:
    v = pose.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_open_palm(pose: dict):
    """True if `pose` looks like a flat, open, forward-facing hand.

    Every sub-test is skipped (with a one-time warning) when the sender
    couldn't read that field, so this degrades gracefully on older LeapC
    bindings instead of failing shut. Returns (bool, reason_string) — the
    reason names the *first failing* gate, which is what makes the debug
    log useful for tuning.
    """
    grab = _num(pose, "grab")
    if grab is None:
        _warn_once("grab", "sender exposed no grab_strength — skipping the primary open-hand gate")
    elif grab > MAX_GRAB_STRENGTH:
        return False, f"too_closed(grab={grab:.2f}>{MAX_GRAB_STRENGTH:.2f})"

    pinch = _num(pose, "pinch")
    if pinch is not None and pinch > MAX_PINCH_STRENGTH:
        return False, f"pinching(pinch={pinch:.2f})"

    extended = pose.get("extended")
    if isinstance(extended, list) and any(e is not None for e in extended):
        n_ext = sum(1 for e in extended if e)
        if n_ext < MIN_EXTENDED_FINGERS:
            return False, f"few_fingers({n_ext}<{MIN_EXTENDED_FINGERS})"
    else:
        _warn_once("extended", "sender exposed no is_extended flags — skipping the finger-count gate")

    conf = _num(pose, "confidence")
    if conf is not None and MIN_CONFIDENCE > 0.0 and conf < MIN_CONFIDENCE:
        return False, f"low_confidence({conf:.2f})"

    if _FACING is not None:
        normal = pose.get("palm_normal")
        if isinstance(normal, list) and len(normal) == 3:
            idx, sign = _FACING
            mag = sum(c * c for c in normal) ** 0.5 or 1.0
            dot = (normal[idx] * sign) / mag
            if dot < PALM_FACING_MIN_DOT:
                return False, f"palm_turned(dot={dot:.2f}<{PALM_FACING_MIN_DOT:.2f})"
        else:
            _warn_once("normal", "sender exposed no palm normal — skipping the facing gate")

    return True, "open_palm"


def _pose_summary(pose: dict) -> str:
    grab = _num(pose, "grab")
    pinch = _num(pose, "pinch")
    extended = pose.get("extended")
    n_ext = sum(1 for e in extended if e) if isinstance(extended, list) else "?"
    parts = [
        f"grab={grab:.2f}" if grab is not None else "grab=?",
        f"pinch={pinch:.2f}" if pinch is not None else "pinch=?",
        f"ext={n_ext}",
    ]
    normal = pose.get("palm_normal")
    if isinstance(normal, list) and len(normal) == 3:
        parts.append("palm_n=[" + ",".join(f"{c:+.2f}" for c in normal) + "]")
    return " ".join(parts)


def _now_ms():
    return int(time.time() * 1000)


def _log(msg):
    if DEBUG_LOG:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def main():
    print(f"[HI5-DEBUG] start {time.time():.3f}s — Leap-native pose detection")
    print(f"[HI5-DEBUG] gates: grab<={MAX_GRAB_STRENGTH} pinch<={MAX_PINCH_STRENGTH} "
          f"extended>={MIN_EXTENDED_FINGERS} facing={PALM_FACING_AXIS or 'off'}")
    if MISS_GRACE_SEC >= HOLD_REQUIRED_SEC:
        print(f"[HI5-DEBUG] WARNING: MISS_GRACE_SEC ({MISS_GRACE_SEC}) >= HOLD_REQUIRED_SEC "
              f"({HOLD_REQUIRED_SEC}) — one detected frame will complete the whole gesture "
              f"on its own. Lower it.")

    outline_mask, filled_mask = load_palm_from_json(PALM_JSON)
    outline_frame = compose_outline_frame(outline_mask)
    send_frame_to_flipdot(outline_frame)
    print(f"Palm outline shown, no intro. Starting detection… (hold {HOLD_REQUIRED_SEC:.1f}s)")

    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.bind(("0.0.0.0", LISTEN_PORT))
    _sock.setblocking(False)  # drained each pass; LOOP_SLEEP_SEC paces the loop
    _log(f"[HI5-DEBUG] UDP listener on :{LISTEN_PORT}")

    hold_start = None
    last_open_seen_time = 0.0
    smoothed_hit = 0.0
    last_hands = None
    last_packet_t = 0.0
    armed = True

    first_poseless_t = None
    seen_pose_ever = False

    last_log_ms = _now_ms()
    last_reason = None
    last_progress_pct = -1

    try:
        while True:
            # Drain the socket every pass: act on the newest packet only, and
            # never let a backlog turn into growing latency.
            got_packet = False
            while True:
                try:
                    data, _addr = _sock.recvfrom(8192)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError as e:
                    _log(f"[HI5-DEBUG] UDP read error: {e}")
                    break
                try:
                    payload = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    _log(f"[HI5-DEBUG] bad packet: {e}")
                    continue
                last_hands = payload.get("hands") or []
                got_packet = True

            now = time.time()
            if got_packet:
                last_packet_t = now

            # A pose is evidence of an open palm only for the packet it arrived
            # in. Re-evaluating the last-received pose on every loop pass (which
            # is what this used to do) meant a hand that had already left kept
            # "detecting" as open until the staleness timeout expired, stacking
            # an extra ~UDP_STALE_SEC of fill on top of MISS_GRACE_SEC. Bridging
            # dropouts is MISS_GRACE_SEC's job, and it alone.
            open_palm_now = False
            hand_gone = False
            if got_packet and last_hands:
                reason = "hand"                         # refined by is_open_palm below
            elif got_packet:
                reason, hand_gone = "hand_gone", True   # explicit {"hands": []}
            elif (now - last_packet_t) > UDP_STALE_SEC:
                reason, hand_gone = "feed_stale", True  # sender/network down
            else:
                reason = "between_packets"              # normal at 30Hz, not absence

            if hand_gone:
                # Positive evidence of absence, not a dropout — cancel the grace
                # window outright instead of letting it coast.
                last_open_seen_time = 0.0
                smoothed_hit = 0.0

            if got_packet and last_hands:
                pose = last_hands[0].get("pose")
                if isinstance(pose, dict):
                    seen_pose_ever = True
                    open_palm_now, reason = is_open_palm(pose)
                else:
                    # Old sender still running on the Beelink. Refuse to guess
                    # from landmarks — guessing is what made this fire on any
                    # hand at all in the first place.
                    reason = "no_pose_in_payload"
                    if first_poseless_t is None:
                        first_poseless_t = now
                    elif not seen_pose_ever and (now - first_poseless_t) >= NO_POSE_GRACE_SEC:
                        maybe_close_serial()
                        fatal(
                            "\nERROR: hands are arriving with no 'pose' block — the Beelink is "
                            "still running the OLD leap_sender.py.\n"
                            "Copy beelink/leap_sender.py to\n"
                            "  C:\\Users\\Creative Machine 02\\Desktop\\"
                            "leapc-python-bindings-main\\leap_sender.py\n"
                            "on the Beelink and restart it, then rerun this script."
                        )

            if open_palm_now:
                last_open_seen_time = now

            smoothed_hit = (1.0 - HYST_ALPHA) * smoothed_hit + HYST_ALPHA * (1.0 if open_palm_now else 0.0)
            effective_open = (smoothed_hit > HYST_THRESH) or ((now - last_open_seen_time) <= MISS_GRACE_SEC)

            # After a success, require one genuinely-not-open frame before the
            # next fill can begin — otherwise leaving your hand up just refills.
            if not effective_open:
                armed = True
            gated_open = effective_open and (armed or not REARM_REQUIRES_RELEASE)

            if DEBUG_LOG:
                now_ms = _now_ms()
                if now_ms - last_log_ms >= LOG_EVERY_MS:
                    if hold_start is not None and gated_open:
                        approx_pct = int(min(100, 100 * (now - hold_start) / HOLD_REQUIRED_SEC))
                    else:
                        approx_pct = 0
                    if reason != last_reason or approx_pct != last_progress_pct:
                        detail = ""
                        if last_hands and isinstance(last_hands[0].get("pose"), dict):
                            detail = " " + _pose_summary(last_hands[0]["pose"])
                        _log(
                            f"[HI5-DEBUG] open={open_palm_now} eff={effective_open} "
                            f"armed={armed} reason={reason}{detail} progress≈{approx_pct}%"
                        )
                        last_reason = reason
                        last_progress_pct = approx_pct
                        last_log_ms = now_ms

            if gated_open:
                if hold_start is None:
                    hold_start = now
                elapsed = now - hold_start
                progress = min(1.0, elapsed / HOLD_REQUIRED_SEC)
                cutoff = HEIGHT - int(progress * HEIGHT)
                if cutoff < 0:
                    cutoff = 0
                frame = compose_fill_frame_from_filled(outline_mask, filled_mask, cutoff)
                send_frame_to_flipdot(frame)
                if progress >= 1.0:
                    _log("[HI5-DEBUG] SUCCESS: filled to 100% — holding, then resetting for next attempt")
                    time.sleep(SUCCESS_HOLD_SEC)
                    hold_start = None
                    smoothed_hit = 0.0
                    last_open_seen_time = 0.0
                    armed = False
                    send_frame_to_flipdot(outline_frame)
            else:
                hold_start = None
                send_frame_to_flipdot(outline_frame)

            time.sleep(LOOP_SLEEP_SEC)
    except KeyboardInterrupt:
        print("Interrupted.")
    finally:
        maybe_close_serial()


if __name__ == "__main__":
    main()
