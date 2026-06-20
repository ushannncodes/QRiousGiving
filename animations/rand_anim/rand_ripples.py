# anim_ripple_to_sunflower.py
# 28x28 ripple (pick one preset) → slow outline+fill reveal of a SUNFLOWER PNG → hold
# Conventions: 0 = black (dot ON), 1 = white (dot OFF)

import os, time, math, random
import numpy as np
from PIL import Image
import serial

# -----------------------------
# Serial / Panel settings
# -----------------------------
SERIAL_PORT  = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE    = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS  = [1, 2, 3, 4]  # 4 stacked 7x28 panels = 28x28
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "50"))
FRAME_DT      = 1.0 / max(FPS, 1.0)

# -----------------------------
# Ripple show controls
# -----------------------------
DURATION_SEC   = float(os.getenv("DURATION_SEC", "7.0"))  # total ripple time before reveal
RIPPLE_PRESET  = os.getenv("RIPPLE_PRESET", "").strip()   # Calm Lake | Skim | Deep Pulse (pick 1 at start)  :contentReference[oaicite:2]{index=2}
RANDOM_SEED    = os.getenv("RANDOM_SEED", "").strip()
CENTER_OFF_X   = float(os.getenv("CENTER_OFF_X", "0.0"))
CENTER_OFF_Y   = float(os.getenv("CENTER_OFF_Y", "0.0"))

# Ripple presets (kept from our previous version)  :contentReference[oaicite:3]{index=3}
PRESETS = {
    "calm lake":  {"speed":6.5, "wavelength":6.0, "thickness":1.3, "fade_sec":3.0, "center_pulse":1.5, "pulse_extra":0.8, "pulse_shape":"cos"},
    "skim":       {"speed":8.5, "wavelength":4.5, "thickness":0.9, "fade_sec":2.5, "center_pulse":1.3, "pulse_extra":0.6, "pulse_shape":"sin"},
    "deep pulse": {"speed":5.0, "wavelength":7.0, "thickness":1.5, "fade_sec":3.5, "center_pulse":1.7, "pulse_extra":1.1, "pulse_shape":"cos"},
}

# Determinism
if RANDOM_SEED:
    try: random.seed(int(RANDOM_SEED))
    except: random.seed(RANDOM_SEED)
else:
    random.seed()

# Choose one preset at start
if RIPPLE_PRESET:
    key = RIPPLE_PRESET.lower()
    if key not in PRESETS:
        print(f"[WARN] Unknown RIPPLE_PRESET='{RIPPLE_PRESET}', defaulting to Calm Lake")
        key = "calm lake"
else:
    key = random.choice(list(PRESETS.keys()))
cfg = PRESETS[key]

# -----------------------------
# Sunflower PNG → mask controls
# -----------------------------
IMAGE_PATH            = os.getenv("IMAGE_PATH", "sunflower.png")  # 28x28 PNG (or will be resized)
THRESH                = int(os.getenv("THRESH", "128"))           # 0..255
SUNFLOWER_INVERT      = os.getenv("SUNFLOWER_INVERT", "1") == "1" # flip foreground/background if needed
REVEAL_OUTLINE_FIRST  = os.getenv("REVEAL_OUTLINE_FIRST", "0") == "1"
OUTLINE_STEPS         = int(os.getenv("OUTLINE_STEPS", str(int(0.4*FPS))))   # ~0.4s default
# FILL_STEPS            = int(os.getenv("FILL_STEPS",    str(int(0.8*FPS))))   # ~0.8s default
FILL_STEPS            = int(os.getenv("FILL_STEPS",    100))   # ~0.8s default

HOLD_FRAMES           = int(os.getenv("HOLD_FRAMES",   str(int(1.2*FPS))))   # ~1.2s default
NUDGE_X               = float(os.getenv("SUNFLOWER_CENTER_OFF_X", "0.0"))
NUDGE_Y               = float(os.getenv("SUNFLOWER_CENTER_OFF_Y", "0.0"))

# -----------------------------
# Geometry precompute
# -----------------------------
CX = (WIDTH  - 1) / 2.0
CY = (HEIGHT - 1) / 2.0
yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]

def distance_map_for_center(cx, cy):
    return np.sqrt((xx - cx)**2 + (yy - cy)**2)

DIST_CENTER = distance_map_for_center(CX, CY)
MAX_RADIUS  = float(np.sqrt(CX**2 + CY**2))

# -----------------------------
# Helpers (pack/send)
# -----------------------------
def pack_flipbytes(frame28):
    """
    Pack 28x28 {0,1} into 4 panel payloads, column-major, 7 rows/byte.
    0 = black (ON), 1 = white (OFF)
    """
    panels = []
    for p in range(4):
        y0 = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for dy in range(7):
                bit = int(frame28[y0 + dy, x]) & 1
                byte |= (bit << dy)
            data.append(byte)
        panels.append(data)
    return panels

def send_frame(frame):
    panels = pack_flipbytes(frame.astype(np.uint8))
    for addr, data in zip(PANEL_ADDRS, panels):
        pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(pkt)
    ser.flush()

# -----------------------------
# Ripple renderer  (adapted from your prior script)  :contentReference[oaicite:4]{index=4}
# -----------------------------
def center_pulse_radius(t_norm: float, base: float, extra: float, shape: str) -> float:
    shape = (shape or "cos").lower()
    if shape == "cos":
        osc = 0.5 * (1.0 + math.cos(2.0 * math.pi * t_norm))
    else:
        osc = 0.5 * (1.0 + math.sin(2.0 * math.pi * (t_norm - 0.25)))
    return base + extra * (osc**2)

def render_ripple_frame(t: float) -> np.ndarray:
    t_norm = max(0.0, min(1.0, t / max(DURATION_SEC, 1e-6)))

    v   = float(cfg["speed"])
    wl  = max(float(cfg["wavelength"]), 1e-6)
    th  = float(cfg["thickness"])
    fad = max(float(cfg["fade_sec"]), 0.0)

    # Global end fade clamp (shrinks visible radius over fade_sec)
    if fad > 0.0 and t >= (DURATION_SEC - fad):
        f = (DURATION_SEC - t) / fad
    else:
        f = 1.0
    r_clip = f * MAX_RADIUS

    cx = CX + CENTER_OFF_X
    cy = CY + CENTER_OFF_Y
    dist = distance_map_for_center(cx, cy)

    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    if r_clip > 0.25:
        saw = (dist - v * t) % wl
        ring_mask = (saw < th) | (saw > (wl - th))
        if f < 1.0:
            ring_mask &= (DIST_CENTER <= (r_clip + th))
        frame[ring_mask] = 0

    r_pulse = center_pulse_radius(t_norm, float(cfg["center_pulse"]),
                                 float(cfg["pulse_extra"]), cfg["pulse_shape"])
    frame[DIST_CENTER <= r_pulse] = 0

    return frame

# -----------------------------
# Sunflower: PNG → mask (0=black ON, 1=white OFF)
# inspired by your PIL-based sender  :contentReference[oaicite:5]{index=5}
# -----------------------------
def load_sunflower_mask(path: str, thr: int, invert: bool, nudge_x: float, nudge_y: float) -> np.ndarray:
    img = Image.open(path).convert("L").resize((WIDTH, HEIGHT), resample=Image.NEAREST)
    arr = np.array(img, dtype=np.uint8)
    if invert:
        arr = 255 - arr
    # Foreground = arr >= thr  → dot ON (black=0)
    mask = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    mask[arr >= thr] = 0

    # Optional subpixel nudge by circular shift (nearest)
    nx = int(round(nudge_x))
    ny = int(round(nudge_y))
    if nx != 0:
        mask = np.roll(mask, shift=nx, axis=1)
    if ny != 0:
        mask = np.roll(mask, shift=ny, axis=0)

    # keep 1px white border clean
    mask[0,:] = 1; mask[-1,:] = 1; mask[:,0] = 1; mask[:,-1] = 1
    return mask

def outline_from_fill(fill_mask: np.ndarray) -> np.ndarray:
    out = np.ones_like(fill_mask, dtype=np.uint8)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if fill_mask[y, x] != 0:
                continue
            edge = False
            for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                ny, nx = y+dy, x+dx
                if ny < 0 or ny >= HEIGHT or nx < 0 or nx >= WIDTH or fill_mask[ny, nx] == 1:
                    edge = True; break
            if edge:
                out[y, x] = 0
    return out

def reveal_sunflower(base_frame: np.ndarray, fill_mask: np.ndarray):
    """
    Phase A: outline sketch in randomized batches
    Phase B: fill bloom from center-of-mass outward
    Phase C: photo hold
    """
    frame = base_frame.copy()

    # A) Outline sketch
    if REVEAL_OUTLINE_FIRST:
        outline = outline_from_fill(fill_mask)
        pts = np.column_stack(np.where(outline == 0))  # (y,x)
        if len(pts) > 0:
            np.random.shuffle(pts)
            steps = max(8, OUTLINE_STEPS)
            chunks = np.array_split(pts, steps)
            for chunk in chunks:
                for (y, x) in chunk:
                    frame[y, x] = 0
                send_frame(frame)
                time.sleep(FRAME_DT)

    # B) Fill bloom — center of mass outward
    ys, xs = np.where(fill_mask == 0)
    if len(xs) == 0:
        # If mask empty, just hold base and bail
        for _ in range(HOLD_FRAMES):
            send_frame(base_frame)
            time.sleep(FRAME_DT)
        return

    cx = float(xs.mean())
    cy = float(ys.mean())
    dist = np.sqrt((xx - cx)**2 + (yy - cy)**2)
    # Only flower pixels get a finite distance; others huge
    L = np.where(fill_mask == 0, dist, 1e9)
    vals = np.unique(L[L < 1e9])

    steps = max(10, FILL_STEPS)
    if len(vals) > steps:
        idx = np.linspace(0, len(vals)-1, steps).astype(int)
        ths = vals[idx]
    else:
        ths = vals

    for th in ths:
        mask = (fill_mask == 0) & (L <= th)
        frame2 = base_frame.copy()
        frame2[mask] = 0
        send_frame(frame2)
        time.sleep(FRAME_DT)

    # C) Hold full sunflower
    for _ in range(HOLD_FRAMES):
        send_frame(fill_mask)
        time.sleep(FRAME_DT)

# -----------------------------
# Main
# -----------------------------
def main():
    print(f"[RIPPLE→SUNFLOWER] preset='{key}'  duration={DURATION_SEC}s  fps={FPS}")
    # Preload sunflower
    try:
        sunflower = load_sunflower_mask(IMAGE_PATH, thr=THRESH, invert=SUNFLOWER_INVERT,
                                        nudge_x=NUDGE_X, nudge_y=NUDGE_Y)
    except Exception as e:
        print(f"[ERROR] Failed to load sunflower PNG '{IMAGE_PATH}': {e}")
        sunflower = np.ones((HEIGHT, WIDTH), dtype=np.uint8)  # fallback: blank

    t0 = time.time()
    last_frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    try:
        # Ripple loop (softly fades near the end by design)
        while True:
            now = time.time()
            t = now - t0
            if t >= DURATION_SEC:
                break
            frame = render_ripple_frame(t)
            send_frame(frame)
            last_frame = frame

            to_sleep = FRAME_DT - (time.time() - now)
            if to_sleep > 0: time.sleep(to_sleep)

        # Reveal sunflower over the last ripple frame
        reveal_sunflower(base_frame=last_frame, fill_mask=sunflower)

    except KeyboardInterrupt:
        print("\n[ANIM] interrupted.")
    finally:
        try: ser.close()
        except Exception: pass

if __name__ == "__main__":
    main()
