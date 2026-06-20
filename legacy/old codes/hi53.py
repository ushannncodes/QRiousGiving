#!/usr/bin/env python3
"""
hi5_interact.py — 28×28 flip-dot + HI-5 open palm flow with terminal preview

- Intro lines scroll horizontally (white background), centered vertically
- HI-5 page: "HI-5" at top (row 1..), row 0 stays white, open-palm outline centered below
- Camera + MediaPipe start only after outline is on the display
- Optional SSH ASCII preview: run with TUI=1
- Uses BIG 5x7 font only

Relies on flipdot_driver.send_frame_to_flipdot(frame28) for your panel protocol.
"""

import os, sys, time, math, subprocess
from typing import List

# ================== CONFIG ==================
WIDTH, HEIGHT = 28, 28

# If your pipeline maps 1→white, keep WHITE_VAL=1 (default). If inverted, set WHITE_VAL=0.
WHITE_VAL = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL = 1 - WHITE_VAL

SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))

HOLD_REQUIRED_SEC = float(os.getenv("HOLD_REQUIRED_SEC", "3.0"))
NEXT_SCRIPT = os.getenv("NEXT_SCRIPT", "/home/pi/Desktop/qr_works.py")

# Brief gaps won’t reset the hold (helps noisy detection)
MISS_GRACE_SEC = float(os.getenv("MISS_GRACE_SEC", "0.4"))

MESSAGES = [
    "HI",
    "I AM A FUTURE DONATION MACHINE",
    "TO LEARN MORE",
    "HI-5",
]

# Scroll tunables (can override via env without editing code)
SCROLL_STEP  = int(os.getenv("SCROLL_STEP", "1"))            # columns per step (bigger=faster)
SCROLL_DELAY = float(os.getenv("SCROLL_DELAY", "0.06"))      # seconds between frames (smaller=faster)

# Terminal preview (ASCII camera + progress bar). 1=on, 0=off.
TUI = os.getenv("TUI", "0") == "1"

# Skip intro text and go straight to palm detection if set
SKIP_INTRO = os.getenv("SKIP_INTRO", "0") == "1"

# Detection tunables (raise or lower to tighten/loosen detection)
ANGLE_PIP_THRESH_DEG = float(os.getenv("ANGLE_PIP_THRESH_DEG", "150"))  # straight-ish finger (lower = more tolerant)
ANGLE_DIP_THRESH_DEG = float(os.getenv("ANGLE_DIP_THRESH_DEG", "140"))
DIST_MARGIN          = float(os.getenv("DIST_MARGIN", "0.015"))         # tip further from wrist than PIP by this
MIN_DET_CONF         = float(os.getenv("MIN_DET_CONF", "0.35"))
MIN_TRACK_CONF       = float(os.getenv("MIN_TRACK_CONF", "0.25"))
MAX_HANDS            = int(os.getenv("MAX_HANDS", "8"))                  # allow multiple people

# Pulse/outline for palm
OUTLINE_THICKNESS = int(os.getenv("OUTLINE_THICKNESS", "1"))  # 1..3 is sensible on 28×28
PULSE_OUTLINE     = os.getenv("PULSE_OUTLINE", "1") == "0"    # 1=on, 0=off

# ================== DEPENDENCIES ==================
def fatal(msg: str):
    print(msg)
    sys.exit(1)

try:
    from picamera2 import Picamera2
except Exception:
    fatal("ERROR: Picamera2 not found. Install: sudo apt-get install -y python3-picamera2")

try:
    import mediapipe as mp
    mp_hands = mp.solutions.hands
except Exception:
    fatal("ERROR: mediapipe not found. Install per-user: python3 -m pip install --user mediapipe==0.10.9 (or mediapipe-rpi)")

# ================== DISPLAY IO ==================
driver_close_serial = None
try:
    from flipdot_driver import send_frame_to_flipdot as _send
    try:
        from flipdot_driver import close_serial as driver_close_serial
    except Exception:
        driver_close_serial = None
    def send_frame_to_flipdot(frame28: List[List[int]]):
        _send(frame28)
except ImportError:
    # Minimal fallback (replace with your real protocol if ever used)
    try:
        import serial
    except Exception:
        fatal("ERROR: pyserial missing. Install: sudo apt-get install -y python3-serial")
    _ser = None
    def _ensure_serial():
        global _ser
        if _ser is None or not _ser.is_open:
            _ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        return _ser
    def send_frame_to_flipdot(frame28: List[List[int]]):
        s = _ensure_serial()
        s.write(bytes([0x80, 0x01]))
        for x in range(WIDTH):
            col_bits = 0
            for y in range(HEIGHT):
                col_bits |= (1 if frame28[y][x] else 0) << y
            s.write(col_bits.to_bytes(4, "little"))
        s.write(b"\xFF"); s.flush()

def maybe_close_serial():
    try:
        if driver_close_serial:
            driver_close_serial()
    except Exception:
        pass

def fill_canvas(val: int):
    send_frame_to_flipdot([[val]*WIDTH for _ in range(HEIGHT)])

def clear_white():
    fill_canvas(WHITE_VAL)

# ================== DRAWING HELPERS (5×7 ONLY) ==================
def blank(fill=BLACK_VAL):
    return [[fill]*WIDTH for _ in range(HEIGHT)]

# 5×7 font (uppercase + digits + hyphen)
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

# Replace smart dashes with plain hyphen so "HI-5" always renders
DASH_FIX = {"—": "-", "–": "-", "−": "-", "‒": "-", "―": "-", "-": "-"}
def sanitize_text(text: str) -> str:
    for k, v in DASH_FIX.items():
        text = text.replace(k, v)
    return text

def _font_defs():
    spacing = int(os.getenv("FONT_SPACING", "1"))  # set to 2 for wider spacing
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
    """Yield frames: white bg, text scrolls right->left, vertically centered."""
    text = sanitize_text(text.upper())
    font, glyph_w, glyph_h, spacing = _font_defs()
    msg_w = len(text) * (glyph_w + spacing) - spacing
    # Build glyph strip
    strip = [[0]*msg_w for _ in range(glyph_h)]
    x = 0
    for ch in text:
        patt = font.get(ch, font["?"])
        for yy, row in enumerate(patt):
            for xx, c in enumerate(row):
                if c == "1":
                    strip[yy][x+xx] = 1
        x += glyph_w + spacing
    y0 = max(0, (HEIGHT - glyph_h)//2)
    # Off-screen right to off-screen left
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

def draw_text_top_centered(frame, text, top_row=1, color=BLACK_VAL):
    """Center text horizontally at a fixed top_row (row 0 left white)."""
    text = sanitize_text(text.upper())
    font, glyph_w, glyph_h, spacing = _font_defs()
    msg_w = len(text) * (glyph_w + spacing) - spacing
    x0 = max(0, (WIDTH - msg_w) // 2)
    y0 = top_row
    for i, ch in enumerate(text):
        draw_char(frame, ch, x0 + i*(glyph_w+spacing), y0, color)

def compose_two_line_center_label(line1: str, line2: str, line_spacing: int = 1):
    """Return a frame with two centered lines using the 5×7 font."""
    f = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
    font, glyph_w, glyph_h, spacing = _font_defs()

    line1 = sanitize_text(line1.upper()); line2 = sanitize_text(line2.upper())
    w1 = len(line1) * (glyph_w + spacing) - (spacing if line1 else 0)
    w2 = len(line2) * (glyph_w + spacing) - (spacing if line2 else 0)

    total_h = glyph_h*2 + line_spacing
    y0 = max(0, (HEIGHT - total_h)//2)
    x1 = max(0, (WIDTH - w1)//2)
    x2 = max(0, (WIDTH - w2)//2)

    # draw line 1
    for i, ch in enumerate(line1):
        draw_char(f, ch, x1 + i*(glyph_w + spacing), y0, BLACK_VAL)
    # draw line 2
    y2 = y0 + glyph_h + line_spacing
    for i, ch in enumerate(line2):
        draw_char(f, ch, x2 + i*(glyph_w + spacing), y2, BLACK_VAL)

    return f

# === Centered text helpers ===
def draw_text_center_centered(frame, text, color=BLACK_VAL):
    """Centers text both horizontally and vertically on the 28×28 canvas."""
    text = sanitize_text(text.upper())
    font, glyph_w, glyph_h, spacing = _font_defs()
    msg_w = len(text) * (glyph_w + spacing) - spacing
    x0 = max(0, (WIDTH  - msg_w) // 2)
    y0 = max(0, (HEIGHT - glyph_h) // 2)
    for i, ch in enumerate(text):
        draw_char(frame, ch, x0 + i*(glyph_w + spacing), y0, color)

def compose_center_label(text):
    f = [[WHITE_VAL]*WIDTH for _ in range(HEIGHT)]
    draw_text_center_centered(f, text, BLACK_VAL)
    return f

# ---------- HAND SHAPE (rounded right-hand, 28×28) ----------
def _put_circle(img, cx, cy, r):
    for y in range(cy - r, cy + r + 1):
        if 0 <= y < HEIGHT:
            for x in range(cx - r, cx + r + 1):
                if 0 <= x < WIDTH and (x - cx) * (x - cx) + (y - cy) * (y - cy) <= r * r:
                    img[y][x] = 1

def _put_rect(img, x0, y0, x1, y1):
    x0, x1 = max(0, x0), min(WIDTH - 1, x1)
    y0, y1 = max(0, y0), min(HEIGHT - 1, y1)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            img[y][x] = 1

def _draw_vertical_capsule(img, xc, y_top, y_bot, width):
    """
    A finger with a rounded tip. width is 3 or 4.
    """
    half = width // 2
    # straight shaft
    _put_rect(img, xc - half, y_top + 1, xc + half, y_bot)
    # rounded fingertip
    _put_circle(img, xc, y_top + 1, half + 1)

def _draw_thick_line(img, x0, y0, x1, y1, radius):
    """simple 'brush line' used for the thumb slant."""
    steps = max(abs(x1 - x0), abs(y1 - y0))
    for i in range(steps + 1):
        t = i / steps if steps else 0
        x = round(x0 + (x1 - x0) * t)
        y = round(y0 + (y1 - y0) * t)
        _put_circle(img, x, y, radius)

def build_hand_mask_rounded() -> List[List[int]]:
    """
    Right-hand, palm facing viewer. Five distinct rounded fingers.
    Designed to look like your reference within 28×28.
    """
    m = [[0] * WIDTH for _ in range(HEIGHT)]

    # --- four straight fingers (pinky..index), rounded tips ---
    # you can tweak centers/top/bottom/width for taste
    fingers = [
    (8,  3, 14, 3),   # index  (near thumb)
    (12, 2, 15, 4),   # middle (tallest & wider)
    (16, 3, 14, 3),   # ring
    (25, 4, 13, 3),   # pinky  (shorter, right-most)
    ]
    for xc, y_top, y_bot, w in fingers:
        _draw_vertical_capsule(m, xc, y_top, y_bot, w)

    # --- slanted thumb (right side) as a thick brushed line + root blob ---
    _draw_thick_line(m, 6, 10, 1, 16, radius=2)  # slanted segment
    _put_rect(m, 5, 13, 8, 17)                   # root into palm

    # --- knuckle bridge (small) so fingers join palm but keep gaps between them ---
    for xc, _, y_bot, w in fingers:
        half = w // 2
        _put_rect(m, xc - half, y_bot + 1, xc + half, y_bot + 2)

    # --- palm block ---
    _put_rect(m, 6, 16, 21, 23)

    # --- wrist ---
    _put_rect(m, 9, 23, 18, 26)

    return m

def outline_from_mask(mask: List[List[int]]) -> List[List[int]]:
    out = [[0] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if mask[y][x]:
                n0 = (y > 0 and mask[y - 1][x])
                n1 = (y < HEIGHT - 1 and mask[y + 1][x])
                n2 = (x > 0 and mask[y][x - 1])
                n3 = (x < WIDTH - 1 and mask[y][x + 1])
                if not (n0 and n1 and n2 and n3):
                    out[y][x] = 1
    return out

# build once
HAND_MASK     = build_hand_mask_rounded()
HAND_OUTLINE  = outline_from_mask(HAND_MASK)

def compose_hand_outline():
    """
    White background, 1-pixel black outline, white fill — no header text.
    """
    f = [[WHITE_VAL] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if HAND_OUTLINE[y][x]:
                f[y][x] = BLACK_VAL
    return f

def compose_hand_filled(progress: float):
    """
    Start from the outline view; fill the hand from bottom to top in black.
    """
    f = [[WHITE_VAL] * WIDTH for _ in range(HEIGHT)]
    # draw outline first
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if HAND_OUTLINE[y][x]:
                f[y][x] = BLACK_VAL
    # then fill interior by cutoff
    cutoff = int((1.0 - max(0.0, min(1.0, progress))) * (HEIGHT - 1))
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if HAND_MASK[y][x] and y >= cutoff:
                f[y][x] = BLACK_VAL
    return f

# ================== TUI (terminal preview) ==================
def tui_clear():
    print("\x1b[2J\x1b[H", end="")  # clear + home

def tui_print_preview(rgb_frame, detected: bool, progress: float):
    if not TUI:
        return
    h, w, _ = rgb_frame.shape
    cols, rows = 48, 18
    stepx = max(1, w // cols)
    stepy = max(1, h // rows)
    palette = " .:-=+*#%@"
    tui_clear()
    print("Camera (ASCII preview) —", "HI-5!" if detected else "…")
    for ry in range(0, h, stepy):
        if ry//stepy >= rows: break
        line = []
        for rx in range(0, w, stepx):
            if rx//stepx >= cols: break
            r,g,b = rgb_frame[ry, rx]
            lum = (0.2126*r + 0.7152*g + 0.0722*b) / 255.0
            ch = palette[int(lum*(len(palette)-1))]
            line.append(ch)
        print("".join(line))
    width = 30
    n = int(max(0.0, min(1.0, progress)) * width)
    print(f"\nHold progress: [{'='*n}{' '*(width-n)}]  {progress*100:4.0f}%")

# ================== OPEN PALM (HI-5) DETECTION ==================
def _dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)

def _angle_deg(a, b, c):
    """Angle ABC (at b) in degrees."""
    bax = a.x - b.x; bay = a.y - b.y
    bcx = c.x - b.x; bcy = c.y - b.y
    num = bax*bcx + bay*bcy
    den = math.hypot(bax, bay) * math.hypot(bcx, bcy) + 1e-9
    val = max(-1.0, min(1.0, num/den))
    return math.degrees(math.acos(val))

def _extended_finger(lm, mcp_i, pip_i, dip_i, tip_i) -> bool:
    """Finger is extended if:
       - tip further from wrist than PIP (distance margin)
       - angles at PIP and DIP are straight-ish
    """
    wrist = lm[0]
    mcp, pip, dip, tip = lm[mcp_i], lm[pip_i], lm[dip_i], lm[tip_i]
    dist_ok = _dist(tip, wrist) > _dist(pip, wrist) + DIST_MARGIN
    ang_pip = _angle_deg(mcp, pip, dip)
    ang_dip = _angle_deg(pip, dip, tip)
    angle_ok = (ang_pip >= ANGLE_PIP_THRESH_DEG) and (ang_dip >= ANGLE_DIP_THRESH_DEG)
    return dist_ok and angle_ok

def _extended_thumb(lm) -> bool:
    """Thumb considered extended if tip is further than IP and angle at IP is open."""
    wrist = lm[0]
    mcp, ip, tip = lm[2], lm[3], lm[4]
    dist_ok = _dist(tip, wrist) > _dist(ip, wrist) + (DIST_MARGIN * 0.6)
    ang_ip = _angle_deg(mcp, ip, tip)
    return dist_ok and (ang_ip >= (ANGLE_PIP_THRESH_DEG - 10))

def is_open_palm(hand_landmarks) -> bool:
    """Open palm if ≥3 of the 4 fingers extended, or 2 + extended thumb."""
    lm = hand_landmarks.landmark
    idx = _extended_finger(lm, 5, 6, 7, 8)
    mid = _extended_finger(lm, 9, 10, 11, 12)
    rng = _extended_finger(lm, 13, 14, 15, 16)
    pky = _extended_finger(lm, 17, 18, 19, 20)
    ext_count = sum([idx, mid, rng, pky])
    if ext_count >= 3:
        return True
    if ext_count >= 2 and _extended_thumb(lm):
        return True
    return False

# ================== MAIN ==================
def main():
    print("Starting HI-5 interactor…")

    # White canvas first
    clear_white()
    time.sleep(0.2)

    if not SKIP_INTRO:
        # 1) Horizontally scrolling intro lines (centered vertically)
        for msg in MESSAGES:
            for frame in render_text_scroller_centered(msg, speed_cols_per_step=SCROLL_STEP):
                send_frame_to_flipdot(frame)
                time.sleep(SCROLL_DELAY)  # scroll pacing

    # 2) Show hand outline FIRST (no label), before cam starts
    send_frame_to_flipdot(compose_hand_outline())
    print(f"HI-5 prompt shown. Starting camera + detection… (hold {HOLD_REQUIRED_SEC:.0f}s)")

    # 3) NOW start camera + mediapipe
    picam = Picamera2()
    picam.configure(picam.create_preview_configuration(
        main={"format": "RGB888", "size": (640, 480)}
    ))
    picam.start()
    try:
        # Auto-exposure/auto-WB usually help
        picam.set_controls({"AeEnable": True, "AwbEnable": True})
    except Exception:
        pass
    time.sleep(0.3)

    # 4) Detection loop (multi-person friendly: any open palm counts)
    hold_start = None
    satisfied = False
    last_event_time = 0.0
    detected_prev = False

    with mp_hands.Hands(static_image_mode=False,
                        max_num_hands=MAX_HANDS,
                        model_complexity=1,
                        min_detection_confidence=MIN_DET_CONF,
                        min_tracking_confidence=MIN_TRACK_CONF) as hands:
        while True:
            rgb = picam.capture_array()  # HxWx3 (RGB)
            res = hands.process(rgb)

            open_palm_now = False
            if res.multi_hand_landmarks:
                for hand in res.multi_hand_landmarks:
                    if is_open_palm(hand):
                        open_palm_now = True
                        break

            now = time.time()
            # grace to ignore brief misses
            effective_open = open_palm_now or (detected_prev and (now - last_event_time) <= MISS_GRACE_SEC)
            if open_palm_now:
                last_event_time = now

            if effective_open:
                if hold_start is None:
                    hold_start = now
                elapsed = now - hold_start
                progress = min(1.0, elapsed / HOLD_REQUIRED_SEC)
                send_frame_to_flipdot(compose_hand_filled(progress))
                if TUI:
                    tui_print_preview(rgb, True, progress)
                if progress >= 1.0:
                    satisfied = True
                    break
            else:
                hold_start = None
                # pulse outline while waiting (no pulse in new hand outline)
                send_frame_to_flipdot(compose_hand_outline())
                if TUI:
                    tui_print_preview(rgb, False, 0.0)

            detected_prev = open_palm_now
            time.sleep(0.06)

    picam.stop()

    # 5) After palm detection completes: show "SCAN / ME" centered (two lines)
    if satisfied:
        send_frame_to_flipdot(compose_two_line_center_label("SCAN", "ME", line_spacing=2))
        time.sleep(3.0)

        # Then launch the next script and EXIT to keep its image on the panel
        print("HI-5 sustained! Launching:", NEXT_SCRIPT)
        maybe_close_serial()
        env = os.environ.copy()
        env.setdefault("FLIPDOT_SERIAL", SERIAL_PORT)
        env.setdefault("FLIPDOT_BAUD", str(BAUD_RATE))
        subprocess.run(["/usr/bin/python3", NEXT_SCRIPT], env=env, check=False)
        return  # do not clear after the next script


    print("Done.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Intentionally do NOT clear; keep whatever is on the display
        print("Interrupted.")
