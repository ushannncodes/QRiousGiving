from PIL import Image
import serial
import time

SERIAL_PORT = "/dev/ttyS0"
BAUDRATE = 9600
PANEL_ADDRESSES = [1, 2, 3, 4]  # top to bottom
IMAGE_FILE = "test2.png"

def encode_panel_data(panel_data):
    bytes_out = bytearray()
    for row in panel_data:
        for byte_i in range(0, 28, 8):
            byte = 0
            for bit_i in range(8):
                col = byte_i + bit_i
                if col >= 28:
                    continue
                pixel = row[col]
                bit = 1 if pixel == 0 else 0  # Black pixel = ON
                byte |= (bit << (7 - bit_i))
            bytes_out.append(byte)
    return bytes_out

def send_panel(address, data, ser):
    packet = bytearray()
    packet.append(0x80)        # header
    packet.append(0x83)        # 28 bytes + refresh
    packet.append(address)     # panel address (1–4)
    packet.extend(data)        # 28 bytes of packed image data
    packet.append(0x8F)        # EOT
    ser.write(packet)
    ser.flush()
    print(f"[DEBUG] Sent to Panel {address}: {packet.hex()}")

def main():
    img = Image.open(IMAGE_FILE).convert("1")
    img = img.resize((28, 28))
    pixels = img.load()

    with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
        for i, panel_addr in enumerate(PANEL_ADDRESSES):
            start_row = i * 7
            end_row = start_row + 7
            panel_rows = [
                [pixels[x, y] for x in range(28)]
                for y in range(start_row, end_row)
            ]
            packed = encode_panel_data(panel_rows)
            send_panel(panel_addr, packed, ser)
            time.sleep(0.1)

if __name__ == "__main__":
    main()
