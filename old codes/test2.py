#!/usr/bin/env python3
# 28x28 flipdot
# Sequence:
# 1) Stem grows (half-size)
# 2) Leaves grow (half-size)
# 3) Sunflower blooms at stem tip (center-out fill, NO outline)
#
# Conventions: 0 = black (dot ON), 1 = white (dot OFF)

import os, time, math
import numpy as np
from collections import deque
from PIL import Image, ImageFilter
import serial

# ---------------- Serial / Panels ----------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# ---------------- Canvas / Timing ----------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "50"))
DT            = 1.0 / max(1.0, FPS)

# ---------------- Sequence timing ----------------
STEM_SEC      = float(os.getenv("STEM_SEC", "1.8"))
LEAVES_SEC    = float(os.getenv("LEAVES_SEC", "2.2"))
BLOOM_FILL    = float(os.getenv("BLOOM_FILL_SEC", "0.75"))
BLOOM_HOLD    = float(os.getenv("BLOOM_HOLD_SEC", "1.2"))
N_STEM        = max(1, int(STEM_SEC   * FPS))
N_LEAVES      = max(1, int(LEAVES_SEC * FPS))

# ---------------- Plant scale & placement ----------------
PLANT_SCALE_X = float(os.getenv("PLANT_SCALE_X", "0.5"))
PLANT_SCALE_Y = float(os.getenv("PLANT_SCALE_Y", "0.5"))
PLANT_ANCHOR  = os.getenv("PLANT_ANCHOR", "bottom-center")  # bottom-center | bottom-left | bottom-right
PLANT_OFF_X   = int(os.getenv("PLANT_OFF_X", "0"))
PLANT_OFF_Y   = int(os.getenv("PLANT_OFF_Y", "0"))

# ---------------- Sunflower image & scale ----------------
SUNFLOWER_PATH = os.getenv("SUNFLOWER_PATH", "sunflower.png")
SUN_INVERT     = (os.getenv("SUNFLOWER_INVERT", "1") == "1")
SUN_SCALE      = float(os.getenv("SUNFLOWER_SCALE", "0.80"))  # 0.6–0.9 usually best
SUN_NUDGE_X    = float(os.getenv("SUNFLOWER_NUDGE_X", "1"))
SUN_NUDGE_Y    = float(os.getenv("SUNFLOWER_NUDGE_Y", "-3"))

# Sharpness knobs (no outline)
THRESH_MODE    = os.getenv("THRESH_MODE", "otsu").lower()     # 'otsu' | 'fixed'
THRESH         = int(os.getenv("THRESH", "185"))              # used when THRESH_MODE='fixed'
SUNFLOWER_SS   = int(os.getenv("SUNFLOWER_SS", "3"))          # supersample factor (2–4 good)
GAUSS_RADIUS   = float(os.getenv("GAUSS_RADIUS", str(0.35)))  # mild pre-blur at SS scale

# --- Breeze / wind sway (after the bloom) ---
BREEZE_SEC     = float(os.getenv("BREEZE_SEC", "5.0"))   # total sway time
BREEZE_HZ      = float(os.getenv("BREEZE_HZ", "1.2"))    # cycles/second
BREEZE_AMPL    = int(os.getenv("BREEZE_AMPL", "1"))      # px; 1 is best on flip-dots
BREEZE_PHASEDY = float(os.getenv("BREEZE_PHASEDY", "0.18"))  # per-row phase offset
BREEZE_DECAY   = float(os.getenv("BREEZE_DECAY", "0.10"))     # 0=no decay envelope


# ---------------- Final silhouettes from your plant masks ----------------
BITROWS_FINAL = [
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0001110000001110000000000000",
"0001111000001110000000000000",
"0001111100001110000000000000",
"0001111110001110000000000000",
"0001111111001110000000000000",
"0001111111101110001111100000",
"0000111111111110011111100000",
"0000011111111110111111000000",
"0000001111111111111111000000",
"0000000000001111111110000000",
"0000000000001111111100000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
]

BITROWS_STEM = [
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
"0000000000001110000000000000",
]

# ---------------- Helpers (pack/send etc.) ----------------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                bit = int(frame28[off + y, x]) & 1
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_frame(img):
    payloads = pack_flipbytes(img.astype(np.uint8))
    for addr, data in zip(PANEL_ADDRS, payloads):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

def rows_to_bool(rows):
    out = np.zeros((HEIGHT, WIDTH), dtype=bool)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row[:WIDTH]):
            out[y, x] = (ch == '1')
    return out

def ease_slow(t):
    t = max(0.0, min(1.0, t))
    return 3*t*t - 2*t*t*t

def bfs_distance(mask, seeds):
    H, W = mask.shape
    INF = 10**9
    dist = np.full((H, W), INF, dtype=np.int32)
    q = deque()
    for sy, sx in seeds:
        if 0 <= sy < H and 0 <= sx < W and mask[sy, sx]:
            dist[sy, sx] = 0; q.append((sy, sx))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y+dy, x+dx
            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx]:
                nd = dist[y, x] + 1
                if nd < dist[ny, nx]:
                    dist[ny, nx] = nd; q.append((ny, nx))
    return dist

# ---------------- Scale & place the plant masks (½ size) ----------------
def scale_bool_mask(mask_bool, sx, sy):
    h, w = mask_bool.shape
    new_w = max(1, int(round(w * sx)))
    new_h = max(1, int(round(h * sy)))
    img = Image.fromarray(mask_bool.astype(np.uint8) * 255, mode="L")
    img = img.resize((new_w, new_h), resample=Image.NEAREST)
    small = (np.array(img, dtype=np.uint8) > 127)
    return small

def place_small_on_canvas(small_bool, anchor="bottom-center", off_x=0, off_y=0):
    H, W = HEIGHT, WIDTH
    sh, sw = small_bool.shape
    canvas = np.zeros((H, W), dtype=bool)

    if anchor == "bottom-left":
        y0 = H - sh; x0 = 0
    elif anchor == "bottom-right":
        y0 = H - sh; x0 = W - sw
    else:
        y0 = H - sh; x0 = (W - sw) // 2

    y0 = max(0, min(H - sh, y0 + off_y))
    x0 = max(0, min(W - sw, x0 + off_x))
    canvas[y0:y0+sh, x0:x0+sw] = small_bool
    return canvas

# Build original full-size masks from your rows
STEM_FULL   = rows_to_bool(BITROWS_STEM)
TARGET_FULL = rows_to_bool(BITROWS_FINAL)

# Scale both to half-size and place on canvas
stem_small   = scale_bool_mask(STEM_FULL,   PLANT_SCALE_X, PLANT_SCALE_Y)
target_small = scale_bool_mask(TARGET_FULL, PLANT_SCALE_X, PLANT_SCALE_Y)

STEM_MASK   = place_small_on_canvas(stem_small,   PLANT_ANCHOR, PLANT_OFF_X, PLANT_OFF_Y)
TARGET_MASK = place_small_on_canvas(target_small, PLANT_ANCHOR, PLANT_OFF_X, PLANT_OFF_Y)
LEAVES_MASK = TARGET_MASK & (~STEM_MASK)

# Precompute stem geometry & leaf distance
ys, xs = np.where(STEM_MASK)
if ys.size:
    stem_bottom_y = ys.max()
    stem_top_y    = ys.min()
    stem_height   = stem_bottom_y - stem_top_y + 1
else:
    stem_bottom_y = HEIGHT-1; stem_top_y = HEIGHT-1; stem_height = 1

leaf_seeds = []
for y, x in zip(ys, xs):
    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
        ny, nx = y+dy, x+dx
        if 0 <= ny < HEIGHT and 0 <= nx < WIDTH and LEAVES_MASK[ny, nx]:
            leaf_seeds.append((ny, nx))
leaf_seeds = list(dict.fromkeys(leaf_seeds))
if LEAVES_MASK.any() and leaf_seeds:
    LEAF_DIST = bfs_distance(LEAVES_MASK, leaf_seeds); leaf_max_d = int(LEAF_DIST[LEAVES_MASK].max())
else:
    LEAF_DIST = np.zeros_like(LEAVES_MASK, dtype=np.int32); leaf_max_d = 1

# ---------------- Sunflower (sharp, no outline) ----------------
def _otsu_threshold(arr_uint8):
    hist, _ = np.histogram(arr_uint8, bins=256, range=(0,255))
    total = arr_uint8.size
    sum_total = (np.arange(256) * hist).sum()
    w0 = 0; sum0 = 0; max_var = -1; thresh = 128
    for t in range(256):
        w0 += hist[t]; sum0 += t*hist[t]
        if w0 == 0 or w0 == total: continue
        w1 = total - w0
        mu0 = sum0 / w0
        mu1 = (sum_total - sum0) / w1
        var_between = w0*w1*(mu0 - mu1)**2
        if var_between > max_var:
            max_var = var_between; thresh = t
    return thresh

def load_sunflower_mask_bool(path, invert=False, scale=0.8):
    """
    Returns BOOL mask True where flower pixels (→ draw black/ON).
    Sharpest pipeline: supersample -> mild blur -> nearest shrink -> threshold
    """
    img = Image.open(path).convert("L")
    SS = max(2, min(4, SUNFLOWER_SS))

    big = img.resize((int(WIDTH*SS), int(HEIGHT*SS)), resample=Image.BICUBIC)
    if GAUSS_RADIUS > 0:
        big = big.filter(ImageFilter.GaussianBlur(radius=GAUSS_RADIUS*SS))

    small_w = max(1, int(round(WIDTH*scale)))
    small = big.resize((small_w, small_w), resample=Image.NEAREST)
    A = np.array(small, dtype=np.uint8)
    if invert: A = 255 - A

    if THRESH_MODE == "otsu":
        t = _otsu_threshold(A)
        flower_small = (A >= t)
    else:  # fixed
        flower_small = (A >= THRESH)

    # Center on 28×28 canvas, clip, keep 1-px white border
    canvas = np.zeros((HEIGHT, WIDTH), dtype=bool)
    x0 = (WIDTH - small_w)//2; y0 = (HEIGHT - small_w)//2
    x1, y1 = min(WIDTH, x0 + small_w), min(HEIGHT, y0 + small_w)
    canvas[y0:y1, x0:x1] = flower_small[0:(y1-y0), 0:(x1-x0)]
    canvas[0,:] = canvas[-1,:] = False
    canvas[:,0] = canvas[:,-1] = False
    return canvas

def place_centered(mask_bool, cx, cy, nudge_x=0.0, nudge_y=0.0):
    H, W = HEIGHT, WIDTH
    h, w = mask_bool.shape
    x0 = int(round(cx - w/2 + nudge_x))
    y0 = int(round(cy - h/2 + nudge_y))
    x1 = max(0, x0); y1 = max(0, y0)
    x2 = min(W, x0 + w); y2 = min(H, y0 + h)
    if x1 >= x2 or y1 >= y2:
        return np.zeros((H, W), dtype=bool)
    sub = mask_bool[(y1 - y0):(y2 - y0), (x1 - x0):(x2 - x0)]
    canvas = np.zeros((H, W), dtype=bool)
    canvas[y1:y2, x1:x2] = sub
    return canvas

# ---------------- Animation frames ----------------
def frame_stem_at(i):
    t = i / max(1, N_STEM - 1)
    img = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    prog   = 0.5 - 0.5 * math.cos(math.pi * t)
    grow_h = int(round(stem_height * prog))
    y_top  = stem_bottom_y - grow_h + 1
    y_top  = max(0, min(HEIGHT-1, y_top))

    for y in range(stem_bottom_y, y_top-1, -1):
        row_mask = STEM_MASK[y, :]
        if row_mask.any():
            img[y, row_mask] = 0
    return img

def frame_leaves_at(i):
    t = i / max(1, N_LEAVES - 1)
    img = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    img[STEM_MASK] = 0
    thresh = int(round(ease_slow(t) * leaf_max_d * 0.95))
    reveal = (LEAVES_MASK & (LEAF_DIST <= thresh))
    img[reveal] = 0
    return img

def reveal_sunflower_at_tip(base_frame):
    # locate stem tip
    ys, xs = np.where(STEM_MASK)
    if ys.size == 0:
        cx, cy = (WIDTH-1)/2.0, HEIGHT//2
    else:
        tip_y = ys.min()
        xs_at_tip = xs[ys == tip_y]
        cx = float(np.median(xs_at_tip)) if xs_at_tip.size else (WIDTH-1)/2.0
        cy = float(tip_y)

    flower_small = load_sunflower_mask_bool(SUNFLOWER_PATH, invert=SUN_INVERT, scale=SUN_SCALE)
    placed_bool  = place_centered(flower_small, cx, cy, nudge_x=SUN_NUDGE_X, nudge_y=SUN_NUDGE_Y)

    # Center-out fill (no outline)
    ys_f, xs_f = np.where(placed_bool)
    if len(xs_f) == 0:
        hold = max(1, int(BLOOM_HOLD * FPS))
        for _ in range(hold):
            send_frame(base_frame); time.sleep(DT)
        return placed_bool  # empty, but keeps type consistent

    cx_f = float(xs_f.mean()); cy_f = float(ys_f.mean())
    X, Y = np.meshgrid(np.arange(WIDTH), np.arange(HEIGHT))
    dist = np.sqrt((X - cx_f)**2 + (Y - cy_f)**2)
    L = np.where(placed_bool, dist, 1e9)
    vals = np.unique(L[L < 1e9])

    fill_steps = max(8, int(BLOOM_FILL * FPS))
    if len(vals) > fill_steps:
        idx = np.linspace(0, len(vals)-1, fill_steps).astype(int)
        ths = vals[idx]
    else:
        ths = vals

    for th in ths:
        mask = placed_bool & (L <= th)
        fr = base_frame.copy()
        fr[mask] = 0
        send_frame(fr); time.sleep(DT)

    # Hold full flower
    hold = max(1, int(BLOOM_HOLD * FPS))
    final = base_frame.copy()
    final[placed_bool] = 0
    for _ in range(hold):
        send_frame(final); time.sleep(DT)

    return placed_bool

def wind_sway(flower_bool):
    """Sways leaves+flower left/right; stem stays fixed. Adds a 1px connector if a gap opens."""
    H, W = HEIGHT, WIDTH
    N = max(1, int(BREEZE_SEC * FPS))
    omega = 2 * math.pi * BREEZE_HZ

    # static parts
    stem = STEM_MASK.copy()
    leaves = LEAVES_MASK.copy()

    for k in range(N):
        t = k / max(1, N-1)
        # simple soft envelope so it eases in/out a bit
        env = (1.0 - BREEZE_DECAY) + BREEZE_DECAY * (math.sin(math.pi * t) ** 2)

        frame = np.ones((H, W), dtype=np.uint8)
        frame[stem] = 0  # draw stem first

        for y in range(H):
            phase_y = y * BREEZE_PHASEDY
            shift = int(round(BREEZE_AMPL * env * math.sin(omega * t + phase_y)))

            # source rows (leaves + flower) for this y
            src_row = (leaves[y, :] | flower_bool[y, :])
            if not src_row.any():
                continue

            # shift without wrap
            dst = np.zeros_like(src_row)
            if shift > 0:
                dst[shift:] = src_row[:-shift]
            elif shift < 0:
                s = -shift
                dst[:-s] = src_row[s:]
            else:
                dst[:] = src_row

            # paint shifted foliage
            frame[y, dst] = 0

            # ---- connector to stem to avoid visual detachment (if needed) ----
            if shift != 0 and dst.any():
                sgn = 1 if shift > 0 else -1
                stem_cols = np.where(stem[y, :])[0]
                if stem_cols.size:
                    for xs in stem_cols:
                        xn = xs + sgn
                        if 0 <= xn < W and (not stem[y, xn]) and frame[y, xn] == 1:
                            # if there is foliage further out in sway direction, add a connector
                            has_foliage = np.any(dst[xn:]) if sgn > 0 else np.any(dst[:xn+1])
                            if has_foliage:
                                frame[y, xn] = 0

        send_frame(frame)
        time.sleep(DT)



# ---------------- Run ----------------
def main():
    print("[PLANT→SUNFLOWER] half-size plant, ultra-sharp bloom at tip (no outline)")
    try:
        last = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

        # Phase 1: stem
        for i in range(N_STEM):
            last = frame_stem_at(i)
            send_frame(last); time.sleep(DT)

        # Phase 2: leaves
        for i in range(N_LEAVES):
            last = frame_leaves_at(i)
            send_frame(last); time.sleep(DT)

        # Phase 3: sunflower bloom at tip  (single call)
        flower_mask = reveal_sunflower_at_tip(last)

        # Phase 4: wind sway (leaves + sunflower head)
        wind_sway(flower_mask)

    except KeyboardInterrupt:
        print("\n[PLANT] interrupted")
    finally:
        try: ser.close()
        except Exception: pass


if __name__ == "__main__":
    main()
