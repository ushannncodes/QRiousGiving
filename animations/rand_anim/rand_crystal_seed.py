import math
import os
import time

import numpy as np
import serial

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]
HEIGHT, WIDTH = 28, 28
FPS = float(os.getenv("FPS", "55"))
FRAME_DT = 1.0 / max(FPS, 1.0)
DURATION_SEC = 10.0


def blank():
    return np.ones((HEIGHT, WIDTH), dtype=np.uint8)


def ease(q):
    q = max(0.0, min(1.0, q))
    return q * q * (3.0 - 2.0 * q)


def dot(frame, x, y, r=0):
    xi, yi = int(round(x)), int(round(y))
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                xx, yy = xi + dx, yi + dy
                if 0 <= xx < WIDTH and 0 <= yy < HEIGHT:
                    frame[yy, xx] = 0


def line(frame, x0, y0, x1, y1):
    steps = int(max(abs(x1 - x0), abs(y1 - y0), 1) * 1.6)
    for i in range(steps + 1):
        q = i / max(steps, 1)
        dot(frame, x0 + (x1 - x0) * q, y0 + (y1 - y0) * q)


def burst(frame, cx, cy, radius, spokes=14, phase=0.0, tail=2.5):
    for i in range(spokes):
        a = phase + math.tau * i / spokes
        r0 = max(0.0, radius - tail)
        line(
            frame,
            cx + r0 * math.cos(a),
            cy + r0 * math.sin(a),
            cx + radius * math.cos(a),
            cy + radius * math.sin(a),
        )


def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        row_off = p * 7
        data = bytearray()
        for x in range(WIDTH):
            byte = 0
            for y in range(7):
                byte |= (int(frame28[row_off + y, x]) & 1) << y
            data.append(byte)
        panels.append(data)
    return panels


def send_frame(ser, frame):
    for addr, data in zip(PANEL_ADDRS, pack_flipbytes(frame)):
        ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
    ser.flush()


def run(render_fn):
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        start = time.time()
        while time.time() - start < DURATION_SEC:
            send_frame(ser, render_fn(time.time() - start))
            time.sleep(FRAME_DT)
    finally:
        ser.close()


SEEDS = [(13.5, 14.0, -math.pi / 2, 7.5, 0)]


def _branches():
    branches = []

    def grow(x, y, a, length, depth):
        if depth > 4 or length < 1.8:
            return
        x2 = x + length * math.cos(a)
        y2 = y + length * math.sin(a)
        branches.append((x, y, x2, y2, depth))
        spread = 0.55 - depth * 0.055
        grow(x2, y2, a - spread, length * 0.68, depth + 1)
        grow(x2, y2, a + spread, length * 0.68, depth + 1)
        if depth < 3:
            grow(x2, y2, a + math.pi + spread * 0.45, length * 0.45, depth + 1)

    for a in (-math.pi / 2, -math.pi / 6, math.pi / 6, math.pi / 2, 5 * math.pi / 6, 7 * math.pi / 6):
        grow(13.5, 14.0, a, 6.8, 0)
    return branches


BRANCHES = _branches()


def _draw_branch(f, br, q):
    x0, y0, x1, y1, depth = br
    line(f, x0, y0, x0 + (x1 - x0) * q, y0 + (y1 - y0) * q)


def render(t):
    f = blank()
    p = max(0.0, min(1.0, t / DURATION_SEC))

    if p < 0.20:
        q = p / 0.20
        dot(f, 13.5, 14, 1)
        for r in range(2, int(12 * q) + 1, 3):
            burst(f, 13.5, 14, r, spokes=6, phase=t * 2, tail=1.5)
    elif p < 0.66:
        q = (p - 0.20) / 0.46
        # Depth-gated fractal growth, like ice finding low-energy paths.
        for br in BRANCHES:
            depth = br[4]
            start = depth * 0.15
            local = max(0.0, min(1.0, (q - start) / 0.22))
            if local > 0:
                _draw_branch(f, br, ease(local))
        # Active crystalline front glints.
        for i, br in enumerate(BRANCHES):
            depth = br[4]
            start = depth * 0.15
            local = max(0.0, min(1.0, (q - start) / 0.22))
            if 0.15 < local < 1.0:
                x0, y0, x1, y1, _ = br
                dot(f, x0 + (x1 - x0) * local, y0 + (y1 - y0) * local, 1 if i % 4 == 0 else 0)
    elif p < 0.84:
        q = (p - 0.66) / 0.18
        for br in BRANCHES:
            _draw_branch(f, br, 1.0)
        burst(f, 13.5, 14, 3 + q * 12, spokes=18, phase=t * 3, tail=4)
        for br in BRANCHES[::3]:
            x0, y0, x1, y1, _ = br
            dot(f, x1, y1)
    else:
        for br in BRANCHES:
            _draw_branch(f, br, 1.0)
        for i, br in enumerate(BRANCHES[::2]):
            if int(t * 8 + i) % 5 < 2:
                x0, y0, x1, y1, _ = br
                dot(f, x1, y1, 1)
    return f


if __name__ == "__main__":
    run(render)
