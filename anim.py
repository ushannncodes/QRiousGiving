# run_show_with_countdown_and_thanks_static.py
# 1) 5→1 countdown with ring (from anim_waves.py)
# 2) run a single random script from ./rand_anim
# 3) show static end card with:
#       THANK
#       YOU :)

import os, time, math, random, subprocess, serial
import numpy as np
import argparse, glob


# -----------------------------
# Serial / Panel settings
# -----------------------------
SERIAL_PORT  = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE    = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS  = [1, 2, 3, 4]   # 4 stacked 7x28 panels = 28x28

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH   = 28, 28
FPS             = float(os.getenv("FPS", "50"))
FRAME_DT        = 1.0 / FPS

# -----------------------------
# Intro countdown timing (same feel as anim_waves.py)
# -----------------------------
INTRO_WHITE_SEC           = float(os.getenv("INTRO_WHITE_SEC", "1.0"))
COUNTDOWN_STEP_SEC        = float(os.getenv("COUNTDOWN_STEP_SEC", "1"))
COUNTDOWN_RING_RADIUS     = int(os.getenv("COUNTDOWN_RING_RADIUS", "11"))
COUNTDOWN_RING_THICKNESS  = int(os.getenv("COUNTDOWN_RING_THICKNESS", "3"))

# -----------------------------
# End card timing (static)
# -----------------------------
END_CARD_TOTAL_SEC  = float(os.getenv("END_CARD_TOTAL_SEC", "3.0"))

# -----------------------------
# Utilities (pack & send)
# -----------------------------
def pack_flipbytes(frame28):
    """
    Pack a 28x28 array (0=black, 1=white) into 4 panel payloads.
    Column-major, 7 rows per byte, panels are 7 rows each (top->bottom).
    """
    panels = []
    for p in range(4):
        row_off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                bit = int(frame28[row_off + y, x])
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(ser, panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(pkt)
    ser.flush()

# -----------------------------
# Countdown digits (7x9) — copied from anim_waves.py
# -----------------------------
DIGITS_7x9 = {
    '1': [
        "0011000","0111000","0011000","0011000","0011000",
        "0011000","0011000","0011000","1111110",
    ],
    '2': [
        "0111110","1100011","0000011","0000110","0001100",
        "0011000","0110000","1100000","1111111",
    ],
    '3': [
        "1111110","0000011","0000011","0011110","0000011",
        "0000011","0000011","1100011","0111110",
    ],
    '4': [
        "0001100","0011100","0111100","1101100","1001100",
        "1111111","0001100","0001100","0001100",
    ],
    '5': [
        "1111111","1100000","1100000","1111110","0000011",
        "0000011","0000011","1100011","0111110",
    ],
}

def _make_brush(thick_px: int):
    if thick_px <= 1:
        return [(0, 0)]
    r = (thick_px - 1) / 2.0
    rceil = int(math.ceil(r))
    offs = []
    r2 = r * r
    for dy in range(-rceil, rceil + 1):
        for dx in range(-rceil, rceil + 1):
            if (dy*dy + dx*dx) <= r2 + 1e-9:
                offs.append((dy, dx))
    return offs

_BRUSH = _make_brush(COUNTDOWN_RING_THICKNESS)

def blit_digit_7x9(frame, ch, top, left):
    glyph = DIGITS_7x9.get(ch)
    if glyph is None:
        return frame
    for r, row in enumerate(glyph):
        yy = top + r
        if yy < 0 or yy >= HEIGHT: 
            continue
        for c, val in enumerate(row):
            xx = left + c
            if xx < 0 or xx >= WIDTH: 
                continue
            if val == '1':
                frame[yy, xx] = 0  # black
    return frame

def render_all_white():
    return np.ones((HEIGHT, WIDTH), dtype=np.uint8)

def render_countdown_frame(number, frac_remaining):
    """
    number: int in [1..5]
    frac_remaining: 1.0 -> 0.0 over the second
    Draws a thin anti-clockwise arc starting at 12 o'clock, plus centered 7x9 digit.
    """
    bw = np.ones((HEIGHT, WIDTH), dtype=np.uint8)  # solid white bg
    cy, cx = HEIGHT // 2, WIDTH // 2
    r = COUNTDOWN_RING_RADIUS

    arc_len = max(0.0, min(1.0, frac_remaining)) * 2.0 * math.pi
    if arc_len > 0:
        a = -math.pi / 2.0
        a_end = a + arc_len
        step = (1.0 / max(6.0, r * 8.0)) * 2.0 * math.pi
        while a <= a_end + 1e-6:
            yy = int(round(cy + r * math.sin(a)))
            xx = int(round(cx + r * math.cos(a)))
            for dy, dx in _BRUSH:
                y = yy + dy
                x = xx + dx
                if 0 <= y < HEIGHT and 0 <= x < WIDTH:
                    bw[y, x] = 0
            a += step

    # 7x9 digit (centered)
    num_ch = str(number)
    top  = cy - 4         # 9 rows tall
    left = cx - 3         # 7 cols wide
    bw = blit_digit_7x9(bw, num_ch, top, left)
    return bw



# -----------------------------
# End-card fonts (uniform 4x5 + spacing=1)
# -----------------------------
FONT_4x5 = {
    "A":["0110","1001","1111","1001","1001"],
    "H":["1001","1001","1111","1001","1001"],
    "K":["1001","1010","1100","1010","1001"],
    "N":["1001","1101","1011","1001","1001"],  # diagonal N
    "O":["1111","1001","1001","1001","1111"],
    "T":["1111","0010","0010","0010","0010"],
    "U":["1001","1001","1001","1001","1111"],
    "Y":["1001","1001","0110","0010","0010"],
    " ":"0000","  ":["0000","0000","0000","0000","0000"],
}

def blit_text_4x5(frame, text, top, left, spacing=1):
    x = left
    for ch in text:
        glyph = FONT_4x5.get(ch.upper())
        if not glyph:
            x += 4 + spacing; continue
        for r, row in enumerate(glyph):
            for c, v in enumerate(row):
                y, xx = top + r, x + c
                if 0 <= y < HEIGHT and 0 <= xx < WIDTH and v == "1":
                    frame[y, xx] = 0
        x += 4 + spacing
    return frame

# 5x5 dot-smiley (unchanged)
SMILEY_5x5 = [
    "01010",  # eyes row
    "00000",
    "10001",  # cheek dots
    "01110",  # mouth
    "00000",
]

def blit_bitmap(frame, bmp, top, left):
    for r, row in enumerate(bmp):
        for c, v in enumerate(row):
            y, x = top + r, left + c
            if 0 <= y < HEIGHT and 0 <= x < WIDTH and v == "1":
                frame[y, x] = 0
    return frame

def render_end_card_static():
    """
    Draws (centered, uniform letter size):
       THANK
       YOU  ☺
    """
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    # --- line 1: THANK ---
    line1 = "THANK"
    w1 = 5*4 + 4*1   # width with 4x5 font + 1px spacing
    total_h = 5 + 1 + 5
    top_block = (HEIGHT - total_h) // 2
    top1 = top_block
    left1 = (WIDTH - w1) // 2
    blit_text_4x5(frame, line1, top1, left1, spacing=1)

    # --- line 2: YOU + smiley ---
    word2 = "YOU"
    w_word2 = 3*4 + 2*1
    gap = 2
    w_smiley = 5
    w2 = w_word2 + gap + w_smiley
    top2 = top_block + 5 + 1
    left2 = (WIDTH - w2) // 2
    blit_text_4x5(frame, word2, top2, left2, spacing=1)

    # Smiley shifted one pixel down
    smiley_left = left2 + w_word2 + gap
    blit_bitmap(frame, SMILEY_5x5, top2 + 1, smiley_left)

    return frame




# -----------------------------
# Run one random script from ./rand_anim
# -----------------------------
def run_one_rand_script(force_pattern=None, remember_path="/tmp/last_rand_anim.txt"):
    """
    Picks and runs exactly one script from ./rand_anim.
    - If force_pattern is given (CLI --script or env FORCE_ANIM), it selects using glob.
    - Otherwise chooses randomly, avoiding the same script as last time (persisted in /tmp).
    """
    folder = os.getenv("RAND_ANIM_DIR", "./rand_anim")
    if not os.path.isdir(folder):
        print(f"[RUN] No folder {folder}; skipping random anim.")
        return

    # 1) collect candidates
    all_py = sorted([f for f in os.listdir(folder) if f.endswith(".py") and not f.startswith("_")])
    if not all_py:
        print(f"[RUN] No .py files in {folder}; skipping.")
        return

    # 2) forced selection (CLI/env)
    if force_pattern:
        # allow basename or wildcard
        patt = force_pattern
        if not any(ch in patt for ch in "*?[]"):
            # treat as simple substring/basename match
            matches = [f for f in all_py if patt in f]
        else:
            matches = [os.path.basename(p) for p in glob.glob(os.path.join(folder, patt))]
            matches = [m for m in matches if m in all_py]
        if not matches:
            print(f"[RUN] No match for --script '{patt}'. Available: {all_py}")
            return
        choice = matches[0]
    else:
        # 3) avoid repeating last played (persisted)
        last = None
        try:
            with open(remember_path, "r") as fh:
                last = fh.read().strip()
        except Exception:
            pass

        pool = [f for f in all_py if f != last] or all_py  # if only one or all == last, allow it
        choice = random.choice(pool)

    path = os.path.join(folder, choice)
    print(f"[RUN] Playing anim: {path}")
    try:
        subprocess.run(["python3", "-u", path], check=False)
    finally:
        # remember even for forced runs, so next random won't immediately repeat it
        try:
            with open(remember_path, "w") as fh:
                fh.write(choice)
        except Exception as e:
            print(f"[RUN] Note: cannot write remember file: {e}")

# def run_one_rand_script():
#     folder = os.getenv("RAND_ANIM_DIR", "./rand_anim")
#     if not os.path.isdir(folder):
#         print(f"[RUN] No folder {folder}; skipping random anim.")
#         return
#     cands = [f for f in os.listdir(folder) if f.endswith(".py") and not f.startswith("_")]
#     if not cands:
#         print(f"[RUN] No .py files in {folder}; skipping.")
#         return
#     choice = random.choice(cands)
#     path = os.path.join(folder, choice)
#     print(f"[RUN] Playing random anim: {path}")
#     subprocess.run(["python3", "-u", path], check=False)

def parse_args():
    ap = argparse.ArgumentParser(description="Countdown → one anim → thank-you card")
    ap.add_argument("--script", help="Force a specific script by name or glob, e.g. bee.py or '*sunflower*.py'")
    return ap.parse_args()

# -----------------------------
# MAIN
# -----------------------------

# EDIT main(): parse args and thread through --script or env FORCE_ANIM
def main():
    args = parse_args()
    force_env = os.getenv("FORCE_ANIM", "").strip() or None

    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        # (unchanged) 1) white intro + 2) countdown ...
        t0 = time.time()
        while (time.time() - t0) < INTRO_WHITE_SEC:
            frame = render_all_white()
            send_to_panels(ser, pack_flipbytes(frame))
            time.sleep(FRAME_DT)

        for n in range(5, 0, -1):
            sec_start = time.time()
            while True:
                elapsed = time.time() - sec_start
                frac_remaining = max(0.0, 1.0 - (elapsed / COUNTDOWN_STEP_SEC))
                frame = render_countdown_frame(n, frac_remaining)
                send_to_panels(ser, pack_flipbytes(frame))
                time.sleep(FRAME_DT)
                if elapsed >= COUNTDOWN_STEP_SEC:
                    break
    finally:
        ser.close()

    # 3) run exactly ONE script (forced or random w/ no-repeat)
    run_one_rand_script(force_pattern=(args.script or force_env))

    # 4) (unchanged) end card loop ...
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        frame = render_end_card_static()
        panels = pack_flipbytes(frame)
        t0 = time.time()
        while (time.time() - t0) < END_CARD_TOTAL_SEC:
            send_to_panels(ser, panels)
            time.sleep(FRAME_DT)
    finally:
        ser.close()




if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[SHOW] Stopped.")
