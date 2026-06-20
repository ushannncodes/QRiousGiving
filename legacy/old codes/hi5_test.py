#!/usr/bin/env python3
"""
hi5_interact_png_palm_clean.py
- PNG palm (no hard-coded palm).
- DEFAULT: one-pixel outline (outside) — crisp boundary that hugs the palm.
- During hold/progress, fills the silhouette bottom->top, outline drawn on top.
- No pulsing or thickness animations.
- Optional --jump-palm / -J (or env JUMP_PALM=1) to skip intro and start at palm.

ENV VARS (common):
  FLIPDOT_SERIAL=/dev/ttyS0
  FLIPDOT_BAUD=57600
  WHITE_VAL=1
  HOLD_REQUIRED_SEC=3.0
  MISS_GRACE_SEC=0.4
  NEXT_SCRIPT=/home/pi/Desktop/qr_works.py
  TUI=0
  JUMP_PALM=0

PNG processing:
  PNG_PATH=/home/pi/Desktop/palm.png
  SUPER=10
  BLUR=1
  THRESH=200
  OUTLINE_PAUSE=1.0

Outline mode (one-pixel boundary):
  OUTLINE_SIDE=outside        # outside | inside | both (default outside)
  OUTLINE_CONN=8              # 4 or 8 connectivity for boundary detection
"""

import os, sys, time, math, subprocess, argparse
from typing import List

# =============== GLOBALS / CONFIG ===============
WIDTH, HEIGHT = 28, 28

WHITE_VAL = int(os.getenv("WHITE_VAL", "1"))
BLACK_VAL = 1 - WHITE_VAL

SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))

HOLD_REQUIRED_SEC = float(os.getenv("HOLD_REQUIRED_SEC", "3.0"))
MISS_GRACE_SEC    = float(os.getenv("MISS_GRACE_SEC", "0.4"))
NEXT_SCRIPT       = os.getenv("NEXT_SCRIPT", "/home/pi/Desktop/qr_works.py")

# Intro scrollers (set [] to skip entirely)
MESSAGES = [
    "HI",
    "I AM A FUTURE DONATION MACHINE",
    "TO LEARN MORE",
    "HI-5",
]
SCROLL_STEP  = int(os.getenv("SCROLL_STEP", "1"))
SCROLL_DELAY = float(os.getenv("SCROLL_DELAY", "0.06"))

# TUI (ASCII preview in terminal)
TUI = os.getenv("TUI", "0") == "1"

# Detection knobs
ANGLE_PIP_THRESH_DEG = float(os.getenv("ANGLE_PIP_THRESH_DEG", "150"))
ANGLE_DIP_THRESH_DEG = float(os.getenv("ANGLE_DIP_THRESH_DEG", "140"))
DIST_MARGIN          = float(os.getenv("DIST_MARGIN", "0.015"))
MIN_DET_CONF         = float(os.getenv("MIN_DET_CONF", "0.35"))
MIN_TRACK_CONF       = float(os.getenv("MIN_TRACK_CONF", "0.25"))
MAX_HANDS            = int(os.getenv("MAX_HANDS", "8"))

# PNG palm processing
PNG_PATH      = os.getenv("PNG_PATH", "/home/pi/Desktop/palm.png")
SUPER         = int(os.getenv("SUPER", "10"))
BLUR          = float(os.getenv("BLUR", "1"))
THRESH        = int(os.getenv("THRESH", "200"))
OUTLINE_PAUSE = float(os.getenv("OUTLINE_PAUSE", "1.0"))

# One-pixel outline config (defaults)
OUTLINE_SIDE  = os.getenv("OUTLINE_SIDE", "outside").lower()  # outside|inside|both
OUTLINE_CONN  = int(os.getenv("OUTLINE_CONN", "8"))           # 4 or 8

# Jump straight to palm stage
JUMP_PALM_DEFAULT = (os.getenv("JUMP_PALM", "0") == "1")

def fatal(msg: str):
    print(msg); sys.exit(1)

# Camera
try:
    from picamera2 import Picamera2
except Exception:
    fatal("ERROR: Picamera2 not found. Install: sudo apt-get install -y python3-picamera2")

# Mediapipe Hands
try:
    import mediapipe as mp
    mp_hands = mp.solutions.hands
except Exception:
    fatal("ERROR: mediapipe not found. Try: python3 -m pip install --user mediapipe==0.10.9")

# Display I/O (uses your flipdot_driver if present; else a minimal serial fallback)
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
        # Example bare-bones streaming; replace per your controller protocol if needed.
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

# =============== Simple 5x7 Text (used for intros / success) ===============
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
    for k, v in DASH_FIX.items():
        text = text.replace(k, v)
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

# =============== PNG PALM PIPELINE ===============
from PIL import Image, ImageFilter

def load_png_to_whitebg_mask(path: str, thresh: int):
    """Return 28x28 mask with 1=white background, 0=black hand."""
    big = Image.open(path).convert("L")
    big = big.resize((28*SUPER, 28*SUPER), Image.LANCZOS)
    if BLUR > 0:
        big = big.filter(ImageFilter.GaussianBlur(BLUR))
    small = big.resize((28, 28), Image.LANCZOS)
    out = []
    for y in range(28):
        row = []
        for x in range(28):
            px = small.getpixel((x, y))
            row.append(1 if px >= thresh else 0)  # 1=white bg, 0=hand
        out.append(row)
    return out

# ---- ONE-PIXEL outline (exact boundary) ----
def _neighbors(y, x, conn=8):
    if conn == 4:
        return [(y-1,x),(y+1,x),(y,x-1),(y,x+1)]
    return [
        (y-1,x-1),(y-1,x),(y-1,x+1),
        (y,  x-1),        (y,  x+1),
        (y+1,x-1),(y+1,x),(y+1,x+1),
    ]

def derive_onepx_outline_masks(white_bg_mask, conn=8):
    """
    Returns three masks: outside, inside, both — each mask: 0=outline, 1=elsewhere.
    - outside: background pixels that touch the hand
    - inside:  hand pixels that touch the background
    - both:    union of the two
    """
    H, W = 28, 28
    hand = [[1 - white_bg_mask[y][x] for x in range(W)] for y in range(H)]  # 1=hand, 0=bg
    outside = [[1]*W for _ in range(H)]
    inside  = [[1]*W for _ in range(H)]

    for y in range(H):
        for x in range(W):
            if hand[y][x] == 1:
                for ny, nx in _neighbors(y,x,conn):
                    if 0 <= ny < H and 0 <= nx < W and hand[ny][nx] == 0:
                        inside[y][x] = 0; break
            else:
                for ny, nx in _neighbors(y,x,conn):
                    if 0 <= ny < H and 0 <= nx < W and hand[ny][nx] == 1:
                        outside[y][x] = 0; break

    both = [[0 if (inside[y][x]==0 or outside[y][x]==0) else 1 for x in range(W)] for y in range(H)]
    return outside, inside, both

def compose_outline_frame_from_mask(outline_mask):
    H, W = 28, 28
    frame = [[WHITE_VAL]*W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if outline_mask[y][x] == 0:
                frame[y][x] = BLACK_VAL
    return frame

def compose_fill_frame_from_png(white_bg_mask, outline_mask, cutoff_row):
    """
    Fill from bottom->top inside the silhouette (0=hand).
    cutoff_row: rows >= cutoff_row become filled (black) inside the hand.
    Always draws the given outline_mask on top.
    """
    H, W = 28, 28
    frame = [[WHITE_VAL]*W for _ in range(H)]
    # outline
    for y in range(H):
        for x in range(W):
            if outline_mask[y][x] == 0:
                frame[y][x] = BLACK_VAL
    # fill
    for y in range(H):
        if y >= cutoff_row:
            for x in range(W):
                if white_bg_mask[y][x] == 0:
                    frame[y][x] = BLACK_VAL
    return frame

# =============== TUI ===============
def tui_clear():
    print("\x1b[2J\x1b[H", end="")

def tui_print_preview(rgb_frame, detected: bool, progress: float):
    if not TUI: return
    h, w, _ = rgb_frame.shape
    cols, rows = 48, 18
    stepx = max(1, w // cols)
    stepy = max(1, h // rows)
    palette = " .:-=+*#%@"
    tui_clear()
    print("Camera (ASCII preview) —", "PALM" if detected else "…")
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

# =============== OPEN-PALM DETECTION ===============
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

def is_open_palm(hand_landmarks) -> bool:
    lm = hand_landmarks.landmark
    idx = _extended_finger(lm, 5, 6, 7, 8)
    mid = _extended_finger(lm, 9, 10, 11, 12)
    rng = _extended_finger(lm, 13, 14, 15, 16)
    pky = _extended_finger(lm, 17, 18, 19, 20)
    ext_count = sum([idx, mid, rng, pky])
    if ext_count >= 3: return True
    if ext_count >= 2 and _extended_thumb(lm): return True
    return False

# =============== MAIN ===============
def main():
    # ---- CLI args ----
    parser = argparse.ArgumentParser(description="HI-5 interactor (PNG palm, one-pixel outline, no pulse)")
    parser.add_argument("-J", "--jump-palm", action="store_true",
                        help="Skip intro and jump straight to palm outline + detection")
    args = parser.parse_args()
    jump_palm = args.jump_palm or JUMP_PALM_DEFAULT

    print("Starting HI-5 interactor (PNG palm, one-pixel outline, no pulse)…")
    if jump_palm:
        print("[Jump] Skipping intro; going straight to palm.")

    # 0) Precompute masks and one-pixel outlines
    white_bg_mask = load_png_to_whitebg_mask(PNG_PATH, THRESH)
    outside_m, inside_m, both_m = derive_onepx_outline_masks(white_bg_mask, conn=OUTLINE_CONN)
    if OUTLINE_SIDE == "inside":
        outline_mask = inside_m
    elif OUTLINE_SIDE == "both":
        outline_mask = both_m
    else:
        outline_mask = outside_m

    outline_frame = compose_outline_frame_from_mask(outline_mask)

    # 1) White canvas + optional intro scrollers
    clear_white()
    time.sleep(0.2)
    if not jump_palm and len(MESSAGES) > 0:
        for msg in MESSAGES:
            for frame in render_text_scroller_centered(msg, speed_cols_per_step=SCROLL_STEP):
                send_frame_to_flipdot(frame)
                time.sleep(SCROLL_DELAY)

    # 2) Show outline before starting detection
    send_frame_to_flipdot(outline_frame)
    time.sleep(OUTLINE_PAUSE if not jump_palm else 0.2)
    print(f"Palm prompt shown. Starting camera + detection… (hold {HOLD_REQUIRED_SEC:.1f}s)")

    # 3) Camera on
    picam = Picamera2()
    picam.configure(picam.create_preview_configuration(main={"format": "RGB888", "size": (640, 480)}))
    picam.start()
    try:
        picam.set_controls({"AeEnable": True, "AwbEnable": True})
    except Exception:
        pass
    time.sleep(0.3)

    # 4) Detection loop
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
            rgb = picam.capture_array()
            res = hands.process(rgb)

            open_palm_now = False
            if res.multi_hand_landmarks:
                for hand in res.multi_hand_landmarks:
                    if is_open_palm(hand):
                        open_palm_now = True
                        break

            now = time.time()
            effective_open = open_palm_now or (detected_prev and (now - last_event_time) <= MISS_GRACE_SEC)
            if open_palm_now:
                last_event_time = now

            if effective_open:
                if hold_start is None:
                    hold_start = now
                elapsed = now - hold_start
                progress = min(1.0, elapsed / HOLD_REQUIRED_SEC)

                # bottom->top fill with static one-pixel outline
                H = 28
                cutoff = H - int(max(0.0, min(1.0, progress)) * (H - 1))
                frame = compose_fill_frame_from_png(white_bg_mask, outline_mask, cutoff)
                send_frame_to_flipdot(frame)

                if TUI: tui_print_preview(rgb, True, progress)
                if progress >= 1.0:
                    satisfied = True
                    break
            else:
                # Idle: static outline (no pulse)
                hold_start = None
                send_frame_to_flipdot(outline_frame)
                if TUI: tui_print_preview(rgb, False, 0.0)

            detected_prev = open_palm_now
            time.sleep(0.06)

    picam.stop()

    # 5) Success -> "SCAN ME", then launch NEXT_SCRIPT
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
