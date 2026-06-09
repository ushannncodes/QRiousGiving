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

# -----------------------------
# Heartbeat & Rings (knobs)
# -----------------------------
BPM                 = float(os.getenv("BPM", "66"))
BEATS_PER_EXPLOSION = int(os.getenv("BEATS_PER_EXPLOSION", "4"))

# start smaller & centered
HEART_BASE_SCALE   = float(os.getenv("HEART_BASE_SCALE", "0.78"))
HEART_PULSE_AMP    = float(os.getenv("HEART_PULSE_AMP", "0.2"))
HEART_EASE_IN      = float(os.getenv("HEART_EASE_IN", "2.2"))
HEART_EASE_OUT     = float(os.getenv("HEART_EASE_OUT", "2.0"))
INTRO_GROW_SEC     = float(os.getenv("INTRO_GROW_SEC", "0.85"))
INTRO_START_SCALE  = float(os.getenv("INTRO_START_SCALE", "0.78"))
HEART_CENTER_OFF_X = float(os.getenv("HEART_CENTER_OFF_X", "0.0"))
HEART_CENTER_OFF_Y = float(os.getenv("HEART_CENTER_OFF_Y", "2"))
# Cleft shaping (tweak to taste)
CLEFT_STRENGTH = float(os.getenv("CLEFT_STRENGTH", "0.38"))  # how deep the notch pushes in
CLEFT_WIDTH    = float(os.getenv("CLEFT_WIDTH", "0.4"))     # horizontal spread of the notch (normalized units)
TIP_SHARPEN    = float(os.getenv("TIP_SHARPEN", "0.2"))     # extra squeeze on lower half to sharpen the point
TOP_SQUASH     = float(os.getenv("TOP_SQUASH", "0.2"))      # gentle vertical squash on upper half (rounds lobes)


# ripple “explosion”
RING_SPEED          = float(os.getenv("RING_SPEED", "14.0"))    # px/sec
RING_THICKNESS      = float(os.getenv("RING_THICKNESS", "1.2"))
RING_MAX_R          = float(os.getenv("RING_MAX_R", "22.0"))
RING_LIFETIME       = float(os.getenv("RING_LIFETIME", "0.9"))
RING_COUNT_ON_BURST = int(os.getenv("RING_COUNT_ON_BURST", "0"))
RING_STAGGER_SEC    = float(os.getenv("RING_STAGGER_SEC", "0.10"))

SPARK_PARTICLES_PER_BURST = int(os.getenv("SPARK_PARTICLES_PER_BURST", "36"))
SPARK_SPEED_MIN           = float(os.getenv("SPARK_SPEED_MIN", "6.0"))   # px/sec
SPARK_SPEED_MAX           = float(os.getenv("SPARK_SPEED_MAX", "14.0"))
SPARK_LIFETIME            = float(os.getenv("SPARK_LIFETIME", "0.9"))    # seconds
SPARK_JITTER              = float(os.getenv("SPARK_JITTER", "0.20"))     # angular noise
USE_SPARKS                = int(os.getenv("USE_SPARKS", "1"))            # 1=on, 0=off
SPARK_LIFETIME = float(os.getenv("SPARK_LIFETIME", "5"))  # was 0.9
SPARK_SPEED_MIN = float(os.getenv("SPARK_SPEED_MIN", "3.0"))  # was 6.0
SPARK_SPEED_MAX = float(os.getenv("SPARK_SPEED_MAX", "7.0"))  # was 14.0
SPARK_TRAIL_SAMPLES = int(os.getenv("SPARK_TRAIL_SAMPLES", "1"))  # how many ghost steps behind
SPARK_TRAIL_STEP    = float(os.getenv("SPARK_TRAIL_STEP", "0.1"))  # seconds between ghost steps




random.seed(2)

# -----------------------------
# Wire format (matches your anim.py)
# -----------------------------
def pack_flipbytes(frame28: np.ndarray):
    """
    frame28: uint8 28x28, where 0 = black (dot ON), 1 = white (off)
    Pack column-major, 7 rows per byte (LSB = top of 7-row block)
    Return list of 4 bytearrays, for panel addresses top->bottom.
    """
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
    """
    Packet: [0x80, 0x83, addr] + 28 bytes + [0x8F]
    """
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

def _heart_implicit(px, py):
    # (x^2 + y^2 - 1)^3 - x^2 y^3 <= 0 -> inside heart
    x, y = px, py
    a = x*x + y*y - 1.0
    return (a*a*a - x*x*(y*y*y))

def draw_heart(scale):
    """
    Return 28x28 uint8 with 0=black(ON), 1=white.
    More defined cleft via a pre-warp on (px,py) before evaluating the implicit.
    """
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx = (WIDTH - 1) / 2.0 + HEART_CENTER_OFF_X
    cy = (HEIGHT - 1) / 2.0 + HEART_CENTER_OFF_Y

    # map to approx [-1.35, +1.35] square, same as before
    base_span = 1.35
    span = base_span / max(scale, 1e-6)

    for y in range(HEIGHT):
        # +y up
        py = ((cy - y) / (HEIGHT * 0.5)) * span
        for x in range(WIDTH):
            px = ((x - cx) / (WIDTH * 0.5)) * span

            # ---------- cleft & silhouette shaping ----------
            # Work on copies so you can read px/py in logs if needed
            qx, qy = px, py

            if qy >= 0.0:
                # Upper lobes:
                # 1) Slight vertical squash so lobes aren’t too ballooned
                qy *= (1.0 + TOP_SQUASH * min(1.0, qy*qy))
                # 2) Center pinch to deepen the cleft — strongest at x≈0 and fades with |x|
                #    Gaussian falloff across x; positive offset pushes qy up (carves notch)
                if CLEFT_STRENGTH != 0.0 and CLEFT_WIDTH > 0.0:
                    pinch = CLEFT_STRENGTH * math.exp(-(qx*qx) / (CLEFT_WIDTH*CLEFT_WIDTH))
                    qy += pinch
            else:
                # Lower half: gently stretch to sharpen the point
                qy *= (1.0 + TIP_SHARPEN * min(1.0, (-qy)))

            # ---------- evaluate classic heart implicit on warped coords ----------
            a = qx*qx + qy*qy - 1.0
            inside = (a*a*a - qx*qx*(qy*qy*qy)) <= 0.0

            if inside:
                frame[y, x] = 0  # dot ON

    return frame


def draw_rings(t_now, rings: deque):
    """
    Return a ring layer (0=black ring ON, 1=white elsewhere).
    """
    layer = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx, cy = (WIDTH - 1) / 2.0, (HEIGHT - 1) / 2.0

    for ring in list(rings):
        age = t_now - ring['t0']
        if age < 0:
            continue
        if age > RING_LIFETIME:
            rings.remove(ring); continue

        r = ring['r0'] + RING_SPEED * age
        if r > RING_MAX_R:
            rings.remove(ring); continue

        rmin = r - RING_THICKNESS
        rmax = r + RING_THICKNESS

        # binary ring
        for y in range(HEIGHT):
            dy = y - cy
            for x in range(WIDTH):
                dx = x - cx
                d = math.hypot(dx, dy)
                if rmin <= d <= rmax:
                    layer[y, x] = 0
    return layer

def spawn_sparks(cx, cy):
    sparks = []
    for i in range(SPARK_PARTICLES_PER_BURST):
        ang = (2*math.pi*i/SPARK_PARTICLES_PER_BURST) + random.uniform(-SPARK_JITTER, SPARK_JITTER)
        spd = random.uniform(SPARK_SPEED_MIN, SPARK_SPEED_MAX)
        sparks.append({
            "t0": 0.0,            # will be filled on schedule
            "x0": cx, "y0": cy,   # start at center
            "vx": math.cos(ang)*spd,
            "vy": math.sin(ang)*spd,
        })
    return sparks

def draw_sparks(t_now, sparks):
    """
    Binary layer: spark pixels are 0 (black/ON), rest 1 (white).
    Longer life + simple 1px trails behind each spark.
    """
    layer = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    alive = []
    for s in sparks:
        age = t_now - s["t0"]
        if 0 <= age <= SPARK_LIFETIME:
            # head (current position)
            x = int(s["x0"] + s["vx"]*age)
            y = int(s["y0"] + s["vy"]*age)
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                layer[y, x] = 0

            # simple ghost trail samples behind the head
            for k in range(1, SPARK_TRAIL_SAMPLES + 1):
                ta = age - k*SPARK_TRAIL_STEP
                if ta <= 0: 
                    break
                tx = int(s["x0"] + s["vx"]*ta)
                ty = int(s["y0"] + s["vy"]*ta)
                if 0 <= tx < WIDTH and 0 <= ty < HEIGHT:
                    layer[ty, tx] = 0

            alive.append(s)
    sparks[:] = alive
    return layer


def composite_min(base, overlay):
    # with 0=black(ON), 1=white: "OR" becomes per-pixel min()
    return np.minimum(base, overlay)

# -----------------------------
# Animation loop
# -----------------------------
def run():
    print(f"[HB] heartbeat starting… BPM={BPM}, burst every {BEATS_PER_EXPLOSION} beats")
    t0 = time.perf_counter()
    period = 60.0 / max(BPM, 1e-6)
    prev_beat_idx = -1
    beat_count = 0
    rings = deque()
    sparks = []   # list of active sparks
    CX, CY = (WIDTH - 1) / 2.0, (HEIGHT - 1) / 2.0

    try:
        while True:
            t = time.perf_counter() - t0

            # --- beat phase/index (compute BEFORE using) ---
            beat_phase = (t / period) % 1.0
            beat_idx   = int(t / period)

            # intro ramp 0..1 over INTRO_GROW_SEC
            intro_u = 1.0 if INTRO_GROW_SEC <= 0 else min(1.0, t / INTRO_GROW_SEC)
            intro_scale = INTRO_START_SCALE + (1.0 - INTRO_START_SCALE) * intro_u

            # heartbeat easing
            eased = _ease_in_out_pow(beat_phase, HEART_EASE_IN, HEART_EASE_OUT)
            pulse_scale = HEART_BASE_SCALE * (1.0 + HEART_PULSE_AMP * (2.0*eased - 1.0))

            # final scale
            scale = intro_scale * pulse_scale

            # new-beat detection & burst scheduling
            if beat_idx != prev_beat_idx:
                prev_beat_idx = beat_idx
                beat_count += 1
                if (beat_count % BEATS_PER_EXPLOSION) == 0:
                    base_t = t
                    for i in range(RING_COUNT_ON_BURST):
                        rings.append({'t0': base_t + i*RING_STAGGER_SEC, 'r0': 0.5})
                    # sparks (starburst)
                    if USE_SPARKS:
                        burst = spawn_sparks(CX, CY)
                        for s in burst:
                            s["t0"] = t  # stamp their start time
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

if __name__ == "__main__":
    run()
