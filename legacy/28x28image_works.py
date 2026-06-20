import serial
from PIL import Image
import time

SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 57600
IMAGE_PATH = "sunflower.png"  # Replace with your 28x28 PNG image
PANEL_ADDRESSES = [1, 2, 3, 4]  # From top to bottom

def load_image_bytes(filename):
    print("[INFO] Loading image:", filename)
    img = Image.open(filename).convert("L").resize((28, 28))
    img = img.point(lambda p: 255 if p > 128 else 0)  # Threshold
    bytes_per_panel = []

    for panel in range(4):  # 4 panels of 7 rows each
        panel_bytes = bytearray()
        for x in range(28):  # each column
            col_byte = 0
            for y in range(7):
                pixel_y = panel * 7 + y
                pixel = img.getpixel((x, pixel_y))
                bit = 1 if pixel == 255 else 0  # white = ON
                col_byte |= (bit << y)
            panel_bytes.append(col_byte)
        bytes_per_panel.append(panel_bytes)

    return bytes_per_panel

def build_packet(address, data_bytes):
    return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])

def main():
    print("[INFO] === Flipdot 28x28 Image Display Script ===")
    image_data = load_image_bytes(IMAGE_PATH)

    print("[INFO] Opening serial port:", SERIAL_PORT)
    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as srl:
        time.sleep(0.2)

        for i, panel_bytes in enumerate(image_data):
            address = PANEL_ADDRESSES[i]
            packet = build_packet(address, panel_bytes)
            print(f"[INFO] Sending to panel {address} ({len(packet)} bytes)")
            srl.write(packet)
            time.sleep(0.1)

    print("[INFO] ✅ Image displayed successfully on 28x28 panel!")

if __name__ == "__main__":
    main()
