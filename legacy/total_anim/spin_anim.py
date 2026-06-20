# anim_spiral_spin.py
import numpy as np
import time
import serial
import math
import random

# -----------------------------
# Flipdot settings (same as anim.py)
# -----------------------------
SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE   = 57600
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH = 28, 28

# Main spiral: looks hypnotic at ~4–6s, 45–60 FPS
DURATION_SEC       = 2.8
FPS                = 60
FRAME_INTERVAL     = 1.0 / FPS
TOTAL_FRAMES       = int(DURATION_SEC * FPS)

# Spiral look & feel
NUM_ARMS           = 3        # 1–4 looks good on 28x28
ARM_THICKNESS_PX   = 1.25     # thickness around the spiral curve
SPIRAL_TIGHTNESS   = 2      # larger = tighter coils
SPIRAL_SPEED_TURNS = 3      # how many rotations over the whole animation
RANDOM_JITTER      = 0.1     # subtle wobble to add life

# End card: circle grow + fill
END_CIRCLE_SEC     = 1.2
END_FILL_SEC       = 0.6

# -----------------------------
# Helpers (same packing/sending as anim.py)
# -----------------------------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = frame28[offset + y, x] & 1
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
# Geometry utils
# -----------------------------
def draw_spiral_frame(t_norm):
    """
    Render NUM_ARMS Archimedean-like spirals that expand outward
    while rotating. t_norm in [0..1].
    0=black (ON), 1=white (OFF) to match your convention.
    """
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx = (WIDTH - 1) / 2.0
    cy = (HEIGHT - 1) / 2.0
    max_r = math.hypot(cx, cy)  # corner radius (~19.8)

    # Spiral base rotation over time (in radians)
    spin = 2 * math.pi * SPIRAL_SPEED_TURNS * t_norm

    # We’ll sample each arm curve at many theta values and stamp pixels near the curve
    # r = a + b*theta   (we use only b; a=0). We clamp to screen extents.
    b = (max_r / (SPIRAL_TIGHTNESS * math.pi))  # “tightness” factor
    thetas = np.linspace(0, SPIRAL_TIGHTNESS * 2 * math.pi, 500)

    for arm in range(NUM_ARMS):
        arm_offset = (2 * math.pi / NUM_ARMS) * arm
        # slight organic jitter per-arm
        jitter = (random.random() - 0.5) * RANDOM_JITTER

        for th in thetas:
            r = b * th
            # rotate whole spiral over time, then shift each arm
            ang = th + spin + arm_offset + jitter

            x = int(round(cx + r * math.cos(ang)))
            y = int(round(cy + r * math.sin(ang)))

            # thickness: stamp a tiny disc
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                draw_disc(frame, x, y, ARM_THICKNESS_PX, val=0)
    return frame

def draw_disc(frame, x, y, radius, val=0):
    rr = int(math.ceil(radius))
    for dy in range(-rr, rr + 1):
        yy = y + dy
        if 0 <= yy < HEIGHT:
            for dx in range(-rr, rr + 1):
                xx = x + dx
                if 0 <= xx < WIDTH:
                    if (dx*dx + dy*dy) <= radius * radius:
                        frame[yy, xx] = val

def draw_circle_outline(frame, cx, cy, r, val=0, thickness=1.2):
    """
    Midpoint-ish circle: mark pixels whose radial distance is within 'thickness' of r.
    """
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - cx, y - cy)
            if abs(d - r) <= thickness:
                frame[y, x] = val

def draw_circle_fill(frame, cx, cy, r, val=0):
    r2 = r * r
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if (x - cx)**2 + (y - cy)**2 <= r2:
                frame[y, x] = val

# -----------------------------
# Main animation
# -----------------------------
def run_spiral_spin():
    # Phase 1: Spirals expand + rotate
    for i in range(TOTAL_FRAMES):
        t_norm = i / max(TOTAL_FRAMES - 1, 1)
        frame = draw_spiral_frame(t_norm)
        panels = pack_flipbytes(frame)
        send_to_panels(panels)
        time.sleep(FRAME_INTERVAL)

    # # Phase 2: Grow a circle outline from center to edges
    # cx = (WIDTH - 1) / 2.0
    # cy = (HEIGHT - 1) / 2.0
    # max_r = math.hypot(cx, cy)

    # grow_frames = max(1, int(END_CIRCLE_SEC * FPS))
    # for i in range(grow_frames):
    #     frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    #     r = (i + 1) / grow_frames * max_r
    #     draw_circle_outline(frame, cx, cy, r, val=0, thickness=1.2)
    #     panels = pack_flipbytes(frame)
    #     send_to_panels(panels)
    #     time.sleep(FRAME_INTERVAL)

    # # Phase 3: Fill the circle inward (quick “whoomp”)
    # fill_frames = max(1, int(END_FILL_SEC * FPS))
    # for i in range(fill_frames):
    #     frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    #     # Fill progresses from edge toward center
    #     r = max_r * (1.0 - (i + 1) / fill_frames)
    #     # Start with full black, then carve out white hole shrinking to 0
    #     draw_circle_fill(frame, cx, cy, max_r, val=0)
    #     if r > 0.5:
    #         draw_circle_fill(frame, cx, cy, r, val=1)
    #     panels = pack_flipbytes(frame)
    #     send_to_panels(panels)
    #     time.sleep(FRAME_INTERVAL)

if __name__ == "__main__":
    try:
        run_spiral_spin()
    except KeyboardInterrupt:
        print("\n[SpiralSpin] interrupted.")
    finally:
        try:
            ser.close()
        except Exception:
            pass
