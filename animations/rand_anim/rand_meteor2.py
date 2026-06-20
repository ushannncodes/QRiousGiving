# anim_meteor_star_coalesce.py
# 🌠 Meteor Shower → TWO pops → guided coalesce into a BRIGHT STAR
# 0 = black (ON), 1 = white (OFF)

import numpy as np
import time, random, serial, os, math

# -----------------------------
# Flipdot / Serial
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
DURATION_S    = float(os.getenv("DURATION_S", "9.0"))
TOTAL_FRAMES  = int(DURATION_S * FPS)

# End handling
# If True, we cut immediately after 2nd pop trigger (no star). Leave "0" to show star ender.
END_ON_SECOND_TRIGGER = (os.getenv("END_ON_SECOND_TRIGGER", "0") == "1")

# -----------------------------
# Night sky (black background)
# -----------------------------
STAR_DENSITY   = float(os.getenv("STAR_DENSITY", "0.025"))
STAR_TWINKLE_P = float(os.getenv("STAR_TWINKLE_P", "0.02"))

# -----------------------------
# Small meteors (top-right -> bottom-left)
# -----------------------------
NUM_SMALL       = int(os.getenv("NUM_SMALL", str(random.choice([2, 3]))))
SMALL_SPEED     = float(os.getenv("SMALL_SPEED", "0.9"))     # px/frame (diag)
SMALL_TRAIL     = int(os.getenv("SMALL_TRAIL", "4"))
SMALL_SPAWN_GAP = int(os.getenv("SMALL_SPAWN_GAP", str(int(0.9 * FPS))))

# -----------------------------
# Big meteors + explosions (two pops)
# -----------------------------
NUM_BIG       = int(os.getenv("NUM_BIG", "2"))
BIG_SPEED     = float(os.getenv("BIG_SPEED", "1.45"))
BIG_TRAIL     = int(os.getenv("BIG_TRAIL", "8"))
BIG_THICK     = int(os.getenv("BIG_THICK", "2"))

EXP_PARTICLES = int(os.getenv("EXP_PARTICLES", "80"))
EXP_MAX_AGE   = int(os.getenv("EXP_MAX_AGE", "24"))
EXP_JIT       = float(os.getenv("EXP_CENTER_JIT", "0.8"))
CORE_FRAMES   = int(os.getenv("CORE_FLASH_AGE", "5"))
CORE_RADIUS   = int(os.getenv("CORE_RADIUS", "4"))
SHOCK_FRAMES  = int(os.getenv("SHOCK_AGE", "16"))
SHOCK_THICK   = int(os.getenv("SHOCK_THICK", "1"))

# -----------------------------
# ⭐ Coalesce-to-Star (Phase C)
# -----------------------------
STAR_TARGETS         = int(os.getenv("STAR_TARGETS", "120"))   # number of star pixels to "lock" visibly
STAR_SPEED           = float(os.getenv("STAR_SPEED", "0.5")) # px per frame for guided rays
STAR_SOURCES         = int(os.getenv("STAR_SOURCES", "6"))    # how many launch points at edges/corners
STAR_TRAIL_TTL       = int(os.getenv("STAR_TRAIL_TTL", "3"))
STAR_OUTLINE_FIRST   = int(os.getenv("STAR_OUTLINE_FIRST", "1")) == 1  # prioritize outline pixels
STAR_HOLD_FRAMES     = int(os.getenv("STAR_HOLD_FRAMES", "70"))

# -----------------------------
# Packing & Sending
# -----------------------------
def pack_flipbytes(frame28):
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

def send_frame(frame):
    panels = pack_flipbytes(frame.astype(np.uint8))
    for addr, data in zip(PANEL_ADDRS, panels):
        pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(pkt)
    ser.flush()

# -----------------------------
# Sky helpers
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
# Meteors & explosions
# -----------------------------
class Meteor:
    def __init__(self, x, y, vx, vy, trail_len=4, thick=1, is_big=False, id_tag=None, spawn_frame=0):
        self.x = x; self.y = y
        self.vx = vx; self.vy = vy
        self.trail_len = trail_len
        self.thick = thick
        self.is_big = is_big
        self.id_tag = id_tag
        self.spawn_frame = spawn_frame
        self.alive = True
        self.trail = []

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
        self._dot(frame, int(round(self.x)), int(round(self.y)))
        for i, (tx, ty) in enumerate(self.trail[:-1]):
            age = len(self.trail) - 1 - i
            keep = (age <= 1) or (self.is_big and age <= 3) or (random.random() < 0.5 / max(1, age))
            if keep:
                self._dot(frame, tx, ty)

    def _dot(self, frame, cx, cy):
        pts = [(cx, cy)]
        if self.thick >= 2:
            pts += [(cx-1, cy), (cx, cy+1)]
        if self.thick >= 3:
            pts += [(cx-1, cy+1), (cx-2, cy), (cx, cy+2)]
        for (px, py) in pts:
            if 0 <= px < WIDTH and 0 <= py < HEIGHT:
                frame[py, px] = 1

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

class Explosion:
    def __init__(self, cx, cy, start_frame, core_frames, core_radius, shock_frames, shock_thick):
        self.cx = cx; self.cy = cy
        self.start_frame = start_frame
        self.core_until  = start_frame + core_frames
        self.particles = []
        for i in range(EXP_PARTICLES):
            a = (2*math.pi) * (i / EXP_PARTICLES) + random.uniform(-0.15, 0.15)
            v = random.uniform(0.6, 1.25)
            self.particles.append(Particle(cx, cy, v*math.cos(a), v*math.sin(a), EXP_MAX_AGE))
        self.shock = Shockwave(cx, cy, shock_frames, shock_thick)
        self.core_radius = core_radius
        self.alive = True

    def update(self, fnow):
        if not self.alive: return
        for p in self.particles: p.update()
        self.particles = [p for p in self.particles if p.alive]
        if self.shock:
            self.shock.update()
            if not self.shock.alive: self.shock = None
        if not self.particles and not self.shock and fnow > self.core_until:
            self.alive = False

    def draw(self, frame, fnow):
        if fnow <= self.core_until:  # bright center flash window
            r2 = self.core_radius * self.core_radius
            for y in range(HEIGHT):
                dy = y - self.cy
                for x in range(WIDTH):
                    dx = x - self.cx
                    if dx*dx + dy*dy <= r2:
                        frame[y, x] = 1
        for p in self.particles: p.draw(frame)
        if self.shock: self.shock.draw(frame)

# -----------------------------
# ⭐ Star mask & coalesce
# -----------------------------

def mask_star_filled():
    """
    Return 28x28 with a centered 5-point filled star.
    Supports center offsets via STAR_CENTER_OFF_X/Y (pixels).
    0 = black (ON), 1 = white (OFF)
    """
    off_x = float(os.getenv("STAR_CENTER_OFF_X", "0"))
    off_y = float(os.getenv("STAR_CENTER_OFF_Y", "2"))  # default: move down by 2px

    cx, cy = (WIDTH - 1) / 2.0 + off_x, (HEIGHT - 1) / 2.0 + off_y
    R  = float(os.getenv("STAR_OUTER_RADIUS", "15"))  # tweak if edges clip
    r  = float(os.getenv("STAR_INNER_RADIUS", "5"))

    # build vertices (10 points: outer, inner alternating)
    verts = []
    for i in range(10):
        ang = -math.pi/2 + i * (math.pi / 5.0)  # start at top
        rad = R if i % 2 == 0 else r
        verts.append((cx + rad*math.cos(ang), cy + rad*math.sin(ang)))

    # rasterize polygon (ray casting)
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if _point_in_poly(x + 0.5, y + 0.5, verts):
                frame[y, x] = 0

    # keep a clean 1px white border
    frame[0,:] = 1; frame[-1,:] = 1; frame[:,0] = 1; frame[:,-1] = 1
    return frame

# def mask_star_filled():
#     """
#     Return 28x28 with a centered 5-point filled star.
#     0 = black (ON), 1 = white (OFF)
#     """
#     cx, cy = (WIDTH - 1) / 2.0, (HEIGHT - 1) / 2.0
#     R  = 13  # outer radius
#     r  = 5   # inner radius
#     # build vertices (10 points: outer, inner alternating)
#     verts = []
#     for i in range(10):
#         ang = -math.pi/2 + i * (math.pi / 5.0)  # start at top
#         rad = R if i % 2 == 0 else r
#         verts.append((cx + rad*math.cos(ang), cy + rad*math.sin(ang)))

#     # rasterize polygon (ray casting)
#     frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
#     for y in range(HEIGHT):
#         for x in range(WIDTH):
#             if _point_in_poly(x + 0.5, y + 0.5, verts):
#                 frame[y, x] = 0

#     # 1px inset so we don't touch frame edge too hard (clean border)
#     frame[0,:] = 1; frame[-1,:] = 1; frame[:,0] = 1; frame[:,-1] = 1
#     return frame

def _point_in_poly(px, py, verts):
    inside = False
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i+1)%n]
        # edge intersects horizontal ray?
        if ((y1 > py) != (y2 > py)) and (px < (x2 - x1) * (py - y1) / (y2 - y1 + 1e-9) + x1):
            inside = not inside
    return inside

def outline_from_fill(fill_mask):
    """
    Returns a mask where only the 1px outline of the star is 0.
    """
    out = np.ones_like(fill_mask, dtype=np.uint8)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if fill_mask[y, x] != 0:
                continue
            # if any 4-neighbour is white, this is an edge pixel
            edge = False
            for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                ny, nx = y+dy, x+dx
                if ny < 0 or ny >= HEIGHT or nx < 0 or nx >= WIDTH or fill_mask[ny, nx] == 1:
                    edge = True; break
            if edge:
                out[y, x] = 0
    return out

def pick_sources(rng, n):
    # corners + mid-edges; jittered
    cands = [(0,0), (WIDTH-1,0), (0,HEIGHT-1), (WIDTH-1,HEIGHT-1),
             (WIDTH//2,0), (WIDTH//2,HEIGHT-1), (0,HEIGHT//2), (WIDTH-1,HEIGHT//2)]
    rng.shuffle(cands)
    cands = cands[:max(1, n)]
    # jitter 0..1px to avoid dead-straight lines
    out = []
    for x,y in cands:
        out.append((int(max(0,min(WIDTH-1, x + rng.randint(0,1)))),
                    int(max(0,min(HEIGHT-1, y + rng.randint(0,1))))))
    return out

def assign_targets_to_sources(targets, sources):
    buckets = [[] for _ in range(len(sources))]
    for i, tgt in enumerate(targets):
        buckets[i % len(sources)].append(tgt)
    return buckets

def coalesce_to_star(rng, base_frame):
    """
    Guided “rays” travel from edge/corner sources toward star pixels, locking them ON.
    """
    star_fill = mask_star_filled()
    star_outline = outline_from_fill(star_fill)
    blacks = np.column_stack(np.where((star_outline if STAR_OUTLINE_FIRST else star_fill) == 0))
    # (y,x) -> (x,y)
    blacks = [(int(x), int(y)) for (y,x) in blacks]
    rng.shuffle(blacks)

    chosen = []
    min_gap = 2
    for (x,y) in blacks:
        ok = True
        for (sx,sy) in chosen:
            if abs(sx-x)+abs(sy-y) < min_gap:
                ok = False; break
        if ok:
            chosen.append((x,y))
        if len(chosen) >= STAR_TARGETS:
            break
    if not chosen:
        chosen = blacks[:min(STAR_TARGETS, len(blacks))]

    # If we prioritised outline, add some fill pixels too
    if STAR_OUTLINE_FIRST:
        fill_blacks = np.column_stack(np.where(star_fill == 0))
        fill_blacks = [(int(x), int(y)) for (y,x) in fill_blacks]
        rng.shuffle(fill_blacks)
        extra = []
        for (x,y) in fill_blacks:
            if (x,y) not in chosen:
                extra.append((x,y))
            if len(extra) >= max(0, STAR_TARGETS - len(chosen)):
                break
        chosen += extra

    sources = pick_sources(rng, STAR_SOURCES)
    buckets = assign_targets_to_sources(chosen, sources)

    guided = []
    for s, bucket in zip(sources, buckets):
        sx, sy = s
        for (tx, ty) in bucket:
            dist = math.hypot(tx - sx, ty - sy)
            max_age = max(1, int(math.ceil(dist / max(STAR_SPEED, 1e-6))))
            guided.append({"src": (sx, sy), "tgt": (tx, ty), "age": 0, "max_age": max_age})

    frozen = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    # animate until all rays have reached targets
    active = guided
    while active:
        # base: black sky + twinkles frozen (use last base_frame for quietness)
        frame = base_frame.copy()

        next_active = []
        for g in active:
            sx, sy = g["src"]; tx, ty = g["tgt"]
            t = min(1.0, g["age"] / max(1, g["max_age"]))
            x = sx + (tx - sx) * t
            y = sy + (ty - sy) * t
            xi = max(0, min(WIDTH - 1, int(round(x))))
            yi = max(0, min(HEIGHT - 1, int(round(y))))

            # short trailing streak
            for k in range(3):
                tb = max(0.0, t - 0.05 * k)
                xb = sx + (tx - sx) * tb
                yb = sy + (ty - sy) * tb
                xbi = max(0, min(WIDTH - 1, int(round(xb))))
                ybi = max(0, min(HEIGHT - 1, int(round(yb))))
                frame[ybi, xbi] = 1
            frame[yi, xi] = 1  # tip a bit brighter (same color here)

            g["age"] += 1
            if g["age"] >= g["max_age"]:
                frozen[ty, tx] = 0  # lock ON
            else:
                next_active.append(g)

        # overlay frozen star pixels
        frame[frozen == 0] = 0
        send_frame(frame)
        time.sleep(FRAME_DT)
        active = next_active

    # settle trails: show the full filled star (for any tiny gaps)
    final = mask_star_filled()
    for _ in range(STAR_HOLD_FRAMES):
        # bright star: simply the final mask (solid)
        send_frame(final)
        time.sleep(FRAME_DT)

# -----------------------------
# Flow control
# -----------------------------
def run_animation():
    star_mask = new_starfield()
    star_on   = (np.random.rand(HEIGHT, WIDTH) < 0.5).astype(np.uint8)

    meteors = []
    explosions = []
    booms_triggered = 0
    ending = False

    small_spawn_frames = [int(0.2*TOTAL_FRAMES) + i * SMALL_SPAWN_GAP for i in range(NUM_SMALL)]

    # Big meteors schedule
    if NUM_BIG <= 1:
        big_spawn_frames = [int(0.50 * TOTAL_FRAMES)]
    else:
        big_spawn_frames = [int(0.45 * TOTAL_FRAMES), int(0.72 * TOTAL_FRAMES)]

    # Big meteor trajectory (from off top-right → off bottom-left)
    big_x0, big_y0 = WIDTH + 2, -2
    diag = math.sqrt(2)
    big_vx = -BIG_SPEED / diag
    big_vy =  BIG_SPEED  / diag

    def maybe_boom_from(m, fnow):
        near_center = (abs(m.x - (WIDTH-1)/2) < 1.0) and (abs(m.y - (HEIGHT-1)/2) < 1.0)
        late = fnow > (m.spawn_frame + int(0.6 * FPS))
        if near_center or late:
            cx = int((WIDTH-1)/2 + random.uniform(-EXP_JIT, EXP_JIT))
            cy = int((HEIGHT-1)/2 + random.uniform(-EXP_JIT, EXP_JIT))
            explosions.append(Explosion(cx, cy, fnow, CORE_FRAMES, CORE_RADIUS, SHOCK_FRAMES, SHOCK_THICK))
            m.alive = False
            return True
        return False

    last_frame = render_black_sky(star_mask, star_on)

    for f in range(TOTAL_FRAMES):
        # If we should end (2 pops done)
        if ending:
            if END_ON_SECOND_TRIGGER:
                break
            if len(explosions) == 0:
                # ⭐ Run the coalesce-to-star phase, then exit
                coalesce_to_star(random, last_frame)
                break

        # Twinkle + base
        star_on[:] = twinkle(star_on, star_mask)
        frame = render_black_sky(star_mask, star_on)

        # Spawn small meteors
        if small_spawn_frames and f == small_spawn_frames[0]:
            small_spawn_frames.pop(0)
            sx = random.randint(WIDTH-6, WIDTH-1)
            sy = random.randint(-1, 3)
            sp = SMALL_SPEED * random.uniform(0.9, 1.1)
            vx, vy = -sp / math.sqrt(2), sp / math.sqrt(2)
            meteors.append(Meteor(sx, sy, vx, vy, trail_len=SMALL_TRAIL, thick=1,
                                  is_big=False, spawn_frame=f))

        # Spawn big meteors
        if big_spawn_frames and f == big_spawn_frames[0]:
            spawn_f = big_spawn_frames.pop(0)
            m = Meteor(big_x0, big_y0, big_vx, big_vy,
                       trail_len=BIG_TRAIL, thick=BIG_THICK,
                       is_big=True, id_tag=None, spawn_frame=spawn_f)
            meteors.append(m)

        # Update meteors
        for m in meteors: m.update()

        # Trigger explosions for big meteors
        for m in list(meteors):
            if m.is_big and m.alive:
                if maybe_boom_from(m, f):
                    booms_triggered += 1
                    meteors.remove(m)
                    if booms_triggered >= NUM_BIG:
                        ending = True  # 2 pops have happened

        meteors = [m for m in meteors if m.alive]

        # Update explosions
        for ex in explosions: ex.update(f)
        explosions[:] = [ex for ex in explosions if ex.alive]

        # Draw meteors and explosions
        for m in meteors: m.draw(frame)
        for ex in explosions: ex.draw(frame, f)

        # send + remember last frame for clean handover into star phase
        send_frame(frame)
        last_frame = frame.copy()
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
