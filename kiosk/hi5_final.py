#!/usr/bin/env python3
"""
HI-5 palm interactor — presence-aware edition

Adds:
- Presence → auto-open after ASSUME_OPEN_SEC (default 5s)
- No presence → exit after IDLE_ABORT_SEC (default 5s) so run_kiosk can relaunch
- Motion-based presence (EMA over downsampled luminance) in addition to hand landmarks

Common env:
  WHITE_VAL ("1"), FLIPDOT_SERIAL ("/dev/ttyS0"), FLIPDOT_BAUD ("57600")
  HOLD_REQUIRED_SEC ("2.0"), MISS_GRACE_SEC ("0.8")
  NEXT_SCRIPT (default: qr_works.py next to this script)
  SCROLL_STEP ("1"), SCROLL_DELAY ("0.1"), FONT_SPACING ("1")
  BIG_HI_PAUSE ("1.2"), TUI ("0")

Palm detection — landmarks come from the HuskyLens's built-in
ALGORITHM_HAND_RECOGNITION (camera hardware was swapped for the HuskyLens;
see STATUS.md — this used to run Picamera2 + MediaPipe Hands, which has no
camera to read from anymore). HuskyLens reports the same 21-point wrist-first
landmark layout MediaPipe uses, so the open-palm geometry below is unchanged,
just fed HuskyLens landmarks instead. Landmarks are raw sensor pixel
coordinates (HuskyLens doesn't report its working resolution), so
DIST_MARGIN/MIN_HAND_AREA are in pixel units now, not the old 0..1 fraction
— defaults are a starting point, expect to retune on real hardware:
  ANGLE_PIP_THRESH_DEG ("130"), ANGLE_DIP_THRESH_DEG ("118"), DIST_MARGIN ("3")
  RELAX_TWO_FINGERS ("1")

HuskyLens:
  HUSKYLENS_I2C_BUS ("1"), HUSKYLENS_I2C_ADDR ("0x50")

Timing / smoothing:
  LOOP_SLEEP_SEC ("0.03")
  HYST_ALPHA ("0.35"), HYST_THRESH ("0.35")

Presence logic (NEW):
  ASSUME_OPEN_SEC ("5.0")     # presence ≥ this → treat as open palm
  IDLE_ABORT_SEC ("5.0")      # no presence ≥ this → exit to kiosk

Fallbacks:
  FALLBACK_BBOX ("1")
  MIN_HAND_AREA ("5000")      # pixel-area of the landmark bbox, not a fraction

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



import os, sys, time, math, json, subprocess

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
MISS_GRACE_SEC    = float(os.getenv("MISS_GRACE_SEC", "1.5"))
NEXT_SCRIPT       = os.getenv("NEXT_SCRIPT", os.path.join(SCRIPT_DIR, "qr_works.py"))

MESSAGES = ["HI","I AM A FUTURE DONATION MACHINE","TO LEARN MORE","HI-5"]
SCROLL_STEP      = int(os.getenv("SCROLL_STEP", "1"))
SCROLL_DELAY     = float(os.getenv("SCROLL_DELAY", "0.1"))
BIG_HI_PAUSE     = float(os.getenv("BIG_HI_PAUSE", "1.2"))
TUI              = os.getenv("TUI", "0") == "1"

# Palm gate
ANGLE_PIP_THRESH_DEG = float(os.getenv("ANGLE_PIP_THRESH_DEG", "130"))
ANGLE_DIP_THRESH_DEG = float(os.getenv("ANGLE_DIP_THRESH_DEG", "118"))
DIST_MARGIN          = float(os.getenv("DIST_MARGIN", "3"))  # pixels, not 0..1 fraction
RELAX_TWO_FINGERS    = os.getenv("RELAX_TWO_FINGERS", "1") == "1"

# HuskyLens
HUSKYLENS_I2C_BUS  = int(os.getenv("HUSKYLENS_I2C_BUS", "1"))
HUSKYLENS_I2C_ADDR = int(os.getenv("HUSKYLENS_I2C_ADDR", "0x50"), 16)

# Timing / smoothing
LOOP_SLEEP_SEC   = float(os.getenv("LOOP_SLEEP_SEC", "0.03"))
HYST_ALPHA       = float(os.getenv("HYST_ALPHA", "0.5"))#tighten this to a higher number
HYST_THRESH      = float(os.getenv("HYST_THRESH", "0.5"))#tighten this to a higher number

# Presence
ASSUME_OPEN_SEC  = float(os.getenv("ASSUME_OPEN_SEC", "5.0"))
IDLE_ABORT_SEC   = float(os.getenv("IDLE_ABORT_SEC", "30"))

# Fallbacks
FALLBACK_BBOX     = os.getenv("FALLBACK_BBOX", "1") == "1"
MIN_HAND_AREA     = float(os.getenv("MIN_HAND_AREA", "5000"))  # pixel area, not a fraction

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
    # No raw camera frame available (HuskyLens does on-device detection
    # only) — just a status line + progress bar, no ASCII image.
    if not TUI: return
    tui_clear()
    title = "HI-5!" if detected else ("(presence)" if presence else "…")
    print("HuskyLens hand tracking —", title)
    width = 30
    n = int(max(0.0, min(1.0, progress)) * width)
    print(f"\nHold progress: [{'='*n}{' '*(width-n)}]  {progress*100:4.0f}%")

# ============== Hand + Presence helpers ==============
class _LM:
    """Minimal MediaPipe-landmark-shaped (.x/.y) point, fed from HuskyLens
    HandResult landmarks so is_open_palm() etc. don't need to change."""
    __slots__ = ("x", "y")
    def __init__(self, x, y):
        self.x = x
        self.y = y


def _dist(a, b): return math.hypot(a.x - b.x, a.y - b.y)

def _angle_deg(a, b, c):
    bax = a.x - b.x; bay = a.y - b.y
    bcx = c.x - b.x; bcy = c.y - b.y
    num = bax*bcx + bay*bcy
    den = math.hypot(bax, bay) * math.hypot(bcx, bcy) + 1e-9
    val = max(-1.0, min(1.0, num/den))
    return math.degrees(math.acos(val))

def _extended_finger(lm, mcp_i, pip_i, dip_i, tip_i) -> bool:
    wrist = lm[0]
    mcp, pip, dip, tip = lm[mcp_i], lm[pip_i], lm[dip_i], lm[tip_i]
    dist_ok = _dist(tip, wrist) > _dist(pip, wrist) + DIST_MARGIN
    ang_pip = _angle_deg(mcp, pip, dip)
    ang_dip = _angle_deg(pip, dip, tip)
    angle_ok = (ang_pip >= ANGLE_PIP_THRESH_DEG) and (ang_dip >= ANGLE_DIP_THRESH_DEG)
    return dist_ok and angle_ok

def _extended_thumb(lm) -> bool:
    wrist = lm[0]
    mcp, ip, tip = lm[2], lm[3], lm[4]
    dist_ok = _dist(tip, wrist) > _dist(ip, wrist) + (DIST_MARGIN * 0.6)
    ang_ip = _angle_deg(mcp, ip, tip)
    return dist_ok and (ang_ip >= (ANGLE_PIP_THRESH_DEG - 10))

def is_open_palm(lm) -> bool:
    idx = _extended_finger(lm, 5, 6, 7, 8)
    mid = _extended_finger(lm, 9, 10, 11, 12)
    rng = _extended_finger(lm, 13, 14, 15, 16)
    pky = _extended_finger(lm, 17, 18, 19, 20)
    ext_count = sum([idx, mid, rng, pky])
    if ext_count >= 3: return True
    if RELAX_TWO_FINGERS and ext_count >= 2: return True
    if ext_count >= 2 and _extended_thumb(lm): return True
    return False

def _bbox_area_norm(lm):
    xs = [p.x for p in lm]; ys = [p.y for p in lm]
    w = max(xs) - min(xs); h = max(ys) - min(ys)
    return w * h

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

    # --- Lazy import: only load once we actually need the sensor ---
    from DFRobot_HuskyLens import DFRobot_HuskyLens_I2C, ALGORITHM_HAND_RECOGNITION

    hl = DFRobot_HuskyLens_I2C(bus=HUSKYLENS_I2C_BUS, addr=HUSKYLENS_I2C_ADDR)
    for attempt in range(10):
        if hl.begin():
            break
        _log(f"[HI5] HuskyLens connect attempt {attempt + 1}/10 failed, retrying…")
        time.sleep(1)
    else:
        fatal("ERROR: could not connect to HuskyLens.")
    hl.write_algo(ALGORITHM_HAND_RECOGNITION)  # write_algo() settles for us

    hold_start = None
    satisfied = False
    last_seen_time = 0.0
    last_presence_time = time.time()
    presence_run_start = None
    absence_run_start  = time.time()
    smoothed_hit = 0.0

    # logging state (NEW)
    last_log_ms = _now_ms()
    last_reason = "idle"
    last_progress_pct = -1

    while True:
        try:
            got = hl.request()
        except Exception as e:
            _log(f"[HI5] HuskyLens read error: {e}")
            time.sleep(0.2)
            continue

        open_palm_now = False
        hand_present_now = False
        reason = "idle"

        if got and hl.count_blocks() > 0:
            hand_present_now = True
            block = hl.blocks()[0]  # largest/first hand only (MAX_HANDS=1 equivalent)
            lm = [_LM(x, y) for x, y in block.landmarks]
            # Primary: palm geometry
            if is_open_palm(lm):
                open_palm_now = True
                reason = "palm_geom"
            # Fallback: bbox area
            elif FALLBACK_BBOX and _bbox_area_norm(lm) >= MIN_HAND_AREA:
                open_palm_now = True
                reason = "bbox_fallback"

        # ---- Presence decision
        presence_now = hand_present_now
        now = time.time()

        PRESENCE_GRACE_SEC = float(os.getenv("PRESENCE_GRACE_SEC", "0.8"))
        if presence_now:
            last_presence_time = now
        presence_now = presence_now or ((now - last_presence_time) <= PRESENCE_GRACE_SEC)

        if presence_now:
            last_presence_time = now
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

        # Smooth + grace
        smoothed_hit = (1.0 - HYST_ALPHA) * smoothed_hit + HYST_ALPHA * (1.0 if open_palm_now else 0.0)
        effective_open = (smoothed_hit > HYST_THRESH) or ((now - last_seen_time) <= MISS_GRACE_SEC)

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
            maybe_close_serial()
            sys.exit(0)

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
