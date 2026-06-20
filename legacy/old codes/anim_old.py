import numpy as np
# import cv2
import time
import random
import serial

# Flipdot settings
SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 57600
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# Animation settings
HEIGHT, WIDTH = 28, 28
DURATION = 3  # seconds
FPS = 60
FRAME_INTERVAL = 1 / FPS
TOTAL_FRAMES = int(DURATION * FPS)
NUM_PARTICLES = 12
EXPLOSION_FRAMES = 14
MAX_FIREWORKS_AT_ONCE = 4


def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = frame28[offset + y, x]
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels


def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()


def generate_firework_frame(center, frame_num):
    canvas = np.ones((HEIGHT, WIDTH), dtype=np.uint8) * 1
    angle_step = 2 * np.pi / NUM_PARTICLES
    for i in range(NUM_PARTICLES):
        angle = i * angle_step + random.uniform(-0.2, 0.2)
        radius = frame_num + random.uniform(0, 1)
        x = int(center[0] + radius * np.cos(angle))
        y = int(center[1] + radius * np.sin(angle))
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            canvas[y, x] = 0
    return canvas


def generate_fireworks_animation():
    active_fireworks = []
    frames = []
    for frame_idx in range(TOTAL_FRAMES):
        # Launch new fireworks randomly
        if len(active_fireworks) < MAX_FIREWORKS_AT_ONCE and random.random() < 0.3:
            center_x = random.randint(8, 20)
            center_y = random.randint(8, 20)
            active_fireworks.append({"center": (center_x, center_y), "frame": 0})

        frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8) * 1
        new_fireworks = []
        for fw in active_fireworks:
            partial = generate_firework_frame(fw["center"], fw["frame"])
            frame = np.minimum(frame, partial)  # Combine by taking the darker pixel
            fw["frame"] += 1
            if fw["frame"] < EXPLOSION_FRAMES:
                new_fireworks.append(fw)
        active_fireworks = new_fireworks
        frames.append(frame)
    return frames


try:
    fireworks_frames = generate_fireworks_animation()
    for frame in fireworks_frames:
        bw = frame.astype(np.uint8)
        panels = pack_flipbytes(bw)
        send_to_panels(panels)
        time.sleep(FRAME_INTERVAL)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    ser.close()
