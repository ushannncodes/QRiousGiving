import serial
from PIL import Image
import time

SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 9600
IMAGE_PATH = "qr2.png"  # Use the labeled test PNG
PANEL_ADDRESSES = [1, 2, 3, 4]       # Top to bottom (7 rows per panel)
def load_image_bytes(filename):
    print("[INFO] Loading image:", filename)
    img = Image.open(filename).convert("1").resize((28, 28))
    pixels = img.load()
    bytes_per_panel = []

    for panel in range(4):
        y_offset = panel * 7
        print(f"\n[DEBUG] Panel {panel+1} rows {y_offset}-{y_offset+6}:")
        
        for y in range(7):
            row_str = "".join(["█" if pixels[x, y + y_offset] == 0 else " " for x in range(28)])
            print(f"Row {y_offset + y}: {row_str}")

        panel_bytes = bytearray()
        for x in range(28):
            col_byte = 0
            for y in range(7):
                pixel = pixels[x, y + y_offset]
                bit = 1 if pixel == 255 else 0
                col_byte |= (bit << y)
            panel_bytes.append(col_byte)

        print(f"[DEBUG] Panel {panel+1} packed bytes: {panel_bytes.hex()}")
        bytes_per_panel.append(panel_bytes)

    return bytes_per_panel

# def load_image_bytes(filename):
#     print("[INFO] Loading image:", filename)
#     img = Image.open(filename).convert("1").resize((28, 28))  # 1-bit B/W
#     pixels = img.load()
#     bytes_per_panel = []

#     for panel in range(4):
#         y_offset = panel * 7
#         panel_bytes = bytearray()

#         for x in range(28):  # each column
#             col_byte = 0
#             for y in range(7):
#                 pixel = pixels[x, y + y_offset]
#                 bit = 1 if pixel == 255 else 0  # white = ON
#                 col_byte |= (bit << y)
#             panel_bytes.append(col_byte)

#         print(f"[DEBUG] Panel {panel+1} data: {panel_bytes.hex()}")
#         bytes_per_panel.append(panel_bytes)

#     return bytes_per_panel

# def build_packet(address, data_bytes):
#     return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])
def build_packet(address, data_bytes):
    return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])


def main():
    
    print("[INFO] === Flipdot Panel Row Debug Test ===")
    image_data = load_image_bytes(IMAGE_PATH)

    print("[INFO] Opening serial port:", SERIAL_PORT)
    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as srl:
        time.sleep(0.2)
        for i, panel_bytes in enumerate(image_data):
            address = PANEL_ADDRESSES[i]
            packet = build_packet(address, panel_bytes)
            print(f"[INFO] Sending to panel {address} ({len(packet)} bytes)")
            srl.write(packet)
            srl.flush()
            time.sleep(0.1)

    print("[INFO] ✅ Test image sent to all panels!")

if __name__ == "__main__":
    main()
