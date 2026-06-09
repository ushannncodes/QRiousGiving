#!/usr/bin/env python3
# cam-final3.py (clean, queue-wired version)
# - Uses a queue-backed serial sender (_queue_send)
# - Single motion-delta block (delta_pixels, ema_delta)
# - Heartbeat JSON with active, ema, delta, delta_ema
# - One-time boot-white frame
# - Cadenced sender that pushes frames on meaningful change (or 1/sec fallback)
# - ASCII preview + 1-line HUD

import os, sys, time, json, threading, queue, signal
import numpy as np
import cv2
import serial

# Optional: optimize OpenCV a bit on Pi
cv2.setUseOptimized(True)
cv2.setNumThreads(1)

# ========== Defaults / Env Knobs ==========
SERIAL_PORT        = os.getenv("FLIPDOT_SERIAL", "/dev/serial0")
BAUD_RATE          = int(os.getenv("FLIPDOT_BAUD", "57600"))
PANEL_ADDRESSES    = [int(x) for x in os.getenv("FLIPDOT_ADDRS", "1,2,3,4").split(",")]

RES_W, RES_H       = [int(x) for x in os.getenv("CAM_RES", "640,480").split(",")]
INFER_W, INFER_H   = [int(x) for x in os.getenv("INFER_RES", "224,168").split(",")]

GUI_DEFAULT        = bool(os.environ.get("DISPLAY"))
USE_GUI            = bool(int(os.getenv("USE_GUI", "1" if GUI_DEFAULT else "0")))
USE_ASCII          = bool(int(os.getenv("USE_ASCII", "1" if not USE_GUI else "0")))
ASCII_RATE         = float(os.getenv("ASCII_RATE", "8.0"))
MIRROR             = bool(int(os.getenv("MIRROR", "1")))

# Super-sampling & binarization
SUPER              = int(os.getenv("SUPER", "5"))
BLUR_SIGMA         = float(os.getenv("BLUR_SIGMA", "1.2"))
BIN_THRESH         = float(os.getenv("BIN_THRESH", "0.50"))
MORPH_RADIUS       = int(os.getenv("MORPH_RADIUS", "0"))
DILATE_ITERS       = int(os.getenv("DILATE_ITERS", "0"))
ERODE_ITERS        = int(os.getenv("ERODE_ITERS", "0"))
USE_DITHER         = bool(int(os.getenv("USE_DITHER", "1")))

# ROI (normalized x,y,w,h in [0,1])
ROI_BOX            = [float(x) for x in os.getenv("ROI_BOX", "0.25,0.10,0.50,0.70").split(",")]
OVERLAY_ROI        = bool(int(os.getenv("OVERLAY_ROI", "0")))

# Heartbeat / presence
CAM_SIGNAL_PATH    = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
ACTIVE_PIXELS_MIN  = int(os.getenv("ACTIVE_PIXELS_MIN", "100"))
EMA_ALPHA          = float(os.getenv("EMA_ALPHA", "0.95"))
HEARTBEAT_EVERY    = float(os.getenv("HEARTBEAT_EVERY", "0.20"))

# Motion smoothing for delta (frame-to-frame flips)
DELTA_ALPHA        = float(os.getenv("DELTA_ALPHA", "0.90"))

# Flipdot send cadence
SEND_MIN_INTERVAL  = float(os.getenv("SEND_MIN_INTERVAL", "0.15"))  # min gap between sends
SEND_MAX_INTERVAL  = float(os.getenv("SEND_MAX_INTERVAL", "1.0"))   # ensure at least 1 send/sec
SEND_DELTA_MIN     = int(os.getenv("SEND_DELTA_MIN", "12"))         # pixels changed to qualify
SEND_LOG           = bool(int(os.getenv("SEND_LOG", "1")))

# HUD debug
HB_DEBUG           = bool(int(os.getenv("HB_DEBUG", "1")))

# ========== Utils ==========
def _bar(pct: float, width: int = 30) -> str:
    pct = max(0.0, min(1.0, pct))
    n = int(pct * width + 0.5)
    return "[" + ("#" * n) + ("-" * (width - n)) + "]"

# 8x8 Bayer threshold map (tiled)
_BAYER_8 = (1/64.0)*np.array([
 [0,32,8,40,2,34,10,42],
 [48,16,56,24,50,18,58,26],
 [12,44,4,36,14,46,6,38],
 [60,28,52,20,62,30,54,22],
 [3,35,11,43,1,33,9,41],
 [51,19,59,27,49,17,57,25],
 [15,47,7,39,13,45,5,37],
 [63,31,55,23,61,29,53,21]
], dtype=np.float32)

def pack_flipbytes(frame28: np.ndarray):
    \"\"\"Pack a 28x28 array of 0/1 into 4 panel payloads (column-major, 7 rows/byte).\"\"\"
    assert frame28.shape == (28,28)
    panels = []
    for p in range(4):
        row_offset = p * 7  # rows per panel = 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = int(frame28[row_offset + y, x]) & 1
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def serial_sender(port, baud, panel_addrs, pkt_queue: queue.Queue, running_flag):
    \"\"\"Drains pkt_queue and writes to serial in panel order.\"\"\"
    try:
        ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        print(f\"[SER] Opened {port} @ {baud}\")
    except Exception as e:
        print(f\"[SER] open failed: {e}\")
        return
    try:
        while running_flag[\"run\"]:
            try:
                packet = pkt_queue.get(timeout=0.05)
                while not pkt_queue.empty():
                    packet = pkt_queue.get_nowait()
            except queue.Empty:
                continue
            try:
                for addr, data in zip(panel_addrs, packet):
                    ser.write(bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F]))
                ser.flush()
            except Exception as e:
                print(f\"[SER] write error: {e}\")
    finally:
        try: ser.close()
        except: pass
        print(\"[SER] Closed\")

# ========== Vision helpers ==========
def to_flipdot_matrix(bgr_frame, ROI_BOX=(0,0,1,1), mirror=True,
                      SUPER=5, BLUR_SIGMA=1.2, BIN_THRESH=0.5,
                      MORPH_RADIUS=0, DILATE_ITERS=0, ERODE_ITERS=0, USE_DITHER=True,
                      return_debug=False, overlay_roi=False):
    \"\"\"Minimal silhouette -> 28x28 pipeline (self-contained).\"\"\"
    # Convert to RGB and crop ROI
    rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
    H, W = rgb.shape[:2]
    rx, ry, rw, rh = ROI_BOX
    x0, y0 = int(rx*W), int(ry*H)
    x1, y1 = int((rx+rw)*W), int((ry+rh)*H)
    roi = rgb[y0:y1, x0:x1]
    RH, RW = roi.shape[:2]

    # Supersampled canvas (simple foreground via luminance + blur + threshold)
    SS = max(1, int(SUPER))
    Hi, Wi = 28*SS, 28*SS
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    if BLUR_SIGMA > 0:
        k = max(1, int(2*BLUR_SIGMA*3)|1)
        norm = cv2.GaussianBlur(norm, (k,k), BLUR_SIGMA)
    norm_f = norm.astype(np.float32)/255.0
    _, bin_lo = cv2.threshold(norm_f, BIN_THRESH, 1.0, cv2.THRESH_BINARY)
    bin_hi = cv2.resize(bin_lo, (Hi,Wi), interpolation=cv2.INTER_LINEAR)

    if MORPH_RADIUS > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*MORPH_RADIUS+1, 2*MORPH_RADIUS+1))
        if DILATE_ITERS: bin_hi = cv2.dilate(bin_hi, k, iterations=DILATE_ITERS)
        if ERODE_ITERS:  bin_hi = cv2.erode (bin_hi, k, iterations=ERODE_ITERS)

    # Downsample to 28x28
    lo = cv2.resize(bin_hi.astype(np.float32), (28,28), interpolation=cv2.INTER_AREA)

    # Threshold (with optional ordered dither)
    if USE_DITHER:
        T = np.tile(_BAYER_8, (28//8+1, 28//8+1))[:28,:28]
        bw = (lo > T).astype(np.uint8)
    else:
        bw = (lo > 0.5).astype(np.uint8)

    # Flip for flipdot convention: 1=black, 0=white
    bw = 1 - bw
    if mirror:
        bw = np.fliplr(bw)

    if not return_debug:
        return bw
    # Debug panels
    cam_vis = cv2.resize(cv2.cvtColor(roi, cv2.COLOR_RGB2BGR), (224,224), interpolation=cv2.INTER_LINEAR)
    sil_vis = cv2.resize((bin_hi*255).astype(np.uint8), (224,224), interpolation=cv2.INTER_NEAREST)
    sil_vis = cv2.cvtColor(sil_vis, cv2.COLOR_GRAY2BGR)
    dot_vis = cv2.resize(bw*255, (224,224), interpolation=cv2.INTER_NEAREST)
    dot_vis = cv2.cvtColor(dot_vis, cv2.COLOR_GRAY2BGR)
    if overlay_roi:
        full_dbg = cv2.cvtColor(bgr_frame.copy(), cv2.COLOR_BGR2RGB)
        cv2.rectangle(full_dbg, (x0,y0), (x1,y1), (0,0,255), 2)
        cam_vis = cv2.resize(full_dbg, (224,224), interpolation=cv2.INTER_LINEAR)
    debug_panel = np.hstack([cam_vis, sil_vis, dot_vis])
    return bw, debug_panel

# ========== Main ==========
def main():
    running_flag = {\"run\": True}
    signal.signal(signal.SIGINT, lambda s,f: running_flag.update(run=False))

    # Camera init (USB cam for portability; swap to Picamera2 if you prefer)
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  RES_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, RES_H)
    cap.set(cv2.CAP_PROP_FPS, 60)

    # Serial sender thread
    pkt_queue = queue.Queue(maxsize=2)
    th = threading.Thread(target=serial_sender, args=(SERIAL_PORT, BAUD_RATE, PANEL_ADDRESSES, pkt_queue, running_flag), daemon=True)
    th.start()

    # Queue helper
    def _queue_send(frame28: np.ndarray):
        pkt = pack_flipbytes(frame28)
        try:
            pkt_queue.put(pkt, timeout=0.001)
        except queue.Full:
            try:
                _ = pkt_queue.get_nowait()
            except queue.Empty:
                pass
            pkt_queue.put_nowait(pkt)

    # One-time boot-white (0=white)
    try:
        white = np.zeros((28,28), dtype=np.uint8)
        _queue_send(white)
        time.sleep(0.05)
        print(\"[BOOT] Sent full-white frame.\")
    except Exception as e:
        print(f\"[BOOT] send failed: {e}\")

    # Heartbeat state
    hb_last = 0.0
    ema_active = 0.0
    is_active = False

    # Motion state
    prev_bw = None
    ema_delta = 0.0

    # Sender state
    last_send = 0.0
    last_frame = None

    # Preview/HUD state
    last_ascii_print = 0.0
    cam_hold_start = None
    dbg_last = 0.0

    while running_flag[\"run\"]:
        ret, bgr = cap.read()
        if not ret or bgr is None:
            time.sleep(0.005)
            continue

        # To 28x28
        bw = to_flipdot_matrix(bgr, ROI_BOX=tuple(ROI_BOX), mirror=MIRROR,
                               SUPER=SUPER, BLUR_SIGMA=BLUR_SIGMA, BIN_THRESH=BIN_THRESH,
                               MORPH_RADIUS=MORPH_RADIUS, DILATE_ITERS=DILATE_ITERS,
                               ERODE_ITERS=ERODE_ITERS, USE_DITHER=USE_DITHER)

        # --- Motion delta (how many dots flipped since last frame) ---
        if prev_bw is None:
            delta_pixels = 0
        else:
            delta_pixels = int(np.sum(bw ^ prev_bw))  # XOR counts flips
        prev_bw = bw.copy()
        ema_delta = DELTA_ALPHA * ema_delta + (1.0 - DELTA_ALPHA) * delta_pixels

        # --- Heartbeat JSON ---
        active_pixels = int(np.sum(bw))
        ema_active = EMA_ALPHA * ema_active + (1.0 - EMA_ALPHA) * active_pixels
        now = time.time()
        if (now - hb_last) >= HEARTBEAT_EVERY:
            is_active = (ema_active >= ACTIVE_PIXELS_MIN)
            try:
                state = {
                    \"ts\": now,
                    \"active_pixels\": active_pixels,
                    \"ema\": float(ema_active),
                    \"delta\": int(delta_pixels),
                    \"delta_ema\": float(ema_delta),
                    \"active\": bool(is_active)
                }
                with open(CAM_SIGNAL_PATH, \"w\") as f:
                    json.dump(state, f)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                print(f\"[HB] write error: {e}\")
            hb_last = now

        # --- ASCII preview ---
        if USE_ASCII and (now - last_ascii_print) >= (1.0 / max(ASCII_RATE, 1.0)):
            os.system(\"clear\")
            print(\"[Preview] '#'=black, '.'=white\")
            print(\"\\n\".join(\"\".join('#' if bw[y, x] else '.' for x in range(28)) for y in range(28)))
            print(\"(Ctrl+C to quit)\")
            last_ascii_print = now

        # --- HUD (persistent single-line) ---
        if HB_DEBUG:
            if is_active:
                cam_hold_start = cam_hold_start or now
            else:
                cam_hold_start = None
            active_for = 0.0 if cam_hold_start is None else (now - cam_hold_start)
            try:
                trigger_target = float(os.getenv(\"TRIGGER_HOLD_SEC\", \"7.0\"))
            except Exception:
                trigger_target = 7.0
            if (now - dbg_last) >= 0.25:
                prog = active_for / trigger_target if trigger_target > 0 else 0.0
                hud = (f\"[HB] active={int(is_active):1d}  \"
                       f\"pix={active_pixels:3d}  \"
                       f\"ema={ema_active:6.1f}  \"
                       f\"thr={ACTIVE_PIXELS_MIN:3d}  \"
                       f\"hold={active_for:4.1f}/{trigger_target:.1f}s  \"
                       f\"{_bar(prog)}  \"
                       f\"dEMA={ema_delta:5.1f}\")
                sys.stdout.write(\"\\r\\033[2K\" + hud)
                sys.stdout.flush()
                dbg_last = now

        # --- Flipdot sender (cadenced) ---
        due_min = (now - last_send) >= SEND_MIN_INTERVAL
        due_max = (now - last_send) >= SEND_MAX_INTERVAL
        if last_frame is None:
            changed = bw.size
        else:
            changed = int(np.sum(bw ^ last_frame))
        big_change = (changed >= SEND_DELTA_MIN)

        if (due_min and big_change) or due_max:
            try:
                _queue_send(bw.astype(np.uint8, copy=False))
            except Exception as e:
                print(f\"[SEND] error: {e}\")
            else:
                last_send = now
                last_frame = bw.copy()
                if SEND_LOG:
                    reason = \"max\" if (due_max and not big_change) else \"change\"
                    print(f\"\\n[SEND] changed={changed:3d}  dEMA={ema_delta:5.1f}  reason={reason}\")

        time.sleep(0.005)

    try: cap.release()
    except: pass
    print(\"\\n[CAM] Stopped.\")

if __name__ == \"__main__\":
    try:
        main()
    except KeyboardInterrupt:
        pass
