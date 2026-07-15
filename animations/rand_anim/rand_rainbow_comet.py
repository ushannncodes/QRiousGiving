# rand_rainbow_comet.py
# Comets sweep across the grid, leave shimmering trails, then form a smile.

import math
import os
import random
import time

import numpy as np
import serial

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS = [1, 2, 3, 4]

HEIGHT, WIDTH = 28, 28
FPS = float(os.getenv("FPS", "55"))
FRAME_DT = 1.0 / max(FPS, 1.0)

SWEEP_SEC = float(os.getenv("COMET_SWEEP_SEC", "4.5"))
FORM_SEC = float(os.getenv("COMET_FORM_SEC", "1.4"))
HOLD_SEC = float(os.getenv("COMET_HOLD_SEC", "1.2"))
COMETS = int(os.getenv("COMETS", "5"))


def seed_random():
    seed = os.getenv("RANDOM_SEED")
    if seed:
        try:
            random.seed(int(seed))
        except ValueError:
            random.seed(seed)
    else:
        random.seed(int(time.time() * 1000) ^ os.getpid())


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


def dot(frame, x, y, radius=0):
    xi, yi = int(round(x)), int(round(y))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                xx, yy = xi + dx, yi + dy
                if 0 <= xx < WIDTH and 0 <= yy < HEIGHT:
                    frame[yy, xx] = 0


def make_smile_points():
    pts = []
    for x in range(8, 20):
        y = int(round(17 + 3.5 * math.sin((x - 8) / 11 * math.pi)))
        pts.append((x, y))
    pts += [(9, 10), (10, 10), (18, 10), (19, 10)]
    for a in np.linspace(0, math.tau, 24, endpoint=False):
        x = int(round(13.5 + 11.0 * math.cos(a)))
        y = int(round(13.5 + 11.0 * math.sin(a)))
        pts.append((x, y))
    return pts


SMILE_POINTS = make_smile_points()


def render_sweeps(t, trail):
    trail[trail > 0] -= 1
    for i in range(COMETS):
        phase = (t * (0.34 + i * 0.045) + i / COMETS) % 1.0
        side = i % 4
        wiggle = math.sin(t * (2.0 + i * 0.3) + i) * 2.7
        if side == 0:
            x = -4 + phase * 36
            y = 5 + i * 4 + wiggle
        elif side == 1:
            x = 24 - phase * 36
            y = 4 + i * 4 - wiggle
        elif side == 2:
            x = 4 + i * 5 + wiggle
            y = -4 + phase * 36
        else:
            x = 24 - i * 4 - wiggle
            y = 31 - phase * 36

        dx = math.cos(t + i)
        dy = math.sin(t * 0.8 + i)
        for back in range(8):
            bx = x - dx * back * 0.85
            by = y - dy * back * 0.85
            xi, yi = int(round(bx)), int(round(by))
            if 0 <= xi < WIDTH and 0 <= yi < HEIGHT:
                trail[yi, xi] = max(trail[yi, xi], 8 - back)

    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    frame[trail > 0] = 0
    return frame


def render_form(t, q, trail):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    for i, (tx, ty) in enumerate(SMILE_POINTS):
        a = i * 2.399 + t * 1.7
        orbit = 13.0 - 6.0 * q
        sx = 13.5 + orbit * math.cos(a)
        sy = 13.5 + orbit * math.sin(a)
        ease = q * q * (3 - 2 * q)
        x = sx + (tx - sx) * ease
        y = sy + (ty - sy) * ease
        dot(frame, x, y)

    if q > 0.72:
        for x, y in SMILE_POINTS:
            if random.random() < (q - 0.72) / 0.28:
                dot(frame, x, y)
    return frame


def render_hold(t):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    for x, y in SMILE_POINTS:
        dot(frame, x, y)
    if int(t * 5) % 6 in (0, 1):
        dot(frame, 10, 10, 1)
        dot(frame, 19, 10, 1)
    return frame


def main():
    seed_random()
    trail = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        start = time.time()
        total = SWEEP_SEC + FORM_SEC + HOLD_SEC
        while time.time() - start < total:
            t = time.time() - start
            if t < SWEEP_SEC:
                frame = render_sweeps(t, trail)
            elif t < SWEEP_SEC + FORM_SEC:
                q = (t - SWEEP_SEC) / max(FORM_SEC, 0.001)
                frame = render_form(t, q, trail)
            else:
                frame = render_hold(t)
            send_frame(ser, frame)
            time.sleep(FRAME_DT)
    finally:
        ser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[rainbow_comet] stopped")
