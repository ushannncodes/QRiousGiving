#!/usr/bin/env python3
import os, time, threading, queue, serial
from typing import List
from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess, sys, pathlib
ANIM_PATH = os.getenv("ANIM_PATH", "/home/pi/Desktop/anim.py")

# ====== CONFIG ======
SERIAL_PORT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE   = int(os.getenv("FLIPDOT_BAUD", "57600"))   # use 9600 if needed
PANEL_ADDRS = [1, 2, 3, 4]                              # your 4-panel stack
WIDTH, HEIGHT = 28, 28                                  # full display size

# ====== Serial ======
def open_serial():
    return serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

ser = open_serial()

# ----- run external /home/pi/Desktop/anim.py safely -----
def run_anim_py():
    """
    Spawns anim.py using the same Python interpreter (your venv).
    Closes our serial first to avoid 'device busy' if anim.py opens it,
    then reopens after the script finishes.
    """
    global ser
    try:
        if ser and ser.is_open:
            ser.close()
    except Exception:
        pass

    try:
        env = os.environ.copy()
        # pass serial settings to anim.py if it reads env vars
        env.setdefault("FLIPDOT_SERIAL", SERIAL_PORT)
        env.setdefault("FLIPDOT_BAUD", str(BAUD_RATE))

        subprocess.run(["/usr/bin/python3", ANIM_PATH], check=False, timeout=180, env=env)

    finally:
        # reopen serial for the API after anim.py exits
        try:
            ser = open_serial()
        except Exception as e:
            print("⚠️ Reopen serial failed:", e)


def send_frame_to_flipdot(frame28: List[List[int]]):
    """
    frame28: 28x28 array of 0/1 (0=white, 1=black).
    TODO: Replace pack/send with your controller’s exact protocol.
    """
    assert len(frame28) == HEIGHT and len(frame28[0]) == WIDTH
    # ---- Example packing skeleton (replace with your working packer) ----
    # Split into 4 panels if required by your controller addressing.
    # For now we pretend we can stream a raw 28x28 bitmap per address.
    for addr in PANEL_ADDRS:
        # Begin packet for panel 'addr'
        ser.write(bytes([0x80, addr]))  # example header; change to yours
        # Pack each column into bytes (28 bits -> 4 bytes, use 32 bits)
        for x in range(WIDTH):
            col_bits = 0
            for y in range(HEIGHT):
                bit = 1 if frame28[y][x] else 0
                col_bits |= (bit << y)   # LSB at y=0; flip if your wiring differs
            # send 4 bytes little-endian (covers 28 rows)
            ser.write(col_bits.to_bytes(4, "little"))
        ser.write(b'\xFF')  # example end marker; change to yours
    ser.flush()

def clear_display():
    send_frame_to_flipdot([[0]*WIDTH for _ in range(HEIGHT)])

# ====== Simple drawing helpers ======
def blank(): return [[0]*WIDTH for _ in range(HEIGHT)]

def draw_text_center(frame, text:str):
    """
    Super-simple 3x5 digit font centered. Replace with your preferred font.
    Only for digits '0'-'9'.
    """
    DIGITS = {
        "0": ["111",
              "101",
              "101",
              "101",
              "111"],
        "1": ["010","110","010","010","111"],
        "2": ["111","001","111","100","111"],
        "3": ["111","001","111","001","111"],
        "4": ["101","101","111","001","001"],
        "5": ["111","100","111","001","111"],
        "6": ["111","100","111","101","111"],
        "7": ["111","001","010","010","010"],
        "8": ["111","101","111","101","111"],
        "9": ["111","101","111","001","111"],
    }
    glyph = DIGITS.get(text, DIGITS["0"])
    gw, gh = 3, 5
    sx = (WIDTH - gw) // 2
    sy = (HEIGHT - gh) // 2
    for j,row in enumerate(glyph):
        for i,ch in enumerate(row):
            frame[sy+j][sx+i] = 1 if ch == "111"[i] and row[i] == "1" else (1 if ch=="1" else 0)

def draw_ring(frame, radius:int, thickness:int=1):
    cx, cy = WIDTH//2, HEIGHT//2
    r2, t2 = radius*radius, (radius-thickness)*(radius-thickness)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d2 = (x-cx)*(x-cx) + (y-cy)*(y-cy)
            if t2 <= d2 <= r2:
                frame[y][x] = 1

import random
def add_firework_sparkles(frame, density=0.06):
    # random sparkles
    import math
    for _ in range(int(WIDTH*HEIGHT*density)):
        x = random.randint(0, WIDTH-1)
        y = random.randint(0, HEIGHT-1)
        frame[y][x] = 1

# ====== Animation sequence ======
def animation_countdown_and_fireworks():
    # 5→1 countdown with ring shrinking
    for n in [5,4,3,2,1]:
        for r in range(12, 6, -1):  # ring radius animation
            f = blank()
            draw_ring(f, r, thickness=2)
            draw_text_center(f, str(n))
            send_frame_to_flipdot(f)
            time.sleep(0.05)
        time.sleep(0.2)

    # Simple fireworks burst for ~2 seconds
    for _ in range(20):
        f = blank()
        add_firework_sparkles(f, density=0.10)
        send_frame_to_flipdot(f)
        time.sleep(0.08)

    clear_display()

# ====== Job queue / worker ======
job_q = queue.Queue()
stop_evt = threading.Event()

def worker():
    while not stop_evt.is_set():
        try:
            job = job_q.get(timeout=0.25)
        except queue.Empty:
            continue

        try:
            if job == "countdown_fireworks":
                animation_countdown_and_fireworks()
            elif job == "anim_py":
                run_anim_py()
            else:
                print(f"[worker] unknown job: {job!r}")
        finally:
            job_q.task_done()

t = threading.Thread(target=worker, daemon=True)
t.start()


# ====== API ======
app = Flask(__name__)
CORS(app)  # allow calls from Framer domain while you’re developing

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/trigger", methods=["POST"])
def trigger():
    secret = os.getenv("TRIGGER_SECRET")
    if secret and request.headers.get("X-Trigger-Secret") != secret:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if job_q.qsize() > 10:
        return jsonify({"ok": False, "error": "Busy"}), 429
    seq = (request.get_json(silent=True) or {}).get("sequence", "countdown_fireworks")
    job_q.put(seq)
    return jsonify({"ok": True, "queued": seq})


if __name__ == "__main__":
    try:
        # Host on all interfaces; change port if you like
        app.run(host="0.0.0.0", port=8080, threaded=True)
    finally:
        stop_evt.set()
        t.join(timeout=1.0)
        ser.close()
