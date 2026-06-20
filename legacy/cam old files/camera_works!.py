import sys
sys.path.append('/usr/lib/python3.11/dist-packages')

import cv2
import numpy as np
import serial
from picamera2 import Picamera2
import mediapipe as mp

# --- Flipdot Setup ---
SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 9600
PANEL_ADDRS = [1, 2, 3, 4]
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

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

def draw_landmarks_on_canvas(landmarks, w, h, canvas):
    points = []
    for lm in landmarks:
        if lm.visibility < 0.5:
            continue
        cx, cy = int(lm.x * w), int(lm.y * h)
        points.append([cx, cy])
    if len(points) >= 3:
        points_np = np.array(points, dtype=np.int32)
        hull = cv2.convexHull(points_np)
        cv2.fillConvexPoly(canvas, hull, 255)

def draw_hand_points(hand_landmarks, w, h, canvas):
    tips = [8, 12]  # Index and middle fingertips (for peace sign)
    for i, lm in enumerate(hand_landmarks.landmark):
        cx, cy = int(lm.x * w), int(lm.y * h)
        if i in tips:
            cv2.circle(canvas, (cx, cy), 8, 255, -1)
        else:
            cv2.circle(canvas, (cx, cy), 4, 255, -1)

def main():
    # Init camera
    picam2 = Picamera2()
    config = picam2.create_preview_configuration({"format": "YUV420"})
    picam2.configure(config)
    picam2.start()

    # Init MediaPipe
    mp_pose = mp.solutions.pose
    mp_hands = mp.solutions.hands
    pose = mp_pose.Pose(min_detection_confidence=0.5)
    hands = mp_hands.Hands(static_image_mode=False, max_num_hands=2)

    target_res = 112  # Lower processing resolution for speed

    try:
        while True:
            # Capture and convert
            frame = picam2.capture_array("main")
            rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
            rgb = cv2.resize(rgb, (target_res, target_res))  # Resize after conversion

            silhouette = np.zeros((target_res, target_res), dtype=np.uint8)

            # Pose & hand detection
            pose_results = pose.process(rgb)
            hand_results = hands.process(rgb)

            if pose_results.pose_landmarks:
                draw_landmarks_on_canvas(pose_results.pose_landmarks.landmark, target_res, target_res, silhouette)

            if hand_results.multi_hand_landmarks:
                for hand_landmarks in hand_results.multi_hand_landmarks:
                    draw_hand_points(hand_landmarks, target_res, target_res, silhouette)

            # Morphology and blur (faster version)
            kernel = np.ones((5, 5), np.uint8)
            closed = cv2.morphologyEx(silhouette, cv2.MORPH_CLOSE, kernel)
            blurred = cv2.blur(closed, (5, 5))
            _, binary = cv2.threshold(blurred, 30, 255, cv2.THRESH_BINARY)

            # Resize to 28x28 for flipdots
            small = cv2.resize(binary, (28, 28), interpolation=cv2.INTER_AREA)
            bw = (small > 30).astype(np.uint8)

            # Send to flipdot
            panels = pack_flipbytes(bw)
            send_to_panels(panels)

            # Show preview
            cam_preview = cv2.resize(rgb, (280, 280))
            dot_preview = cv2.resize(bw * 255, (280, 280), interpolation=cv2.INTER_NEAREST)
            dot_preview = cv2.cvtColor(dot_preview, cv2.COLOR_GRAY2BGR)
            combined = np.hstack((cam_preview, dot_preview))
            cv2.imshow("Left: Camera Feed | Right: Flipdot Preview", combined)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        picam2.stop()
        ser.close()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
