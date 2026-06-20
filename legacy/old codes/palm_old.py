#!/usr/bin/env python3
# palm_outline_fill.py — show outline of your PNG first, then fill bottom→top

import time
from PIL import Image
import serial
import os

# ---------- CONFIG ----------
SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))
PANEL_ADDRESSES = [1, 2, 3, 4]           # top → bottom
PNG_PATH = os.getenv("PNG_PATH", "/home/pi/Desktop/palm2.png")

# Vis / animation
THRESH      = int(os.getenv("THRESH", "170"))  # 0..255; higher makes the hand “grow”
ANIM_STEPS  = int(os.getenv("ANIM_STEPS", "28"))
ANIM_DELAY  = float(os.getenv("ANIM_DELAY", "0.05"))
OUTLINE_PAUSE = float(os.getenv("OUTLINE_PAUSE", "0.5"))

# Your controller expects 1=white, 0=black (based on your previous scripts)
WHITE_BIT = 1
BLACK_BIT = 0

# ---------- IMAGE → 28×28 MASKS ----------
def load_png_to_mask(path, thresh=170):
    """
    Returns a 28x28 list-of-lists where:
      1 = white (background), 0 = black (hand)
    """
    img = Image.open(path).convert("L").resize((28, 28), Image.LANCZOS)
    # threshold: white >= thresh, hand < thresh
    arr = []
    for y in range(28):
        row = []
        for x in range(28):
            px = img.getpixel((x, y))
            row.append(1 if px >= thresh else 0)
        arr.append(row)
    return arr

def derive_outline(white_bg_mask):
    """
    Given 1=white,0=black(hand), return an outline mask with 0 where the outline should be,
    1 elsewhere (so it can be sent directly).
    """
    H, W = 28, 28
    out = [[1]*W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if white_bg_mask[y][x] == 0:  # hand pixel
                # 4-neighbor check: if any neighbor is white, this border pixel is outline
                nbr_w = (
                    (y > 0        and white_bg_mask[y-1][x] == 1) or
                    (y < H-1      and white_bg_mask[y+1][x] == 1) or
                    (x > 0        and white_bg_mask[y][x-1] == 1) or
                    (x < W-1      and white_bg_mask[y][x+1] == 1)
                )
                if nbr_w:
                    out[y][x] = 0  # outline (black)
    return out

# ---------- FRAME → BYTES → SERIAL ----------
def frame_to_panel_bytes(frame_28x28):
    """
    Convert 28x28 (values 0/1) into 4 blocks of 28 bytes, each byte = a column’s 7 bits.
    Bit 0 = top row within that 7-row panel block.
    """
    blocks = []
    for panel in range(4):
        y0 = panel * 7
        col_bytes = bytearray()
        for x in range(28):
            b = 0
            for bit in range(7):
                val = frame_28x28[y0 + bit][x]  # 0 black, 1 white
                b |= ((WHITE_BIT if val == 1 else BLACK_BIT) & 1) << bit
            col_bytes.append(b)
        blocks.append(col_bytes)
    return blocks

def build_packet(address, data_bytes):
    # Matches your working protocol: 0x80, 0x83, <addr>, <28 bytes>, 0x8F
    return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])

def send_frame(frame_28x28, ser):
    blocks = frame_to_panel_bytes(frame_28x28)
    for i, data in enumerate(blocks):
        pkt = build_packet(PANEL_ADDRESSES[i], data)
        ser.write(pkt)
        ser.flush()
        time.sleep(0.02)

# ---------- COMPOSITING ----------
def compose_outline_frame(outline_mask):
    """Outline only (black) on white background."""
    H, W = 28, 28
    frame = [[1]*W for _ in range(H)]  # white everywhere
    for y in range(H):
        for x in range(W):
            if outline_mask[y][x] == 0:
                frame[y][x] = 0
    return frame

def compose_fill_frame(silhouette_white_bg, outline_mask, cutoff_row):
    """
    silhouette_white_bg: 1=white bg, 0=hand
    outline_mask: 1=not outline, 0=outline
    cutoff_row: rows >= cutoff_row inside the hand become black (filled)
    """
    H, W = 28, 28
    frame = [[1]*W for _ in range(H)]  # start white
    # draw outline
    for y in range(H):
        for x in range(W):
            if outline_mask[y][x] == 0:
                frame[y][x] = 0
    # fill from bottom up inside the hand
    for y in range(H):
        if y >= cutoff_row:
            for x in range(W):
                if silhouette_white_bg[y][x] == 0:
                    frame[y][x] = 0
    return frame

# ---------- MAIN ----------
def main():
    print("[INFO] Loading PNG:", PNG_PATH)
    white_bg_mask = load_png_to_mask(PNG_PATH, THRESH)
    outline_mask = derive_outline(white_bg_mask)

    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as ser:
        time.sleep(0.2)

        # 1) Show outline
        print("[INFO] Showing outline…")
        send_frame(compose_outline_frame(outline_mask), ser)
        time.sleep(OUTLINE_PAUSE)

        # 2) Animate fill from bottom to top
        print("[INFO] Animating fill…")
        H = 28
        for step in range(ANIM_STEPS):
            # cutoff decreases from H to 0
            cutoff = H - int((step + 1) * (H / ANIM_STEPS))
            frame = compose_fill_frame(white_bg_mask, outline_mask, cutoff)
            send_frame(frame, ser)
            time.sleep(ANIM_DELAY)

    print("[INFO] Done.")

if __name__ == "__main__":
    main()
