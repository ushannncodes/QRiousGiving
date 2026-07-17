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


def circle(frame, cx, cy, r, phase=0.0, skip=1):
    pts = max(16, int(abs(r) * 9))
    for i in range(pts):
        if skip > 1 and i % skip:
            continue
        a = phase + math.tau * i / pts
        dot(frame, cx + r * math.cos(a), cy + r * math.sin(a))


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


def flower_points():
    pts = []
    for y in range(2, 27):
        for x in range(2, 26):
            px, py = x - 13.5, y - 14.0
            r = math.hypot(px, py)
            a = math.atan2(py, px)
            petal = 5.5 + 3.1 * abs(math.sin(5 * a))
            if r <= 2.4 or (2.3 <= r <= petal and abs(math.sin(5 * a)) > 0.35):
                pts.append((x, y))
    return pts


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


def render(t):
    f = blank()
    p = max(0.0, min(1.0, t / DURATION_SEC))
    cx = cy = 13.5

    if p < 0.36:
        q = p / 0.36
        for arm in range(5):
            for i in range(34):
                u = i / 33
                r = 13.5 * (1 - q) + 1.5 + 9.0 * u * q
                a = arm * math.tau / 5 + u * 2.7 * math.tau + t * 3.2
                dot(f, cx + r * math.cos(a), cy + r * math.sin(a))
        circle(f, cx, cy, 2 + q * 3, phase=t * 4, skip=2)
    elif p < 0.56:
        q = (p - 0.36) / 0.20
        burst(f, cx, cy, 2 + q * 16, spokes=22, phase=t * 4, tail=5)
        for r in (3 + q * 8, 7 + q * 6):
            circle(f, cx, cy, r, phase=t * 5, skip=2)
    elif p < 0.82:
        q = ease((p - 0.56) / 0.26)
        pts = flower_points()
        for i, (tx, ty) in enumerate(pts):
            a = i * 2.399 + t * 3.0
            sx = cx + (13 - 10 * q) * math.cos(a)
            sy = cy + (13 - 10 * q) * math.sin(a)
            dot(f, sx + (tx - sx) * q, sy + (ty - sy) * q)
        burst(f, cx, cy, 5 + q * 8, spokes=14, phase=t * 2, tail=2)
    else:
        for x, y in flower_points():
            dot(f, x, y)
        for i in range(18):
            if (i + int(t * 8)) % 4 in (0, 1):
                a = i * math.tau / 18 + t
                dot(f, cx + 12 * math.cos(a), cy + 11 * math.sin(a))
    return f


if __name__ == "__main__":
    run(render)
