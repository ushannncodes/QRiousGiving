#!/usr/bin/env python3
import serial, time, sys

PORTS = ["/dev/ttyS0", "/dev/ttyAMA0", "/dev/serial0"]  # try your real one first
BAUDS = [9600, 19200, 38400, 57600]
ADDRS = list(range(1, 9))  # try 1..8

def make_pattern(toggle):
    # 28 bytes: 7 rows packed per column -> 1 byte per column, 28 columns
    # simple checkerboard-ish flip
    b = bytearray()
    for x in range(28):
        col = 0
        for y in range(7):
            bit = ((x + y + toggle) & 1)
            col |= (bit << y)
        b.append(col)
    return b

def send_frame(ser, addr, data):
    pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
    ser.write(pkt)
    ser.flush()

def try_combo(port, baud):
    try:
        ser = serial.Serial(port, baud, timeout=0.2, write_timeout=0.5)
    except Exception as e:
        print(f"[{port} {baud}] open fail: {e}")
        return False
    print(f"[{port} {baud}] OPEN")
    ok = False
    try:
        for toggle in (0,1,0,1):
            data = make_pattern(toggle)
            for addr in ADDRS:
                try:
                    send_frame(ser, addr, data)
                except Exception as e:
                    print(f"  write err addr {addr}: {e}")
            time.sleep(0.15)
        ok = True
    finally:
        ser.close()
        print(f"[{port} {baud}] CLOSE")
    return ok

if __name__ == "__main__":
    print("Power-cycle panels after changing DIP! Trying ports/bauds/addresses…")
    for port in PORTS:
        for baud in BAUDS:
            try_combo(port, baud)
    print("Done. Did any panel flicker? Note which baud/port worked.")
