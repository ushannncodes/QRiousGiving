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
    steps = int(max(abs(x1 - x0), abs(y1 - y0), 1) * 1.8)
    for i in range(steps + 1):
        q = i / max(steps, 1)
        dot(frame, x0 + (x1 - x0) * q, y0 + (y1 - y0) * q)


def burst(frame, cx, cy, radius, spokes=12, phase=0.0, tail=2.5):
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


def _cradle_points():
    pts = []
    for x in range(5, 24):
        pts.append((x, 6))
    for cx in (8, 11, 14, 17, 20):
        for a_i in range(14):
            a = a_i * math.tau / 14
            pts.append((cx + 1.8 * math.cos(a), 18 + 1.8 * math.sin(a)))
        pts.append((cx, 7))
        pts.append((cx, 10))
        pts.append((cx, 13))
        pts.append((cx, 16))
    return [(int(round(x)), int(round(y))) for x, y in pts]


CRADLE_POINTS = _cradle_points()


def render(t):
    f = blank()
    p = min(1, t / DURATION_SEC)
    pivot = (14, 3)
    line(f, 5, 3, 23, 3)

    if p < 0.64:
        q = p / 0.64
        for arm in range(3):
            a = math.sin(t * (1.2 + arm * 0.35)) * (0.9 - arm * 0.18)
            length = 9 + arm * 4
            x = pivot[0] + length * math.sin(a)
            y = pivot[1] + length * math.cos(a)
            line(f, pivot[0], pivot[1], x, y)
            dot(f, x, y, 1)
            for k in range(5):
                dot(f, x - k * math.sin(a), y - k * math.cos(a))
        if q > 0.35:
            burst(f, 14, 18, (q - 0.35) * 13, 12, t * 2, 3)
    elif p < 0.84:
        q = ease((p - 0.64) / 0.20)
        pts = CRADLE_POINTS
        for i, (tx, ty) in enumerate(pts):
            sx = 14 + 11 * math.sin(i * 2.399)
            sy = 18 + 7 * math.cos(i * 1.7)
            dot(f, sx + (tx - sx) * q, sy + (ty - sy) * q)
    else:
        for x, y in CRADLE_POINTS:
            dot(f, x, y)
        # Final balanced cradle with a subtle transfer pulse.
        pulse = int(t * 5) % 5
        if pulse in (0, 1):
            burst(f, 8 + pulse * 12, 18, 4, 8, t, 1.5)
    return f


if __name__ == "__main__":
    run(render)
