# anim_flyby_balloons.py
import numpy as np
import time, math, random, serial, os

# -----------------------------
# Flipdot / Serial (same style as anim.py)
# -----------------------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]   # 4 x (7x28) stacked = 28x28
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "55"))
FRAME_DT      = 1.0 / FPS

# Total duration split into: fly-by, settle, sway
DUR_FLYBY_S   = float(os.getenv("DUR_FLYBY_S", "5.0"))
DUR_SWAY_S    = float(os.getenv("DUR_SWAY_S", "3.0"))
TOTAL_FRAMES  = int((DUR_FLYBY_S + DUR_SWAY_S) * FPS)

# -----------------------------
# Style toggles
# -----------------------------
SHAPE         = os.getenv("SHAPE", "bird")   # "balloon" or "bird"
RND_SEED      = int(os.getenv("RND_SEED", str(int(time.time()))))

# Fly-by tuning
BALLOON_RADIUS      = int(os.getenv("BALLOON_RADIUS", "2"))  # 2–3 looks best
TRAIL_MAX_LIFETIME  = int(os.getenv("TRAIL_MAX_LIFETIME", "9"))
TRAIL_SPAWN_RATE    = float(os.getenv("TRAIL_SPAWN_RATE", "0.65"))
TRAIL_SPREAD        = float(os.getenv("TRAIL_SPREAD", "1.2"))  # random jitter behind balloon
FLY_SPEED_X         = float(os.getenv("FLY_SPEED_X", "0.55"))
FLY_SPEED_Y         = float(os.getenv("FLY_SPEED_Y", "-0.75"))

# Sway/top balloons
TOP_BALLOONS        = int(os.getenv("TOP_BALLOONS", "3"))
SWAY_AMPLITUDE      = float(os.getenv("SWAY_AMPLITUDE", "2.0"))
SWAY_SPEED          = float(os.getenv("SWAY_SPEED", "1.2"))

random.seed(RND_SEED)

# -----------------------------
# Panel pack/send (same idea as anim.py)
# -----------------------------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                bit = int(frame28[offset + y, x])  # 0 or 1
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
# Drawing helpers (0 = black ON pixel, 1 = white background)
# -----------------------------
def draw_disc(img, cx, cy, r):
    r2 = r*r
    x0, x1 = max(0, int(cx-r-1)), min(WIDTH-1, int(cx+r+1))
    y0, y1 = max(0, int(cy-r-1)), min(HEIGHT-1, int(cy+r+1))
    for y in range(y0, y1+1):
        for x in range(x0, x1+1):
            if (x-cx)*(x-cx) + (y-cy)*(y-cy) <= r2:
                img[y, x] = 0

def draw_string(img, cx, cy, length=3):
    # tiny dotted “string” just below balloon
    for i in range(length):
        yy = cy + i + 1
        if 0 <= yy < HEIGHT and 0 <= cx < WIDTH:
            if i % 2 == 0:
                img[yy, cx] = 0

def draw_bird(img, cx, cy):
    # a tiny ‘V’ bird: three pixels; adjust for visibility
    pts = [(cx, cy), (cx-1, cy+1), (cx+1, cy+1)]
    for x, y in pts:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            img[y, x] = 0

def draw_balloon(img, cx, cy, r):
    draw_disc(img, cx, cy, r)
    draw_string(img, cx, cy + r, length=3)

def draw_shape(img, cx, cy, r):
    if SHAPE.lower() == "bird":
        draw_bird(img, cx, cy)
    else:
        draw_balloon(img, cx, cy, r)

# -----------------------------
# Trail model
# -----------------------------
class Trail:
    __slots__ = ("x", "y", "life")
    def __init__(self, x, y, life):
        self.x = x; self.y = y; self.life = life

def sprinkle_trail(img, trails):
    # Render live trail dots
    alive = []
    for t in trails:
        if 0 <= t.x < WIDTH and 0 <= t.y < HEIGHT and t.life > 0:
            img[int(t.y), int(t.x)] = 0
            t.life -= 1
            if t.life > 0:
                alive.append(t)
    return alive

# -----------------------------
# Main animation
# -----------------------------
def run():
    # Initial fly-by start slightly off-screen bottom-left or bottom
    start_x = random.uniform(-4.0, 2.0)
    start_y = random.uniform(HEIGHT + 2.0, HEIGHT + 6.0)
    vx      = FLY_SPEED_X * (0.85 + 0.3 * random.random())
    vy      = FLY_SPEED_Y * (0.85 + 0.3 * random.random())

    x, y    = start_x, start_y
    trails  = []

    total_frames_fly = int(DUR_FLYBY_S * FPS)
    total_frames_swy = int(DUR_SWAY_S * FPS)

    # Preselect top balloons positions (finish state)
    top_centers = []
    top_y = random.randint(2, 5)
    for i in range(TOP_BALLOONS):
        cx = int(WIDTH*(i+1)/(TOP_BALLOONS+1))
        # tiny jitter so they don’t line up perfectly
        cx += random.randint(-1, 1)
        top_centers.append((cx, top_y))

    # ------------- Phase 1: Fly-by -------------
    for f in range(total_frames_fly):
        frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

        # Update balloon/bird position
        x += vx
        y += vy

        # Draw main shape
        draw_shape(frame, int(round(x)), int(round(y)), BALLOON_RADIUS)

        # Spawn a few sparkle trail points behind the motion
        if random.random() < TRAIL_SPAWN_RATE:
            # take a few points behind the balloon with jitter
            behind_x = x - 1.2 * vx + random.uniform(-TRAIL_SPREAD, TRAIL_SPREAD)
            behind_y = y - 1.2 * vy + random.uniform(-TRAIL_SPREAD, TRAIL_SPREAD)
            trails.append(Trail(int(round(behind_x)), int(round(behind_y)),
                                random.randint(TRAIL_MAX_LIFETIME//2, TRAIL_MAX_LIFETIME)))

        trails = sprinkle_trail(frame, trails)

        # Stop fly-by once fully off the top-right
        if x > WIDTH + 4 and y < -4:
            # fast-forward to sway
            break

        # Push to panels
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

    # ------------- Phase 2: Sway at top -------------
    # A few balloons gently swaying; subtle strings
    t0 = time.time()
    for f in range(total_frames_swy):
        t = time.time() - t0
        frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

        for i, (cx0, cy0) in enumerate(top_centers):
            # different phase per balloon
            phase = i * 0.9 + 0.3
            sway  = SWAY_AMPLITUDE * math.sin(SWAY_SPEED * t + phase)
            cx    = int(round(cx0 + sway))
            draw_balloon(frame, cx, cy0, BALLOON_RADIUS)

        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

# -----------------------------
# Go!
# -----------------------------
if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("Exiting…")
    finally:
        try:
            ser.close()
        except Exception:
            pass
