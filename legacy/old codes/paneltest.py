import serial
import time

# === CONFIG ===
SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 9600
PANEL_ADDRESSES = [1, 2, 3, 4]  # Default guess: top to bottom

def build_packet(address, data_bytes):
    return bytearray([0x80, 0x83, address]) + data_bytes + bytearray([0x8F])

def test_panel_order():
    print("[TEST] === Panel Address Mapping Test ===")
    print("[INFO] Each panel will display a unique horizontal line.")
    print("[INFO] Observe which section lights up for each address.")

    # Each panel gets a different bit lit (row 0–3 for visibility)
    patterns = [
        [0b00000001] * 28,  # row 0
        [0b00000010] * 28,  # row 1
        [0b00000100] * 28,  # row 2
        [0b00001000] * 28   # row 3
    ]

    try:
        with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as srl:
            time.sleep(0.2)
            for i, address in enumerate(PANEL_ADDRESSES):
                print(f"[TEST] Sending to panel address {address} (highlighting row {i})")
                data = bytearray(patterns[i])
                packet = build_packet(address, data)
                srl.write(packet)
                time.sleep(1.5)

        print("\n✅ Test complete. Note which panel responds to which address.")
        print("➡️  Update PANEL_ADDRESSES = [...] accordingly in your image display script.")

    except Exception as e:
        print("[ERROR] Could not open serial port or send data.")
        print(e)

if __name__ == "__main__":
    test_panel_order()

