# rand_neon_tunnel.py
# Concentric square tunnels collapse into a bright diamond pulse.

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
FPS = float(os.getenv("FPS", "60"))
FRAME_DT = 1.0 / max(FPS, 1.0)

DURATION_SEC = float(os.getenv("TUNNEL_SEC", "5.8"))
HOLD_SEC = float(os.getenv("TUNNEL_HOLD_SEC", "1.0"))


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


def set_px(frame, x, y):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y, x] = 0


def draw_square(frame, cx, cy, r, phase=0):
    r = int(round(r))
    if r <= 0:
        set_px(frame, int(cx), int(cy))
        return
    left, right = int(round(cx - r)), int(round(cx + r))
    top, bottom = int(round(cy - r)), int(round(cy + r))
    for x in range(left, right + 1):
        if (x + phase) % 2 == 0:
            set_px(frame, x, top)
            set_px(frame, x, bottom)
    for y in range(top, bottom + 1):
        if (y + phase) % 2 == 0:
            set_px(frame, left, y)
            set_px(frame, right, y)


def draw_diamond(frame, cx, cy, r, sparkle=False):
    r = int(round(r))
    for dy in range(-r, r + 1):
        span = r - abs(dy)
        for dx in range(-span, span + 1):
            if abs(dx) + abs(dy) >= r - 1:
                set_px(frame, int(cx + dx), int(cy + dy))
    if sparkle:
        for _ in range(9):
            a = random.random() * math.tau
            rr = random.uniform(3.0, 12.0)
            set_px(frame, int(round(cx + rr * math.cos(a))), int(round(cy + rr * math.sin(a))))


def render(t):
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    cx = 13.5 + 1.2 * math.sin(t * 1.1)
    cy = 13.5 + 0.9 * math.cos(t * 0.9)
    p = min(1.0, t / max(DURATION_SEC, 0.001))

    if p < 0.76:
        spin = int(t * 12)
        for i in range(9):
            raw = ((i * 3.2 + t * 9.0) % 15.5)
            r = 14.0 - raw
            if 1.0 <= r <= 14.0:
                draw_square(frame, cx, cy, r, spin + i)

        for y in range(HEIGHT):
            for x in range(WIDTH):
                wave = math.sin((x - y) * 0.55 + t * 5.2)
                edge = min(x, y, WIDTH - 1 - x, HEIGHT - 1 - y)
                if wave > 0.78 and edge < 4:
                    set_px(frame, x, y)
    elif p < 0.92:
        q = (p - 0.76) / 0.16
        for r in np.linspace(13.0 * (1.0 - q), 2.0, 6):
            draw_square(frame, cx, cy, r, int(t * 20))
        draw_diamond(frame, cx, cy, 2 + q * 8)
    else:
        pulse = 8.0 + 2.0 * math.sin(t * 7.0)
        draw_diamond(frame, 13.5, 13.5, pulse, sparkle=True)
        draw_diamond(frame, 13.5, 13.5, 3.0)

    return frame


def main():
    seed = os.getenv("RANDOM_SEED")
    if seed:
        random.seed(seed)
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
        print("\n[neon_tunnel] stopped")
