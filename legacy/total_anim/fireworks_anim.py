# anim_fireworks_heart_v2.py
import numpy as np
import time
import random
import serial
import math
import os

# -----------------------------
# Flipdot / Serial
# -----------------------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]   # 4 x (7x28) stacked = 28x28
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "60"))
FRAME_DT      = 1.0 / FPS
DURATION_S    = float(os.getenv("DURATION_S", "7.0"))
TOTAL_FRAMES  = int(DURATION_S * FPS)  # not used now but kept for parity

# -----------------------------
# Firework tuning (LESS CROWDED)
# -----------------------------
NUM_RAYS                = int(os.getenv("NUM_RAYS", "12"))    # was 16
RAY_JITTER              = float(os.getenv("RAY_JITTER", "0.16"))
RADIAL_SPEED_PX_PER_F   = float(os.getenv("RADIAL_SPEED", "0.9"))
SPAWN_PROB_PER_FRAME    = float(os.getenv("SPAWN_P", "0.12")) # was 0.22
MAX_CONCURRENT_BURSTS   = int(os.getenv("MAX_BURSTS", "3"))   # was 4
BURST_LIFETIME_FRAMES   = int(os.getenv("BURST_LIFE", "18"))
TRAIL_TTL_FRAMES        = int(os.getenv("TRAIL_TTL", "4"))    # was 6 (shorter trails)
SPAWN_COOLDOWN_FRAMES   = int(os.getenv("SPAWN_COOLDOWN", "6"))

# How long the fireworks run before the heart
FIREWORKS_S      = float(os.getenv("FIREWORKS_S", "3"))  # try 3.4–4.2
FIREWORKS_FRAMES = int(FIREWORKS_S * FPS)

# Corner bursts to guarantee “different corners”
CORNER_CENTERS     = [(6,6), (21,6), (6,21), (21,21)]
CORNER_GAP_FRAMES  = int(os.getenv("CORNER_GAP", "12"))  # slight spacing increase
corner_schedule = []
t = 0
for c in CORNER_CENTERS:
    corner_schedule.append((t, c))
    t += CORNER_GAP_FRAMES

# Final heart card
END_HEART_HOLD_FRAMES = int(os.getenv("HEART_HOLD", "40"))

# Heart shape knobs (for optional cleft heart)
HEART_SIZE   = float(os.getenv("HEART_SIZE", "9"))     # overall scale (8..11 good)
HEART_SX     = float(os.getenv("HEART_SX", "1.05"))    # widen a touch
HEART_SY     = float(os.getenv("HEART_SY", "0.95"))    # squish vertically slightly
HEART_CLEFT  = float(os.getenv("HEART_CLEFT", "0.2"))  # 0..0.5 depth of top notch
HEART_TIP    = float(os.getenv("HEART_TIP", "0.35"))   # 0..0.6 bottom “pull” (sharper tip)

# -----------------------------
# Packing & send
# -----------------------------
def pack_flipbytes(frame28):
    """
    Input: frame28 (H,W) with 0=black(ON), 1=white
    Output: list of 4 payloads (each 7x28) column-major, 7 rows/byte
    """
    panels = []
    for p in range(4):
        row_offset = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for r in range(7):
                y = row_offset + r
                bit = 0 if frame28[y, x] == 0 else 1
                byte |= (bit << r)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

# -----------------------------
# Firework primitives
# -----------------------------
def clampi(v, lo, hi):
    return max(lo, min(hi, int(v)))

def spawn_burst(center, base_angle=0.0):
    rays = []
    step = 2.0 * math.pi / NUM_RAYS
    for i in range(NUM_RAYS):
        a = base_angle + i * step + random.uniform(-RAY_JITTER, RAY_JITTER)
        rays.append(a)
    return {"center": center, "angles": rays, "age": 0}

def draw_burst(trail_ttl, burst):
    """
    Place spark tips and short trails for the given burst age.
    """
    cx, cy = burst["center"]
    radius = burst["age"] * RADIAL_SPEED_PX_PER_F

    for a in burst["angles"]:
        # current tip
        x = cx + radius * math.cos(a)
        y = cy + radius * math.sin(a)
        xi, yi = clampi(x, 0, WIDTH-1), clampi(y, 0, HEIGHT-1)

        # trail behind tip (two back-steps)
        for back in (0.0, 0.6, 1.2):
            xr = cx + max(radius - back, 0.0) * math.cos(a)
            yr = cy + max(radius - back, 0.0) * math.sin(a)
            xb, yb = clampi(xr, 0, WIDTH-1), clampi(yr, 0, HEIGHT-1)
            trail_ttl[yb, xb] = max(trail_ttl[yb, xb], TRAIL_TTL_FRAMES)

        trail_ttl[yi, xi] = max(trail_ttl[yi, xi], TRAIL_TTL_FRAMES)

def compose_frame_from_trails(trail_ttl):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    frame[trail_ttl > 0] = 0
    return frame

# -----------------------------
# Heart renderer: deeper cleft + sharper tip (filled) — OPTIONAL
# (kept here if you want to switch to this silhouette later)
# -----------------------------
def draw_big_heart_cleft(frame, cx, cy, size=9, sx=1.05, sy=0.95, cleft=0.35, tip=0.35):
    """
    Filled heart using classic implicit curve with extra shaping.
    0=black ON, 1=white.
    """
    frame[:, :] = 1
    for y in range(HEIGHT):
        for x in range(WIDTH):
            # normalized coords with aspect tweak; +y up
            px = (x - cx) / float(size) / sx
            py = (cy - y) / float(size) / sy

            # tip sharpening: stretch bottom half downward
            if py < 0:
                py *= (1.0 + tip * (-py))  # stronger pull as you go downwards

            v = (px*px + py*py - 1.0)**3 - (px*px) * (py**3)
            if v <= 0.0:
                frame[y, x] = 0

    # Cleft notch near top center
    if cleft > 0:
        notch_r = max(1.0, size * cleft)
        notch_cx = cx
        notch_cy = int(cy - size * 0.55)
        r2 = notch_r * notch_r
        for y in range(HEIGHT):
            dy = y - notch_cy
            for x in range(WIDTH):
                dx = x - notch_cx
                if (dx*dx + dy*dy) <= r2 and y <= cy:
                    frame[y, x] = 1
    return frame

# --- Same helpers/mask as your anim_confetti ending ---
def _erode_black(mask, radius=1):
    """
    mask: np.uint8 28x28, 0=black (heart), 1=white (bg)
    Erode black region by 'radius' (4-neighbourhood), shrinking the heart by 1px.
    """
    h, w = mask.shape
    out = np.ones_like(mask, dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            if mask[y, x] != 0:
                continue
            ok = True
            for dy, dx in ((0,1),(1,0),(0,-1),(-1,0)):
                ny, nx = y+dy, x+dx
                if ny < 0 or ny >= h or nx < 0 or nx >= w or mask[ny, nx] != 0:
                    ok = False
                    break
            out[y, x] = 0 if ok else 1
    if radius > 1:
        for _ in range(radius-1):
            out = _erode_black(out, 1)
    return out

def mask_heart():
    """
    Implicit heart; shifted down by 1px and eroded by 1px to increase the white border.
    0 = black (ON), 1 = white (OFF)
    """
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    # Center & scale (match confetti)
    cx = (WIDTH - 1) / 2.0
    cy = (HEIGHT - 1) / 2.0 + 1.0   # +1px DOWN
    span = 1.25                      # base size; erosion will thin by ~1px

    for y in range(HEIGHT):
        py = ((cy - y) / (HEIGHT * 0.5)) * span  # +y up
        for x in range(WIDTH):
            px = ((x - cx) / (WIDTH * 0.5)) * span
            v = (px*px + py*py - 1.0)
            f = v*v*v - (px*px)*(py*py*py)
            if f <= 0.0:
                frame[y, x] = 0

    # Erode the black region by 1px to create a slightly larger white border
    frame = _erode_black(frame, radius=1)

    # Ensure a clean 1px white border at the very edges
    frame[0, :]  = 1
    frame[-1, :] = 1
    frame[:, 0]  = 1
    frame[:, -1] = 1
    return frame

# -----------------------------
# Animation loop
# -----------------------------
def run():
    rng = random.Random()
    active = []
    trail_ttl = np.zeros((HEIGHT, WIDTH), dtype=np.int16)

    # schedule corner bursts
    corner_map = {f: c for (f, c) in corner_schedule}
    spawn_cooldown = 0

    for f in range(FIREWORKS_FRAMES):
        # decay trails
        trail_ttl[trail_ttl > 0] -= 1

        # deterministic corner bursts
        if f in corner_map:
            active.append(spawn_burst(corner_map[f], base_angle=rng.uniform(0, math.pi)))
            spawn_cooldown = SPAWN_COOLDOWN_FRAMES

        # random center-ish spawns with cooldown
        if spawn_cooldown > 0:
            spawn_cooldown -= 1
        elif len(active) < MAX_CONCURRENT_BURSTS and rng.random() < SPAWN_PROB_PER_FRAME:
            cxr = rng.randint(8, 19)
            cyr = rng.randint(8, 19)
            active.append(spawn_burst((cxr, cyr), base_angle=rng.uniform(0, math.pi)))
            spawn_cooldown = SPAWN_COOLDOWN_FRAMES

        # draw and age bursts
        next_active = []
        for b in active:
            draw_burst(trail_ttl, b)
            b["age"] += 1
            if b["age"] < BURST_LIFETIME_FRAMES:
                next_active.append(b)
        active = next_active

        # compose & send
        frame = compose_frame_from_trails(trail_ttl)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # --- End card: HEART (same silhouette as anim_confetti) ---
    final = mask_heart()
    for _ in range(END_HEART_HOLD_FRAMES):
        panels = pack_flipbytes(final)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass
