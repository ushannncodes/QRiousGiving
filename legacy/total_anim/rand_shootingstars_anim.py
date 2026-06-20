# anim_starry_star_custom.py  (4s blink intro → fly → explode; half-size star; clean halo)
# Black sky (0), white stars & icon (1).

import numpy as np
import time, random, serial, os, math

# -----------------------------
# Flipdot / Serial (same as anim.py)
# -----------------------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "60"))
DT            = 1.0 / FPS

# Phases
BLINK_INTRO_S = float(os.getenv("BLINK_INTRO_S", "4.0"))  # NEW: 4s blinking stars
FLY_S         = float(os.getenv("FLY_S", "1.3"))          # star flies BL->TR
EXPLODE_S     = float(os.getenv("EXPLODE_S", "1.1"))      # sparkle linger

# -----------------------------
# Sky / Blink tuning
# -----------------------------
STAR_COUNT       = int(os.getenv("STAR_COUNT", "22"))
BLINK_RATE_HZ    = float(os.getenv("BLINK_RATE_HZ", "7.0"))
BLINK_PCT        = float(os.getenv("BLINK_PCT", "0.35"))  # fraction of stars that flip per tick

# -----------------------------
# Icon fly + trail tuning
# -----------------------------
TRAIL_LEN        = int(os.getenv("TRAIL_LEN", "0"))
TRAIL_MAX_THICK  = int(os.getenv("TRAIL_MAX_THICK", "3"))
TRAIL_GAP_STEP   = int(os.getenv("TRAIL_GAP_STEP", "3"))  # stylised gaps in ribbons

# Clean halo around moving star (removes stray star pixels near/behind it)
HALO_RX          = int(os.getenv("HALO_RX", "5"))
HALO_RY          = int(os.getenv("HALO_RY", "4"))
HALO_TRAIL_BONUS = int(os.getenv("HALO_TRAIL_BONUS", "2"))

# Explosion tuning
EXP_PARTICLES     = int(os.getenv("EXP_PARTICLES", "26"))
EXP_RADIUS_PX     = float(os.getenv("EXP_RADIUS_PX", "11.0"))
EXP_SPREAD_JITTER = float(os.getenv("EXP_SPREAD_JITTER", "0.30"))

# -----------------------------
# Helpers (pack/send)
# -----------------------------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = int(frame28[offset + y, x]) & 1
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

def blank_black():
    return np.zeros((HEIGHT, WIDTH), dtype=np.uint8)  # 0=black

def in_bounds(x, y):
    return 0 <= x < WIDTH and 0 <= y < HEIGHT

# -----------------------------
# Star field + blinking
# -----------------------------
def random_starfield(n=STAR_COUNT):
    coords = set()
    tries = 0
    while len(coords) < n and tries < n * 20:
        x = random.randint(1, WIDTH - 2)
        y = random.randint(1, HEIGHT - 2)
        coords.add((x, y)); tries += 1
    return list(coords)

def place_stars(frame, stars, mask=None):
    if mask is None:
        for (x, y) in stars: frame[y, x] = 1
    else:
        for i, (x, y) in enumerate(stars):
            if mask[i]: frame[y, x] = 1

def make_mask(n, val=True):
    return np.ones((n,), dtype=bool) if val else np.zeros((n,), dtype=bool)

def twinkle_step(mask, pct):
    n = len(mask)
    k = max(1, int(round(n * pct)))
    for i in random.sample(range(n), k):
        mask[i] = not mask[i]

# -----------------------------
# Your 28×28 custom STAR bitmap → crop → half-scale
# -----------------------------
STAR_RAW = [
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000100000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000011111000000000000",
"0000000000011111000000000000",
"0000000000011111000000000000",
"0000111111111111111111100000",
"0000001111111111111111000000",
"0000000111111111111110000000",
"0000000011111111111000000000",
"0000000011111111110000000000",
"0000000001111111100000000000",
"0000000001111111110000000000",
"0000000011111111111000000000",
"0000000011111001111100000000",
"0000000111110000111110000000",
"0000000111100000011110000000",
"0000001111000000001111000000",
"0000001110000000000111000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
]

def crop_bitmap(bmp_lines):
    h, w = len(bmp_lines), len(bmp_lines[0])
    top, bottom = None, None
    left, right = w, -1
    for y, row in enumerate(bmp_lines):
        if '1' in row:
            if top is None: top = y
            bottom = y
            l = row.find('1'); r = row.rfind('1')
            left = min(left, l); right = max(right, r)
    if top is None: return ["0"]
    return [row[left:right+1] for row in bmp_lines[top:bottom+1]]

def downscale_half(bmp_lines):
    """1/2 scale in X and Y using 2x2 block-OR so silhouette stays bold."""
    h, w = len(bmp_lines), len(bmp_lines[0])
    out = []
    for by in range(0, h, 2):
        row = []
        for bx in range(0, w, 2):
            v = '0'
            for dy in (0,1):
                for dx in (0,1):
                    if by+dy < h and bx+dx < w and bmp_lines[by+dy][bx+dx] == '1':
                        v = '1'
            row.append(v)
        out.append(''.join(row))
    return out

STAR_HALF = downscale_half(crop_bitmap(STAR_RAW))

def blit_bitmap(frame, bmp, cx, cy):
    h = len(bmp); w = len(bmp[0])
    for by in range(h):
        row = bmp[by]
        for bx in range(w):
            if row[bx] != '1': continue
            x = cx - (w // 2) + bx
            y = cy - (h // 2) + by
            if in_bounds(x, y): frame[y, x] = 1

# -----------------------------
# Twin streak ribbons (behind the star)
# Motion: BL -> TR, so trail extends down-left of (cx, cy).
# -----------------------------
def draw_twin_streaks(frame, cx, cy, length=TRAIL_LEN, max_thick=TRAIL_MAX_THICK, gap_step=TRAIL_GAP_STEP):
    for k in range(length):
        if gap_step > 0 and (k % gap_step) == (gap_step - 1):
            continue  # stylised gaps
        tx = cx - k
        ty = cy + k
        thick = max(1, int(round(max_thick * (1.0 - k / max(length-1,1)))))
        for ribbon_off in (-2, 0):
            for t in range(-thick, thick+1):
                rx = tx + ribbon_off + t
                ry = ty
                if in_bounds(rx, ry): frame[ry, rx] = 1

# -----------------------------
# HALO: erase stray stars around/behind the star
# -----------------------------
def wipe_halo(frame, cx, cy, rx=HALO_RX, ry=HALO_RY, trail_bonus=HALO_TRAIL_BONUS):
    rxsq = float(rx*rx); rysq = float(ry*ry)
    for y in range(cy - ry - trail_bonus, cy + ry + 1):
        for x in range(cx - rx - trail_bonus, cx + rx + 1):
            if not in_bounds(x, y): continue
            dx = x - cx; dy = y - cy
            inside = (dx*dx)/max(rxsq,1) + (dy*dy)/max(rysq,1) <= 1.0
            along_trail = (dx <= 0) and (dy >= 0) and (abs(dx) + abs(dy) <= (rx + trail_bonus))
            if inside or along_trail:
                frame[y, x] = 0  # wipe to black

# -----------------------------
# Sparkle Explosion
# -----------------------------
class SparkleExplosion:
    def __init__(self, center, particles=EXP_PARTICLES, radius=EXP_RADIUS_PX):
        self.cx, self.cy = center
        self.N = particles
        self.radius = radius
        self.angles = [(2*math.pi*i/self.N) + random.uniform(-EXP_SPREAD_JITTER, EXP_SPREAD_JITTER)
                       for i in range(self.N)]
    def draw(self, frame, t_norm):
        r = t_norm * self.radius
        drop_every = 3 if t_norm > 0.7 else 9999
        for i, ang in enumerate(self.angles):
            if (i % drop_every) == 0 and t_norm > 0.7:
                continue
            x = int(round(self.cx + r * math.cos(ang)))
            y = int(round(self.cy + r * math.sin(ang)))
            if in_bounds(x, y): frame[y, x] = 1

# -----------------------------
# Main
# -----------------------------
def main():
    random.seed(os.urandom(8))

    # Starfield for intro + faint backdrop during fly
    stars = random_starfield(STAR_COUNT)
    mask  = make_mask(len(stars), True)

    blink_frames   = int(round(BLINK_INTRO_S * FPS))
    fly_frames     = int(round(FLY_S        * FPS))
    explode_frames = int(round(EXPLODE_S    * FPS))

    # ---- 4s BLINK INTRO ----
    next_blink_tick = 0.0
    for _ in range(blink_frames):
        frame = blank_black()
        now = time.time()
        if now >= next_blink_tick:
            twinkle_step(mask, BLINK_PCT)
            next_blink_tick = now + (1.0 / max(BLINK_RATE_HZ, 0.01))
        place_stars(frame, stars, mask=mask)
        send_to_panels(pack_flipbytes(frame)); time.sleep(DT)

    # ---- STAR ICON FLY (BL -> TR) with clean halo ----
    start_x, start_y = -6, HEIGHT + 6
    end_x,   end_y   = WIDTH + 6, -6
    cx, cy = start_x, start_y

    for i in range(fly_frames):
        t = i / max(fly_frames-1, 1)
        cx = int(round(start_x + (end_x - start_x) * t))
        cy = int(round(start_y + (end_y - start_y) * t))

        frame = blank_black()
        place_stars(frame, stars)            # background stars
        wipe_halo(frame, cx, cy)             # remove strays near icon
        draw_twin_streaks(frame, cx, cy)     # twin streaks behind
        blit_bitmap(frame, STAR_HALF, cx, cy)# half-scale star

        send_to_panels(pack_flipbytes(frame)); time.sleep(DT)

    # ---- Explosion at exit point ----
    exit_cx = min(max(cx, 0), WIDTH - 1)
    exit_cy = min(max(cy, 0), HEIGHT - 1)
    boom = SparkleExplosion(center=(exit_cx, exit_cy))
    for j in range(explode_frames):
        t_norm = j / max(explode_frames-1, 1)
        frame = blank_black()
        place_stars(frame, stars)
        boom.draw(frame, t_norm)
        send_to_panels(pack_flipbytes(frame)); time.sleep(DT)

    time.sleep(0.15)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass
