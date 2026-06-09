# anim_sprout_breeze_locked_twigs.py
# Phases:
#   1) Stem grows (mask-driven)
#   2) Leaves bloom (distance field)
#   3) Breeze sway (leaves only) with stem-lock connectors:
#      - 1px connector normally
#      - up to 2px "twig" at peak breeze so nothing detaches
#
# Notes:
# - 0 = black(ON), 1 = white(OFF) for flipdots
# - Keeps global +1px RIGHT shift
# - Uses your exact plant & breeze settings

import numpy as np
import time, os, serial, math
from collections import deque

# ---------------- Serial / Panels ----------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# ---------------- Canvas / Timing ----------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "50"))
DT            = 1.0 / FPS

# ---- Plant (Phases 1–2) ----
DURATION    = float(os.getenv("DURATION", "4.5"))
PHASE_SPLIT = float(os.getenv("PHASE_SPLIT", "0.45"))
NFRAMES_PLANT = max(1, int(DURATION * FPS))

# ---- Breeze (Phase 3) ----
BREEZE_SEC     = float(os.getenv("BREEZE_SEC", "5.6"))
BREEZE_HZ      = float(os.getenv("BREEZE_HZ", "1.4"))
BREEZE_AMPL    = int(os.getenv("BREEZE_AMPL", "1"))     # 1px is best for 28×28
BREEZE_PHASEDY = float(os.getenv("BREEZE_PHASEDY", "0.18"))
BREEZE_DECAY   = float(os.getenv("BREEZE_DECAY", "0.12"))
BREEZE_FRAMES  = max(1, int(BREEZE_SEC * FPS))

# ---------------- Global X shift -----------------
X_SHIFT = int(os.getenv("X_SHIFT", "1"))  # +1 → shift right by 1

# ---------------- Final silhouette (1=plant, 0=bg) ----------------
BITROWS_FINAL = [
"0000000000000000000000000000",
"0001110000000000000000000000",
"0001111000000000000000000000",
"0001111100000000000000000000",
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

# ---------------- Stem-only mask (1=stem, 0=not stem) ------------
BITROWS_STEM = [
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
"0000000000000000000000000000",
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

# ---------------- Helpers ----------------
def pack_flipbytes(frame28):
    """Pack a 28x28 (0=black,1=white) into 4x panel payloads (column-major, 7 rows/byte)."""
    panels = []
    for p in range(4):
        off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                bit = int(frame28[off + y, x])
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

def map_rows_to_frame(rows, one_is_black=True):
    """rows: list[str] '0'/'1'. If one_is_black, '1'->black(0), '0'->white(1). Else returns bool mask."""
    if one_is_black:
        out = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
        for y, row in enumerate(rows):
            for x, ch in enumerate(row[:WIDTH]):
                out[y, x] = 0 if ch == '1' else 1
        return out
    else:
        out = np.zeros((HEIGHT, WIDTH), dtype=bool)
        for y, row in enumerate(rows):
            for x, ch in enumerate(row[:WIDTH]):
                out[y, x] = (ch == '1')
        return out

def ease_slow(t):
    t = max(0.0, min(1.0, t))
    return 3*t*t - 2*t*t*t  # cubic in/out



def bfs_distance(mask, seeds):
    """Manhattan distance within mask from multiple seeds."""
    H, W = mask.shape
    INF = 10**9
    dist = np.full((H, W), INF, dtype=np.int32)
    q = deque()

    # normalize seeds to nearest valid pixel
    for sy, sx in seeds:
        if 0 <= sy < H and 0 <= sx < W and mask[sy, sx]:
            dist[sy, sx] = 0
            q.append((sy, sx))
        else:
            found = False
            for r in range(1, 4):
                for dy in range(-r, r+1):
                    for dx in range(-r, r+1):
                        ny, nx = sy + dy, sx + dx
                        if 0 <= ny < H and 0 <= nx < W and mask[ny, nx]:
                            if dist[ny, nx] > 0:
                                dist[ny, nx] = 0
                                q.append((ny, nx))
                                found = True
                                break
                    if found: break
                if found: break

    # standard BFS
    while q:
        y, x = q.popleft()   # <-- no colon here
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx]:
                nd = dist[y, x] + 1
                if nd < dist[ny, nx]:
                    dist[ny, nx] = nd
                    q.append((ny, nx))
    return dist



def shift_right(arr, fill_white=True):
    """Shift an image/mask right by X_SHIFT. For uint8 images: fill with white(1). For bool: False."""
    if X_SHIFT == 0: return arr
    if arr.dtype == np.uint8:
        out = np.ones_like(arr) if fill_white else np.zeros_like(arr)
        src_x0 = 0; dst_x0 = X_SHIFT
        w = WIDTH - X_SHIFT
        out[:, dst_x0:dst_x0+w] = arr[:, src_x0:src_x0+w]
        return out
    else:
        out = np.zeros_like(arr, dtype=bool)
        src_x0 = 0; dst_x0 = X_SHIFT
        w = WIDTH - X_SHIFT
        out[:, dst_x0:dst_x0+w] = arr[:, src_x0:src_x0+w]
        return out

# ---------------- Build masks (then shift) ----------------
TARGET_raw    = map_rows_to_frame(BITROWS_FINAL, one_is_black=True)   # uint8: 0=black,1=white
STEM_MASK_raw = map_rows_to_frame(BITROWS_STEM,  one_is_black=False)  # bool mask

TARGET    = shift_right(TARGET_raw, fill_white=True)
STEM_MASK = shift_right(STEM_MASK_raw, fill_white=False)
LEAVES_MASK = (TARGET == 0) & (~STEM_MASK)

ys, xs = np.where(STEM_MASK)
stem_top_y  = ys.min() if ys.size else HEIGHT-1
stem_height = HEIGHT - stem_top_y

H, W = HEIGHT, WIDTH
leaf_seeds = []
for y, x in zip(ys, xs):
    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
        ny, nx = y+dy, x+dx
        if 0 <= ny < H and 0 <= nx < W and LEAVES_MASK[ny, nx]:
            leaf_seeds.append((ny, nx))
leaf_seeds = list(dict.fromkeys(leaf_seeds))
LEAF_DIST = bfs_distance(LEAVES_MASK, leaf_seeds)
leaf_max_d = int(LEAF_DIST[LEAVES_MASK].max()) if LEAVES_MASK.any() else 1

# Cache stem columns per row for fast connectors
STEM_COLS_PER_ROW = [np.where(STEM_MASK[y, :])[0] for y in range(H)]

# ---------------- Plant (Phases 1–2) ----------------
def plant_frame_at(i):
    t = i / max(1, NFRAMES_PLANT - 1)
    img = np.ones_like(TARGET, dtype=np.uint8)

    if t <= PHASE_SPLIT:
        # Stem grows bottom→top (cosine-ish ease)
        prog   = 0.5 - 0.5*np.cos(np.pi * (t / max(1e-6, PHASE_SPLIT)))
        grow_h = int(stem_height * prog)
        y_top  = HEIGHT - grow_h
        for y in range(HEIGHT-1, y_top-1, -1):
            row_mask = STEM_MASK[y, :]
            if row_mask.any():
                img[y, row_mask] = 0
        return img

    # Full stem + slow leaves bloom
    img[STEM_MASK] = 0
    ph2    = (t - PHASE_SPLIT) / max(1e-6, (1.0 - PHASE_SPLIT))
    thresh = int(ease_slow(ph2) * leaf_max_d * 0.9)
    reveal = (LEAF_DIST <= thresh)
    img[reveal & LEAVES_MASK] = 0
    img[STEM_MASK] = 0
    return img

# ---------------- Breeze (Phase 3): locked + 2px twigs near peak ----------------
def breeze_frame_at(k):
    """Leaves sway per-row; add 1–2px connectors on the sway side of each stem column if needed."""
    t = k / max(1, BREEZE_FRAMES - 1)
    omega = 2 * math.pi * BREEZE_HZ
    env = (1.0 - BREEZE_DECAY) + BREEZE_DECAY * (math.sin(math.pi * t) ** 2)

    img = np.ones((H, W), dtype=np.uint8)
    img[STEM_MASK] = 0  # paint stem first

    for y in range(H):
        phase_y = y * BREEZE_PHASEDY
        shift = int(round(BREEZE_AMPL * env * math.sin(omega * t + phase_y)))

        src_row = LEAVES_MASK[y, :]
        if not src_row.any():
            continue

        # shift without wrapping
        dst_row = np.zeros_like(src_row)
        if shift > 0:
            dst_row[shift:] = src_row[:-shift]
        elif shift < 0:
            s = -shift
            dst_row[:-s] = src_row[s:]
        else:
            dst_row[:] = src_row

        # paint shifted leaves
        leaf_cols = np.where(dst_row)[0]
        if leaf_cols.size:
            img[y, leaf_cols] = 0

        # ---- stem-lock connectors ----
        # base connector length
        twig_len = 1
        # at peak breeze, allow a second dot (2px twig) if AMPL ≥ 1
        if env > 0.72 and BREEZE_AMPL >= 1:
            twig_len = 2

        if shift != 0 and STEM_COLS_PER_ROW[y].size and leaf_cols.size:
            sgn = 1 if shift > 0 else -1
            for xs in STEM_COLS_PER_ROW[y]:
                # only add connectors if there are leaves somewhere beyond the stem in sway direction
                has_leaf_beyond = np.any(leaf_cols > xs) if sgn > 0 else np.any(leaf_cols < xs)
                if not has_leaf_beyond:
                    continue
                # place up to twig_len pixels outward from the stem
                for d in range(1, twig_len + 1):
                    xn = xs + sgn * d
                    if 0 <= xn < W and (not STEM_MASK[y, xn]) and img[y, xn] == 1:
                        img[y, xn] = 0  # connector/twig

    return img

# ---------------- Run ----------------
try:
    # Phases 1–2
    for i in range(NFRAMES_PLANT):
        frame = plant_frame_at(i)
        send_to_panels(pack_flipbytes(frame))
        time.sleep(DT)

    # Phase 3: Breeze with connectors/twigs
    for k in range(BREEZE_FRAMES):
        frame = breeze_frame_at(k)
        send_to_panels(pack_flipbytes(frame))
        time.sleep(DT)

    # Hold final silhouette
    hold = float(os.getenv("HOLD_FINAL_SEC", "1.8"))
    final = np.ones_like(TARGET, dtype=np.uint8)
    final[STEM_MASK] = 0
    final[LEAVES_MASK] = 0
    t_end = time.time() + hold
    while time.time() < t_end:
        send_to_panels(pack_flipbytes(final))
        time.sleep(0.08)

except KeyboardInterrupt:
    print("[SPROUT] exit")
finally:
    ser.close()
