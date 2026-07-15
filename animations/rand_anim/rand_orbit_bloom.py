# rand_orbit_bloom.py
# Orbiting dots spiral inward, bloom into a flower, then sparkle around it.

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

DURATION_SEC = float(os.getenv("ORBIT_BLOOM_SEC", "6.5"))
HOLD_SEC = float(os.getenv("ORBIT_BLOOM_HOLD_SEC", "1.2"))
PARTICLES = int(os.getenv("ORBIT_BLOOM_PARTICLES", "34"))


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
    xi = int(round(x))
    yi = int(round(y))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                xx, yy = xi + dx, yi + dy
                if 0 <= xx < WIDTH and 0 <= yy < HEIGHT:
                    frame[yy, xx] = 0


def make_flower_mask():
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx, cy = 13.5, 14.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            px = x - cx
            py = y - cy
            r = math.hypot(px, py)
            a = math.atan2(py, px)
            petal_edge = 5.8 + 2.9 * abs(math.sin(5 * a))
            center = r <= 2.6
            petals = 2.3 <= r <= petal_edge and abs(math.sin(5 * a)) > 0.36
            stem = 13 <= x <= 14 and 18 <= y <= 25
            leaf_l = ((x - 10.5) / 3.7) ** 2 + ((y - 21.0) / 1.8) ** 2 <= 1.0
            leaf_r = ((x - 17.0) / 3.4) ** 2 + ((y - 22.0) / 1.7) ** 2 <= 1.0
            if center or petals or stem or leaf_l or leaf_r:
                frame[y, x] = 0
    frame[[0, -1], :] = 1
    frame[:, [0, -1]] = 1
    return frame


FLOWER = make_flower_mask()
FLOWER_POINTS = list(zip(*np.where(FLOWER == 0)))


def render(t):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx, cy = 13.5, 13.5
    progress = min(1.0, t / max(DURATION_SEC, 0.001))

    if progress < 0.72:
        p = progress / 0.72
        for i in range(PARTICLES):
            phase = (i / PARTICLES) * math.tau
            wobble = 0.9 * math.sin(t * 2.4 + i * 1.7)
            radius = (12.5 * (1.0 - p) + 3.0 * p) + wobble
            angle = phase + t * (2.2 + 0.35 * (i % 5)) + p * math.tau * 1.2
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            dot(frame, x, y)

        ring_r = 2.0 + 8.5 * p
        for a in np.linspace(0, math.tau, 48, endpoint=False):
            if int((a * 8 + t * 9) % 3) == 0:
                dot(frame, cx + ring_r * math.cos(a), cy + ring_r * math.sin(a))
    elif progress < 0.9:
        p = (progress - 0.72) / 0.18
        ordered = sorted(FLOWER_POINTS, key=lambda pt: abs(pt[0] - 14) + abs(pt[1] - 14))
        reveal = int(len(ordered) * p)
        for y, x in ordered[:reveal]:
            frame[y, x] = 0
        for i in range(18):
            a = i * 0.9 + t * 5.0
            r = 10.0 - 6.0 * p
            dot(frame, cx + r * math.cos(a), cy + r * math.sin(a))
    else:
        frame[:, :] = FLOWER
        for i in range(10):
            if int(t * 12 + i * 3) % 7 in (0, 1):
                a = i * math.tau / 10 + t * 0.7
                dot(frame, cx + 11.5 * math.cos(a), cy + 10.5 * math.sin(a))

    return frame


def main():
    seed_random()
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        start = time.time()
        total = DURATION_SEC + HOLD_SEC
        while time.time() - start < total:
            send_frame(ser, render(time.time() - start))
            time.sleep(FRAME_DT)
    finally:
        ser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[orbit_bloom] stopped")
