#!/usr/bin/env python3
import serial, time, argparse

def col_bytes(bit):
    b = bytearray()
    for x in range(28):
        col = 0
        for y in range(7):
            col |= (bit << y)
        b.append(col)
    return b

ALL_ON  = col_bytes(1)
ALL_OFF = col_bytes(0)
CHECK_A = bytearray((0x55,)*28)  # 01010101 down each column
CHECK_B = bytearray((0xAA,)*28)  # 10101010

def send(ser, addr, data):
    pkt = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
    ser.write(pkt)
    ser.flush()

def probe_at_baud(port, baud, amax):
    print(f"\n=== Probing {port} @ {baud} ===")
    try:
        ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
    except Exception as e:
        print("Open failed:", e); return
    try:
        for addr in range(1, amax+1):
            print(f"Addr {addr}: ON → OFF → CHECKER")
            send(ser, addr, ALL_ON);   time.sleep(0.12)
            send(ser, addr, ALL_OFF);  time.sleep(0.08)
            send(ser, addr, CHECK_A);  time.sleep(0.08)
            send(ser, addr, CHECK_B);  time.sleep(0.08)
    finally:
        ser.close()
        print("Done @", baud)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)   # try 9600 first, then 57600
    ap.add_argument("--addr-max", type=int, default=16) # scan up to 16 addresses
    args = ap.parse_args()
    probe_at_baud(args.port, args.baud, args.addr_max)
