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


GLYPHS = {
    "E": ["111", "100", "111", "100", "111"],
    "I": ["111", "010", "010", "010", "111"],
    "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"],
    "S": ["111", "100", "111", "001", "111"],
}


def _text_points(text, left, top):
    pts = []
    x = left
    for ch in text:
        glyph = GLYPHS.get(ch)
        if glyph is None:
            x += 2
            continue
        for r, row in enumerate(glyph):
            for c, v in enumerate(row):
                if v == "1":
                    pts.append((x + c, top + r))
        x += 4
    return pts


SMILE_TEXT_POINTS = _text_points("SMILE", 4, 8)


def _typewriter(f, y_offset=0):
    y_offset = round(y_offset)
    rect(f, 3, 21 + y_offset, 24, 25 + y_offset, fill=False)
    line(f, 4, 20 + y_offset, 23, 20 + y_offset)


def _smiley_points():
    pts = set()

    for i in range(72):
        a = math.tau * i / 72
        pts.add((round(14 + math.cos(a) * 8), round(13 + math.sin(a) * 8)))

    pts.update(
        {
            (8, 19),
            (9, 20),
            (10, 20),
            (18, 20),
            (19, 20),
            (20, 19),
        }
    )

    for cx in (10, 18):
        pts.add((cx, 10))
        pts.add((cx - 1, 10))
        pts.add((cx, 11))
        pts.add((cx - 1, 11))

    for i in range(24):
        a = math.pi * (0.18 + 0.64 * i / 23)
        pts.add((round(14 + math.cos(a) * 5), round(14 + math.sin(a) * 4)))

    return sorted((x, y) for x, y in pts if 0 <= x < 28 and 0 <= y < 28)


SMILEY_POINTS = _smiley_points()


def render(t):
    f = blank()
    p = min(1, t / DURATION_SEC)

    if p < 0.58:
        _typewriter(f)
        q = p / 0.58
        typed = int(q * 72)
        for i in range(typed):
            x = 5 + (i % 18)
            y = 5 + (i // 18) * 4
            if i % 5 == 0:
                rect(f, x, y, x + 1, y + 2, fill=False)
            else:
                dot(f, x, y)
        carriage = 5 + (typed % 18)
        line(f, carriage, 18, carriage + math.sin(t * 20) * 3, 6 + (typed // 18) * 4)
        if int(t * 9) % 2 == 0:
            burst(f, carriage, 18, 3, 7, t * 4, 1.5)
    elif p < 0.74:
        _typewriter(f)
        q = ease((p - 0.58) / 0.16)
        for i, (tx, ty) in enumerate(SMILE_TEXT_POINTS):
            sx = 5 + (i % 18)
            sy = 5 + (i // 18) * 3
            dot(f, sx + (tx - sx) * q, sy + (ty - sy) * q)
    elif p < 0.82:
        q = ease((p - 0.74) / 0.08)
        _typewriter(f, q * 8)
        for x, y in SMILE_TEXT_POINTS:
            dot(f, x, y)
    elif p < 0.91:
        q = ease((p - 0.82) / 0.09)
        count = max(len(SMILE_TEXT_POINTS), len(SMILEY_POINTS))
        for i in range(count):
            sx, sy = SMILE_TEXT_POINTS[i % len(SMILE_TEXT_POINTS)]
            tx, ty = SMILEY_POINTS[(i * 7) % len(SMILEY_POINTS)]
            dot(f, sx + (tx - sx) * q, sy + (ty - sy) * q)
    else:
        for x, y in SMILEY_POINTS:
            dot(f, x, y)
    return f


if __name__ == "__main__":
    run(render)
