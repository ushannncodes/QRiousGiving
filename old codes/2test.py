from PIL import Image
import serial
import time
import os

# === CONFIG ===
IMAGE_PATH = 'test3.png'       # Your image file
#IMAGE_PATH = 'artwork2.jpg'   
SERIAL_PORT = '/dev/ttyS0'
BAUDRATE = 9600
ADDRESS = 0x01
WIDTH = 28
HEIGHT = 28

def load_image_28x7_bytes(path):
    print(f"[INFO] Loading image from: {path}")
    if not os.path.exists(path):
        print(f"[ERROR] File '{path}' not found.")
        return None

    try:
        img = Image.open(path)
        img = img.convert('1')                          # black/white
        img = img.resize((WIDTH, 7))                    # Only 7 rows for mirrored panel
        print("[INFO] Image resized to 28x7")

        image_bytes = bytearray()

        for x in range(WIDTH):
            col_byte = 0
            for y in range(7):
                pixel = img.getpixel((x, y))
                if pixel != 0:
                    col_byte |= (1 << y)
            image_bytes.append(col_byte)

        print(f"[INFO] Converted to {len(image_bytes)} bytes (expected: 28)")
        return image_bytes

    except Exception as e:
        print(f"[ERROR] Image conversion failed: {e}")
        return None


def send_image_28x7(image_bytes):
    print("[INFO] Sending image using 0x83 (28-byte format)...")
    with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as srl:
        transmission = bytearray([0x80, 0x83, ADDRESS])
        transmission += image_bytes
        transmission.append(0x8F)
        print(f"[INFO] Sending {len(transmission)} bytes...")
        srl.write(transmission)
        print("[INFO] Done.")


# def load_image_28x28_bytes(path):
#     print(f"[INFO] Loading image from: {path}")
#     if not os.path.exists(path):
#         print(f"[ERROR] File '{path}' not found.")
#         return None

#     try:
#         img = Image.open(path)
#         img = img.convert('1')                        # Convert to black & white
#         img = img.resize((WIDTH, HEIGHT))             # Resize to 28×28
#         print("[INFO] Image loaded and resized to 28×28")

#         top_bytes = bytearray()
#         bottom_bytes = bytearray()

#         for x in range(WIDTH):
#             top_byte = 0
#             bottom_byte = 0
#             for y in range(HEIGHT):
#                 pixel = img.getpixel((x, y))
#                 bit = 1 if pixel == 0 else 0  # Black = ON
#                 if y < 7:
#                     top_byte |= (bit << y)
#                 elif y < 14:
#                     bottom_byte |= (bit << (y - 7))
#                 # Ignore rows 14–27 if needed later
#             top_bytes.append(top_byte)
#             bottom_bytes.append(bottom_byte)

#         image_bytes = top_bytes + bottom_bytes
#         print(f"[INFO] Converted image to {len(image_bytes)} bytes (expected: 56)")
#         return image_bytes

#     except Exception as e:
#         print(f"[ERROR] Failed to process image: {e}")
#         return None

# def send_image_to_flipdot(image_bytes):
#     print("[INFO] Preparing to send data to flipdot display...")

#     try:
#         with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as srl:
#             print(f"[INFO] Serial port {SERIAL_PORT} opened at {BAUDRATE} baud.")

#             transmission = bytearray([0x80, 0x85, ADDRESS])
#             transmission += image_bytes
#             transmission.append(0x8F)

#             print(f"[INFO] Sending {len(transmission)} bytes...")
#             srl.write(transmission)
#             print("[INFO] Data sent successfully!")

#     except serial.SerialException as e:
#         print(f"[ERROR] Serial communication failed: {e}")

#     except Exception as e:
#         print(f"[ERROR] Unexpected error: {e}")

# === MAIN ===
print("[INFO] === Flipdot 28x28 Image Display Script ===")
# img_data = load_image_28x28_bytes(IMAGE_PATH)


# if img_data:
#     send_image_to_flipdot(img_data)
# else:
#     print("[ERROR] Image conversion failed. Exiting.")


img_data = load_image_28x7_bytes(IMAGE_PATH)
if img_data:
    send_image_28x7(img_data)
