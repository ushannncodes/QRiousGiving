import sys
sys.path.append('/usr/lib/python3.11/dist-packages')

import cv2
import time
import numpy as np
import serial
import threading
from picamera2 import Picamera2
import mediapipe as mp

# --- Flipdot Setup ---
SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 9600
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# --- Settings ---
UPDATE_INTERVAL = 0.01  # ~20 FPS
DEBUG_MODE = True  # Start with debug mode on
latest_frame = None
frame_lock = threading.Lock()

FINGER_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),     # Thumb
    (0, 5), (5, 6), (6, 7), (7, 8),     # Index
    (0, 9), (9, 10), (10, 11), (11, 12),# Middle
    (0, 13), (13, 14), (14, 15), (15, 16), # Ring
    (0, 17), (17, 18), (18, 19), (19, 20)  # Pinky
]

def camera_loop(picam2):
    global latest_frame
    while True:
        frame = picam2.capture_array("main")
        with frame_lock:
            latest_frame = frame

def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                bit = frame28[offset + y, x]
                byte |= (bit << y)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

def draw_landmarks_on_canvas(landmarks, w, h, canvas, draw_debug=False, debug_img=None):
    head_points = [0, 1, 2, 3, 4]
    torso_points = [11, 12, 23, 24]

    for idx in head_points:
        lm = landmarks[idx]
        if lm.visibility > 0.5:
            cx, cy = int(lm.x * w), int(lm.y * h)
            cv2.circle(canvas, (cx, cy), 10, 255, -1)
            if draw_debug:
                cv2.circle(debug_img, (cx, cy), 3, (0, 255, 0), -1)

    for idx in torso_points:
        lm = landmarks[idx]
        if lm.visibility > 0.5:
            cx, cy = int(lm.x * w), int(lm.y * h)
            cv2.circle(canvas, (cx, cy), 8, 255, -1)
            if draw_debug:
                cv2.circle(debug_img, (cx, cy), 3, (0, 255, 0), -1)

    def connect(a, b):
        if landmarks[a].visibility > 0.5 and landmarks[b].visibility > 0.5:
            x1, y1 = int(landmarks[a].x * w), int(landmarks[a].y * h)
            x2, y2 = int(landmarks[b].x * w), int(landmarks[b].y * h)
            cv2.line(canvas, (x1, y1), (x2, y2), 255, 6)
            if draw_debug:
                cv2.line(debug_img, (x1, y1), (x2, y2), (255, 0, 0), 1)

    connect(11, 12)
    connect(23, 24)
    connect(11, 23)
    connect(12, 24)
    connect(0, 11)
    connect(0, 12)

def draw_hand_points_and_connect(hand_landmarks, pose_landmarks, w, h, canvas):
    for start_idx, end_idx in FINGER_CONNECTIONS:
        start = hand_landmarks.landmark[start_idx]
        end = hand_landmarks.landmark[end_idx]
        x1, y1 = int(start.x * w), int(start.y * h)
        x2, y2 = int(end.x * w), int(end.y * h)
        cv2.line(canvas, (x1, y1), (x2, y2), 255, 4)
        cv2.circle(canvas, (x1, y1), 2, 255, -1)
        cv2.circle(canvas, (x2, y2), 2, 255, -1)

    if pose_landmarks:
        wrist = hand_landmarks.landmark[0]
        if wrist.visibility > 0.5:
            wx, wy = int(wrist.x * w), int(wrist.y * h)
            for sid in [11, 12]:
                if pose_landmarks[sid].visibility > 0.5:
                    sx, sy = int(pose_landmarks[sid].x * w), int(pose_landmarks[sid].y * h)
                    cv2.line(canvas, (wx, wy), (sx, sy), 255, 4)

def apply_bayer_dithering(gray_img):
    bayer4x4 = np.array([
        [ 0,  8,  2, 10],
        [12,  4, 14,  6],
        [ 3, 11,  1,  9],
        [15,  7, 13,  5]
    ], dtype=np.uint8)
    bayer_threshold_map = bayer4x4 * (255 // 16)
    h, w = gray_img.shape
    tiled_thresh = np.tile(bayer_threshold_map, (h // 4 + 1, w // 4 + 1))[:h, :w]
    dithered = gray_img > tiled_thresh
    return dithered.astype(np.uint8)

def main():
    global latest_frame, DEBUG_MODE

    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"format": "YUV420", "size": (224, 160)})
    picam2.configure(config)
    picam2.start()
    time.sleep(1)

    cam_thread = threading.Thread(target=camera_loop, args=(picam2,), daemon=True)
    cam_thread.start()

    mp_pose = mp.solutions.pose
    mp_hands = mp.solutions.hands
    pose = mp_pose.Pose(min_detection_confidence=0.5)
    hands = mp_hands.Hands(static_image_mode=False, max_num_hands=2, min_detection_confidence=0.6)

    last_update_time = 0
    last_bw = None
    prev_bw = np.zeros((28, 28), dtype=np.uint8)

    try:
        while True:
            with frame_lock:
                frame = latest_frame.copy() if latest_frame is not None else None
            if frame is None:
                continue

            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
            except:
                continue

            h, w = 224, 160
            silhouette = np.zeros((h, w), dtype=np.uint8)
            silhouette_debug = np.zeros((h, w, 3), dtype=np.uint8) if DEBUG_MODE else None

            pose_results = pose.process(rgb)
            hand_results = hands.process(rgb)

            if hand_results.multi_hand_landmarks:
                for hand_landmarks in hand_results.multi_hand_landmarks:
                    draw_hand_points_and_connect(hand_landmarks, pose_results.pose_landmarks.landmark if pose_results.pose_landmarks else None, w, h, silhouette)

            if pose_results.pose_landmarks:
                draw_landmarks_on_canvas(pose_results.pose_landmarks.landmark, w, h, silhouette, DEBUG_MODE, silhouette_debug)

            closed = cv2.morphologyEx(silhouette, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            blurred = cv2.GaussianBlur(closed, (5, 5), 0)
            thickened = cv2.dilate(blurred, np.ones((3, 3), np.uint8), iterations=2)
            downscaled = cv2.resize(thickened, (28, 28), interpolation=cv2.INTER_AREA)

            dithered = apply_bayer_dithering(downscaled)
            dithered = 1 - dithered
            dithered = np.fliplr(dithered)

            # EVEN LESS smoothing for faster reaction
            smoothed = cv2.addWeighted(dithered.astype(np.float32), 0.99, prev_bw.astype(np.float32), 0.01, 0)
            bw = (smoothed > 0.5).astype(np.uint8)
            prev_bw = bw.copy()

            now = time.time()
            if now - last_update_time > UPDATE_INTERVAL:
                if last_bw is None or not np.array_equal(bw, last_bw):
                    panels = pack_flipbytes(bw)
                    send_to_panels(panels)
                    last_bw = bw.copy()
                    last_update_time = now

            if DEBUG_MODE:
                cam_preview = cv2.resize(rgb, (320, 240))
                sil_preview = cv2.resize(silhouette_debug, (320, 240)) if silhouette_debug is not None else np.zeros((240, 320, 3), dtype=np.uint8)
                dot_preview = cv2.resize(bw * 255, (280, 280), interpolation=cv2.INTER_NEAREST)
                dot_preview = cv2.cvtColor(dot_preview, cv2.COLOR_GRAY2BGR)
                dot_preview = cv2.resize(dot_preview, (640, 280), interpolation=cv2.INTER_NEAREST)
                top = np.hstack((cam_preview, sil_preview))
                combined = np.vstack((top, dot_preview))
                cv2.imshow("Debug View: Camera | Silhouette | Flipdot", combined)
                key = cv2.waitKey(1)
                if key == ord('q'):
                    break
                elif key == ord('d'):
                    DEBUG_MODE = not DEBUG_MODE
                    if not DEBUG_MODE:
                        cv2.destroyAllWindows()

    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        picam2.stop()
        ser.close()
        if DEBUG_MODE:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
