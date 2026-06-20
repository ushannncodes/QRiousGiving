# rand_confetti.py — top-rain → bounce/pile → silky morph into HEART/HI
import os, time, math, random
import numpy as np
import serial

# -----------------------------
# Serial / Panel settings
# -----------------------------
SERIAL_PORT  = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE    = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS  = [1, 2, 3, 4]   # 4 stacked 7x28 panels = 28x28
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "50"))

# Phases (seconds)
RAIN_SEC      = float(os.getenv("RAIN_SEC", "4.0"))   # spawn, fall, bounce, pile
SETTLE_SEC    = float(os.getenv("SETTLE_SEC", "0.3")) # brief calm after rain
REVEAL_SEC    = float(os.getenv("REVEAL_SEC", "5")) # morph duration
HOLD_SEC      = float(os.getenv("HOLD_SEC", "1.2"))   # hold the final shape

# Physics (means; jittered per run)
SPAWN_RATE_MEAN = float(os.getenv("SPAWN_RATE", "80"))   # particles/sec
GRAVITY_MEAN    = float(os.getenv("GRAVITY", "18.0"))    # px/s^2
BOUNCE_MEAN     = float(os.getenv("BOUNCE_DAMP", "0.45"))
JITTER_X_MEAN   = float(os.getenv("JITTER_X", "0.15"))   # horizontal drift accel
FRICTION_MEAN   = float(os.getenv("FRICTION_X", "0.9"))
MAX_PARTS_MEAN  = int(os.getenv("MAX_PARTS", "500"))
STACK_MARGIN    = int(os.getenv("STACK_MARGIN", "0"))

# Finale controls
TARGET       = os.getenv("TARGET", "HEART").upper()       # "HI" or "HEART"
FILL_STYLE   = os.getenv("FILL_STYLE", "RANDOM").upper()  # "RANDOM" | "CENTER_OUT"

# Randomization controls
VARIANCE        = float(os.getenv("VARIANCE", "1.0"))    # 0=calm, 1=default, >1 = wild
SEED_ENV        = os.getenv("SEED", "").strip()          # set to reproduce a run
SPAWN_VARIANT   = os.getenv("SPAWN_VARIANT", "").upper() # force a variant name

# Optional tunables
WIND_MAXV       = float(os.getenv("WIND_MAXV", "0.60"))
LAND_NOISE      = float(os.getenv("LAND_NOISE", "0.25"))

# Morph smoothness knobs
RELEASE_LOCAL = float(os.getenv("RELEASE_LOCAL", "0.9"))  # erase origin after this local progress
RELEASE_DIST  = float(os.getenv("RELEASE_DIST",  "1.0"))   # ...and moved ≥ this many pixels

REVEAL_WINDOW = float(os.getenv("REVEAL_WINDOW", "0.75"))  # fraction of phase during which pixels *start*
PIXEL_MIN_DUR = float(os.getenv("PIXEL_MIN_DUR", "0.20"))  # per-pixel travel min (as fraction of REVEAL)
PIXEL_MAX_DUR = float(os.getenv("PIXEL_MAX_DUR", "0.45"))  # per-pixel travel max

# -----------------------------
# Utils: packing and send (0 = black/ON, 1 = white/OFF)
# -----------------------------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        r0 = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for dy in range(7):
                bit = int(frame28[r0 + dy, x]) & 1
                byte |= (bit << dy)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

# -----------------------------
# Target masks (1=white bg, 0=black)
# -----------------------------
def _erode_black(mask, radius=1):
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
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx = (WIDTH - 1) / 2.0
    cy = (HEIGHT - 1) / 2.0 + 1.0   # +1px DOWN
    span = 1.25
    for y in range(HEIGHT):
        py = ((cy - y) / (HEIGHT * 0.5)) * span  # +y up
        for x in range(WIDTH):
            px = ((x - cx) / (WIDTH * 0.5)) * span
            v = (px*px + py*py - 1.0)
            f = v*v*v - (px*px)*(py*py*py)
            if f <= 0.0:
                frame[y, x] = 0
    frame = _erode_black(frame, radius=1)
    frame[0, :] = 1; frame[-1, :] = 1; frame[:, 0] = 1; frame[:, -1] = 1
    return frame

def mask_HI():
    m = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    def rect(x0,y0,w,h):
        x1,y1 = x0+w, y0+h
        x0=max(0,x0); y0=max(0,y0); x1=min(WIDTH,x1); y1=min(HEIGHT,y1)
        if x1>x0 and y1>y0: m[y0:y1, x0:x1] = 0
    gw, gh = 22, 18
    x_off = (WIDTH - gw)//2
    y_off = (HEIGHT - gh)//2
    # H
    rect(x_off + 0,  y_off + 0,   3, gh)
    rect(x_off + 9,  y_off + 0,   3, gh)
    rect(x_off + 0,  y_off + gh//2 - 2, 12, 4)
    # I
    rect(x_off + 15, y_off + 0,   3, gh)
    # !
    rect(x_off + 20, y_off + 0,   2, gh-4)
    rect(x_off + 20, y_off + gh-3, 2, 3)
    return m

def target_mask():
    return mask_HI() if TARGET == "HI" else mask_heart()

# -----------------------------
# Particle system
# -----------------------------
class Part:
    __slots__ = ("x","y","vx","vy","bd","fx")
    def __init__(self, x, y, vx, vy, bounce_damp, friction_x):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.bd, self.fx = bounce_damp, friction_x

def compute_heights(occupancy):
    # Return the y of the TOP of the pile (first 0 from the TOP).
    h = [HEIGHT for _ in range(WIDTH)]
    for x in range(WIDTH):
        for y in range(0, HEIGHT):           # TOP → BOTTOM
            if occupancy[y, x] == 0:
                h[x] = y
                break
    return h

def clamp(v, lo, hi): return lo if v < lo else hi if v > hi else v

# Low-freq wind via random walk
class Wind:
    def __init__(self, rng, accel=0.04, decay=0.96, maxv=0.45):
        self.v = 0.0
        self.rng = rng
        self.accel = accel
        self.decay = decay
        self.maxv = maxv
    def step(self, dt):
        if self.rng.random() < 0.02:
            goal = self.rng.uniform(-self.maxv, self.maxv)
            self.v += (goal - self.v) * 0.08
        self.v = clamp(self.v * self.decay + self.rng.uniform(-self.accel, self.accel), -self.maxv, self.maxv)
        return self.v

# -----------------------------
# Main
# -----------------------------
def main():
    # Seed
    if SEED_ENV:
        seed = int(SEED_ENV, 0) if all(c in "0123456789xXabcdefABCDEF" for c in SEED_ENV) else hash(SEED_ENV)
    else:
        seed = (int.from_bytes(os.urandom(8), "little") ^ int(time.time_ns()) ^ os.getpid())
    rng = random.Random(seed)

    dt = 1.0 / FPS
    rain_frames   = int(RAIN_SEC   * FPS)
    settle_frames = int(SETTLE_SEC * FPS)
    reveal_frames = int(REVEAL_SEC * FPS)
    hold_frames   = int(HOLD_SEC   * FPS)

    # Per-run jitter around means
    def jitter(val, pct):
        spread = pct * VARIANCE
        lo = val * (1.0 - spread)
        hi = val * (1.0 + spread)
        return rng.uniform(lo, hi)

    SPAWN_RATE   = max(5.0,  jitter(SPAWN_RATE_MEAN, 0.35))
    GRAVITY      = max(6.0,  jitter(GRAVITY_MEAN,    0.25))
    BOUNCE_BASE  = clamp(jitter(BOUNCE_MEAN,         0.30), 0.20, 0.80)
    FRICTION_BASE= clamp(jitter(FRICTION_MEAN,       0.20), 0.60, 0.95)
    JITTER_X     = max(0.02, jitter(JITTER_X_MEAN,   0.60))
    MAX_PARTS    = int(max(80,  jitter(MAX_PARTS_MEAN, 0.25)))

    # --- Only top-rain, with multiple variants ---
    variants = ["UNIFORM", "TWO_BANDS", "CENTER_BURST", "WAVY", "SHEETS", "PULSES"]
    SPAWN_STYLE = SPAWN_VARIANT if SPAWN_VARIANT in variants else rng.choice(variants)

    wind = Wind(rng, accel=0.05*VARIANCE, decay=0.97, maxv=WIND_MAXV*VARIANCE)

    occupancy = np.ones((HEIGHT, WIDTH), dtype=np.uint8)  # 1=white bg, 0=dot
    parts = []
    spawn_accum = 0.0

    tgt = target_mask()
    tgt_coords = [(x,y) for y in range(HEIGHT) for x in range(WIDTH) if tgt[y,x]==0]

    # FILL order
    if FILL_STYLE == "CENTER_OUT":
        cx = (WIDTH - 1) / 2.0
        cy = (HEIGHT - 1) / 2.0 + 1.0
        tgt_coords.sort(key=lambda p: (p[0]-cx)**2 + (p[1]-cy)**2)
    else:
        rng.shuffle(tgt_coords)

    print(f"[CONFETTI] seed={seed} variant={SPAWN_STYLE} rate={SPAWN_RATE:.1f} "
          f"grav={GRAVITY:.1f} bounce~{BOUNCE_BASE:.2f} fric~{FRICTION_BASE:.2f} "
          f"jitterX={JITTER_X:.3f} max={MAX_PARTS} style={FILL_STYLE}")

    # Helpers to sample spawn x from different top-rain patterns
    t0 = rng.uniform(0, 1000)  # phase offset for WAVY/PULSES
    def spawn_x_uniform():        return rng.uniform(0, WIDTH-1)
    def spawn_x_two_bands():
        band_w = rng.uniform(6, 10)
        left_band = rng.choice([True, False])
        return rng.uniform(0, band_w) if left_band else rng.uniform(WIDTH - band_w, WIDTH-1)
    def spawn_x_center_burst():
        center = (WIDTH-1)/2.0 + rng.uniform(-2.0, 2.0)
        spread = rng.uniform(3.0, 8.0)
        return clamp(rng.gauss(center, spread), 0.0, WIDTH-1.0)
    def spawn_x_wavy(frame_idx):
        w = 2*math.pi / rng.uniform(14.0, 24.0)
        amp = rng.uniform(10.0, 13.5)
        mid = (WIDTH-1)/2.0
        return clamp(mid + amp * math.sin(w*(frame_idx) + t0) + rng.uniform(-1.5, 1.5), 0.0, WIDTH-1.0)
    def spawn_x_sheets():
        sheet_cols = rng.sample(range(WIDTH), k=rng.randint(2, 5))
        return float(rng.choice(sheet_cols)) + rng.uniform(-0.4, 0.4)
    def spawn_x_pulses(frame_idx):
        pulse_len = rng.randint(3, 6)
        pulse_idx = (frame_idx // pulse_len)
        base = (pulse_idx * rng.uniform(6.0, 10.0) + t0) % (WIDTH-1)
        return clamp(base + rng.uniform(-2.5, 2.5), 0.0, WIDTH-1.0)

    def pick_spawn_x(frame_idx):
        if SPAWN_STYLE == "UNIFORM":       return spawn_x_uniform()
        if SPAWN_STYLE == "TWO_BANDS":     return spawn_x_two_bands()
        if SPAWN_STYLE == "CENTER_BURST":  return spawn_x_center_burst()
        if SPAWN_STYLE == "WAVY":          return spawn_x_wavy(frame_idx)
        if SPAWN_STYLE == "SHEETS":        return spawn_x_sheets()
        if SPAWN_STYLE == "PULSES":        return spawn_x_pulses(frame_idx)
        return spawn_x_uniform()

    # ------------- Phase A: Rain → Bounce → Pile -------------
    for f_idx in range(rain_frames):
        # spawn
        spawn_accum += SPAWN_RATE * dt
        to_spawn = int(spawn_accum)
        if to_spawn > 0:
            spawn_accum -= to_spawn
            quota = min(to_spawn, max(0, MAX_PARTS - len(parts)))
            for _ in range(quota):
                x = pick_spawn_x(f_idx); y = -1.0
                vx = rng.uniform(-0.35, 0.35)
                vy = rng.uniform(0.0, 0.6)
                bd = clamp(BOUNCE_BASE * rng.uniform(0.85, 1.15), 0.15, 0.90)
                fx = clamp(FRICTION_BASE * rng.uniform(0.85, 1.15), 0.50, 0.98)
                parts.append(Part(x,y,vx,vy,bd,fx))

        # clear dynamic layer (keep pile as 0s)
        frame = occupancy.copy()

        # wind + column heights
        wvx = wind.step(dt)
        heights = compute_heights(occupancy)

        # integrate
        for p in parts:
            p.vy += GRAVITY * dt
            p.vx += (rng.uniform(-JITTER_X, JITTER_X) + wvx) * dt

            nx = p.x + p.vx
            ny = p.y + p.vy

            # collide with side walls
            if nx < 0:
                nx = 0; p.vx = -p.vx * 0.5
            elif nx > (WIDTH-1):
                nx = WIDTH-1; p.vx = -p.vx * 0.5

            # collide with the pile/ground
            ground_y = heights[int(clamp(round(nx),0,WIDTH-1))] - 1 - STACK_MARGIN
            if ny >= ground_y:
                if p.vy > 2.0:
                    ny = ground_y
                    p.vy = -p.vy * p.bd
                    p.vx *= p.fx
                    if abs(p.vy) < 0.9:
                        gx, gy = int(round(nx)), int(round(ny))
                        if 0 <= gx < WIDTH and 0 <= gy < HEIGHT:
                            occupancy[gy, gx] = 0
                            p.y = -9999
                            continue
                else:
                    gx, gy = int(round(nx)), int(ground_y)
                    if 0 <= gx < WIDTH and 0 <= gy < HEIGHT:
                        occupancy[gy, gx] = 0
                        p.y = -9999
                        continue

            # keep falling
            p.x, p.y = nx, ny
            ix, iy = int(round(p.x)), int(round(p.y))
            if 0 <= ix < WIDTH and 0 <= iy < HEIGHT:
                frame[iy, ix] = 0

        # cull retired
        parts = [p for p in parts if p.y > -500]

        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(dt)

    # ------------- Phase B: Settle -------------
    for _ in range(settle_frames):
        panels = pack_flipbytes(occupancy)
        send_to_panels(panels)
        time.sleep(dt)

    # ------------- Phase C: Silky morph into target -------------
    # Build source/target pairs
    pile_coords = [(x,y) for y in range(HEIGHT) for x in range(WIDTH) if occupancy[y,x]==0]
    rng.shuffle(pile_coords)

    tgt_coords_ordered = list(tgt_coords)  # already ordered by FILL_STYLE above
    k = min(len(pile_coords), len(tgt_coords_ordered))
    pairs = list(zip(pile_coords[:k], tgt_coords_ordered[:k]))

    # Global timing for reveal
    steps = max(1, reveal_frames)
    dt_u  = 1.0 / max(1.0, steps)      # normalized 0..1

    # Clamp morph parameters
    rw = max(0.1, min(0.95, REVEAL_WINDOW))
    dmin = max(0.05, min(0.9, PIXEL_MIN_DUR))
    dmax = max(dmin, min(0.95, PIXEL_MAX_DUR))

    # Per-pixel schedules (evenly distributed starts + tiny jitter)
    start_u = np.empty(k, dtype=np.float32)
    dur_u   = np.empty(k, dtype=np.float32)
    jitter  = 0.25 / max(1, k)
    for i in range(k):
        base = (i + 0.5) / k           # low-discrepancy spacing in (0,1)
        s = base * rw + rng.uniform(-jitter, jitter)
        s = max(0.0, min(1.0 - dmin, s))
        d = rng.uniform(dmin, dmax)
        if s + d > 1.0:
            d = 1.0 - s
        start_u[i] = s
        dur_u[i]   = max(dmin, d)

    # Easing for motion
    motion_ease_type = rng.choice(["ease_out", "ease_inout", "linear"])
    def ease_motion(x):
        if motion_ease_type == "ease_out":
            return 1 - (1 - x)**3
        if motion_ease_type == "ease_inout":
            return (4*x*x*x) if x < 0.5 else 1 - (-2*x + 2)**3 / 2
        return x

    # Track which sources are "released" (original pile pixel erased)
    released = np.zeros(k, dtype=bool)

    # Render morph frames
    u = 0.0
    for step in range(steps):
        u = (step + 1) * dt_u   # 0→1 over reveal
        # Start from the **pile** so nothing pops away
        frame = occupancy.copy()

        for i, ((sx, sy), (tx, ty)) in enumerate(pairs):
            su = start_u[i]
            du = dur_u[i]
            if u <= su:
                # not started yet; keep pile pixel (already in frame)
                continue

            local = (u - su) / max(1e-6, du)
            if local >= 1.0:
                x, y = tx, ty  # finished
            else:
                w = ease_motion(local)
                x = int(round(sx + (tx - sx) * w))
                y = int(round(sy + (ty - sy) * w))

            # draw the moving/finished pixel
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                frame[y, x] = 0

            # decide when to "un-pin" (erase) the original pile pixel
            if not released[i]:
                dx = (tx - sx) * min(1.0, local)
                dy = (ty - sy) * min(1.0, local)
                dist = (dx*dx + dy*dy) ** 0.5
                if local >= RELEASE_LOCAL and dist >= RELEASE_DIST:
                    released[i] = True

            if released[i]:
                if 0 <= sx < WIDTH and 0 <= sy < HEIGHT:
                    frame[sy, sx] = 1  # erase old pile location

        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(dt)

    # ------------- Phase D: Hold -------------
    final = target_mask()
    for _ in range(hold_frames):
        panels = pack_flipbytes(final)
        send_to_panels(panels)
        time.sleep(dt)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[CONFETTI] bye")
    finally:
        try: ser.close()
        except: pass
