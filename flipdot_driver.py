# ~/Desktop/flipdot_driver.py
import os, serial

# Import your existing helpers from qr_works
# (qr_works must be on the same Desktop; Python will find it when you run from there)
from qr_works import build_packet, load_image_bytes, PANEL_ADDRESSES
try:
    # If your qr_works defines these, we’ll use them as defaults
    from qr_works import SERIAL_PORT as QR_SERIAL_PORT, BAUD_RATE as QR_BAUD_RATE
except Exception:
    QR_SERIAL_PORT, QR_BAUD_RATE = "/dev/ttyS0", 57600

# Allow env vars to override
SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", QR_SERIAL_PORT)
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", str(QR_BAUD_RATE)))

# We’ll make a tiny 1-bit PIL image from the 28x28 frame and
# hand it to your existing load_image_bytes().
try:
    from PIL import Image
except Exception as e:
    raise SystemExit(
        "Pillow (PIL) not installed. Install with:\n"
        "  sudo apt-get install -y python3-pil\n"
        "or: python3 -m pip install --user Pillow"
    )

_ser = None

def _ensure_serial():
    global _ser
    if _ser is None or not _ser.is_open:
        _ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    return _ser

def close_serial():
    global _ser
    try:
        if _ser and _ser.is_open:
            _ser.close()
    except Exception:
        pass

def _frame_to_image(frame28):
    """
    frame28: list of 28 rows x 28 cols, 0/1 values
    returns a 28x28 1-bit PIL Image (white=off/black=on as your load_image_bytes expects).
    If your pipeline expects inverted colors, flip 'val' below.
    """
    img = Image.new("1", (28, 28), 0)  # 0=black
    px = img.load()
    for y in range(28):
        row = frame28[y]
        for x in range(28):
            val = 255 if row[x] else 0     # 1->white (set to 0 if your packer expects 1=black)
            px[x, y] = val
    return img

def send_frame_to_flipdot(frame28):
    """
    Convert a 28x28 frame into your panel bytes using load_image_bytes(),
    then build and send packets with build_packet() to each address.
    """
    s = _ensure_serial()

    # Use your existing conversion that qr_works uses for QR images
    img = _frame_to_image(frame28)
    image_data = load_image_bytes(img)  # -> list of per-panel byte arrays, same order as PANEL_ADDRESSES

    for i, panel_bytes in enumerate(image_data):
        addr = PANEL_ADDRESSES[i]
        pkt = build_packet(addr, panel_bytes)
        s.write(pkt)
    s.flush()
