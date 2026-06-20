#!/usr/bin/env python3
import sys
sys.path.append('/usr/lib/python3.11/dist-packages')  # adjust if needed

import os
import cv2
import time
import signal
import argparse
import numpy as np
import serial
import threading
from picamera2 import Picamera2
import mediapipe as mp

# -----------------------
# Defaults (tuned for 9600 bps controllers)
# -----------------------
SERIAL_PORT_DEFAULT = "/dev/ttyS0"
BAUD_RATE_DEFAULT = 9600
PANEL_ADDRS_DEFAULT = [1, 2, 3, 4]
UPDATE_INTERVAL_DEFAULT = 0.12  # seconds (~8 fps ceiling at 9600 bps)

# --- Shared state ---
latest_frame = None
frame_lock = threading.Lock()
running = True

# OpenCV perf knobs
cv2.setUseOptimized(True)
cv2.setNumThreads(1)


def camera_loop(picam2):
    global latest_frame, running
    while running:
        try:
            frame = picam2.capture_array("main")
            with frame_lock:
                latest_frame = frame
        except Exception as e:
            print(f"[CAM] capture error: {e}")
            time.sleep(0.01)


def pack_flipbytes(frame28):
    """
    Pack a 28x28 binary numpy array (0/1) into 4*28 bytes for four 7x28 panels.
    Each column -> 7 rows -> 1 byte, LSB at top.
    """
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = int(frame28[offset + y, x]) & 1
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels


def send_to_panels(ser, panel_addrs, panels):
    for addr, data in zip(panel_addrs, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()


def draw_landmarks_on_canvas(landmarks, w, h, canvas):
    points = []
    for lm in landmarks:
        if hasattr(lm, "visibility") and lm.visibility < 0.5:
            continue
        cx, cy = int(lm.x * w), int(lm.y * h)
        points.append([cx, cy])
    if len(points) >= 3:
        points_np = np.array(points, dtype=np.int32)
        hull = cv2.convexHull(points_np)
        cv2.fillConvexPoly(canvas, hull, 255)


def draw_hand_points(hand_landmarks, w, h, canvas):
    # Emphasise peace sign fingertips (index=8, middle=12)
    tips = {8, 12}
    for i, lm in enumerate(hand_landmarks.landmark):
        cx, cy = int(lm.x * w), int(lm.y * h)
        r = 8 if i in tips else 4
        cv2.circle(canvas, (cx, cy), r, 255, -1)


def to_flipdot_matrix(
    rgb_small,
    pose,
    hands,
    mirror=True,
    morph_kernel=5,
    use_blur=False
):
    """
    Process an RGB frame (already downscaled for inference) into 28x28 binary (0/1)
    with white background, black silhouette.
    """
    h, w = rgb_small.shape[:2]
    silhouette = np.zeros((h, w), dtype=np.uint8)

    # Pose & hands
    pose_results = pose.process(rgb_small)
    hand_results = hands.process(rgb_small)

    if pose_results.pose_landmarks:
        draw_landmarks_on_canvas(pose_results.pose_landmarks.landmark, w, h, silhouette)

    if hand_results.multi_hand_landmarks:
        for hand_landmarks in hand_results.multi_hand_landmarks:
            draw_hand_points(hand_landmarks, w, h, silhouette)

    # Morph cleanup (tunable)
    if morph_kernel and morph_kernel > 1:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (morph_kernel, morph_kernel))
        silhouette = cv2.morphologyEx(silhouette, cv2.MORPH_CLOSE, k)

    if use_blur:
        silhouette = cv2.GaussianBlur(silhouette, (5, 5), 0)

    # Threshold and downscale -> 28x28; Flipdot expects 1 for black dot up
    _, binary = cv2.threshold(silhouette, 30, 255, cv2.THRESH_BINARY)
    small = cv2.resize(binary, (28, 28), interpolation=cv2.INTER_AREA)

    # Make background white (0), silhouette black (1)
    bw = (small > 30).astype(np.uint8)  # 1 where silhouette present
    bw = 1 - bw  # invert so 1 = black dot, 0 = white (background)

    if mirror:
        bw = np.fliplr(bw)

    return bw


def ascii_preview(bw):
    """
    Print a tiny ASCII preview in terminal. '#' for black, '.' for white.
    """
    lines = []
    for y in range(28):
        row = ''.join('#' if bw[y, x] else '.' for x in range(28))
        lines.append(row)
    return "\n".join(lines)


def handle_sigint(signum, frame):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser(description="Headless flipdot pose+hands over SSH (optimized)")
    parser.add_argument("--serial", default=SERIAL_PORT_DEFAULT, help="Serial port to flipdot controller")
    parser.add_argument("--baud", type=int, default=BAUD_RATE_DEFAULT, help="Serial baud rate")
    parser.add_argument("--panel-addrs", default="1,2,3,4", help="Comma-separated panel addresses (4 addrs)")
    parser.add_argument("--update-interval", type=float, default=UPDATE_INTERVAL_DEFAULT, help="Seconds between panel updates")
    parser.add_argument("--mirror", action="store_true", default=True, help="Mirror horizontally (camera->display)")
    parser.add_argument("--no-mirror", dest="mirror", action="store_false", help="Disable mirroring")

    # Performance-tuning flags
    parser.add_argument("--resolution", default="320x240", help="Camera capture WxH (default 320x240)")
    parser.add_argument("--infer", default="160x120", help="Inference WxH for MediaPipe (default 160x120)")
    parser.add_argument("--delta-threshold", type=float, default=0.02, help="Mean delta threshold to trigger send (0.01–0.05)")
    parser.add_argument("--morph-kernel", type=int, default=5, help="Morph close kernel size (odd int, 0/1 disables)")
    parser.add_argument("--blur", action="store_true", help="Enable Gaussian blur in cleanup (off by default)")

    # ASCII terminal preview
    parser.add_argument("--ascii", action="store_true", help="Print 28x28 ASCII preview to terminal")
    parser.add_argument("--ascii-rate", type=float, default=5.0, help="ASCII preview max FPS (if --ascii)")

    args = parser.parse_args()

    # Parse panel addrs
    try:
        panel_addrs = [int(x.strip()) for x in args.panel_addrs.split(",")]
        if len(panel_addrs) != 4:
            raise ValueError
    except Exception:
        print("[ERR] --panel-addrs must be 4 comma-separated integers, e.g. 1,2,3,4")
        sys.exit(1)

    # Serial
    try:
        ser = serial.Serial(args.serial, args.baud, timeout=1)
        print(f"[SER] Opened {args.serial} @ {args.baud}")
    except Exception as e:
        print(f"[ERR] Failed to open serial {args.serial}: {e}")
        sys.exit(1)

    # Camera sizes
    try:
        w_str, h_str = args.resolution.lower().split("x")
        cam_w, cam_h = int(w_str), int(h_str)
        iw_str, ih_str = args.infer.lower().split("x")
        infer_w, infer_h = int(iw_str), int(ih_str)
    except Exception:
        print("[ERR] --resolution/--infer must be like 320x240")
        sys.exit(1)

    # Camera
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"format": "YUV420", "size": (cam_w, cam_h)})
    picam2.configure(config)
    picam2.start()
    time.sleep(1.0)
    print(f"[CAM] Started at {cam_w}x{cam_h} | Inference {infer_w}x{infer_h}")

    # Camera thread
    cam_thread = threading.Thread(target=camera_loop, args=(picam2,), daemon=True)
    cam_thread.start()

    # MediaPipe (lighter models)
    mp_pose = mp.solutions.pose
    mp_hands = mp.solutions.hands
    pose = mp_pose.Pose(
        model_complexity=0,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
        smooth_landmarks=False
    )
    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.6
    )

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    last_update = 0.0
    last_ascii = 0.0
    prev_bw = None

    try:
        while running:
            with frame_lock:
                frame = None if latest_frame is None else latest_frame.copy()

            if frame is None:
                time.sleep(0.003)
                continue

            # Convert YUV420 -> RGB
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
            except Exception as e:
                print(f"[PROC] Frame convert error: {e}")
                time.sleep(0.003)
                continue

            # Downscale ONLY for inference
            rgb_small = cv2.resize(rgb, (infer_w, infer_h), interpolation=cv2.INTER_AREA)

            # Build 28x28 for flipdot
            bw = to_flipdot_matrix(
                rgb_small,
                pose,
                hands,
                mirror=args.mirror,
                morph_kernel=args.morph_kernel,
                use_blur=args.blur
            )

            now = time.time()
            # Change-detection + paced sending
            if now - last_update >= args.update_interval:
                do_send = True
                if prev_bw is not None:
                    delta = np.mean(np.abs(bw - prev_bw))
                    do_send = delta > args.delta_threshold
                if do_send:
                    try:
                        panels = pack_flipbytes(bw)
                        send_to_panels(ser, panel_addrs, panels)
                        prev_bw = bw
                    except Exception as e:
                        print(f"[SER] write error: {e}")
                last_update = now

            if args.ascii and (now - last_ascii) >= (1.0 / max(args.ascii_rate, 0.1)):
                os.system("clear")
                print("[Preview] 28x28 (\"#\"=black dot, \".\"=white)")
                print(ascii_preview(bw))
                print("\n[Info] Ctrl+C to quit.")
                last_ascii = now

            time.sleep(0.001)

    finally:
        print("\n[SHUTDOWN] Stopping…")
        try:
            pose.close()
            hands.close()
        except Exception:
            pass
        try:
            picam2.stop()
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass
        print("[SHUTDOWN] Done.")


if __name__ == "__main__":
    main()
