from PIL import Image
import serial
import time
import os

# === CONFIG ===
IMAGE_PATH   = 'fd1.png'
SERIAL_PORT  = '/dev/ttyS0'
BAUDRATE     = 57600
PANEL_ADDRS  = [0x01, 0x02, 0x03, 0x04]  # top to bottom
WIDTH        = 28
HEIGHT       = 28

# Orientation tweaks (toggle if things look flipped)
INVERT_COLORS   = True   # True: black pixel => dot ON (1)
FLIP_HORIZONTAL = False  # mirror left/right
FLIP_VERTICAL   = False  # mirror top/bottom
BIT_MSB_TOP     = False  # False: row0->bit0 (LSB). True: row0->bit7 (MSB)

# Protocol bytes (common on some flipdot controllers; adjust if yours differs)
STX   = 0x80
CMD   = 0x85   # "write columns" on some controllers; change if needed
ETX   = 0x8F

def load_image_bw(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    img = Image.open(path).convert('L')
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)
    if FLIP_HORIZONTAL:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if FLIP_VERTICAL:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    # Threshold to 1-bit, no dithering (clean shapes for flipdots)
    img = img.convert('1', dither=Image.NONE)
    return img

def get_bit(value, bit_index):
    return (value >> bit_index) & 1

def pack_7_rows_to_byte(img, col, row_start):
    """
    Pack 7 pixels from rows [row_start .. row_start+6] at fixed column into one byte.
    By default row_start maps to LSB (bit 0). If BIT_MSB_TOP=True, row_start maps to bit 7.
    """
    b = 0
    for r in range(7):
        y = row_start + r
        if y >= HEIGHT:
            continue
        # In mode '1', pixel is 255 for white, 0 for black
        px = img.getpixel((col, y))  # 0 or 255
        on = 1 if (px == 0) else 0   # black=1
        if not INVERT_COLORS:
            on ^= 1  # invert mapping if needed

        if not BIT_MSB_TOP:
            # row_start -> bit0 (LSB)
            b |= (on & 1) << r
        else:
            # row_start -> bit7 (MSB), then downward
            b |= (on & 1) << (7 - 1 - r)  # use bits 6..0 (since only 7 rows)
    return b

def slice_panel_bytes(img, panel_index):
    """
    For panel 0..3 (top..bottom), take its 7-row slice and pack 28 bytes (one per column).
    panel 0: rows 0..6
    panel 1: rows 7..13
    panel 2: rows 14..20
    panel 3: rows 21..27
    """
    row_start = panel_index * 7
    out = bytearray()
    for x in range(WIDTH):
        out.append(pack_7_rows_to_byte(img, x, row_start))
    return out  # length 28

def send_panel_columns(port, baud, address, col_bytes):
    """
    Send a single panel's 28 column bytes to its address.
    Packet format: [STX, CMD, ADDR, 28 bytes, ETX]  (adjust if your controller differs)
    """
    with serial.Serial(port, baud, timeout=1) as srl:
        packet = bytearray([STX, CMD, address]) + bytearray(col_bytes) + bytearray([ETX])
        # Optional: short pause before sending
        time.sleep(0.01)
        srl.write(packet)
        srl.flush()
        # Optional: short pause between panels
        time.sleep(0.01)

def main():
    print("[INFO] === Flipdot 28x28 Image Display ===")
    img = load_image_bw(IMAGE_PATH)
    print("[INFO] Image loaded, thresholded to 1-bit")

    # Quick preview of first few columns of top panel for sanity
    preview = [slice_panel_bytes(img, 0)[i] for i in range( min(8, WIDTH) )]
    print("[DEBUG] Top panel first columns (hex):", ' '.join(f'{b:02X}' for b in preview))

    # Send each panel slice to its address
    for p, addr in enumerate(PANEL_ADDRS):
        panel_bytes = slice_panel_bytes(img, p)   # 28 bytes
        print(f"[INFO] Panel {p} addr 0x{addr:02X} -> {len(panel_bytes)} bytes")
        send_panel_columns(SERIAL_PORT, BAUDRATE, addr, panel_bytes)

    print("[INFO] Done.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[ERROR]", e)
