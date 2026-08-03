#!/usr/bin/env python3
"""
HI-5 palm interactor — presence-aware edition

Adds:
- Presence → auto-open after ASSUME_OPEN_SEC (default 5s)
- No presence → exit after IDLE_ABORT_SEC (default 30s) so run_kiosk can relaunch
- Motion-based presence (EMA over downsampled luminance) in addition to hand landmarks

Exit codes: 0 = ran to completion (the success path, which chains into
NEXT_SCRIPT/qr_works.py); EXIT_IDLE_ABORT (3) = gave up with nobody there.
run_kiosk.py skips its post-HI5 scan grace on 3 — see the constant's comment.
Either way we leave the idle hourglass on the panel on the way out, because
flipdots hold their last frame mechanically (see draw_idle_handoff_frame).

Common env:
  WHITE_VAL ("1"), FLIPDOT_SERIAL ("/dev/ttyS0"), FLIPDOT_BAUD ("57600")
  HOLD_REQUIRED_SEC ("1.5")
  MISS_GRACE_SEC ("0.35") — bridges a brief tracking dropout mid-hold; must
    stay well below HOLD_REQUIRED_SEC (see the note at its definition)
  NEXT_SCRIPT (default: qr_works.py next to this script)
  SCROLL_STEP ("1"), SCROLL_DELAY ("0.1"), FONT_SPACING ("1")
  BIG_HI_PAUSE ("1.2"), TUI ("0")

Palm detection — hand data comes from a Leap Motion Controller (a Beelink
Windows PC runs the actual sensor + Ultraleap tracking and streams UDP to
this Pi; see LEAP_HANDOFF.md), not the HuskyLens or the original
Picamera2 + MediaPipe Hands setup (see STATUS.md for that history).

The open-palm test thresholds Leap's own pose signals — Gemini already
knows whether the hand is open, so there's nothing to re-derive. Chiefly
`grab` (0.0 = flat open hand, 1.0 = closed fist), plus per-digit
is_extended and pinch strength. This replaced a landmark-geometry test
with a bbox-area fallback that fired for *any* hand in view; see the
comment above is_open_palm() for why both were unfixable on a Leap feed,
and kiosk/hi5_palm_debug.py for a no-intro harness for tuning these:
  MAX_GRAB_STRENGTH ("0.20")    — above this = too closed
  MAX_PINCH_STRENGTH ("0.40")   — above this = pinching, not a flat palm
  MIN_EXTENDED_FINGERS ("4")    — of 5; 4 tolerates a tucked thumb
  MIN_CONFIDENCE ("0.0")        — Leap's own tracking confidence
  PALM_FACING_AXIS ("")         — off by default; set to a Leap axis
    ("y", "-y", "z", …) to also require the palm to face that way. Mount-
    dependent, so tune it by watching hi5_palm_debug.py's palm_n= values.
  PALM_FACING_MIN_DOT ("0.5")   — how strictly, once the axis is set

REQUIRES a leap_sender.py new enough to send the "pose" block. The Beelink
runs a hand-copied sender, so if hands arrive without one this exits back
to the kiosk after NO_POSE_GRACE_SEC rather than silently reverting to
"any hand counts".

Leap Motion feed:
  LISTEN_PORT ("5111")     — UDP port, must match beelink/leap_sender.py's RPI_PORT
  UDP_STALE_SEC ("0.5")    — no packets at all for this long means the sender
                             or network is down; a hand *leaving* arrives as an
                             explicit empty-hands packet instead
  NO_POSE_GRACE_SEC ("5.0")

Timing / smoothing:
  LOOP_SLEEP_SEC ("0.03")
  HYST_ALPHA ("0.35"), HYST_THRESH ("0.35")

Presence logic (NEW):
  ASSUME_OPEN_SEC ("5.0")     # presence ≥ this → treat as open palm
  IDLE_ABORT_SEC ("5.0")      # no presence ≥ this → exit to kiosk

Palm asset:
  PALM_JSON (default: assets/palm_combo.json relative to this script)

"""
# --- FAST FIRST FRAME: draw a big, centered "HI" immediately, then continue ---
import os, sys, time

# Use the same panel polarity as the rest of the file
WHITE_VAL_EARLY = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL_EARLY = 1 - WHITE_VAL_EARLY

# One knob to control how long the bitmap HI stays on screen
BIG_HI_PAUSE = float(os.getenv("BIG_HI_PAUSE", "2"))

# Flag to tell main() we've already shown the HI so it won't redraw/clear it
_FAST_FIRST_SENT = False

# Minimal 5x7 font + scaler (matches the later text renderer look)
FONT5x7_MIN = {
    "H": ["10001","10001","11111","10001","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
}

def _render_big_text_center_early(text: str, max_scale: int = 4):
    text = text.upper()
    glyph_w, glyph_h = 5, 7
    # largest scale that fits 28x28; spacing = scale
    for scale in range(max_scale, 0, -1):
        spacing = scale
        msg_w = len(text) * (glyph_w * scale + spacing) - spacing
        msg_h = glyph_h * scale
        if msg_w <= 28 and msg_h <= 28:
            break
    x0 = max(0, (28 - msg_w)//2)
    y0 = max(0, (28 - msg_h)//2)
    frame = [[WHITE_VAL_EARLY]*28 for _ in range(28)]
    cursor_x = x0
    for ch in text:
        patt = FONT5x7_MIN.get(ch, ["00000"]*7)
        for ry, row in enumerate(patt):
            for rx, c in enumerate(row):
                if c == "1":
                    for yy in range(scale):
                        fy = y0 + ry*scale + yy
                        if 0 <= fy < 28:
                            for xx in range(scale):
                                fx = cursor_x + rx*scale + xx
                                if 0 <= fx < 28:
                                    frame[fy][fx] = BLACK_VAL_EARLY
        cursor_x += glyph_w * scale + scale
    return frame

def _pack_28x28_to_panels(bw28):
    panels = []
    for p in range(4):
        off = p*7
        data = bytearray()
        for x in range(28):
            b = 0
            for y in range(7):
                b |= (int(bw28[off+y][x]) & 1) << y
            data.append(b)
        panels.append(data)
    return panels

def _send_one_frame_fast(bw28):
    try:
        import serial
        SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
        BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0, write_timeout=0)
        for addr, data in zip([1,2,3,4], _pack_28x28_to_panels(bw28)):
            ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
        ser.flush(); ser.close()
    except Exception as e:
        print(f"[HI5] fast first frame skipped: {e}", flush=True)

try:
    # send the fast bitmap HI (centered, scaled)
    _send_one_frame_fast(_render_big_text_center_early("HI", max_scale=4))
    _FAST_FIRST_SENT = True
    print("[HI5] start (fast first frame sent) → continuing setup…", flush=True)
    # keep it on screen for BIG_HI_PAUSE seconds
    time.sleep(BIG_HI_PAUSE)
except Exception:
    pass
# --- END FAST FIRST FRAME BLOCK ---



import os, sys, time, json, subprocess

# Force line-buffering (or fully unbuffered) for immediate logs
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    try:
        # Fallback for older Pythons
        sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 1)  # line-buffered
    except Exception:
        pass


from typing import List

# ============== CONFIG ==============
WIDTH, HEIGHT = 28, 28
WHITE_VAL = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL = 1 - WHITE_VAL

SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

HOLD_REQUIRED_SEC = float(os.getenv("HOLD_REQUIRED_SEC", "1.5"))
# Bridges a brief tracking dropout mid-hold. Must stay well BELOW
# HOLD_REQUIRED_SEC: it keeps the fill counting after the last open frame, so
# at the old 1.5s (== the hold) a single detected frame completed the whole
# gesture on its own.
MISS_GRACE_SEC    = float(os.getenv("MISS_GRACE_SEC", "0.35"))
NEXT_SCRIPT       = os.getenv("NEXT_SCRIPT", os.path.join(SCRIPT_DIR, "qr_works.py"))

MESSAGES = ["HI","I AM A FUTURE DONATION MACHINE","TO LEARN MORE","HI-5"]
SCROLL_STEP      = int(os.getenv("SCROLL_STEP", "1"))
SCROLL_DELAY     = float(os.getenv("SCROLL_DELAY", "0.1"))
BIG_HI_PAUSE     = float(os.getenv("BIG_HI_PAUSE", "1.2"))
TUI              = os.getenv("TUI", "0") == "1"

# Palm gate — thresholds on Leap's own pose signals (see is_open_palm below)
MAX_GRAB_STRENGTH    = float(os.getenv("MAX_GRAB_STRENGTH", "0.20"))
MAX_PINCH_STRENGTH   = float(os.getenv("MAX_PINCH_STRENGTH", "0.40"))
MIN_EXTENDED_FINGERS = int(os.getenv("MIN_EXTENDED_FINGERS", "4"))
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", "0.0"))
PALM_FACING_AXIS     = os.getenv("PALM_FACING_AXIS", "").strip()
PALM_FACING_MIN_DOT  = float(os.getenv("PALM_FACING_MIN_DOT", "0.5"))

# Leap Motion UDP feed
LISTEN_PORT   = int(os.getenv("LISTEN_PORT", "5111"))
UDP_STALE_SEC = float(os.getenv("UDP_STALE_SEC", "0.5"))
# How long to tolerate hands arriving with no "pose" block (an old
# leap_sender.py still running on the Beelink) before giving up and exiting
# back to the kiosk. Not fatal-with-error: run_kiosk.py would just relaunch
# us into the same wall, so a clean exit lets the FSM move on.
NO_POSE_GRACE_SEC = float(os.getenv("NO_POSE_GRACE_SEC", "5.0"))

# Timing / smoothing
LOOP_SLEEP_SEC   = float(os.getenv("LOOP_SLEEP_SEC", "0.03"))
HYST_ALPHA       = float(os.getenv("HYST_ALPHA", "0.5"))#tighten this to a higher number
HYST_THRESH      = float(os.getenv("HYST_THRESH", "0.5"))#tighten this to a higher number

# Presence
ASSUME_OPEN_SEC  = float(os.getenv("ASSUME_OPEN_SEC", "5.0"))
IDLE_ABORT_SEC   = float(os.getenv("IDLE_ABORT_SEC", "30"))

# Exit code telling run_kiosk.py "I gave up; nobody was ever here". It skips
# its post-HI5 scan grace on this code, because that grace only exists to give
# a real visitor time to scan the QR — and on this path there is no visitor
# and no QR. Plain exit 0 still means "ran to completion" (the success path,
# which chains into qr_works.py) and still earns the full grace.
EXIT_IDLE_ABORT  = 3

# Touched the moment the palm hold completes, i.e. the moment we commit to the
# QR flow. run_kiosk.py's HI5_IDLE_TIMEOUT_SEC counts from when it *spawned*
# us, and we stay alive through the "SCAN ME" card and qr_works.py — so
# without this, finishing the hold just before that cap expires gets us killed
# mid-QR. run_kiosk.py stops enforcing the cap once this file appears; it
# deletes it before each spawn, so a leftover can't disarm a later run.
HI5_COMMIT_PATH  = os.getenv("HI5_COMMIT_PATH", "/tmp/hi5_committed")

PALM_JSON         = os.getenv("PALM_JSON", os.path.join(SCRIPT_DIR, "..", "assets", "palm_combo.json"))

# Logging (NEW)
DEBUG_LOG        = os.getenv("DEBUG_LOG", "1") == "1"
LOG_EVERY_MS     = int(os.getenv("LOG_EVERY_MS", "350"))   # throttle logs


def fatal(msg: str):
    print(msg); sys.exit(1)

# ============== Display I/O ==============
# Same panel protocol _send_one_frame_fast/_pack_28x28_to_panels above use
# (qr_works.py / attract_v2.py / anim.py all speak it too):
#   [0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F] per panel.
# This used to import a `flipdot_driver` module for this, but that file
# lives in legacy/ (not on kiosk/'s import path) — the ImportError fallback
# silently kicked in and sent a different, incompatible packet shape
# instead, so nothing past the very first "HI" frame ever actually reached
# a real panel. Just send the real protocol directly.
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

def fill_canvas(val: int):
    send_frame_to_flipdot([[val]*WIDTH for _ in range(HEIGHT)])

def clear_white():
    print(f"[HI5] cleared {time.time():.3f}s → drawing big HI…intro text....")
    fill_canvas(WHITE_VAL)

def draw_idle_handoff_frame():
    """Leave the panel on the idle hourglass instead of a stale HI-5 outline.

    Flipdots are bistable: the dots physically hold their last position with
    no power and no refresh, so whatever we sent last stays mechanically on
    the panel until some *other* process writes over it. On the abort paths
    we're about to exit and close the serial port, and attract_leap.py takes
    a moment to come back up — without this the panel sits on a half-drawn
    HI-5 belonging to a stage that already ended.

    Drawing hourglass.py's t=0 frame (rather than blanking) means the handoff
    to attract_leap.py's idle animation is seamless: it resets to a full glass
    on each return to idle, so its first frame is this same frame.

    Polarity: frame_at() returns truthy for *ink* (walls and sand). Deliberately
    do NOT reuse this file's WHITE_VAL/BLACK_VAL here — hi5_final.py and
    attract_leap.py read the same WHITE_VAL env var with opposite meanings
    (ink=BLACK_VAL here, ink=WHITE_VAL there; see attract_leap.py's docstring),
    and they only agree while both are left at their opposite defaults. Since
    this is attract_leap.py's artwork, map it with attract_leap.py's rule and
    default, so the frame we leave is bit-identical to the one it draws a
    moment later no matter how WHITE_VAL is set.

    Best-effort: any failure here must not stop us exiting, since a stuck HI5
    is worse than a stale frame.
    """
    try:
        from hourglass import frame_at  # sits next to this file
        ink_bit = int(os.getenv("WHITE_VAL", "0"))  # attract_leap.py's rule
        bg_bit  = 1 - ink_bit
        send_frame_to_flipdot([[ink_bit if cell else bg_bit for cell in row]
                               for row in frame_at(0.0)])
    except Exception as e:
        print(f"[HI5] idle handoff frame failed ({e}); leaving panel as-is")

# ============== Text (5x7) ==============
def blank(fill=BLACK_VAL):
    return [[fill]*WIDTH for _ in range(HEIGHT)]

FONT5x7 = {
    " ": ["00000","00000","00000","00000","00000","00000","00000"],
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","11110","10001","10001","10001","11110"],
    "C": ["01110","10001","10000","10000","10000","10001","01110"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","11110","10000","10000","10000","11111"],
    "F": ["11111","10000","11110","10000","10000","10000","10000"],
    "G": ["01110","10001","10000","10111","10001","10001","01111"],
    "H": ["10001","10001","11111","10001","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["00001","00001","00001","00001","10001","10001","01110"],
    "K": ["10001","10010","11100","10010","10010","10001","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10010","10001","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","01010","00100","00100","00100","01010","10001"],
    "Y": ["10001","01010","00100","00100","00100","00100","00100"],
    "Z": ["11111","00010","00100","00100","01000","10000","11111"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    "?": ["01110","10001","00010","00100","00100","00000","00100"],
    "0": ["01110","10001","10001","10001","10001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","11110","00001","00001","10001","01110"],
    "6": ["00110","01000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00010","11100"],
}
DASH_FIX = {"—": "-", "–": "-", "−": "-", "‒": "-", "―": "-", "-": "-"}

def sanitize_text(text: str) -> str:
    for k, v in DASH_FIX.items(): text = text.replace(k, v)
    return text

def _font_defs():
    spacing = int(os.getenv("FONT_SPACING", "1"))
    return (FONT5x7, 5, 7, spacing)

def draw_char(frame, ch, x0, y0, color=BLACK_VAL):
    font, glyph_w, glyph_h, _ = _font_defs()
    patt = font.get(ch.upper(), font["?"])
    for y, row in enumerate(patt):
        fy = y0 + y
        if 0 <= fy < HEIGHT:
            for x, c in enumerate(row):
                fx = x0 + x
                if 0 <= fx < WIDTH and c == "1":
                    frame[fy][fx] = color

def render_text_scroller_centered(text: str, speed_cols_per_step=1):
    text = sanitize_text(text.upper())
    font, glyph_w, glyph_h, spacing = _font_defs()
    msg_w = len(text) * (glyph_w + spacing) - spacing
    strip = [[0]*msg_w for _ in range(glyph_h)]
    x = 0
    for ch in text:
        patt = font.get(ch, font["?"])
        for yy, row in enumerate(patt):
            for xx, c in enumerate(row):
                if c == "1": strip[yy][x+xx] = 1
        x += glyph_w + spacing
    y0 = max(0, (HEIGHT - glyph_h)//2)
    for offset in range(WIDTH, -msg_w, -speed_cols_per_step):
        f = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
        for yy in range(glyph_h):
            fy = y0 + yy
            if 0 <= fy < HEIGHT:
                for xx in range(msg_w):
                    fx = offset + xx
                    if 0 <= fx < WIDTH and strip[yy][xx]:
                        f[fy][fx] = BLACK_VAL
        yield f

def draw_block(frame, x0, y0, size, color):
    for yy in range(size):
        fy = y0 + yy
        if 0 <= fy < HEIGHT:
            for xx in range(size):
                fx = x0 + xx
                if 0 <= fx < WIDTH:
                    frame[fy][fx] = color

def compose_big_text_center(text: str, max_scale: int = 4):
    text = sanitize_text(text.upper())
    font, glyph_w, glyph_h, _ = _font_defs()
    for scale in range(max_scale, 0, -1):
        spacing = scale
        msg_w = len(text) * (glyph_w * scale + spacing) - spacing
        msg_h = glyph_h * scale
        if msg_w <= WIDTH and msg_h <= HEIGHT:
            break
    x0 = max(0, (WIDTH  - msg_w)//2)
    y0 = max(0, (HEIGHT - msg_h)//2)
    frame = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
    cursor_x = x0
    for ch in text:
        patt = font.get(ch, font["?"])
        for ry, row in enumerate(patt):
            for rx, c in enumerate(row):
                if c == "1":
                    draw_block(frame, cursor_x + rx*scale, y0 + ry*scale, scale, BLACK_VAL)
        cursor_x += glyph_w * scale + spacing
    return frame

# ============== Palm JSON ==============
def load_palm_from_json(path: str):
    if not os.path.exists(path): fatal(f"ERROR: JSON not found: {path}")
    with open(path, "r") as f: data = json.load(f)
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
    frame = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if outline[y][x] == 1: frame[y][x] = BLACK_VAL
    return frame

def compose_fill_frame_from_filled(outline, filled, cutoff_row: int):
    frame = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if outline[y][x] == 1: frame[y][x] = BLACK_VAL
        if y >= cutoff_row:
            for x in range(WIDTH):
                if filled[y][x] == 1: frame[y][x] = BLACK_VAL
    return frame

# ============== TUI ==============
def tui_clear(): print("\x1b[2J\x1b[H", end="")
def tui_print_preview(detected: bool, progress: float, presence: bool):
    # No raw camera frame available (Leap does on-device tracking only,
    # we just get landmark coordinates) — just a status line + progress
    # bar, no ASCII image.
    if not TUI: return
    tui_clear()
    title = "HI-5!" if detected else ("(presence)" if presence else "…")
    print("Leap Motion hand tracking —", title)
    width = 30
    n = int(max(0.0, min(1.0, progress)) * width)
    print(f"\nHold progress: [{'='*n}{' '*(width-n)}]  {progress*100:4.0f}%")

# ============== Hand + Presence helpers ==============
# Open-palm detection uses the pose signals Leap's Gemini tracking model
# already computes (grab_strength, per-digit is_extended, palm normal),
# shipped in the UDP payload's "pose" block by beelink/leap_sender.py.
#
# It used to re-derive finger extension from the 21 2D landmarks, with a
# bounding-box-area fallback. Both are wrong on a Leap feed: the landmarks
# are a 2D projection (PROJECT_AXES drops one of Leap's three axes, taking
# most of the finger-curl information with it), and the bbox fallback's
# MIN_HAND_AREA was tuned in HuskyLens sensor *pixels* while Leap landmarks
# are *millimetres* — 5000 became a ~70x70mm box, smaller than a fist, so it
# fired for any hand in view regardless of shape and the fill ran to 100%
# whatever you did. Don't reintroduce either; threshold pose.grab.
# See LEAP_HANDOFF.md, and kiosk/hi5_palm_debug.py for the tuning harness.

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_warned = set()


def _warn_once(key: str, msg: str):
    if key not in _warned:
        _warned.add(key)
        _log(f"[HI5] {msg}")


def _parse_facing_axis(spec: str):
    if not spec:
        return None
    sign = -1.0 if spec.startswith("-") else 1.0
    axis = spec.lstrip("-").lower()
    if axis not in _AXIS_INDEX:
        fatal(f"ERROR: PALM_FACING_AXIS={spec!r} — expected x, y or z, optionally '-'-prefixed")
    return _AXIS_INDEX[axis], sign


_FACING = _parse_facing_axis(PALM_FACING_AXIS)


def _num(pose, key):
    v = pose.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_open_palm(pose: dict):
    """True if `pose` looks like a flat, open, forward-facing hand.

    Sub-tests whose field the sender couldn't read (value None) are skipped
    with a one-time warning, so older LeapC bindings degrade to a looser test
    rather than failing shut. Returns (bool, reason) — reason names the first
    failing gate, which is what makes the debug log useful for tuning.
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


def _now_ms():
    return int(time.time() * 1000)

def _log(msg):
    if DEBUG_LOG:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


# ============== MAIN ==============
def main():
    print(f"[HI5] start {time.time():.3f}s")

    outline_mask, filled_mask = load_palm_from_json(PALM_JSON)
    if not _FAST_FIRST_SENT:
        clear_white()


    # Intro text
    if len(MESSAGES) > 0 and MESSAGES[0].strip().upper() == "HI":
        if not _FAST_FIRST_SENT:
            send_frame_to_flipdot(compose_big_text_center("HI", max_scale=4))
            time.sleep(BIG_HI_PAUSE)
        scroll_list = MESSAGES[1:]
    else:
        scroll_list = MESSAGES

    for msg in scroll_list:
        for frame in render_text_scroller_centered(msg, speed_cols_per_step=SCROLL_STEP):
            send_frame_to_flipdot(frame); time.sleep(SCROLL_DELAY)

    outline_frame = compose_outline_frame(outline_mask)
    send_frame_to_flipdot(outline_frame)
    time.sleep(0.8)
    print(f"Palm prompt shown. Starting camera + detection… (hold {HOLD_REQUIRED_SEC:.1f}s)")

    # --- Leap Motion UDP listener (Beelink PC -> UDP -> Pi). Safe to bind
    # the same port attract_leap.py uses — run_kiosk.py's state machine
    # guarantees that process is fully stopped before this one starts. ---
    import socket as _socket

    _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    _sock.bind(("0.0.0.0", LISTEN_PORT))
    _sock.setblocking(False)  # drained each pass; LOOP_SLEEP_SEC paces the loop
    _log(f"[HI5] UDP listener on :{LISTEN_PORT}")

    hold_start = None
    satisfied = False
    last_seen_time = 0.0
    last_open_seen_time = 0.0
    last_presence_time = time.time()
    presence_run_start = None
    absence_run_start  = time.time()
    smoothed_hit = 0.0
    last_hands = None
    last_packet_t = 0.0
    first_poseless_t = None
    seen_pose_ever = False

    # logging state (NEW)
    last_log_ms = _now_ms()
    last_reason = "idle"
    last_progress_pct = -1

    while True:
        # Drain the socket every pass: act on the newest packet only, and never
        # let a backlog turn into growing latency.
        got_packet = False
        while True:
            try:
                data, _addr = _sock.recvfrom(8192)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                _log(f"[HI5] UDP read error: {e}")
                break
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                _log(f"[HI5] bad packet: {e}")
                continue
            last_hands = payload.get("hands") or []
            got_packet = True

        now = time.time()
        if got_packet:
            last_packet_t = now

        # A pose is evidence of an open palm only for the packet it arrived in.
        # Re-evaluating the last-received pose on every loop pass (which is what
        # this used to do) meant a hand that had already left kept "detecting"
        # as open until the staleness timeout expired, stacking an extra
        # ~UDP_STALE_SEC of fill on top of MISS_GRACE_SEC — enough that showing
        # a palm briefly and yanking it away still filled to 100%. Bridging
        # dropouts is MISS_GRACE_SEC's job, and it alone.
        open_palm_now = False
        hand_present_now = bool(got_packet and last_hands)
        hand_gone = False
        if hand_present_now:
            reason = "hand"                          # refined by is_open_palm below
        elif got_packet:
            reason, hand_gone = "hand_gone", True    # explicit {"hands": []}
        elif (now - last_packet_t) > UDP_STALE_SEC:
            reason, hand_gone = "feed_stale", True   # sender/network down
        else:
            reason = "between_packets"               # normal at 30Hz, not absence

        if hand_gone:
            # Positive evidence of absence, not a dropout — cancel the grace
            # window outright instead of letting it coast.
            last_open_seen_time = 0.0
            smoothed_hit = 0.0

        if hand_present_now:
            pose = last_hands[0].get("pose")  # first/preferred hand only
            if isinstance(pose, dict):
                seen_pose_ever = True
                open_palm_now, reason = is_open_palm(pose)
            else:
                # Old leap_sender.py on the Beelink. Refuse to guess from the
                # landmarks — guessing is what made this fire on any hand at all.
                reason = "no_pose_in_payload"
                if first_poseless_t is None:
                    first_poseless_t = now
                elif not seen_pose_ever and (now - first_poseless_t) >= NO_POSE_GRACE_SEC:
                    _log("[HI5] ABORT: hands arriving with no 'pose' block — the Beelink "
                         "is running an old leap_sender.py (see LEAP_HANDOFF.md). "
                         "Exiting to kiosk.")
                    # Same deal as the idle abort: nobody got a QR here either,
                    # so don't make the kiosk sit out the scan grace.
                    draw_idle_handoff_frame()
                    maybe_close_serial()
                    sys.exit(EXIT_IDLE_ABORT)

        # ---- Presence decision
        # Presence must survive the gaps between packets (unlike open-palm
        # detection above, which must not), so it keys off packet recency
        # rather than "did a packet land on this exact pass".
        presence_now = bool(last_hands) and (now - last_packet_t) <= UDP_STALE_SEC

        PRESENCE_GRACE_SEC = float(os.getenv("PRESENCE_GRACE_SEC", "0.8"))
        if presence_now:
            last_presence_time = now
        # Grace-extended presence: still count as "present" for a short
        # window after the last *real* sighting (hand_present_now), so a
        # one-frame dropout doesn't immediately read as absence.
        # last_presence_time must only ever be set from a real detection
        # (above) — NOT re-stamped just because we're currently inside the
        # grace window (that used to happen below), or the window would
        # perpetually re-arm itself off its own truthiness and absence could
        # never accumulate, making IDLE_ABORT_SEC unreachable.
        presence_now = presence_now or ((now - last_presence_time) <= PRESENCE_GRACE_SEC)

        if presence_now:
            absence_run_start = None
            if presence_run_start is None:
                presence_run_start = now
        else:
            presence_run_start = None
            if absence_run_start is None:
                absence_run_start = now

        presence_cont = (now - presence_run_start) if presence_run_start else 0.0
        absence_cont  = (now - absence_run_start)  if absence_run_start  else 0.0

        # Auto-assume only if we've seen a hand recently (guard against empty scene)
        ALLOW_AUTO_ASSUME     = os.getenv("ALLOW_AUTO_ASSUME", "0") == "1" #change this to 1 if we want the safeguard to be ON
        HAND_SEEN_WITHIN_SEC  = float(os.getenv("HAND_SEEN_WITHIN_SEC", "1.0"))
        recent_hand_seen      = (now - last_seen_time) <= HAND_SEEN_WITHIN_SEC

        if (ALLOW_AUTO_ASSUME
            and not open_palm_now
            and presence_cont >= ASSUME_OPEN_SEC
            and recent_hand_seen):
            open_palm_now = True
            reason = "presence_auto_assume"

        if hand_present_now:
            last_seen_time = now
        if open_palm_now:
            last_open_seen_time = now

        # Smooth + grace. Grace is keyed off last_open_seen_time, not
        # last_seen_time — it exists to bridge a brief tracking dropout
        # while the hand stays open, not to keep counting while a
        # present-but-different hand shape (e.g. a peace sign) is in view.
        smoothed_hit = (1.0 - HYST_ALPHA) * smoothed_hit + HYST_ALPHA * (1.0 if open_palm_now else 0.0)
        effective_open = (smoothed_hit > HYST_THRESH) or ((now - last_open_seen_time) <= MISS_GRACE_SEC)

        # ---- Throttled debug log (NEW)
        if DEBUG_LOG:
            now_ms = _now_ms()
            if now_ms - last_log_ms >= LOG_EVERY_MS:
                # Rough progress estimate (human-friendly)
                if hold_start is not None and effective_open:
                    approx_pct = int(min(100, 100 * (time.time() - hold_start) / HOLD_REQUIRED_SEC))
                else:
                    approx_pct = 0

                # Only print when reason changes OR progress moves
                if reason != last_reason or (effective_open and approx_pct != last_progress_pct):
                    _log(
                        f"[HI5] open={open_palm_now} eff_open={effective_open} "
                        f"reason={reason} presence={presence_now} "
                        f"presence_cont={presence_cont:.1f}s absence_cont={absence_cont:.1f}s "
                        f"progress≈{approx_pct}%"
                    )
                    last_reason = reason
                    last_progress_pct = approx_pct
                    last_log_ms = now_ms

        # ---- Idle abort: no humans for ≥ IDLE_ABORT_SEC → exit
        if absence_cont >= IDLE_ABORT_SEC:
            _log(f"[HI5] ABORT: no presence for {absence_cont:.1f}s → exit to kiosk")
            draw_idle_handoff_frame()
            maybe_close_serial()
            sys.exit(EXIT_IDLE_ABORT)

        # ---- Render
        if effective_open:
            if hold_start is None:
                hold_start = now
            elapsed = now - hold_start
            progress = min(1.0, elapsed / HOLD_REQUIRED_SEC)
            cutoff = HEIGHT - int(progress * HEIGHT)
            if cutoff < 0: cutoff = 0
            frame = compose_fill_frame_from_filled(outline_mask, filled_mask, cutoff)
            send_frame_to_flipdot(frame)
            if TUI: tui_print_preview(True, progress, presence_now)
            if progress >= 1.0:
                _log("[HI5] SUCCESS: filled to 100% — chaining to NEXT_SCRIPT")
                # Tell run_kiosk.py to stop enforcing its idle cap — from here
                # on we're the QR flow, not an idle hi-5 stage.
                try:
                    with open(HI5_COMMIT_PATH, "w") as f:
                        f.write(str(time.time()))
                except Exception as e:
                    print(f"[HI5] could not write commit marker ({e})")
                satisfied = True
                break
        else:
            hold_start = None
            send_frame_to_flipdot(outline_frame)
            if TUI: tui_print_preview(False, 0.0, presence_now)

        time.sleep(LOOP_SLEEP_SEC)

    if satisfied:
        def compose_two_line_center_label(line1: str, line2: str, line_spacing: int = 2):
            f = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
            font, glyph_w, glyph_h, spacing = _font_defs()
            line1 = sanitize_text(line1.upper()); line2 = sanitize_text(line2.upper())
            w1 = len(line1) * (glyph_w + spacing) - (spacing if line1 else 0)
            w2 = len(line2) * (glyph_w + spacing) - (spacing if line2 else 0)
            total_h = glyph_h*2 + line_spacing
            y0 = max(0, (HEIGHT - total_h)//2)
            x1 = max(0, (WIDTH - w1)//2); x2 = max(0, (WIDTH - w2)//2)
            for i, ch in enumerate(line1): draw_char(f, ch, x1 + i*(glyph_w + spacing), y0, BLACK_VAL)
            y2 = y0 + glyph_h + line_spacing
            for i, ch in enumerate(line2): draw_char(f, ch, x2 + i*(glyph_w + spacing), y2, BLACK_VAL)
            return f

        send_frame_to_flipdot(compose_two_line_center_label("SCAN", "ME", line_spacing=2))
        time.sleep(3.0)
        maybe_close_serial()
        env = os.environ.copy()
        env.setdefault("FLIPDOT_SERIAL", SERIAL_PORT)
        env.setdefault("FLIPDOT_BAUD", str(BAUD_RATE))
        subprocess.run(["/usr/bin/python3", NEXT_SCRIPT], env=env, check=False)
        return

    print("Done.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
