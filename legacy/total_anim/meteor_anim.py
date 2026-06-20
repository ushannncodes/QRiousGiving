# anim_meteor_party_blackboom.py
# 🌠 Meteor Shower Party — black background + bigger/longer explosion
import numpy as np
import time, random, serial, os, math

# -----------------------------
# Flipdot / Serial (same wiring as your anim.py)
# -----------------------------
SERIAL_PORT  = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE    = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS  = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28
FPS           = float(os.getenv("FPS", "60"))
FRAME_DT      = 1.0 / FPS
DURATION_S    = float(os.getenv("DURATION_S", "9.0"))  # 7–10s
TOTAL_FRAMES  = int(DURATION_S * FPS)

# -----------------------------
# Night sky (now black background)
# -----------------------------
# On flipdots here: 0 = black (ON), 1 = white (OFF). We render background as 0.
STAR_DENSITY   = float(os.getenv("STAR_DENSITY", "0.025"))  # sparse twinkles
STAR_TWINKLE_P = float(os.getenv("STAR_TWINKLE_P", "0.02"))

# -----------------------------
# Small meteors (top-right -> bottom-left)
# -----------------------------
NUM_SMALL       = int(os.getenv("NUM_SMALL", str(random.choice([2, 3]))))
SMALL_SPEED     = float(os.getenv("SMALL_SPEED", "0.9"))    # px/frame (diag)
SMALL_TRAIL     = int(os.getenv("SMALL_TRAIL", "6"))        # frames of trail
SMALL_SPAWN_GAP = int(os.getenv("SMALL_SPAWN_GAP", str(int(0.9 * FPS))))

# -----------------------------
# Big meteor + BIGGER explosion
# -----------------------------
BIG_SPEED        = float(os.getenv("BIG_SPEED", "1.45"))
BIG_TRAIL        = int(os.getenv("BIG_TRAIL", "8"))
BIG_THICK        = int(os.getenv("BIG_THICK", "3"))

EXP_PARTICLES    = int(os.getenv("EXP_PARTICLES", "120"))    # was ~36 → bigger
EXP_MAX_AGE      = int(os.getenv("EXP_MAX_AGE", "28"))      # linger longer
EXP_CENTER_JIT   = float(os.getenv("EXP_CENTER_JIT", "0.5"))
CORE_FLASH_AGE   = int(os.getenv("CORE_FLASH_AGE", "5"))    # bright core frames
CORE_RADIUS      = int(os.getenv("CORE_RADIUS", "5"))       # bigger flash radius
SHOCK_AGE        = int(os.getenv("SHOCK_AGE", "26"))        # shockwave lifetime
SHOCK_THICK      = int(os.getenv("SHOCK_THICK", "1"))

# -----------------------------
# Packing & Sending (unchanged)
# -----------------------------
def pack_flipbytes(frame28):
    """
    Pack 28x28 (uint8 0/1) into four 7x28 panel payloads, column-wise, 7 rows/byte.
    """
    panels = []
    for p in range(4):
        y0 = p * 7
        data = bytearray()
        for x in range(WIDTH):
            b = 0
            for ry in range(7):
                bit = int(frame28[y0 + ry, x]) & 1
                b |= (bit << ry)
            data.append(b)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(pkt)
    ser.flush()

# -----------------------------
# Sky
# -----------------------------
def new_starfield():
    mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if random.random() < STAR_DENSITY:
                mask[y, x] = 1
    return mask

def twinkle(star_on, star_mask):
    flip = (np.random.rand(HEIGHT, WIDTH) < STAR_TWINKLE_P).astype(np.uint8)
    to_flip = (flip == 1) & (star_mask == 1)
    star_on[to_flip] ^= 1
    return star_on

def render_black_sky(star_mask, star_on):
    # Start all black (0); draw stars as white (1)
    frame = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    frame[(star_mask == 1) & (star_on == 1)] = 1
    return frame

# -----------------------------
# Meteors
# -----------------------------
class Meteor:
    def __init__(self, x, y, vx, vy, trail_len=4, thick=1, is_big=False):
        self.x = x; self.y = y
        self.vx = vx; self.vy = vy
        self.trail_len = trail_len
        self.thick = thick
        self.is_big = is_big
        self.alive = True
        self.trail = []  # list[(ix,iy)]

    def update(self):
        if not self.alive: return
        self.x += self.vx; self.y += self.vy
        ix, iy = int(round(self.x)), int(round(self.y))
        self.trail.append((ix, iy))
        if len(self.trail) > self.trail_len:
            self.trail.pop(0)
        if ix < -3 or iy >= HEIGHT + 3 or ix >= WIDTH + 3 or iy < -3:
            self.alive = False

    def draw(self, frame):
        # Draw head white (1) + fading trail (white but sparser)
        self._dot(frame, int(round(self.x)), int(round(self.y)), bold=True)
        for i, (tx, ty) in enumerate(self.trail[:-1]):
            age = len(self.trail) - 1 - i
            keep = (age <= 1) or (self.is_big and age <= 3) or (random.random() < 0.5 / max(1, age))
            if keep:
                self._dot(frame, tx, ty, bold=False)

    def _dot(self, frame, cx, cy, bold=False):
        pts = [(cx, cy)]
        if self.thick >= 2:
            # slight thickness perpendicular to ~45° motion
            pts += [(cx-1, cy), (cx, cy+1)]
        if self.thick >= 3:
            pts += [(cx-1, cy+1), (cx-2, cy), (cx, cy+2)]
        for (px, py) in pts:
            if 0 <= px < WIDTH and 0 <= py < HEIGHT:
                frame[py, px] = 1  # white on black

# -----------------------------
# Explosion bits
# -----------------------------
class Particle:
    def __init__(self, x, y, vx, vy, max_age):
        self.x = x; self.y = y
        self.vx = vx; self.vy = vy
        self.age = 0; self.max_age = max_age
        self.alive = True

    def update(self):
        if not self.alive: return
        self.x += self.vx; self.y += self.vy
        self.age += 1
        if self.age >= self.max_age:
            self.alive = False

    def draw(self, frame):
        if not self.alive: return
        # fade late by skipping some draws
        if self.age > self.max_age * 0.7 and random.random() < 0.4: 
            return
        ix, iy = int(round(self.x)), int(round(self.y))
        if 0 <= ix < WIDTH and 0 <= iy < HEIGHT:
            frame[iy, ix] = 1

class Shockwave:
    def __init__(self, cx, cy, max_age, thick=1, speed=1.25):
        self.cx = cx; self.cy = cy
        self.age = 0; self.max_age = max_age
        self.thick = thick
        self.speed = speed
        self.alive = True

    def update(self):
        if not self.alive: return
        self.age += 1
        if self.age >= self.max_age:
            self.alive = False

    def draw(self, frame):
        if not self.alive: return
        r = self.age * self.speed
        r_in  = max(0.0, r - self.thick * 0.5)
        r_out = r + self.thick * 0.5
        r2_in, r2_out = r_in*r_in, r_out*r_out
        for y in range(HEIGHT):
            dy = y - self.cy
            for x in range(WIDTH):
                dx = x - self.cx
                d2 = dx*dx + dy*dy
                if r2_in <= d2 <= r2_out:
                    frame[y, x] = 1

def make_explosion(cx, cy):
    parts = []
    for i in range(EXP_PARTICLES):
        a = (2*math.pi) * (i / EXP_PARTICLES) + random.uniform(-0.15, 0.15)
        v = random.uniform(0.6, 1.25)
        parts.append(Particle(cx, cy, v*math.cos(a), v*math.sin(a), EXP_MAX_AGE))
    shock = Shockwave(cx, cy, SHOCK_AGE, SHOCK_THICK)
    return parts, shock

def draw_core_flash(frame, cx, cy, radius):
    r2 = radius*radius
    for y in range(HEIGHT):
        dy = y - cy
        for x in range(WIDTH):
            dx = x - cx
            if dx*dx + dy*dy <= r2:
                frame[y, x] = 1

# -----------------------------
# Flow control
# -----------------------------
def run_animation():
    star_mask = new_starfield()
    star_on   = (np.random.rand(HEIGHT, WIDTH) < 0.5).astype(np.uint8)

    meteors = []
    particles = []
    shock = None
    core_flash_until = -1

    # Spawn schedule
    t0 = 0
    small_spawn_frames = [t0 + i * SMALL_SPAWN_GAP for i in range(NUM_SMALL)]
    big_spawn_frame = int(TOTAL_FRAMES * 0.4)   # big one mid-timeline
    burst_done = False

    # Big meteor trajectory (off top-right → off bottom-left)
    big_x0, big_y0 = WIDTH + 2, -2
    diag = math.sqrt(2)
    big_vx = -BIG_SPEED / diag
    big_vy =  BIG_SPEED / diag

    for f in range(TOTAL_FRAMES):
        # Twinkle stars
        star_on[:] = twinkle(star_on, star_mask)

        # Spawn small meteors
        if small_spawn_frames and f == small_spawn_frames[0]:
            small_spawn_frames.pop(0)
            sx = random.randint(WIDTH-6, WIDTH-1)
            sy = random.randint(-1, 3)
            sp = SMALL_SPEED * random.uniform(0.9, 1.1)
            vx, vy = -sp / math.sqrt(2), sp / math.sqrt(2)
            meteors.append(Meteor(sx, sy, vx, vy, trail_len=SMALL_TRAIL, thick=1, is_big=False))

        # Spawn big meteor
        if f == big_spawn_frame:
            meteors.append(Meteor(big_x0, big_y0, big_vx, big_vy, trail_len=BIG_TRAIL, thick=BIG_THICK, is_big=True))

        # Update
        for m in meteors: m.update()
        meteors = [m for m in meteors if m.alive]

        for p in particles: p.update()
        particles = [p for p in particles if p.alive]

        if shock: 
            shock.update()
            if not shock.alive: shock = None

        # Trigger explosion near center (or fallback if late)
        if not burst_done:
            for m in meteors:
                if m.is_big:
                    if (abs(m.x - (WIDTH-1)/2) < 1.0 and abs(m.y - (HEIGHT-1)/2) < 1.0) or (f > big_spawn_frame + int(0.6*FPS)):
                        cx = int((WIDTH-1)/2 + random.uniform(-EXP_CENTER_JIT, EXP_CENTER_JIT))
                        cy = int((HEIGHT-1)/2 + random.uniform(-EXP_CENTER_JIT, EXP_CENTER_JIT))
                        particles, shock = make_explosion(cx, cy)
                        core_flash_until = f + CORE_FLASH_AGE
                        burst_done = True
                        break

        # Render (black background with twinkles)
        frame = render_black_sky(star_mask, star_on)

        # Draw meteors
        for m in meteors:
            m.draw(frame)

        # Explosion visuals
        if burst_done:
            if f <= core_flash_until:
                # bright core flash first few frames
                # center = explosion origin = shock center if exists, else screen center
                if shock:
                    draw_core_flash(frame, shock.cx, shock.cy, CORE_RADIUS)
                else:
                    draw_core_flash(frame, WIDTH//2, HEIGHT//2, CORE_RADIUS)
            for p in particles:
                p.draw(frame)
            if shock:
                shock.draw(frame)

        # Send
        bw = frame.astype(np.uint8)
        panels = pack_flipbytes(bw)
        send_to_panels(panels)
        time.sleep(FRAME_DT)

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    try:
        run_animation()
    except KeyboardInterrupt:
        print("\n[ANIM] Interrupted.")
    finally:
        try:
            ser.close()
        except Exception:
            pass
