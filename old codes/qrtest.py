import qrcode
from PIL import Image
import serial
import time

def generate_qr_matrix(data, size=28):
    qr = qrcode.QRCode(box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert('1')
    img = img.resize((size, size), Image.NEAREST)
    pixels = img.load()

    matrix = []
    for y in range(size):
        row = []
        for x in range(size):
            row.append(1 if pixels[x, y] == 0 else 0)  # black = 1
        matrix.append(row)
    return matrix

def convert_rows_to_columns(data_rows):
    cols = []
    for col in range(28):
        for block in [range(0, 14), range(14, 28)]:
            bytes_col = 0
            for bit in range(7):  # Flipdot supports 7 rows per byte
                row_index = block.start + bit
                if row_index < 28 and data_rows[row_index][col]:
                    bytes_col |= (1 << bit)
            cols.append(bytes_col)
    return cols[:56]  # 28 bytes for each of 2 panels

def send_to_panel(srl, addr, data):
    msg = bytearray([0x80, 0x83, addr])  # header, 28-bytes, refresh
    msg.extend(data)
    msg.append(0x8F)  # end
    srl.write(msg)

data = "https://google.com"  # Your QR data
qr_matrix = generate_qr_matrix(data)
columns = convert_rows_to_columns(qr_matrix)

top_panel_data = columns[:28]
bottom_panel_data = columns[28:]

with serial.Serial("/dev/ttyS0", 9600, timeout=1) as srl:
    send_to_panel(srl, 1, top_panel_data)
    send_to_panel(srl, 2, bottom_panel_data)
