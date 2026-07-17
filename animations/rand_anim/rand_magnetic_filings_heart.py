# rand_magnetic_filings_heart.py
# Magnetic filings align to invisible fields, then crystallise into a heart.
# Standalone 28x28 flip-dot animation. 0 = black/on, 1 = white/off.

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
DURATION_SEC = float(os.getenv("MAGNETIC_FILINGS_SEC", "10.0"))


def ease(q):
    q = max(0.0, min(1.0, q))
    return q * q * (3.0 - 2.0 * q)


def blank():
    return np.ones((HEIGHT, WIDTH), dtype=np.uint8)


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


def send_to_panels(ser, panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
    ser.flush()


def _heart_mask_points():
    pts = []
    for y in range(2, 26):
        for x in range(2, 26):
            px = (x - 13.5) / 9.5
            py = (14.6 - y) / 8.6
            v = px * px + py * py - 1.0
            if v * v * v - px * px * py * py * py <= 0.0:
                pts.append((x, y))
    pts.sort(key=lambda p: (abs(p[0] - 13.5) + abs(p[1] - 14), p[1], p[0]))
    return pts


HEART_POINTS = _heart_mask_points()
HEART_SET = set(HEART_POINTS)


def _heart_lattice_points():
    pts = []
    for x, y in HEART_POINTS:
        boundary = any((x + dx, y + dy) not in HEART_SET for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        if boundary or (x + y) % 2 == 0:
            pts.append((x, y))
    pts.sort(key=lambda p: (p[1], p[0]))
    return pts


HEART_LATTICE = _heart_lattice_points()


def _filing_seed(i):
    # Low-discrepancy deterministic grain positions.
    x = 2.5 + ((i * 7) % 23)
    y = 2.5 + ((i * 11 + i // 3) % 23)
    jitter_x = 0.35 * math.sin(i * 1.7)
    jitter_y = 0.35 * math.sin(i * 2.3 + 1.0)
    return x + jitter_x, y + jitter_y


def _field_direction(x, y, poles):
    fx = fy = 0.0
    for px, py, strength in poles:
        dx = x - px
        dy = y - py
        d2 = dx * dx + dy * dy + 2.0
        inv = strength / d2
        fx += inv * dx
        fy += inv * dy
    a = math.atan2(fy, fx)
    return a


def _field_position(i, t, pull=1.0):
    base_x, base_y = _filing_seed(i)
    wobble = 0.7 * math.sin(t * 1.8 + i * 0.37)
    poles = [
        (8.5 + 1.4 * math.sin(t * 0.8), 12.0 + math.sin(t * 0.7), 1.0),
        (18.5 + 1.4 * math.cos(t * 0.8), 12.0 + math.cos(t * 0.7), -1.0),
    ]
    a = _field_direction(base_x, base_y, poles)
    lane = (i % 9) - 4
    x = base_x + math.cos(a) * lane * 0.45 * pull + math.cos(a + math.pi / 2) * wobble
    y = base_y + math.sin(a) * lane * 0.45 * pull + math.sin(a + math.pi / 2) * wobble
    return x, y, a


def _target_point(i):
    return HEART_LATTICE[(i * 17) % len(HEART_LATTICE)]


def render(t):
    frame = blank()
    p = max(0.0, min(1.0, t / max(DURATION_SEC, 0.001)))
    grain_count = 118

    if p < 0.24:
        q = p / 0.24
        # Loose filings on the plate begin to twitch and align.
        for i in range(grain_count):
            x0, y0 = _filing_seed(i)
            xf, yf, a = _field_position(i, t, pull=q)
            x = x0 + (xf - x0) * ease(q)
            y = y0 + (yf - y0) * ease(q)
            if i % 3 == 0:
                line(frame, x - math.cos(a) * 0.9, y - math.sin(a) * 0.9, x + math.cos(a) * 0.9, y + math.sin(a) * 0.9)
            else:
                dot(frame, x, y)
        circle(frame, 13.5, 13.5, 2 + 7 * q, phase=t * 2, skip=5)
    elif p < 0.50:
        q = (p - 0.24) / 0.26
        # Two invisible poles sweep through the field; filings form elegant flux lines.
        for i in range(grain_count):
            x, y, a = _field_position(i, t * 1.25, pull=1.2)
            x += 1.4 * math.sin(q * math.pi + i * 0.11)
            y += 0.9 * math.cos(q * math.pi * 1.3 + i * 0.17)
            if i % 2 == 0:
                line(frame, x - math.cos(a) * 1.2, y - math.sin(a) * 1.2, x + math.cos(a) * 1.2, y + math.sin(a) * 1.2)
            else:
                dot(frame, x, y)
        circle(frame, 8.5, 12.0, 2.0 + math.sin(t * 5), phase=t, skip=2)
        circle(frame, 18.5, 12.0, 2.0 + math.cos(t * 5), phase=-t, skip=2)
    elif p < 0.78:
        q = ease((p - 0.50) / 0.28)
        # The field collapses inward; grains slide into the final heart lattice.
        for i in range(grain_count):
            sx, sy, a = _field_position(i, t, pull=1.0)
            tx, ty = _target_point(i)
            swirl = (1.0 - q) * 3.0
            x = sx + (tx - sx) * q + swirl * math.sin(t * 3 + i * 0.23)
            y = sy + (ty - sy) * q + swirl * math.cos(t * 2.6 + i * 0.19)
            if q < 0.72 and i % 4 == 0:
                line(frame, x - math.cos(a) * 0.8, y - math.sin(a) * 0.8, x + math.cos(a) * 0.8, y + math.sin(a) * 0.8)
            else:
                dot(frame, x, y)
        circle(frame, 13.5, 14, 11 - 6 * q, phase=t * 3, skip=3)
    elif p < 0.90:
        q = (p - 0.78) / 0.12
        # A final magnetic snap locks the image in.
        step = max(1, int(7 - q * 5))
        for x, y in HEART_LATTICE[::step]:
            dot(frame, x, y)
        for r in range(2, int(13 * q) + 2, 3):
            circle(frame, 13.5, 14, r, phase=t * 4, skip=3)
    else:
        # Hold: the heart breathes with tiny edge glints, like settled metal.
        for x, y in HEART_LATTICE:
            dot(frame, x, y)
        for i in range(18):
            if int(t * 9 + i) % 6 < 2:
                a = i * math.tau / 18
                dot(frame, 13.5 + 11 * math.cos(a), 14 + 9 * math.sin(a))
    return frame


def main():
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        start = time.time()
        while time.time() - start < DURATION_SEC:
            frame = render(time.time() - start)
            send_to_panels(ser, pack_flipbytes(frame))
            time.sleep(FRAME_DT)
    finally:
        ser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[magnetic_filings_heart] stopped")
