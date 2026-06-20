#!/usr/bin/env python3
# 28x28 flipdot
# Sequence:
# 1) Stem grows (half-size)
# 2) Leaves grow (half-size)
# 3) Sunflower blooms at stem tip (outline → fill → hold)
#
# Conventions: 0 = black (dot ON), 1 = white (dot OFF)

import os, time, math, random
import numpy as np
from collections import deque
from PIL import Image
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
BLOOM_OUTLINE = float(os.getenv("BLOOM_OUTLINE_SEC", "0"))
BLOOM_FILL    = float(os.getenv("BLOOM_FILL_SEC", "0.75"))
BLOOM_HOLD    = float(os.getenv("BLOOM_HOLD_SEC", "1.2"))

N_STEM   = max(1, int(STEM_SEC   * FPS))
N_LEAVES = max(1, int(LEAVES_SEC * FPS))

# ---------------- Plant scale & placement ----------------
PLANT_SCALE_X = float(os.getenv("PLANT_SCALE_X", "0.5"))  # 0.5 = half width
PLANT_SCALE_Y = float(os.getenv("PLANT_SCALE_Y", "0.5"))  # 0.5 = half height
# Anchor placement for the scaled plant
PLANT_ANCHOR = os.getenv("PLANT_ANCHOR", "bottom-center") # bottom-center | bottom-left | bottom-right
PLANT_OFF_X  = int(os.getenv("PLANT_OFF_X", "0"))         # extra pixel nudges after anchoring
PLANT_OFF_Y  = int(os.getenv("PLANT_OFF_Y", "0"))

# ---------------- Sunflower image & scale ----------------
SUNFLOWER_PATH   = os.getenv("SUNFLOWER_PATH", "sunflower.png")
SUN_THR          = int(os.getenv("SUNFLOWER_THRESH", "100"))
SUN_INVERT       = (os.getenv("SUNFLOWER_INVERT", "1") == "1")
SUN_SCALE        = float(os.getenv("SUNFLOWER_SCALE", "0.8"))  # relative to canvas; ~0.6–0.9 looks good
SUN_NUDGE_X      = float(os.getenv("SUNFLOWER_NUDGE_X", "1")) # local nudge around stem tip (px)
SUN_NUDGE_Y      = float(os.getenv("SUNFLOWER_NUDGE_Y", "-3"))

# ---------------- Final silhouettes from your plant masks ----------------
# (Kept exactly from your script; these define the original full-size plant shape)
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
    """Pack 28x28 (0/1) into four 7-row chunks, column-major."""
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
    """Nearest-neighbor downscale then return SMALL mask as bool array."""
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
        y0 = H - sh
        x0 = 0
    elif anchor == "bottom-right":
        y0 = H - sh
        x0 = W - sw
    else:  # bottom-center
        y0 = H - sh
        x0 = (W - sw) // 2

    y0 = max(0, min(H - sh, y0 + off_y))
    x0 = max(0, min(W - sw, x0 + off_x))

    canvas[y0:y0+sh, x0:x0+sw] = small_bool
    return canvas

# Build original full-size masks from your rows
STEM_FULL   = rows_to_bool(BITROWS_STEM)     # True where stem
TARGET_FULL = rows_to_bool(BITROWS_FINAL)    # True where (stem OR leaves)

# Scale both to half-size and place on canvas
stem_small   = scale_bool_mask(STEM_FULL,   PLANT_SCALE_X, PLANT_SCALE_Y)
target_small = scale_bool_mask(TARGET_FULL, PLANT_SCALE_X, PLANT_SCALE_Y)

STEM_MASK   = place_small_on_canvas(stem_small,   PLANT_ANCHOR, PLANT_OFF_X, PLANT_OFF_Y)
TARGET_MASK = place_small_on_canvas(target_small, PLANT_ANCHOR, PLANT_OFF_X, PLANT_OFF_Y)

LEAVES_MASK = TARGET_MASK & (~STEM_MASK)  # leaves are what's in TARGET but not stem

# Precompute stem geometry & leaf distance
ys, xs = np.where(STEM_MASK)
if ys.size:
    stem_bottom_y = ys.max()
    stem_top_y    = ys.min()
    stem_height   = stem_bottom_y - stem_top_y + 1
else:
    stem_bottom_y = HEIGHT-1
    stem_top_y    = HEIGHT-1
    stem_height   = 1

# seeds = leaf pixels next to stem pixels (to “grow out” from stem)
leaf_seeds = []
for y, x in zip(ys, xs):
    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
        ny, nx = y+dy, x+dx
        if 0 <= ny < HEIGHT and 0 <= nx < WIDTH and LEAVES_MASK[ny, nx]:
            leaf_seeds.append((ny, nx))
leaf_seeds = list(dict.fromkeys(leaf_seeds))

if LEAVES_MASK.any() and leaf_seeds:
    LEAF_DIST = bfs_distance(LEAVES_MASK, leaf_seeds)
    leaf_max_d = int(LEAF_DIST[LEAVES_MASK].max())
else:
    LEAF_DIST = np.zeros_like(LEAVES_MASK, dtype=np.int32)
    leaf_max_d = 1

# ---------------- Sunflower helpers ----------------

# replace your load_sunflower_mask(...) internals where the image is resized/thresholded
base = Image.open(path).convert("L")

# 3× supersample, then nearest shrink → crisper edges on 28×28
SS = 3
big = base.resize((WIDTH*SS, HEIGHT*SS), resample=Image.BICUBIC)  # smooth up
big = big.point(lambda p: p)  # no-op (you can contrast here if needed)
small = big.resize((int(round(WIDTH*scale)), int(round(HEIGHT*scale))), resample=Image.NEAREST)

A = np.array(small, dtype=np.uint8)
if invert:
    A = 255 - A
mask = np.ones((small.size[1], small.size[0]), dtype=np.uint8)
mask[A >= thr] = 0  # 0=black(ON), 1=white(OFF)


def load_sunflower_mask(path, thr=128, invert=False, scale=0.75):
    # replace your load_sunflower_mask(...) internals where the image is resized/thresholded
    base = Image.open(path).convert("L")

    # 3× supersample, then nearest shrink → crisper edges on 28×28
    SS = 3
    big = base.resize((WIDTH*SS, HEIGHT*SS), resample=Image.BICUBIC)  # smooth up
    big = big.point(lambda p: p)  # no-op (you can contrast here if needed)
    small = big.resize((int(round(WIDTH*scale)), int(round(HEIGHT*scale))), resample=Image.NEAREST)

    A = np.array(small, dtype=np.uint8)
    if invert:
        A = 255 - A
    mask = np.ones((small.size[1], small.size[0]), dtype=np.uint8)
    mask[A >= thr] = 0  # 0=black(ON), 1=white(OFF)
    #     """
    # Load sunflower PNG → threshold to 0/1, scale to (28*scale), return small bool mask.
    # 0=black(ON), 1=white(OFF) → We return bool mask 'True for flower pixels (ON)'.
    # """
    # try:
    #     img = Image.open(path).convert("L")
    # except Exception as e:
    #     print(f"[WARN] cannot open sunflower '{path}': {e}")
    #     # fallback to small 1-px dot
    #     arr = np.zeros((max(1,int(28*scale)), max(1,int(28*scale))), dtype=bool)
    #     arr[arr.shape[0]//2, arr.shape[1]//2] = True
    #     return arr

    # if invert:
    #     img = Image.eval(img, lambda p: 255 - p)
    # w_small = max(1, int(round(28 * scale)))
    # h_small = w_small
    # img = img.resize((w_small, h_small), resample=Image.NEAREST)
    # A = np.array(img, dtype=np.uint8)
    # flower = (A >= thr)  # True where flower pixels
    # # keep white 1px border OFF when placed later (we’ll clip edges naturally)
    return flower

def thicken_outline(fill_mask_uint8):
    H, W = fill_mask_uint8.shape
    fill = (fill_mask_uint8 == 0)
    outl = np.zeros_like(fill, dtype=bool)
    ys, xs = np.where(fill)
    for y, x in zip(ys, xs):
        edge = False
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y+dy, x+dx
            if ny < 0 or ny >= H or nx < 0 or nx >= W or not fill[ny, nx]:
                edge = True; break
        if edge: outl[y, x] = True
    # expand outline by 1px (Manhattan)
    thick = outl.copy()
    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
        thick |= np.roll(np.roll(outl, dy, axis=0), dx, axis=1)
    # compose: keep existing fill, but ensure outline pixels are ON (black=0)
    result = fill_mask_uint8.copy()
    result[thick] = 0
    return result


def place_centered(mask_bool, cx, cy, nudge_x=0.0, nudge_y=0.0):
    """Place a small bool mask centered at (cx,cy) with pixel nudges; clip to canvas."""
    H, W = HEIGHT, WIDTH
    h, w = mask_bool.shape
    x0 = int(round(cx - w/2 + nudge_x))
    y0 = int(round(cy - h/2 + nudge_y))

    # clip paste
    x1 = max(0, x0); y1 = max(0, y0)
    x2 = min(W, x0 + w); y2 = min(H, y0 + h)
    if x1 >= x2 or y1 >= y2:
        return np.zeros((H, W), dtype=bool)

    sub = mask_bool[(y1 - y0):(y2 - y0), (x1 - x0):(x2 - x0)]
    canvas = np.zeros((H, W), dtype=bool)
    canvas[y1:y2, x1:x2] = sub
    return canvas

def outline_from_fill_bool(fill_bool):
    H, W = fill_bool.shape
    out = np.zeros_like(fill_bool, dtype=bool)
    ys, xs = np.where(fill_bool)
    for y, x in zip(ys, xs):
        edge = False
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y+dy, x+dx
            if ny < 0 or ny >= H or nx < 0 or nx >= W or not fill_bool[ny, nx]:
                edge = True; break
        if edge:
            out[y, x] = True
    return out

# ---------------- Animation frames ----------------
def frame_stem_at(i):
    """Grow stem upward within scaled mask bounds."""
    t = i / max(1, N_STEM - 1)
    img = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    # Progress from bottom toward top
    prog   = 0.5 - 0.5 * math.cos(math.pi * t)  # smooth
    grow_h = int(round(stem_height * prog))
    y_top  = stem_bottom_y - grow_h + 1
    y_top  = max(0, min(HEIGHT-1, y_top))

    for y in range(stem_bottom_y, y_top-1, -1):
        row_mask = STEM_MASK[y, :]
        if row_mask.any():
            img[y, row_mask] = 0
    return img

def frame_leaves_at(i):
    """Reveal leaves gradually using distance threshold, with full stem visible."""
    t = i / max(1, N_LEAVES - 1)
    img = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    img[STEM_MASK] = 0

    thresh = int(round(ease_slow(t) * leaf_max_d * 0.95))
    reveal = (LEAVES_MASK & (LEAF_DIST <= thresh))
    img[reveal] = 0
    return img

def reveal_sunflower_at_tip(base_frame):
    """Outline → fill → hold of sunflower placed at the stem tip."""
    # Stem tip (top-most y of stem), use its median x to center the bloom
    ys, xs = np.where(STEM_MASK)
    if ys.size == 0:
        cx, cy = (WIDTH-1)/2.0, HEIGHT//2
    else:
        tip_y = ys.min()
        xs_at_tip = xs[ys == tip_y]
        cx = float(np.median(xs_at_tip)) if xs_at_tip.size else (WIDTH-1)/2.0
        cy = float(tip_y)

    small = load_sunflower_mask(SUNFLOWER_PATH, thr=SUN_THR, invert=SUN_INVERT, scale=SUN_SCALE)  # :contentReference[oaicite:3]{index=3}
    placed_bool = place_centered(small, cx, cy, nudge_x=SUN_NUDGE_X, nudge_y=SUN_NUDGE_Y)

    # # A) outline sketch
    # outline = outline_from_fill_bool(placed_bool)
    # pts = np.column_stack(np.where(outline))
    # np.random.shuffle(pts)
    # steps = max(6, int(BLOOM_OUTLINE * FPS))
    # chunks = np.array_split(pts, steps) if len(pts) >= steps else [pts]

    # acc = base_frame.copy()
    # for chunk in chunks:
    #     for (y, x) in chunk:
    #         acc[y, x] = 0
    #     send_frame(acc); time.sleep(DT)

    # B) center-out fill
    ys_f, xs_f = np.where(placed_bool)
    if len(xs_f) == 0:
        # nothing to fill; just hold outline
        hold = max(1, int(BLOOM_HOLD * FPS))
        for _ in range(hold):
            send_frame(acc); time.sleep(DT)
        return

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

    # C) hold full flower
    hold = max(1, int(BLOOM_HOLD * FPS))
    final = base_frame.copy()
    final[placed_bool] = 0
    for _ in range(hold):
        send_frame(final); time.sleep(DT)

# ---------------- Run ----------------
def main():
    print("[PLANT→SUNFLOWER] half-size plant, bloom at tip")
    try:
        # Phase 1: stem
        last = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
        for i in range(N_STEM):
            last = frame_stem_at(i)
            send_frame(last); time.sleep(DT)

        # Phase 2: leaves
        for i in range(N_LEAVES):
            last = frame_leaves_at(i)
            send_frame(last); time.sleep(DT)

        # Phase 3: sunflower bloom at tip
        reveal_sunflower_at_tip(last)

    except KeyboardInterrupt:
        print("\n[PLANT] interrupted")
    finally:
        try: ser.close()
        except Exception: pass

if __name__ == "__main__":
    main()
