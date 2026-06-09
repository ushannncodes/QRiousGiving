# anim_fireworks_heart_coalesce.py
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
FPS           = float(os.getenv("FPS", "160"))
FRAME_DT      = 1.0 / FPS

# Phase A: Fireworks base length (randomized per run ±0.4s)
FIREWORKS_S   = float(os.getenv("FIREWORKS_S", "2"))

# -----------------------------
# Phase A: Firework (random) tuning
# -----------------------------
NUM_RAYS                = int(os.getenv("NUM_RAYS", "12"))
RAY_JITTER              = float(os.getenv("RAY_JITTER", "0.16"))
RADIAL_SPEED_PX_PER_F   = float(os.getenv("RADIAL_SPEED", "0.95"))
SPAWN_PROB_PER_FRAME    = float(os.getenv("SPAWN_P", "0.12"))
MAX_CONCURRENT_BURSTS   = int(os.getenv("MAX_BURSTS", "3"))
BURST_LIFETIME_FRAMES   = int(os.getenv("BURST_LIFE", "16"))
TRAIL_TTL_FRAMES        = int(os.getenv("TRAIL_TTL", "4"))
SPAWN_COOLDOWN_FRAMES   = int(os.getenv("SPAWN_COOLDOWN", "6"))

# Fixed four corners for guaranteed variety
CORNER_CENTERS     = [(6,6), (21,6), (6,21), (21,21)]
CORNER_GAP_FRAMES  = int(os.getenv("CORNER_GAP", "12"))

# -----------------------------
# Phase B: Coalescence tuning
# -----------------------------
COALESCE_TARGETS        = int(os.getenv("COALESCE_TARGETS", "90"))   # target heart pixels
COALESCE_SPEED          = float(os.getenv("COALESCE_SPEED", "1.10")) # px per frame
COALESCE_SOURCES        = int(os.getenv("COALESCE_SOURCES", "6"))    # how many launch points
COALESCE_TRAIL_TTL      = int(os.getenv("COALESCE_TRAIL_TTL", "5"))
COALESCE_HOLD_FRAMES    = int(os.getenv("COALESCE_HOLD_FRAMES", "100"))

# -----------------------------
# Packing & send
# 0 = black (ON), 1 = white (OFF)
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
# Randomization helpers
# -----------------------------
def _maybe_seed(rng: random.Random):
    s = os.getenv("RANDOM_SEED")
    if s is not None:
        try:
            rng.seed(int(s))
        except:
            rng.seed(s)
    else:
        try:
            rng.seed(int(time.time() * 1000) ^ int.from_bytes(os.urandom(8), "little"))
        except:
            rng.seed(int(time.time() * 1000))

def _jitter_point(pt, rng, r=3):
    x, y = pt
    return (max(0, min(WIDTH-1, x + rng.randint(-r, r))),
            max(0, min(HEIGHT-1, y + rng.randint(-r, r))))

# -----------------------------
# Heart mask (filled)
# -----------------------------
def _erode_black(mask, radius=1):
    """
    mask: np.uint8 28x28, 0=black (heart), 1=white (bg)
    Erode black region by 'radius' (4-neighbourhood).
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
    Implicit heart; shifted down by 1px and lightly eroded for cleaner border.
    0 = black (ON), 1 = white (OFF)
    """
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

    # optional 1px erosion to thin border a touch (comment out if you prefer fuller heart)
    frame = _erode_black(frame, radius=1)

    # ensure clean white border at the very edges
    frame[0, :]  = 1
    frame[-1, :] = 1
    frame[:, 0]  = 1
    frame[:, -1] = 1
    return frame

def heart_black_coords(mask):
    ys, xs = np.where(mask == 0)
    return list(zip(xs.tolist(), ys.tolist()))

# -----------------------------
# Phase A: Random fireworks primitives
# -----------------------------
def spawn_burst(center, base_angle, num_rays, ray_jitter):
    rays = []
    step = 2.0 * math.pi / max(1, num_rays)
    for i in range(num_rays):
        a = base_angle + i * step + random.uniform(-ray_jitter, ray_jitter)
        rays.append(a)
    return {"center": center, "angles": rays, "age": 0}

def draw_burst(trail_ttl, burst, radial_speed_px_per_f, trail_ttl_frames):
    cx, cy = burst["center"]
    radius = burst["age"] * radial_speed_px_per_f

    for a in burst["angles"]:
        # tip
        x = cx + radius * math.cos(a)
        y = cy + radius * math.sin(a)
        xi = max(0, min(WIDTH - 1, int(x)))
        yi = max(0, min(HEIGHT - 1, int(y)))

        # trail (a couple steps back)
        for back in (0.0, 0.6, 1.2):
            xr = cx + max(radius - back, 0.0) * math.cos(a)
            yr = cy + max(radius - back, 0.0) * math.sin(a)
            xb = max(0, min(WIDTH - 1, int(xr)))
            yb = max(0, min(HEIGHT - 1, int(yr)))
            trail_ttl[yb, xb] = max(trail_ttl[yb, xb], trail_ttl_frames)

        trail_ttl[yi, xi] = max(trail_ttl[yi, xi], trail_ttl_frames)

def compose_frame_from_trails(trail_ttl, frozen=None):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    frame[trail_ttl > 0] = 0
    if frozen is not None:
        frame[frozen == 0] = 0
    return frame

# -----------------------------
# Phase B: Guided/coalescing bursts
# -----------------------------
def pick_sources(rng, n):
    # prefer corners + mid-edges for variety
    candidates = [(0,0), (WIDTH-1,0), (0,HEIGHT-1), (WIDTH-1,HEIGHT-1),
                  (WIDTH//2,0), (WIDTH//2,HEIGHT-1), (0,HEIGHT//2), (WIDTH-1,HEIGHT//2)]
    rng.shuffle(candidates)
    return candidates[:max(1, n)]

def assign_targets_to_sources(targets, sources):
    # round-robin distribute targets across sources
    buckets = [[] for _ in range(len(sources))]
    for i, tgt in enumerate(targets):
        buckets[i % len(sources)].append(tgt)
    return buckets

def coalesce_phase(rng, trail_ttl, heart_mask):
    # 1) choose target pixels inside the heart (spread out)
    blacks = heart_black_coords(heart_mask)
    rng.shuffle(blacks)

    # lightly subsample with spacing so we don’t over-crowd rays
    chosen = []
    min_gap = 2  # pixels
    for x, y in blacks:
        ok = True
        for (sx, sy) in chosen:
            if abs(sx - x) + abs(sy - y) < min_gap:
                ok = False
                break
        if ok:
            chosen.append((x, y))
        if len(chosen) >= COALESCE_TARGETS:
            break
    if not chosen:
        chosen = blacks[: min(COALESCE_TARGETS, len(blacks))]

    # 2) choose source points
    sources = pick_sources(rng, COALESCE_SOURCES)
    buckets = assign_targets_to_sources(chosen, sources)

    # 3) build “guided rays”: each is a point moving toward its assigned target
    guided = []  # list of dicts {src:(x,y), tgt:(x,y), age:0, max_age:int}
    for s, bucket in zip(sources, buckets):
        sx, sy = s
        for (tx, ty) in bucket:
            dist = math.hypot(tx - sx, ty - sy)
            max_age = max(1, int(math.ceil(dist / max(COALESCE_SPEED, 1e-6))))
            guided.append({"src": (sx, sy), "tgt": (tx, ty), "age": 0, "max_age": max_age})

    # frozen pixels: once a ray reaches its target, keep it ON (black)
    frozen = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    # 4) simulate frames until all guided rays reach their targets
    active = guided
    while active:
        # decay trails
        trail_ttl[trail_ttl > 0] -= 1

        next_active = []
        for g in active:
            sx, sy = g["src"]
            tx, ty = g["tgt"]
            t = min(1.0, g["age"] / max(1, g["max_age"]))
            # position along the line
            x = sx + (tx - sx) * t
            y = sy + (ty - sy) * t
            xi = max(0, min(WIDTH - 1, int(round(x))))
            yi = max(0, min(HEIGHT - 1, int(round(y))))

            # draw a short trail behind
            steps_back = 3
            for k in range(steps_back):
                tb = max(0.0, t - 0.04 * k)
                xb = sx + (tx - sx) * tb
                yb = sy + (ty - sy) * tb
                xbi = max(0, min(WIDTH - 1, int(round(xb))))
                ybi = max(0, min(HEIGHT - 1, int(round(yb))))
                trail_ttl[ybi, xbi] = max(trail_ttl[ybi, xbi], COALESCE_TRAIL_TTL)

            # tip brighter/longer
            trail_ttl[yi, xi] = max(trail_ttl[yi, xi], COALESCE_TRAIL_TTL + 2)

            g["age"] += 1
            if g["age"] >= g["max_age"]:
                # lock target pixel as frozen heart
                frozen[ty, tx] = 0
            else:
                next_active.append(g)

        active = next_active

        # compose and send
        frame = compose_frame_from_trails(trail_ttl, frozen=frozen)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # After all locked, gently settle remaining trails
    for _ in range(int(0.15 * FPS)):
        trail_ttl[trail_ttl > 0] -= 1
        frame = compose_frame_from_trails(trail_ttl, frozen=frozen)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # Merge with full heart to fill any tiny gaps (keeps your preferred silhouette)
    final_heart = heart_mask.copy()
    final_heart[final_heart == 0] = 0  # already black
    return final_heart

# -----------------------------
# Animation loop
# -----------------------------
def run():
    rng = random.Random()
    _maybe_seed(rng)

    # ---- Per-run randomized knobs (± ranges tuned for 28x28) ----
    num_rays_cur       = max(8, int(NUM_RAYS + rng.randint(-2, 2)))
    ray_jitter_cur     = max(0.08, RAY_JITTER * (1.0 + rng.uniform(-0.25, 0.25)))
    spawn_p_cur        = max(0.04, SPAWN_PROB_PER_FRAME * (1.0 + rng.uniform(-0.35, 0.35)))
    max_bursts_cur     = max(1, MAX_CONCURRENT_BURSTS + rng.randint(-1, 1))
    burst_life_cur     = max(10, BURST_LIFETIME_FRAMES + rng.randint(-4, 3))
    trail_ttl_cur      = max(2, TRAIL_TTL_FRAMES + rng.randint(-1, 1))
    radial_speed_cur   = max(0.7, RADIAL_SPEED_PX_PER_F * (1.0 + rng.uniform(-0.15, 0.20)))

    # fireworks section length ±0.4s (min 2.0s)
    fw_frames_cur      = max(int((FIREWORKS_S + rng.uniform(-0.4, 0.4)) * FPS), int(2.0 * FPS))

    # jitter corner centers so they’re not identical every run
    corners_jit  = [_jitter_point(pt, rng, r=3) for pt in CORNER_CENTERS]

    # schedule corner bursts
    corner_map = {}
    t = 0
    for c in corners_jit:
        corner_map[t] = c
        t += CORNER_GAP_FRAMES

    # ---- Phase A: random fireworks
    active = []
    trail_ttl = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
    spawn_cooldown = 0

    for f in range(fw_frames_cur):
        trail_ttl[trail_ttl > 0] -= 1

        # deterministic corner bursts
        if f in corner_map:
            active.append(
                spawn_burst(
                    corner_map[f],
                    base_angle=rng.uniform(0, math.pi),
                    num_rays=num_rays_cur,
                    ray_jitter=ray_jitter_cur
                )
            )
            spawn_cooldown = SPAWN_COOLDOWN_FRAMES

        # random center spawns with cooldown
        if spawn_cooldown > 0:
            spawn_cooldown -= 1
        elif len(active) < max_bursts_cur and rng.random() < spawn_p_cur:
            cxr = rng.randint(6, 21)
            cyr = rng.randint(6, 21)
            active.append(
                spawn_burst(
                    (cxr, cyr),
                    base_angle=rng.uniform(0, math.pi),
                    num_rays=num_rays_cur,
                    ray_jitter=ray_jitter_cur
                )
            )
            spawn_cooldown = SPAWN_COOLDOWN_FRAMES

        # draw + age bursts
        next_active = []
        for b in active:
            draw_burst(trail_ttl, b, radial_speed_cur, trail_ttl_cur)
            b["age"] += 1
            if b["age"] < burst_life_cur:
                next_active.append(b)
        active = next_active

        # compose & send
        frame = compose_frame_from_trails(trail_ttl)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # brief settle
    for _ in range(int(0.10 * FPS)):
        trail_ttl[trail_ttl > 0] -= 1
        frame = compose_frame_from_trails(trail_ttl)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # ---- Phase B: coalesce into heart
    heart = mask_heart()
    final_heart = coalesce_phase(rng, trail_ttl, heart)

    # ---- Phase C: hold the finished heart
    for _ in range(COALESCE_HOLD_FRAMES):
        panels = pack_flipbytes(final_heart)
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
