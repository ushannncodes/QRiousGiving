#!/usr/bin/env python3
import numpy as np
import time, math, random, serial, os
from collections import deque

# -----------------------------
# Flipdot / Serial settings
# -----------------------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]  # 4 stacked 7x28 panels = 28x28 (top->bottom)
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "60"))
FRAME_DT      = 1.0 / FPS
DURATION_S    = float(os.getenv("DURATION_S", "10.0"))  # << end after 8s

# -----------------------------
# Heartbeat & Rings (knobs)
# -----------------------------
BPM                 = float(os.getenv("BPM", "66"))
BEATS_PER_EXPLOSION = int(os.getenv("BEATS_PER_EXPLOSION", "4"))

HEART_BASE_SCALE   = float(os.getenv("HEART_BASE_SCALE", "0.78"))
HEART_PULSE_AMP    = float(os.getenv("HEART_PULSE_AMP", "0.2"))
HEART_EASE_IN      = float(os.getenv("HEART_EASE_IN", "2.2"))
HEART_EASE_OUT     = float(os.getenv("HEART_EASE_OUT", "2.0"))
INTRO_GROW_SEC     = float(os.getenv("INTRO_GROW_SEC", "0.85"))
INTRO_START_SCALE  = float(os.getenv("INTRO_START_SCALE", "0.78"))
HEART_CENTER_OFF_X = float(os.getenv("HEART_CENTER_OFF_X", "0.0"))
HEART_CENTER_OFF_Y = float(os.getenv("HEART_CENTER_OFF_Y", "2"))
CLEFT_STRENGTH = float(os.getenv("CLEFT_STRENGTH", "0.38"))
CLEFT_WIDTH    = float(os.getenv("CLEFT_WIDTH", "0.4"))
TIP_SHARPEN    = float(os.getenv("TIP_SHARPEN", "0.2"))
TOP_SQUASH     = float(os.getenv("TOP_SQUASH", "0.2"))

# ripple “explosion”
RING_SPEED          = float(os.getenv("RING_SPEED", "14.0"))
RING_THICKNESS      = float(os.getenv("RING_THICKNESS", "1.2"))
RING_MAX_R          = float(os.getenv("RING_MAX_R", "22.0"))
RING_LIFETIME       = float(os.getenv("RING_LIFETIME", "0.9"))
RING_COUNT_ON_BURST = int(os.getenv("RING_COUNT_ON_BURST", "0"))
RING_STAGGER_SEC    = float(os.getenv("RING_STAGGER_SEC", "0.10"))

# -----------------------------
# Sparks (randomized each run)
# -----------------------------
_SPARK_LIFE_BASE      = float(os.getenv("SPARK_LIFETIME", "5.0"))
SPARK_LIFETIME_MIN    = float(os.getenv("SPARK_LIFETIME_MIN", str(max(0.3, 0.7 * _SPARK_LIFE_BASE))))
SPARK_LIFETIME_MAX    = float(os.getenv("SPARK_LIFETIME_MAX", str(1.3 * _SPARK_LIFE_BASE)))
SPARK_SPEED_MIN       = float(os.getenv("SPARK_SPEED_MIN", "3.0"))
SPARK_SPEED_MAX       = float(os.getenv("SPARK_SPEED_MAX", "7.0"))
SPARK_PARTICLES_MIN   = int(os.getenv("SPARK_PARTICLES_MIN", "24"))
SPARK_PARTICLES_MAX   = int(os.getenv("SPARK_PARTICLES_MAX", "44"))
SPARK_JITTER          = float(os.getenv("SPARK_JITTER", "0.20"))
SPARK_TRAIL_SAMPLES   = int(os.getenv("SPARK_TRAIL_SAMPLES", "1"))
SPARK_TRAIL_STEP      = float(os.getenv("SPARK_TRAIL_STEP", "0.10"))
USE_SPARKS            = int(os.getenv("USE_SPARKS", "1"))
SPARK_START_JITTER    = float(os.getenv("SPARK_START_JITTER", "0.12"))

# Unique randomness per run
_seed = int.from_bytes(os.urandom(8), "big") ^ int(time.time_ns()) ^ os.getpid()
random.seed(_seed)
print(f"[RNG] seed={_seed}")

# -----------------------------
# Wire format
# -----------------------------
def pack_flipbytes(frame28: np.ndarray):
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

# -----------------------------
# Heart math / drawing
# -----------------------------
def _ease_in_out_pow(x, pin=2.0, pout=2.0):
    x = max(0.0, min(1.0, x))
    if x < 0.5:
        return 0.5 * (2*x)**pin
    else:
        return 1.0 - 0.5 * (2*(1-x))**pout

def draw_heart(scale):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx = (WIDTH - 1) / 2.0 + HEART_CENTER_OFF_X
    cy = (HEIGHT - 1) / 2.0 + HEART_CENTER_OFF_Y
    base_span = 1.35
    span = base_span / max(scale, 1e-6)

    for y in range(HEIGHT):
        py = ((cy - y) / (HEIGHT * 0.5)) * span
        for x in range(WIDTH):
            px = ((x - cx) / (WIDTH * 0.5)) * span

            qx, qy = px, py
            if qy >= 0.0:
                qy *= (1.0 + TOP_SQUASH * min(1.0, qy*qy))
                if CLEFT_STRENGTH != 0.0 and CLEFT_WIDTH > 0.0:
                    pinch = CLEFT_STRENGTH * math.exp(-(qx*qx) / (CLEFT_WIDTH*CLEFT_WIDTH))
                    qy += pinch
            else:
                qy *= (1.0 + TIP_SHARPEN * min(1.0, (-qy)))

            a = qx*qx + qy*qy - 1.0
            if (a*a*a - qx*qx*(qy*qy*qy)) <= 0.0:
                frame[y, x] = 0
    return frame

def draw_rings(t_now, rings: deque):
    layer = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx, cy = (WIDTH - 1) / 2.0, (HEIGHT - 1) / 2.0
    for ring in list(rings):
        age = t_now - ring['t0']
        if age < 0: continue
        if age > RING_LIFETIME:
            rings.remove(ring); continue
        r = ring['r0'] + RING_SPEED * age
        if r > RING_MAX_R:
            rings.remove(ring); continue
        rmin, rmax = r - RING_THICKNESS, r + RING_THICKNESS
        for y in range(HEIGHT):
            dy = y - cy
            for x in range(WIDTH):
                dx = x - cx
                if rmin <= math.hypot(dx, dy) <= rmax:
                    layer[y, x] = 0
    return layer

def spawn_sparks(cx, cy):
    sparks = []
    n = random.randint(SPARK_PARTICLES_MIN, max(SPARK_PARTICLES_MIN, SPARK_PARTICLES_MAX))
    for _ in range(n):
        ang = random.uniform(0.0, 2.0*math.pi) + random.uniform(-SPARK_JITTER, SPARK_JITTER)
        spd = random.uniform(SPARK_SPEED_MIN, SPARK_SPEED_MAX)
        life = random.uniform(SPARK_LIFETIME_MIN, SPARK_LIFETIME_MAX)
        tdelay = random.uniform(0.0, SPARK_START_JITTER)
        sparks.append({
            "t0": 0.0,
            "x0": cx, "y0": cy,
            "vx": math.cos(ang)*spd,
            "vy": math.sin(ang)*spd,
            "life": life,
            "tdelay": tdelay,
        })
    return sparks

def draw_sparks(t_now, sparks):
    layer = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    alive = []
    for s in sparks:
        age = t_now - (s["t0"] + s.get("tdelay", 0.0))
        if age < 0:
            alive.append(s); continue
        if 0 <= age <= s.get("life", _SPARK_LIFE_BASE):
            x = int(s["x0"] + s["vx"]*age)
            y = int(s["y0"] + s["vy"]*age)
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                layer[y, x] = 0
            for k in range(1, SPARK_TRAIL_SAMPLES + 1):
                ta = age - k*SPARK_TRAIL_STEP
                if ta <= 0: break
                tx = int(s["x0"] + s["vx"]*ta)
                ty = int(s["y0"] + s["vy"]*ta)
                if 0 <= tx < WIDTH and 0 <= ty < HEIGHT:
                    layer[ty, tx] = 0
            alive.append(s)
    sparks[:] = alive
    return layer

def composite_min(base, overlay):
    return np.minimum(base, overlay)

# -----------------------------
# Animation loop
# -----------------------------
def run():
    print(f"[HB] heartbeat starting… BPM={BPM}, burst every {BEATS_PER_EXPLOSION} beats, duration={DURATION_S:.2f}s")
    t0 = time.perf_counter()
    period = 60.0 / max(BPM, 1e-6)
    prev_beat_idx = -1
    beat_count = 0
    rings = deque()
    sparks = []
    CX, CY = (WIDTH - 1) / 2.0, (HEIGHT - 1) / 2.0

    try:
        while True:
            t = time.perf_counter() - t0
            if t >= DURATION_S:
                break  # << hard stop after target duration

            beat_phase = (t / period) % 1.0
            beat_idx   = int(t / period)

            intro_u = 1.0 if INTRO_GROW_SEC <= 0 else min(1.0, t / INTRO_GROW_SEC)
            intro_scale = INTRO_START_SCALE + (1.0 - INTRO_START_SCALE) * intro_u

            eased = _ease_in_out_pow(beat_phase, HEART_EASE_IN, HEART_EASE_OUT)
            pulse_scale = HEART_BASE_SCALE * (1.0 + HEART_PULSE_AMP * (2.0*eased - 1.0))
            scale = intro_scale * pulse_scale

            if beat_idx != prev_beat_idx:
                prev_beat_idx = beat_idx
                beat_count += 1
                if (beat_count % BEATS_PER_EXPLOSION) == 0:
                    base_t = t
                    for i in range(RING_COUNT_ON_BURST):
                        rings.append({'t0': base_t + i*RING_STAGGER_SEC, 'r0': 0.5})
                    if USE_SPARKS:
                        burst = spawn_sparks(CX, CY)
                        for s in burst:
                            s["t0"] = t
                        sparks.extend(burst)

            heart = draw_heart(scale)
            ring_layer = draw_rings(t, rings)
            frame = composite_min(heart, ring_layer)

            if USE_SPARKS:
                spark_layer = draw_sparks(t, sparks)
                frame = np.minimum(frame, spark_layer)

            panels = pack_flipbytes(frame)
            send_to_panels(panels)

            time.sleep(FRAME_DT)

    except KeyboardInterrupt:
        print("\n[HB] stopping…")
    finally:
        try:
            ser.close()
            print("[SER] Closed")
        except Exception:
            pass
        print("[HB] done.")

if __name__ == "__main__":
    run()
