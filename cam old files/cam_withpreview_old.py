#!/usr/bin/env python3
import sys
sys.path.append('/usr/lib/python3.11/dist-packages')

import os, cv2, time, signal, argparse, numpy as np, serial, threading, queue
from picamera2 import Picamera2
import mediapipe as mp

# ========= Defaults tuned for 57_600 bps =========
SERIAL_PORT_DEFAULT = "/dev/serial0"   # use serial0 symlink
BAUD_RATE_DEFAULT   = 57600
PANEL_ADDRS_DEFAULT = [1, 2, 3, 4]

# OpenCV perf knobs
cv2.setUseOptimized(True)
cv2.setNumThreads(1)

# Shared state
latest_frame = None
frame_lock = threading.Lock()
running = True

# ---------- Serial: non-blocking sender with coalescing ----------
def serial_sender(port, baud, panel_addrs, pkt_queue: queue.Queue):
    try:
        ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        print(f"[SER] Opened {port} @ {baud}")
    except Exception as e:
        print(f"[ERR] Serial open failed: {e}")
        return
    try:
        while running:
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
                print(f"[SER] write error: {e}")
    finally:
        try: ser.close()
        except: pass
        print("[SER] Closed")

# ---------- Camera thread ----------
def camera_loop(picam2):
    global latest_frame, running
    while running:
        try:
            f = picam2.capture_array("main")
            with frame_lock:
                latest_frame = f
        except Exception as e:
            print(f"[CAM] capture error: {e}")
            time.sleep(0.003)

# ---------- Packing ----------
def pack_flipbytes(frame28):
    panels = []
    for p in range(4):
        offset = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for y in range(7):
                byte |= (int(frame28[offset + y, x]) & 1) << y
            data.append(byte)
        panels.append(data)
    return panels

# ---------- Drawing ----------
def draw_landmarks_on_canvas(landmarks, w, h, canvas):
    pts = []
    for lm in landmarks:
        if getattr(lm, "visibility", 1.0) < 0.5:
            continue
        pts.append([int(lm.x * w), int(lm.y * h)])
    if len(pts) >= 3:
        hull = cv2.convexHull(np.array(pts, dtype=np.int32))
        cv2.fillConvexPoly(canvas, hull, 255)

def draw_hand_points(hand_landmarks, w, h, canvas, debug=None):
    tips = {8, 12}  # peace sign
    for i, lm in enumerate(hand_landmarks.landmark):
        cx, cy = int(lm.x * w), int(lm.y * h)
        r = 8 if i in tips else 4
        cv2.circle(canvas, (cx, cy), r, 255, -1)
        if debug is not None:
            cv2.circle(debug, (cx, cy), max(2, r//2), (255,255,255), -1)

# ---------- Processing (now returns debug views) ----------
def to_flipdot_matrix(rgb_small, pose, hands, mirror, morph_kernel, use_blur,
                      motion_prev=None, motion_gain=1.0, return_debug=False):
    h, w = rgb_small.shape[:2]
    sil = np.zeros((h, w), dtype=np.uint8)
    dbg = None
    if return_debug:
        dbg = rgb_small.copy()  # RGB format (will convert to BGR to show)

    pr = pose.process(rgb_small)
    hr = hands.process(rgb_small)

    got_any = False
    if pr.pose_landmarks:
        draw_landmarks_on_canvas(pr.pose_landmarks.landmark, w, h, sil); got_any = True
        if dbg is not None:
            for lm in pr.pose_landmarks.landmark:
                if getattr(lm, "visibility", 1.0) < 0.5: continue
                cx, cy = int(lm.x * w), int(lm.y * h)
                cv2.circle(dbg, (cx, cy), 2, (255,255,255), -1)

    if hr.multi_hand_landmarks:
        for hlm in hr.multi_hand_landmarks:
            draw_hand_points(hlm, w, h, sil, debug=dbg); got_any = True

    # Motion fallback
    if not got_any and motion_prev is not None:
        gray = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2GRAY)
        diff = cv2.absdiff(gray, motion_prev)
        _, mot = cv2.threshold(diff, 12, 255, cv2.THRESH_BINARY)
        if motion_gain != 1.0:
            mot = np.clip(mot.astype(np.float32) * motion_gain, 0, 255).astype(np.uint8)
        sil = cv2.max(sil, mot)
        motion_next = gray
    else:
        motion_next = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2GRAY)

    if morph_kernel > 1:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (morph_kernel, morph_kernel))
        sil = cv2.morphologyEx(sil, cv2.MORPH_CLOSE, k)
    if use_blur:
        sil = cv2.GaussianBlur(sil, (5, 5), 0)

    small = cv2.resize(sil, (28, 28), interpolation=cv2.INTER_AREA)
    bw = (small > 30).astype(np.uint8)
    bw = 1 - bw
    if mirror:
        bw = np.fliplr(bw)

    if not return_debug:
        return bw, motion_next

    # Build debug panes
    cam_vis = cv2.resize(dbg if dbg is not None else rgb_small, (224,224), interpolation=cv2.INTER_LINEAR)
    cam_vis = cv2.cvtColor(cam_vis, cv2.COLOR_RGB2BGR)
    sil_vis = cv2.resize(sil, (224,224), interpolation=cv2.INTER_NEAREST)
    sil_vis = cv2.cvtColor(sil_vis, cv2.COLOR_GRAY2BGR)
    dot_vis = cv2.resize(bw*255, (224,224), interpolation=cv2.INTER_NEAREST)
    dot_vis = cv2.cvtColor(dot_vis, cv2.COLOR_GRAY2BGR)
    debug_panel = np.hstack([cam_vis, sil_vis, dot_vis])

    return bw, motion_next, debug_panel

# ---------- Signals ----------
def handle_sigint(signum, frame):
    global running
    running = False

# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(description="Flipdot reactive with optional GUI preview")
    parser.add_argument("--serial", default=SERIAL_PORT_DEFAULT)
    parser.add_argument("--baud", type=int, default=BAUD_RATE_DEFAULT)
    parser.add_argument("--panel-addrs", default="1,2,3,4")

    parser.add_argument("--resolution", default="320x240", help="Camera WxH")
    parser.add_argument("--infer", default="128x96", help="Inference WxH for MediaPipe")
    parser.add_argument("--mirror", action="store_true", default=True)
    parser.add_argument("--no-mirror", dest="mirror", action="store_false")

    # Reactivity & quality
    parser.add_argument("--delta-threshold", type=float, default=0.015)
    parser.add_argument("--morph-kernel", type=int, default=3)
    parser.add_argument("--blur", action="store_true")
    parser.add_argument("--no-pose", action="store_true")
    parser.add_argument("--no-hands", action="store_true")
    parser.add_argument("--motion-fallback", action="store_true")
    parser.add_argument("--motion-gain", type=float, default=1.0)

    # Pacing
    parser.add_argument("--min-interval", type=float, default=0.015)
    parser.add_argument("--no-interval", action="store_true")

    # Previews
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--ascii-rate", type=float, default=10.0)
    parser.add_argument("--gui", action="store_true", help="Show OpenCV window with debug panes")

    # Turbo preset
    parser.add_argument("--turbo", action="store_true")

    args = parser.parse_args()

    if args.turbo:
        args.infer = "112x84"
        args.delta_threshold = 0.010
        args.morph_kernel = 3
        args.no_interval = True
        args.motion_fallback = True
        args.motion_gain = 1.3

    # Panel addresses
    try:
        panel_addrs = [int(x.strip()) for x in args.panel_addrs.split(",")]
        assert len(panel_addrs) == 4
    except Exception:
        print("[ERR] --panel-addrs must be 4 comma-separated ints"); sys.exit(1)

    # Sizes
    try:
        cw, ch = map(int, args.resolution.lower().split("x"))
        iw, ih = map(int, args.infer.lower().split("x"))
    except Exception:
        print("[ERR] --resolution/--infer like 320x240 / 128x96"); sys.exit(1)

    # Camera (video config + FPS hint)
    picam2 = Picamera2()
    cfg = picam2.create_video_configuration(main={"format": "YUV420", "size": (cw, ch)})
    picam2.configure(cfg)
    try:
        picam2.set_controls({"FrameRate": 60})
    except Exception:
        pass
    picam2.start(); time.sleep(0.6)
    print(f"[CAM] {cw}x{ch} | infer {iw}x{ih} | baud {args.baud}")

    # Threads
    pkt_queue = queue.Queue(maxsize=2)
    sender_t = threading.Thread(target=serial_sender, args=(args.serial, args.baud, panel_addrs, pkt_queue), daemon=True)
    sender_t.start()

    cam_t = threading.Thread(target=camera_loop, args=(picam2,), daemon=True)
    cam_t.start()

    # MediaPipe (light)
    mp_pose = mp.solutions.pose
    mp_hands = mp.solutions.hands
    pose = None if args.no_pose else mp_pose.Pose(
        model_complexity=0, min_detection_confidence=0.5, min_tracking_confidence=0.5, smooth_landmarks=False
    )
    hands = None if args.no_hands else mp_hands.Hands(
        static_image_mode=False, max_num_hands=2, min_detection_confidence=0.6, min_tracking_confidence=0.6
    )

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    last_send = 0.0
    prev_bw = None
    motion_prev = None
    last_ascii_print = 0.0

    try:
        while running:
            with frame_lock:
                frame = None if latest_frame is None else latest_frame.copy()
            if frame is None:
                time.sleep(0.001); continue

            # YUV -> RGB
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
            except Exception as e:
                print(f"[PROC] convert err: {e}"); continue

            rgb_small = cv2.resize(rgb, (iw, ih), interpolation=cv2.INTER_AREA)

            # Dummy processors if disabled
            _pose = pose if pose is not None else type("N", (), {"process": lambda *_: type("R", (), {"pose_landmarks": None})})()
            _hands = hands if hands is not None else type("N", (), {"process": lambda *_: type("R", (), {"multi_hand_landmarks": None})})()

            if args.gui:
                bw, motion_prev, debug_panel = to_flipdot_matrix(
                    rgb_small, _pose, _hands, args.mirror, args.morph_kernel, args.blur,
                    motion_prev=motion_prev if args.motion_fallback else None,
                    motion_gain=args.motion_gain, return_debug=True
                )
            else:
                bw, motion_prev = to_flipdot_matrix(
                    rgb_small, _pose, _hands, args.mirror, args.morph_kernel, args.blur,
                    motion_prev=motion_prev if args.motion_fallback else None,
                    motion_gain=args.motion_gain, return_debug=False
                )

            # Event-driven send
            do_send = False
            if prev_bw is None:
                do_send = True
            else:
                delta = float(np.mean(np.abs(bw - prev_bw)))
                if delta > args.delta_threshold:
                    do_send = True if args.no_interval else (time.time() - last_send) >= args.min_interval

            if do_send:
                pkt = pack_flipbytes(bw)
                try:
                    pkt_queue.put(pkt, timeout=0.001)
                except queue.Full:
                    try: _ = pkt_queue.get_nowait()
                    except queue.Empty: pass
                    try: pkt_queue.put_nowait(pkt)
                    except: pass
                prev_bw = bw
                last_send = time.time()

            # ASCII or GUI preview
            if args.ascii and (time.time() - last_ascii_print) >= (1.0 / max(args.ascii_rate, 1.0)):
                os.system("clear")
                print("[Preview] '#'=black, '.'=white")
                print("\n".join("".join('#' if bw[y, x] else '.' for x in range(28)) for y in range(28)))
                print("(Ctrl+C to quit)")
                last_ascii_print = time.time()

            if args.gui:
                cv2.imshow("Flipdot Debug  |  Cam  |  Silhouette  |  28x28", debug_panel)
                if (cv2.waitKey(1) & 0xFF) == ord('q'):
                    break

            time.sleep(0.0005)

    finally:
        print("\n[SHUTDOWN] Stopping…")
        try:
            if pose: pose.close()
            if hands: hands.close()
        except: pass
        try: picam2.stop()
        except: pass
        if args.gui:
            try: cv2.destroyAllWindows()
            except: pass
        print("[SHUTDOWN] Done.")

if __name__ == "__main__":
    main()
