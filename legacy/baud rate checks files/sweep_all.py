#!/usr/bin/env python3
import serial, time

PORTS = ["/dev/serial0", "/dev/ttyS0"]      # will try both
BAUDS = [9600, 19200, 38400, 57600]         # try known options
ADDRS = list(range(1, 33))                  # 1..32

def make_all(bit):
    b = bytearray()
    for _ in range(28):
        col = 0
        for y in range(7):
            col |= (bit << y)
        b.append(col)
    return b

ALL_ON  = make_all(1)
ALL_OFF = make_all(0)

def send(ser, addr, payload):
    pkt = bytearray([0x80, 0x83, addr]) + payload + bytearray([0x8F])
    ser.write(pkt)
    ser.flush()

print("Sweeping ports/bauds/addrs. Watch for any flicker.")
for port in PORTS:
    for baud in BAUDS:
        try:
            ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        except Exception as e:
            print(f"[{port} {baud}] open fail: {e}")
            continue
        print(f"[{port} {baud}] OPEN")
        try:
            for addr in ADDRS:
                # Big on/off pulses per address
                send(ser, addr, ALL_ON);  time.sleep(0.15)
                send(ser, addr, ALL_OFF); time.sleep(0.10)
            print(f"[{port} {baud}] PASS")
        except Exception as e:
            print(f"[{port} {baud}] write error: {e}")
        finally:
            ser.close()
            print(f"[{port} {baud}] CLOSE")
print("Done. Which port/baud produced ANY flicker?")
