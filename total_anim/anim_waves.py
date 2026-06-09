# anim_waves.py
import numpy as np
import time
import math
import random
import serial
import os

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
HEIGHT, WIDTH   = 28, 28
FPS             = float(os.getenv("FPS", "50"))

# Intro sequence timing
INTRO_WHITE_SEC           = float(os.getenv("INTRO_WHITE_SEC", "1.0"))
COUNTDOWN_STEP_SEC        = float(os.getenv("COUNTDOWN_STEP_SEC", "0.5"))  # per number (5..1) #change this later to 1.5
COUNTDOWN_RING_RADIUS     = int(os.getenv("COUNTDOWN_RING_RADIUS", "11"))
COUNTDOWN_RING_THICKNESS  = int(os.getenv("COUNTDOWN_RING_THICKNESS", "3"))

# Waves loop timing
DURATION_SEC    = float(os.getenv("DURATION", "12"))  # waves loop length before repeating
FRAME_DT        = 1.0 / FPS
TOTAL_FRAMES    = int(DURATION_SEC * FPS)

# -----------------------------
# Wave Style Controls (play here)
# -----------------------------
SEED = 42                 # change for a different “sea”
random.seed(SEED)
np.random.seed(SEED)

# (amplitude_px, wavelength_px, speed_px_per_sec, direction_deg, phase_deg, steepness_gamma)

WAVE_LAYERS = [
    (6.0, 10.0, 10.0,   0.0,   0.0, 2.2),   # main big crest
    (3.0,  8.0, 14.0,  25.0,  30.0, 2.0),   # secondary diagonal chop
    (1.5,  6.0, 20.0, -20.0,  60.0, 2.5),   # fine froth
]

# WAVE_LAYERS = [
#     (4.5, 22.0,  8.0,   0.0,  0.0, 1.3),
#     (3.0, 16.0, 12.0,  25.0, 45.0, 1.6),
#     (1.5, 10.0, 18.0, -10.0, 90.0, 1.9),
# ]

SKEW_X = 0.18
SKEW_Y = 0.00

# Solid-white background; waves are drawn as black.
SEA_THRESHOLD      = float(os.getenv("SEA_THRESHOLD", "0.5"))  # higher => less sea coverage
CREST_THRESHOLD    = 0.62
CREST_BOOST        = 0.40
FOAM_SPECKLE_RATE  = 0.30
FOAM_SOLIDIFY      = 0.25
VIGNETTE_STRENGTH  = 0.15

HORIZON_BAND_ENABLE  = True
HORIZON_BAND_TOP     = 2
HORIZON_BAND_DARKEN  = 0.35

BOAT_ENABLE = True
BOAT_COL    = 18
BOAT_ROW    = 12
BOAT_W      = 7
BOAT_H      = 2

# -----------------------------
# Utilities
# -----------------------------
def pack_flipbytes(frame28):
    """
    Pack a 28x28 array (0=black, 1=white) into 4 panel payloads.
    Column-major, 7 rows per byte, panels are 7 rows each (top->bottom).
    """
    panels = []
    for p in range(4):
        row_off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                bit = int(frame28[row_off + y, x])
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(pkt)
    ser.flush()

# -----------------------------
# WAVE FIELD
# -----------------------------
def _layer_field(xx, yy, t, amp, lam, speed, theta_deg, phase_deg, gamma):
    theta = math.radians(theta_deg)
    k     = (2.0 * math.pi) / lam
    omega = k * speed
    phase = math.radians(phase_deg)
    s =  xx * math.cos(theta) + yy * math.sin(theta)
    arg = (k * s) - (omega * t) + phase
    base = np.sin(arg)
    steep = np.sign(base) * (np.abs(base) ** gamma)
    return amp * steep

def _compose_waves(t):
    y = np.arange(HEIGHT).reshape(-1, 1).astype(np.float32)
    x = np.arange(WIDTH).reshape(1, -1).astype(np.float32)

    cx = (x - (WIDTH  - 1)/2.0) + SKEW_X * (y - (HEIGHT - 1)/2.0)
    cy = (y - (HEIGHT - 1)/2.0) + SKEW_Y * (x - (WIDTH  - 1)/2.0)

    field = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    for (amp, lam, speed, deg, ph, gamma) in WAVE_LAYERS:
        field += _layer_field(cx, cy, t, amp, lam, speed, deg, ph, gamma)

    max_abs = sum(abs(a) for (a, *_rest) in WAVE_LAYERS)
    if max_abs < 1e-6:
        norm = np.zeros_like(field)
    else:
        norm = (field + max_abs) / (2.0 * max_abs)

    if VIGNETTE_STRENGTH > 0:
        yy = (y - (HEIGHT - 1)/2.0) / (HEIGHT/2.0)
        xx = (x - (WIDTH  - 1)/2.0) / (WIDTH/2.0)
        rad2 = (xx**2 + yy**2)
        vignette = 1.0 - VIGNETTE_STRENGTH * np.clip(rad2, 0, 1)
        norm *= vignette

    if HORIZON_BAND_ENABLE and HORIZON_BAND_TOP > 0:
        h = max(0, min(HEIGHT, HORIZON_BAND_TOP))
        norm[:h, :] *= (1.0 - HORIZON_BAND_DARKEN)

    return np.clip(norm, 0.0, 1.0)

def _foam_mask(norm_field, t):
    crest = norm_field >= CREST_THRESHOLD
    if not crest.any():
        return crest
    rnd = np.random.rand(*norm_field.shape)
    speck = rnd < FOAM_SPECKLE_RATE
    foam = crest & speck
    if FOAM_SOLIDIFY > 0:
        rnd2 = np.random.rand(HEIGHT-1, WIDTH-1) < FOAM_SOLIDIFY
        big = np.zeros_like(foam)
        big[:-1, :-1] |= rnd2
        big[1:,  :-1] |= rnd2
        big[:-1, 1: ] |= rnd2
        big[1:,  1: ] |= rnd2
        foam |= (crest & big)
    return foam

def _boat_silhouette(frame_bw):
    r0 = max(0, min(HEIGHT-1, BOAT_ROW))
    c0 = max(0, min(WIDTH-1,  BOAT_COL))
    for r in range(r0, min(HEIGHT, r0 + BOAT_H)):
        for c in range(c0, min(WIDTH,  c0 + BOAT_W)):
            ramp = abs((c - (c0 + BOAT_W/2.0)) / (BOAT_W/2.0))
            if r == r0 and ramp < 0.95:
                frame_bw[r, c] = 0
            if r == r0+1 and ramp < 0.65:
                frame_bw[r, c] = 0
    return frame_bw

# -----------------------------
# WAVE SILHOUETTE RENDERER
# -----------------------------
# Each crest: (x0, height, width, lean, speed)
CRESTS = [
    (7.0, 15.0, 4.0, 1.2,  6.0),   # left claw
    (14.0,12.0, 3.6, 0.9,  7.5),   # middle
    (22.0,10.0, 3.2, 0.8,  9.0),   # right
]

SEA_BASELINE_Y = 25
SEA_TILT       = -0.2
FOAM_BAND_PX   = 2
FOAM_RATE      = 0.25
FOAM_ONLY_BOTTOM = True
FUJI_ENABLE    = False

def _crest_curve_y(x, t, x0,h,w,lean,speed):
    drift = speed * t
    xp = x - (x0 + drift)
    g = math.exp(-(xp*xp)/(2*w*w))
    y = SEA_BASELINE_Y - h*g
    y -= lean * g * (x-x0)/max(1.0,w)
    wob = 0.6*math.sin((x*0.7) + 1.3*t)
    return y + wob + SEA_TILT*x

def render_waves_frame(t):
    bw = np.ones((HEIGHT,WIDTH),dtype=np.uint8)
    crest_y = np.full(WIDTH, SEA_BASELINE_Y, dtype=np.float32)
    for x in range(WIDTH):
        yx = SEA_BASELINE_Y + SEA_TILT*x
        for (x0,h,w,lean,speed) in CRESTS:
            yx = min(yx, _crest_curve_y(x,t,x0,h,w,lean,speed))
        crest_y[x] = yx
    # fill below crest
    for x in range(WIDTH):
        cy=int(round(crest_y[x])); cy=max(-1,min(HEIGHT-1,cy))
        if cy+1<HEIGHT: bw[cy+1:,x]=0
    # foam speckles
    for x in range(WIDTH):
        cy=int(round(crest_y[x]))
        for d in range(1,FOAM_BAND_PX+1):
            y=cy+d
            if 0<=y<HEIGHT:
                if FOAM_ONLY_BOTTOM and y<HEIGHT//3: continue
                if random.random()<FOAM_RATE: bw[y,x]=0
    # tiny Fuji
    if FUJI_ENABLE:
        for y in range(16,21):
            for x in range(12,16):
                if y>=20-(x-12):
                    bw[y,x]=1  # keep white
    return bw


# -----------------------------
# COUNTDOWN DIGITS (5x7 bitmaps)
# -----------------------------
DIGITS_5x7 = {
    '1': [
        "00100",
        "01100",
        "00100",
        "00100",
        "00100",
        "00100",
        "01110",
    ],
    '2': [
        "01110",
        "10001",
        "00001",
        "00010",
        "00100",
        "01000",
        "11111",
    ],
    '3': [
        "11110",
        "00001",
        "00001",
        "01110",
        "00001",
        "00001",
        "11110",
    ],
    '4': [
        "00010",
        "00110",
        "01010",
        "10010",
        "11111",
        "00010",
        "00010",
    ],
    '5': [
        "11111",
        "10000",
        "10000",
        "11110",
        "00001",
        "00001",
        "11110",
    ],
}

# -----------------------------
# COUNTDOWN DIGITS (7x9 bitmaps) — bigger & crisper
# -----------------------------
DIGITS_7x9 = {
    '1': [
        "0011000",
        "0111000",
        "0011000",
        "0011000",
        "0011000",
        "0011000",
        "0011000",
        "0011000",
        "1111110",
    ],
    '2': [
        "0111110",
        "1100011",
        "0000011",
        "0000110",
        "0001100",
        "0011000",
        "0110000",
        "1100000",
        "1111111",
    ],
    '3': [
        "1111110",
        "0000011",
        "0000011",
        "0011110",
        "0000011",
        "0000011",
        "0000011",
        "1100011",
        "0111110",
    ],
    '4': [
        "0001100",
        "0011100",
        "0111100",
        "1101100",
        "1001100",
        "1111111",
        "0001100",
        "0001100",
        "0001100",
    ],
    '5': [
        "1111111",
        "1100000",
        "1100000",
        "1111110",
        "0000011",
        "0000011",
        "0000011",
        "1100011",
        "0111110",
    ],
}

def blit_digit_7x9(frame, ch, top, left):
    glyph = DIGITS_7x9.get(ch)
    if glyph is None:
        return frame
    for r, row in enumerate(glyph):
        yy = top + r
        if yy < 0 or yy >= HEIGHT: 
            continue
        for c, val in enumerate(row):
            xx = left + c
            if xx < 0 or xx >= WIDTH: 
                continue
            if val == '1':
                frame[yy, xx] = 0  # black
    return frame


def blit_digit_5x7(frame, ch, top, left):
    glyph = DIGITS_5x7.get(ch)
    if glyph is None: return frame
    for r, row in enumerate(glyph):
        yy = top + r
        if yy < 0 or yy >= HEIGHT: continue
        for c, val in enumerate(row):
            xx = left + c
            if xx < 0 or xx >= WIDTH: continue
            if val == '1':
                frame[yy, xx] = 0  # black
    return frame

# -----------------------------
# COUNTDOWN RING
# -----------------------------
def _disk_offsets(radius_pix):
    """Return integer (dy,dx) offsets inside a filled disk of given radius."""
    r = max(0, int(round(radius_pix)))
    offs = []
    rr = r * r
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy*dy + dx*dx <= rr:
                offs.append((dy, dx))
    return offs

# Cache brush based on COUNTDOWN_RING_THICKNESS (odd looks best)
_BRUSH = _disk_offsets(max(1, COUNTDOWN_RING_THICKNESS // 2))

def _make_brush(thick_px: int):
    """
    Return integer (dy, dx) offsets for a circular brush whose *visual* diameter
    matches thick_px (so thick_px=1 -> single pixel; =2 -> small disk; etc).
    """
    if thick_px <= 1:
        return [(0, 0)]
    r = (thick_px - 1) / 2.0
    rceil = int(math.ceil(r))
    offs = []
    r2 = r * r
    for dy in range(-rceil, rceil + 1):
        for dx in range(-rceil, rceil + 1):
            if (dy*dy + dx*dx) <= r2 + 1e-9:
                offs.append((dy, dx))
    return offs

# Cache brush once; update if you change COUNTDOWN_RING_THICKNESS at runtime
_BRUSH = _make_brush(COUNTDOWN_RING_THICKNESS)

def render_countdown_frame(number, frac_remaining):
    """
    number: int in [1..5]
    frac_remaining: 1.0 -> 0.0 over the second
    Draws a *thin, precise* anti-clockwise arc starting at 12 o'clock.
    Uses 7x9 digits centered on the display.
    """
    bw = np.ones((HEIGHT, WIDTH), dtype=np.uint8)  # solid white bg
    cy, cx = HEIGHT // 2, WIDTH // 2
    r = COUNTDOWN_RING_RADIUS

    # arc length (anti-clockwise) starting at 12 o'clock
    arc_len = max(0.0, min(1.0, frac_remaining)) * 2.0 * math.pi
    if arc_len > 0:
        a = -math.pi / 2.0            # start at 12 o'clock
        a_end = a + arc_len
        # step ~ every ~0.6 px along circumference for smoothness
        step = (1.0 / max(6.0, r * 8.0)) * 2.0 * math.pi
        brush = _BRUSH
        while a <= a_end + 1e-6:
            yy = int(round(cy + r * math.sin(a)))
            xx = int(round(cx + r * math.cos(a)))
            for dy, dx in brush:
                y = yy + dy
                x = xx + dx
                if 0 <= y < HEIGHT and 0 <= x < WIDTH:
                    bw[y, x] = 0
            a += step

    # 7x9 digit (centered)
    num_ch = str(number)
    top  = cy - 4         # 9 rows tall
    left = cx - 3         # 7 cols wide
    bw = blit_digit_7x9(bw, num_ch, top, left)
    return bw



# -----------------------------
# INTRO (solid white)
# -----------------------------
def render_all_white():
    return np.ones((HEIGHT, WIDTH), dtype=np.uint8)

# -----------------------------
# MAIN LOOP
# -----------------------------
def main():
    try:
        # 1) Solid-white intro
        t0 = time.time()
        while True:
            now = time.time()
            if (now - t0) >= INTRO_WHITE_SEC:
                break
            frame = render_all_white()
            panels = pack_flipbytes(frame)
            send_to_panels(panels)
            time.sleep(FRAME_DT)

        # 2) Countdown 5 -> 1 with anti-clockwise shrinking arc
        for n in range(5, 0, -1):
            sec_start = time.time()
            while True:
                now = time.time()
                elapsed = now - sec_start
                frac_remaining = max(0.0, 1.0 - (elapsed / COUNTDOWN_STEP_SEC))
                frame = render_countdown_frame(n, frac_remaining)
                panels = pack_flipbytes(frame)
                send_to_panels(panels)
                time.sleep(FRAME_DT)
                if elapsed >= COUNTDOWN_STEP_SEC:
                    break

        # 3) Waves loop (repeat)
        start = time.time()
        while True:
            t = (time.time() - start) % DURATION_SEC
            frame = render_waves_frame(t)
            panels = pack_flipbytes(frame)
            send_to_panels(panels)
            time.sleep(FRAME_DT)

    except KeyboardInterrupt:
        print("\n[WAVES] Stopping…")
    finally:
        ser.close()

if __name__ == "__main__":
    main()
