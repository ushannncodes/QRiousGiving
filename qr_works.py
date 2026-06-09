import serial
from PIL import Image
import qrcode
import time
import os

# SERIAL_PORT = "/dev/ttyS0"
# BAUD_RATE = 57600
# PANEL_ADDRESSES = [1, 2, 3, 4]  # From top to bottom
# QR_TEXT = "bit.ly/qriousgiving"  # ← Replace with your text or link

SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE = int(os.getenv("FLIPDOT_BAUD", "57600"))
PANEL_ADDRESSES = [1, 2, 3, 4]
QR_TEXT = os.getenv("QR_TEXT", "bit.ly/qriousgiving")



def generate_qr_image(text):
    import qrcode
    from PIL import Image

    qr = qrcode.QRCode(
        version=2,  # 25x25
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=0,  # No default padding
    )
    qr.add_data(text)
    qr.make(fit=True)

    # Get raw QR matrix (list of lists of booleans)
    matrix = qr.get_matrix()  # 25x25

    # Create a new 28x28 canvas and center the QR code (1px margin)
    canvas = Image.new("1", (28, 28), color=255) #255 here refers to white bg
    for y in range(25):
        for x in range(25):
            if matrix[y][x]:
               
                canvas.putpixel((x + 1, y + 1), 0)  # Shift by 1px to center #0 here refers to black QR

    return canvas




def load_image_bytes(img):
    pixels = img.load()
    bytes_per_panel = []

    for panel in range(4):
        y_offset = panel * 7
        panel_bytes = bytearray()

        for x in range(28):
            col_byte = 0
            for y in range(7):
                pixel = pixels[x, y + y_offset]
                bit = 1 if pixel == 255 else 0
                col_byte |= (bit << y)
            panel_bytes.append(col_byte)

        bytes_per_panel.append(panel_bytes)

    return bytes_per_panel

def build_packet(address, data_bytes):
    return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])

def main():
    print("[INFO] Generating QR code...")
    img = generate_qr_image(QR_TEXT)

    print("[INFO] Converting to panel bytes...")
    image_data = load_image_bytes(img)

    print("[INFO] Opening serial port:", SERIAL_PORT)
    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as srl:
        time.sleep(0.2)

        for i, panel_bytes in enumerate(image_data):
            address = PANEL_ADDRESSES[i]
            packet = build_packet(address, panel_bytes)
            print(f"[INFO] Sending to panel {address}")
            srl.write(packet)
            srl.flush()
            time.sleep(0.1)

    print("[INFO] ✅ QR code displayed!")

if __name__ == "__main__":
    main()
