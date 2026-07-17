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


def rect(frame, x0, y0, x1, y1, fill=False):
    if fill:
        for y in range(int(y0), int(y1) + 1):
            for x in range(int(x0), int(x1) + 1):
                dot(frame, x, y)
        return
    line(frame, x0, y0, x1, y0)
    line(frame, x1, y0, x1, y1)
    line(frame, x1, y1, x0, y1)
    line(frame, x0, y1, x0, y0)


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


def star_points(cx=13.5, cy=13.5, outer=10.5, inner=4.2):
    pts = []
    verts = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + math.tau * i / 10
        verts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    for i, (x0, y0) in enumerate(verts):
        x1, y1 = verts[(i + 1) % len(verts)]
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2)
        for s in range(steps + 1):
            q = s / max(steps, 1)
            pts.append((int(round(x0 + (x1 - x0) * q)), int(round(y0 + (y1 - y0) * q))))
    return sorted(set((x, y) for x, y in pts if 0 <= x < WIDTH and 0 <= y < HEIGHT))


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


def _skyline(f, hscale=1.0):
    heights = [5, 9, 6, 12, 8, 10, 7]
    x = 1
    for i, h in enumerate(heights):
        w = 3 if i % 2 else 4
        top = 26 - int(h * hscale)
        rect(f, x, top, x + w, 26, fill=False)
        if hscale > 0.8:
            for wy in range(top + 2, 25, 3):
                dot(f, x + 1, wy)
        x += w


def render(t):
    f = blank()
    p = max(0.0, min(1.0, t / DURATION_SEC))

    if p < 0.24:
        q = ease(p / 0.24)
        _skyline(f, q)
        for x in range(28):
            if (x + int(t * 6)) % 4 == 0:
                dot(f, x, 3 + (x * 7) % 12)
    elif p < 0.50:
        _skyline(f, 1)
        q = (p - 0.24) / 0.26
        x = 3 + q * 22
        y = 22 - 17 * math.sin(q * math.pi)
        angle = math.atan2(-17 * math.cos(q * math.pi), 22)
        dot(f, x, y, 1)
        for k in range(9):
            dot(f, x - math.cos(angle) * k, y - math.sin(angle) * k)
    elif p < 0.72:
        _skyline(f, 1)
        q = (p - 0.50) / 0.22
        for cx, cy, off in ((8, 8, 0), (20, 7, 0.18), (14, 13, 0.34)):
            r = max(0, (q - off) * 20)
            if r > 0:
                burst(f, cx, cy, min(r, 12), spokes=16, phase=t * 3 + off, tail=4)
                circle(f, cx, cy, min(r * 0.7, 7), phase=t * 4, skip=3)
    elif p < 0.88:
        q = ease((p - 0.72) / 0.16)
        _skyline(f, 1)
        pts = star_points(14, 10, outer=10, inner=4)
        for i, (tx, ty) in enumerate(pts):
            sx = 14 + 13 * math.cos(i * 2.399 + t)
            sy = 10 + 13 * math.sin(i * 2.399 + t)
            dot(f, sx + (tx - sx) * q, sy + (ty - sy) * q)
    else:
        _skyline(f, 1)
        for x, y in star_points(14, 10, outer=10, inner=4):
            dot(f, x, y)
        for i in range(14):
            if int(t * 9 + i) % 4 == 0:
                burst(f, 14, 10, 4 + (i % 4) * 2, spokes=8, phase=t + i, tail=1)
    return f


if __name__ == "__main__":
    run(render)
