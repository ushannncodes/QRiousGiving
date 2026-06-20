# anim_confetti.py
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
REVEAL_SEC    = float(os.getenv("REVEAL_SEC", "2.2")) # attract into HI!/♥
HOLD_SEC      = float(os.getenv("HOLD_SEC", "1.2"))   # hold the final shape

# Physics
SPAWN_RATE    = float(os.getenv("SPAWN_RATE", "40"))  # particles/sec
GRAVITY       = float(os.getenv("GRAVITY", "18.0"))   # px/s^2
BOUNCE_DAMP   = float(os.getenv("BOUNCE_DAMP", "0.45"))
MAX_PARTS     = int(os.getenv("MAX_PARTS", "220"))    # enough to make the shape
JITTER_X      = float(os.getenv("JITTER_X", "0.15"))  # horizontal drift
FRICTION_X    = float(os.getenv("FRICTION_X", "0.8")) # reduce x vel on bounce
STACK_MARGIN  = int(os.getenv("STACK_MARGIN", "0"))   # keep 0 for tight pile

# Target
TARGET        = os.getenv("TARGET", "HEART").upper()     # "HI" or "HEART"

# ----------------------------
# Utils: packing and send
# 0 = black (ON), 1 = white (OFF)
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
# Target mask builders (1=white bg, 0=black dot)
# -----------------------------
# --- add this helper once (above mask_heart) ---
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
    """
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    # Center & scale
    cx = (WIDTH - 1) / 2.0
    cy = (HEIGHT - 1) / 2.0 + 1.0   # +1px DOWN
    span = 1.25                     # base size; erosion will thin by ~1px

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
    frame[0, :] = 1
    frame[-1, :] = 1
    frame[:, 0] = 1
    frame[:, -1] = 1

    return frame


def mask_HI():
    """Blocky 'HI!' mask (centered), tuned for 28x28."""
    m = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    def rect(x0,y0,w,h):
        x1,y1 = x0+w, y0+h
        x0=max(0,x0); y0=max(0,y0); x1=min(WIDTH,x1); y1=min(HEIGHT,y1)
        if x1>x0 and y1>y0: m[y0:y1, x0:x1] = 0

    # layout box
    gw, gh = 22, 18
    x_off = (WIDTH - gw)//2
    y_off = (HEIGHT - gh)//2

    # H
    rect(x_off + 0, y_off + 0, 3, gh)   # left bar
    rect(x_off + 9, y_off + 0, 3, gh)   # right bar
    rect(x_off + 0, y_off + gh//2 - 2, 12, 4)  # crossbar

    # I
    rect(x_off + 15, y_off + 0, 3, gh)

    # !
    rect(x_off + 20, y_off + 0, 2, gh-4)  # main
    rect(x_off + 20, y_off + gh-3, 2, 3)  # dot

    return m

def target_mask():
    return mask_HI() if TARGET == "HI" else mask_heart()

# -----------------------------
# Particle system (subpixel)
# Each particle: x, y, vx, vy (floats)
# -----------------------------
class Part:
    __slots__ = ("x","y","vx","vy")
    def __init__(self, x, y, vx, vy):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy

# For piling we track column heights (integral “top” where a new dot can rest)
def compute_heights(occupancy):
    h = [HEIGHT for _ in range(WIDTH)]
    for x in range(WIDTH):
        for y in range(HEIGHT-1, -1, -1):
            if occupancy[y, x] == 0:
                h[x] = y
                break
    return h

def clamp(v, lo, hi): return lo if v < lo else hi if v > hi else v

# -----------------------------
# Main
# -----------------------------
def main():
    rng = random.Random()
    dt = 1.0 / FPS

    # Phase durations -> frame counts
    rain_frames   = int(RAIN_SEC   * FPS)
    settle_frames = int(SETTLE_SEC * FPS)
    reveal_frames = int(REVEAL_SEC * FPS)
    hold_frames   = int(HOLD_SEC   * FPS)

    # State
    parts = []
    occupancy = np.ones((HEIGHT, WIDTH), dtype=np.uint8)  # 1=white bg, 0=dot
    spawn_accum = 0.0

    tgt = target_mask()
    tgt_coords = [(x,y) for y in range(HEIGHT) for x in range(WIDTH) if tgt[y,x]==0]
    rng.shuffle(tgt_coords)  # random assignment target order

    # ------------- Phase A: Rain → Bounce → Pile -------------
    for f in range(rain_frames):
        # spawn
        spawn_accum += SPAWN_RATE * dt
        to_spawn = int(spawn_accum)
        if to_spawn > 0:
            spawn_accum -= to_spawn
            for _ in range(min(to_spawn, max(0, MAX_PARTS - len(parts)))):
                x = rng.uniform(0, WIDTH-1)
                y = -1.0
                vx = rng.uniform(-0.3, 0.3)
                vy = rng.uniform(0.0, 0.6)
                parts.append(Part(x,y,vx,vy))

        # clear dynamic layer (keep pile as 0s)
        frame = occupancy.copy()

        # column heights from pile
        heights = compute_heights(occupancy)

        # integrate
        for p in parts:
            # physics
            p.vy += GRAVITY * dt
            p.vx += rng.uniform(-JITTER_X, JITTER_X) * dt

            nx = p.x + p.vx
            ny = p.y + p.vy

            # collide with side walls (soft)
            if nx < 0:
                nx = 0; p.vx = -p.vx * 0.5
            elif nx > (WIDTH-1):
                nx = WIDTH-1; p.vx = -p.vx * 0.5

            # collide with the pile/ground
            ground_y = heights[int(clamp(round(nx),0,WIDTH-1))] - 1 - STACK_MARGIN
            if ny >= ground_y:
                # landed; stick if slow, else bounce
                if p.vy > 2.0:
                    ny = ground_y
                    p.vy = -p.vy * BOUNCE_DAMP
                    p.vx *= FRICTION_X
                    # if very gentle after bounce, convert to pile
                    if abs(p.vy) < 0.9:
                        gx, gy = int(round(nx)), int(round(ny))
                        if 0 <= gx < WIDTH and 0 <= gy < HEIGHT:
                            occupancy[gy, gx] = 0  # make it permanent
                            # move particle offscreen to retire
                            p.y = -9999
                            continue
                else:
                    # stick
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

    # ------------- Phase C: Reveal (attract to shape) -------------
    # Extract current loose dots = pile cells
    pile_coords = [(x,y) for y in range(HEIGHT) for x in range(WIDTH) if occupancy[y,x]==0]
    # choose as many targets as we have dots (or vice versa)
    k = min(len(pile_coords), len(tgt_coords))
    # one-to-one pairing (random already applied to tgt_coords)
    pairs = list(zip(rng.sample(pile_coords, k), tgt_coords[:k]))

    # Move via linear interpolation steps
    for step in range(max(1, reveal_frames)):
        t = (step + 1) / max(1.0, reveal_frames)
        frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
        for (sx,sy), (tx,ty) in pairs:
            x = int(round(sx + (tx - sx) * t))
            y = int(round(sy + (ty - sy) * t))
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                frame[y, x] = 0
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(dt)

    # ------------- Phase D: Hold final shape -------------
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
