#!/usr/bin/env python3
# cam-final2_ascii_flip.py  (no boot animation; optional BOOT_MODE)
# - BOOT_MODE env: "none" (default), "white", or "black"
#   Example: BOOT_MODE=white FLIP_BLACK_IS=1 ... python3 cam-final2_ascii_flip.py

import sys
sys.path.append('/usr/lib/python3.11/dist-packages')

import os, cv2, time, signal, numpy as np, serial, threading, queue, json
from picamera2 import Picamera2
import mediapipe as mp

# ======= DEFAULTS =======
SERIAL_PORT_DEFAULT = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
BAUD_RATE_DEFAULT   = int(os.getenv("FLIPDOT_BAUD", "57600"))
PANEL_ADDRS_DEFAULT = [int(x) for x in os.getenv("FLIPDOT_ADDRS","1,2,3,4").split(",")]

RESOLUTION_DEFAULT  = (640, 480)
INFER_DEFAULT       = (224, 168)
MIRROR_DEFAULT      = True

GUI_DEFAULT         = bool(os.environ.get("DISPLAY"))
USE_GUI             = bool(int(os.getenv("USE_GUI", "1" if GUI_DEFAULT else "0")))
USE_ASCII           = bool(int(os.getenv("USE_ASCII", "0" if not USE_GUI else "0")))
ASCII_RATE_DEFAULT  = float(os.getenv("ASCII_RATE","8.0"))
PAUSE_FLAG = os.getenv("FD_PAUSE_FLAG", "/tmp/fd_pause")

# Low-res tunables
SUPER_DEFAULT        = 5
BLUR_SIGMA_DEFAULT   = 2.0
BIN_THRESH_DEFAULT   = 0.35
MORPH_RADIUS_DEFAULT = 0
DILATE_ITERS_DEFAULT = 0
ERODE_ITERS_DEFAULT  = 0
USE_DITHER_DEFAULT   = False

# --- ROI and zoom (env-parsed ROI box: "x,y,w,h" in 0..1) -----------------
def _parse_box(s):
    try:
        a = [float(x) for x in s.split(",")]
        if len(a) != 4: raise ValueError
        x,y,w,h = a
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.01, min(1.0 - x, w))
        h = max(0.01, min(1.0 - y, h))
        return (x,y,w,h)
    except Exception:
        return (0,0,1,1)

ROI_BOX_DEFAULT      = _parse_box(os.getenv("ROI_BOX", "0,0,1,1"))
AUTO_ZOOM_DEFAULT    = bool(int(os.getenv("AUTO_ZOOM", "0")))
ZOOM_PAD_DEFAULT     = float(os.getenv("ZOOM_PAD", "0.20"))
ZOOM_MIN_AREA_DEFAULT= int(os.getenv("ZOOM_MIN_AREA", "1800"))
ZOOM_SMOOTH_DEFAULT  = float(os.getenv("ZOOM_SMOOTH", "0.35"))
# ---------------------------------------------------------------------------

# Heartbeat / presence
CAM_SIGNAL_PATH    = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
ACTIVE_PIXELS_MIN  = int(os.getenv("ACTIVE_PIXELS_MIN", "80"))
EMA_ALPHA          = float(os.getenv("EMA_ALPHA", "0.95"))
HEARTBEAT_EVERY    = float(os.getenv("HEARTBEAT_EVERY", "0.25"))
DELTA_ALPHA        = float(os.getenv("DELTA_ALPHA", "0.90"))
TRIGGER_HOLD_SEC   = float(os.getenv("TRIGGER_HOLD_SEC", "4.0"))

SMOOTH_ALPHA_DEFAULT = float(os.getenv("SMOOTH_ALPHA", "0.20"))  # keep smear for soft transitions
MOTION_THRESH        = int(os.getenv("MOTION_THRESH", "6"))

# Flipdot cadence
SEND_MIN_INTERVAL  = float(os.getenv("SEND_MIN_INTERVAL", "0.08"))
SEND_MAX_INTERVAL  = float(os.getenv("SEND_MAX_INTERVAL", "0.50"))
SEND_DELTA_MIN     = int(os.getenv("SEND_DELTA_MIN", "2"))
SEND_LOG           = bool(int(os.getenv("SEND_LOG", "1")))

# Polarity
BLACK_IS           = int(os.getenv("FLIP_BLACK_IS", "0"))  # 1 => bit=1 means BLACK on panel

# Boot behaviour
BOOT_MODE          = os.getenv("BOOT_MODE", "none").lower()  # "none" | "white" | "black"

cv2.setUseOptimized(True); cv2.setNumThreads(1)

# ---------- Serial sender ----------
def pack_flipbytes(frame28):
    assert frame28.shape==(28,28)
    panels = []
    for p in range(4):
        offset = p*7
        data = bytearray()
        for x in range(28):
            b=0
            for y in range(7):
                b |= (int(frame28[offset+y,x]) & 1) << y
            data.append(b)
        panels.append(data)
    return panels

def serial_sender(port, baud, panel_addrs, pkt_queue: queue.Queue, running_flag):
    try:
        ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        print(f"[SER] Opened {port} @ {baud}")
    except Exception as e:
        print(f"[ERR] Serial open failed: {e}")
        return
    try:
        while running_flag["run"]:
            try:
                packet = pkt_queue.get(timeout=0.02)
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
latest_frame = None
frame_lock = threading.Lock()
def camera_loop(picam2, running_flag):
    global latest_frame
    while running_flag["run"]:
        try:
            f = picam2.capture_array("main")
            with frame_lock:
                latest_frame = f
        except Exception as e:
            print(f"[CAM] capture error: {e}")
            time.sleep(0.003)

# ---------- Helpers ----------
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

def _bar(pct: float, width: int = 30) -> str:
    pct = max(0.0, min(1.0, pct)); n = int(pct * width + 0.5)
    return "[" + ("#" * n) + ("-" * (width - n)) + "]"

def _square_clamp(x0,y0,x1,y1,W,H):
    w = x1-x0; h = y1-y0
    if w<=0 or h<=0: return 0,0,W,H
    if w>h: d=(w-h)//2; y0-=d; y1+=d
    else:   d=(h-w)//2; x0-=d; x1+=d
    x0=max(0,x0); y0=max(0,y0); x1=min(W,x1); y1=min(H,y1)
    return x0,y0,x1,y1

def _ema_box(prev, curr, alpha):
    if prev is None: return curr
    px0,py0,px1,py1 = prev; cx0,cy0,cx1,cy1 = curr
    x0=int(round(alpha*px0+(1-alpha)*cx0))
    y0=int(round(alpha*py0+(1-alpha)*cy0))
    x1=int(round(alpha*px1+(1-alpha)*cx1))
    y1=int(round(alpha*py1+(1-alpha)*cy1))
    return (x0,y0,x1,y1)

def _draw_pose_convex(pr_landmarks, W, H, canvas):
    pts=[]
    for lm in pr_landmarks:
        if getattr(lm,"visibility",1.0)<0.5: continue
        pts.append([int(lm.x*W), int(lm.y*H)])
    if len(pts)>=3:
        hull = cv2.convexHull(np.array(pts, dtype=np.int32))
        cv2.fillConvexPoly(canvas, hull, 255)

def _draw_head_shoulders_from_pose(landmarks, W, H, canvas):
    idx = mp.solutions.pose.PoseLandmark
    need = ["LEFT_EAR","RIGHT_EAR","LEFT_EYE","RIGHT_EYE","NOSE","LEFT_SHOULDER","RIGHT_SHOULDER"]
    pts = {}
    for name in need:
        lm = landmarks[getattr(idx, name).value]
        if getattr(lm,"visibility",1.0) < 0.5: return
        pts[name] = np.array([lm.x*W, lm.y*H], dtype=np.float32)
    left_ear,right_ear = pts["LEFT_EAR"], pts["RIGHT_EAR"]
    left_eye,right_eye = pts["LEFT_EYE"], pts["RIGHT_EYE"]
    ls,rs = pts["LEFT_SHOULDER"], pts["RIGHT_SHOULDER"]
    ear_mid = 0.5*(left_ear+right_ear)
    eye_mid = 0.5*(left_eye+right_eye)
    head_dir = eye_mid - ear_mid
    head_w = max(8.0, np.linalg.norm(left_ear-right_ear)) * 1.10
    head_h = head_w * 1.25
    angle = np.degrees(np.arctan2(head_dir[1], head_dir[0]))
    center = eye_mid + 0.20*(eye_mid-ear_mid)
    c = tuple(np.round(center).astype(int)); axes=(int(round(head_w*0.5)), int(round(head_h*0.5)))
    cv2.ellipse(canvas, c, axes, angle, 0, 360, 255, -1, cv2.LINE_AA)
    neck_w = head_w * 0.42; neck_len = head_h * 0.30
    nx=int(round(neck_w*0.5)); ny=int(round(neck_len)); cx,cy=c; neck_top_y=cy+int(head_h*0.45)
    neck_poly = np.array([(cx-nx,neck_top_y),(cx+nx,neck_top_y),(cx+nx,neck_top_y+ny),(cx-nx,neck_top_y+ny)], dtype=np.int32)
    cv2.fillConvexPoly(canvas, neck_poly, 255)
    shoulder_vec = rs - ls; shoulder_w = float(np.linalg.norm(shoulder_vec))
    if shoulder_w < 4: return
    spread = 1.10*shoulder_w; depth = 0.28*spread
    axis = shoulder_vec/shoulder_w; normal = np.array([-axis[1],axis[0]], dtype=np.float32)
    left_ext = ls - axis*(0.05*spread); right_ext = rs + axis*(0.05*spread)
    topL, topR = left_ext, right_ext; botL, botR = left_ext + normal*depth, right_ext + normal*depth
    shp = np.array([topL, topR, botR, botL], dtype=np.int32)
    cv2.fillConvexPoly(canvas, shp, 255)
    r=max(1, int(round(0.06*spread))); cv2.circle(canvas, tuple(np.round(topL).astype(int)), r, 255, -1, cv2.LINE_AA); cv2.circle(canvas, tuple(np.round(topR).astype(int)), r, 255, -1, cv2.LINE_AA)

def to_flipdot_matrix(
    rgb_small, pose, hands, mirror=True,
    SUPER=SUPER_DEFAULT, BLUR_SIGMA=BLUR_SIGMA_DEFAULT, BIN_THRESH=BIN_THRESH_DEFAULT,
    MORPH_RADIUS=MORPH_RADIUS_DEFAULT, DILATE_ITERS=DILATE_ITERS_DEFAULT,
    ERODE_ITERS=ERODE_ITERS_DEFAULT, USE_DITHER=USE_DITHER_DEFAULT,
    ROI_BOX=ROI_BOX_DEFAULT, AUTO_ZOOM=AUTO_ZOOM_DEFAULT,
    ZOOM_PAD=ZOOM_PAD_DEFAULT, ZOOM_MIN_AREA=ZOOM_MIN_AREA_DEFAULT, ZOOM_SMOOTH=ZOOM_SMOOTH_DEFAULT,
    zoom_box_state=None, return_debug=False
):
    # ROI crop
    h,w = rgb_small.shape[:2]
    rx,ry,rw,rh = ROI_BOX
    x0,y0 = int(rx*w), int(ry*h); x1,y1 = int((rx+rw)*w), int((ry+rh)*h)
    roi = rgb_small[y0:y1, x0:x1]; H,W = roi.shape[:2]

    # Supersampled canvas
    SS=max(1,int(SUPER)); Hi,Wi = 28*SS, 28*SS
    sil_hi = np.zeros((Hi,Wi), dtype=np.uint8); sx,sy = Wi/float(W), Hi/float(H)

    # MediaPipe
    pr = pose.process(roi); hr = hands.process(roi)

    # ---- DISTANCE-GATE (react only when person is near enough) -----------------
    DIST_MAX_M       = float(os.getenv("DIST_MAX_M", "1.5"))   # react only if <= this
    DIST_REF_M       = float(os.getenv("DIST_REF_M", "1.5"))   # calibration distance
    SHOULDER_REF_PX  = float(os.getenv("SHOULDER_REF_PX", "0"))
    CALIB_SNAP       = bool(int(os.getenv("CALIB_SNAP", "0")))
    est_dist_m = None
    if pr.pose_landmarks:
        idx = mp.solutions.pose.PoseLandmark
        L = pr.pose_landmarks.landmark[idx.LEFT_SHOULDER.value]
        R = pr.pose_landmarks.landmark[idx.RIGHT_SHOULDER.value]
        Lp = np.array([L.x * int(W*sx), L.y * int(H*sy)], dtype=np.float32)
        Rp = np.array([R.x * int(W*sx), R.y * int(H*sy)], dtype=np.float32)
        shoulder_px = float(np.linalg.norm(Rp - Lp))
        if CALIB_SNAP:
            print(f"[CAL] shoulder_px_now={shoulder_px:.1f} @ {DIST_REF_M}m → set SHOULDER_REF_PX")
        if SHOULDER_REF_PX > 1.0 and shoulder_px > 1.0:
            est_dist_m = DIST_REF_M * (SHOULDER_REF_PX / shoulder_px)
    # ---------------------------------------------------------------------------

    # Build silhouette from pose + hands
    if pr.pose_landmarks:
        canvas=np.zeros_like(sil_hi); _draw_pose_convex(pr.pose_landmarks.landmark, int(W*sx), int(H*sy), canvas); sil_hi=np.maximum(sil_hi,canvas)
        _draw_head_shoulders_from_pose(pr.pose_landmarks.landmark, int(W*sx), int(H*sy), sil_hi)
    if hr.multi_hand_landmarks:
        for hlm in hr.multi_hand_landmarks:
            canvas=np.zeros_like(sil_hi)
            pts = np.array([[hlm.landmark[i].x * int(W*sx), hlm.landmark[i].y * int(H*sy)] for i in range(21)], dtype=np.float32)
            palm_px = max(8.0, float(np.linalg.norm(pts[5] - pts[17])))
            base = max(2.0, 0.14 * palm_px)
            palm_order = [0,5,9,13,17]; cv2.fillConvexPoly(canvas, pts[palm_order].astype(np.int32), 255)
            fingers = {"index":[5,6,7,8],"middle":[9,10,11,12],"ring":[13,14,15,16],"pinky":[17,18,19,20],"thumb":[1,2,3,4]}
            taper=[1.00,0.80,0.60,0.50]; thumb_taper=[0.90,0.70,0.55,0.45]
            for name, chain in fingers.items():
                tlist = thumb_taper if name=="thumb" else taper
                for seg in range(3):
                    a,b = chain[seg], chain[seg+1]
                    ax,ay = int(pts[a][0]), int(pts[a][1]); bx,by = int(pts[b][0]), int(pts[b][1])
                    rad = max(1, int(round(base*tlist[seg])))
                    cv2.line(canvas, (ax,ay), (bx,by), 255, rad, cv2.LINE_AA)
                tx,ty = int(pts[chain[-1]][0]), int(pts[chain[-1]][1]); tip_r=max(1,int(round(base*tlist[-1])))
                cv2.circle(canvas,(tx,ty),tip_r,255,-1, cv2.LINE_AA)
            sil_hi=np.maximum(sil_hi,canvas)

    # Clean + threshold
    if BLUR_SIGMA_DEFAULT>0:
        k=max(1, int(2*BLUR_SIGMA_DEFAULT*3)|1)
        sil_f = cv2.GaussianBlur(sil_hi.astype(np.float32)/255.0,(k,k),BLUR_SIGMA_DEFAULT)
    else:
        sil_f = sil_hi.astype(np.float32)/255.0
    _, bin_hi = cv2.threshold(sil_f, BIN_THRESH_DEFAULT, 1.0, cv2.THRESH_BINARY)
    if MORPH_RADIUS_DEFAULT>0:
        k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*MORPH_RADIUS_DEFAULT+1,2*MORPH_RADIUS_DEFAULT+1))
        if DILATE_ITERS_DEFAULT: bin_hi=cv2.dilate(bin_hi,k,iterations=DILATE_ITERS_DEFAULT)
        if ERODE_ITERS_DEFAULT:  bin_hi=cv2.erode (bin_hi,k,iterations=ERODE_ITERS_DEFAULT)

    # Auto-zoom
    zoom_box_out = zoom_box_state
    ys, xs = np.where(bin_hi>0.5)
    if AUTO_ZOOM and len(xs)>0:
        x0z,x1z = int(xs.min()), int(xs.max()); y0z,y1z = int(ys.min()), int(ys.max())
        pad_x=int(ZOOM_PAD*(x1z-x0z+1)); pad_y=int(ZOOM_PAD*(y1z-y0z+1))
        x0z-=pad_x; x1z+=pad_x; y0z-=pad_y; y1z+=pad_y
        x0z,y0z,x1z,y1z = _square_clamp(x0z,y0z,x1z,y1z,Wi,Hi)
        area = max(1,(x1z-x0z))*max(1,(y1z-y0z))
        if area >= ZOOM_MIN_AREA_DEFAULT:
            if zoom_box_state is not None:
                x0z,y0z,x1z,y1z=_ema_box(zoom_box_state,(x0z,y0z,x1z,y1z),ZOOM_SMOOTH_DEFAULT)
            zoom_box_out=(x0z,y0z,x1z,y1z); crop=bin_hi[y0z:y1z,x0z:x1z]
            lo=cv2.resize(crop.astype(np.float32),(28,28),interpolation=cv2.INTER_AREA)
        else:
            lo=cv2.resize(bin_hi.astype(np.float32),(28,28),interpolation=cv2.INTER_AREA)
    else:
        lo=cv2.resize(bin_hi.astype(np.float32),(28,28),interpolation=cv2.INTER_AREA)

    # Dither / binarize
    if USE_DITHER_DEFAULT:
        T = np.tile(_BAYER_8,(28//8+1,28//8+1))[:28,:28]
        bw = (lo > T).astype(np.uint8)
    else:
        bw = (lo > 0.5).astype(np.uint8)

    # One-and-only inversion knob (default OFF)
    INVERT_OUTPUT = int(os.getenv("INVERT_OUTPUT", "0"))
    if INVERT_OUTPUT:
        bw = 1 - bw

    # If no detection, keep a clean white background
    if np.count_nonzero(bw) <= 2:
        bw = np.zeros((28, 28), np.uint8)

    # ---- Apply near-only distance gate (ignore farther than DIST_MAX_M) ----
    if est_dist_m is not None and est_dist_m > DIST_MAX_M:
        bw[:] = 0
    # -----------------------------------------------------------------------

    if mirror:
        bw = np.fliplr(bw)

    if not return_debug:
        return bw, zoom_box_out

    # Debug images
    roi_bgr = cv2.cvtColor(roi, cv2.COLOR_RGB2BGR)
    cam_vis = cv2.resize(roi_bgr,(224,224),interpolation=cv2.INTER_LINEAR)
    sil_vis = cv2.resize((bin_hi*255).astype(np.uint8),(224,224),interpolation=cv2.INTER_NEAREST)
    sil_vis = cv2.cvtColor(sil_vis, cv2.COLOR_GRAY2BGR)
    dot_vis = cv2.resize(bw*255,(224,224),interpolation=cv2.INTER_NEAREST)
    dot_vis = cv2.cvtColor(dot_vis, cv2.COLOR_GRAY2BGR)
    debug_panel = np.hstack([cam_vis, sil_vis, dot_vis])
    return bw, zoom_box_out, debug_panel

def main():
    running = {"run": True}
    def handle_sig(signum, frame): running.update(run=False)
    signal.signal(signal.SIGINT, handle_sig); signal.signal(signal.SIGTERM, handle_sig)

    serial_port, baud_rate, panel_addrs = SERIAL_PORT_DEFAULT, BAUD_RATE_DEFAULT, PANEL_ADDRS_DEFAULT
    cw,ch = RESOLUTION_DEFAULT; iw,ih = INFER_DEFAULT; mirror=MIRROR_DEFAULT
    ascii_rate = ASCII_RATE_DEFAULT

    # Camera (Picamera2)
    picam2 = Picamera2()
    cfg = picam2.create_video_configuration(main={"format":"YUV420","size":(cw,ch)})
    picam2.configure(cfg)
    try: picam2.set_controls({"FrameRate":60})
    except Exception: pass
    picam2.start(); time.sleep(0.6)
    print(f"[CAM] {cw}x{ch} | infer {iw}x{ih}")
    # after picam2.start(); numbers are (x, y, w, h) in sensor pixels
    if os.getenv("SCALER_CROP", ""):
        try:
            x,y,w,h = [int(v) for v in os.getenv("SCALER_CROP").split(",")]
            picam2.set_controls({"ScalerCrop": (x,y,w,h)})
            print(f"[CAM] ScalerCrop set to {(x,y,w,h)}")
        except Exception as e:
            print(f"[CAM] ScalerCrop failed: {e}")


    # Optional exposure lock (env-gated) – helps reduce light-flicker motion
    if os.getenv("AE_LOCK", "0") == "1":
        try:
            picam2.set_controls({"AeEnable": False, "AwbEnable": False})
            if os.getenv("EXPOSURE_US"):
                picam2.set_controls({"ExposureTime": int(os.getenv("EXPOSURE_US"))})
            if os.getenv("GAIN", ""):
                picam2.set_controls({"AnalogueGain": float(os.getenv("GAIN"))})
            print("[CAM] AE/AWB locked.")
        except Exception as e:
            print(f"[CAM] AE lock failed: {e}")

    # Threads: serial + camera
    pkt_queue = queue.Queue(maxsize=2)
    threading.Thread(target=serial_sender, args=(serial_port, baud_rate, panel_addrs, pkt_queue, running), daemon=True).start()
    threading.Thread(target=camera_loop, args=(picam2, running), daemon=True).start()

    # MediaPipe
    mp_pose = mp.solutions.pose; mp_hands = mp.solutions.hands
    pose = mp_pose.Pose(model_complexity=0, min_detection_confidence=0.5, min_tracking_confidence=0.5, smooth_landmarks=False)
    hands = mp_hands.Hands(static_image_mode=False, max_num_hands=2, min_detection_confidence=0.6, min_tracking_confidence=0.6)

    # Queue helper
    def _queue_send(frame28: np.ndarray):
        pkt = pack_flipbytes(frame28)
        try:
            pkt_queue.put(pkt, timeout=0.001)
        except queue.Full:
            try: _ = pkt_queue.get_nowait()
            except queue.Empty: pass
            pkt_queue.put_nowait(pkt)

    # --- Boot mode (NO ANIMATION by default) ---
    if BOOT_MODE in ("white","black"):
        try:
            frame = np.zeros((28,28), dtype=np.uint8) if BOOT_MODE=="white" else np.ones((28,28), dtype=np.uint8)
            out = frame if BLACK_IS==1 else (1-frame)
            _queue_send(out); time.sleep(0.05)
            print(f"[BOOT] Sent {BOOT_MODE} frame (explicit).")
        except Exception as e:
            print(f"[BOOT] send failed: {e}")

    last_send = 0.0; last_frame=None
    ema_delta=0.0
    ema_active=0.0
    is_active=False
    cam_hold_start=None

    last_ascii_print = 0.0
    zoom_box_hi = None
    prev_gray28 = None
    bg28 = None

    # Main loop
    while running["run"]:
        with frame_lock:
            frame = None if latest_frame is None else latest_frame.copy()
        if frame is None:
            time.sleep(0.002); continue

        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
        except Exception as e:
            print(f"[PROC] convert err: {e}"); continue

        rgb_small = cv2.resize(rgb, (iw, ih), interpolation=cv2.INTER_AREA)

        if USE_GUI:
            bw, zoom_box_hi, debug_panel = to_flipdot_matrix(rgb_small, pose, hands, mirror, zoom_box_state=zoom_box_hi, return_debug=True)
            cv2.imshow("cam|mask|28x28", debug_panel); cv2.waitKey(1)
        else:
            bw, zoom_box_hi = to_flipdot_matrix(rgb_small, pose, hands, mirror, zoom_box_state=zoom_box_hi, return_debug=False)

        # --- LIGHT-INVARIANT MOTION mix (keeps smear but freezes under motion) ---
        gray   = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2GRAY).astype(np.float32)
        gray28 = cv2.resize(gray, (28, 28), interpolation=cv2.INTER_AREA)

        # init background once
        if bg28 is None:
            bg28 = gray28.copy()

        # raw delta
        diff = gray28 - bg28

        # 1) compensate global luminance flicker
        if os.getenv("ILLUMI_FIX", "1") == "1":
            diff = diff - np.median(diff)

        # 2) threshold
        thr = float(os.getenv("MOTION_THRESH", "6"))
        motion = (np.abs(diff) > thr).astype(np.uint8)

        # 3) reject global-change frames by area
        global_frac = motion.mean()
        reject_cut = float(os.getenv("REJECT_GLOBAL", "0.25"))
        if global_frac > reject_cut:
            motion[:] = 0

        # 4) fatten motion, then mix with silhouette
        motion = cv2.dilate(motion, np.ones((3,3), np.uint8), iterations=2)

        mix = os.getenv("MOTION_MIX", "AUTO").upper()  # AUTO|OR|XOR
        pix_count = int(np.sum(bw))  # silhouette density
        too_full  = pix_count > int(0.80 * (28*28))
        if mix == "XOR" or (mix == "AUTO" and too_full):
            bw = (bw ^ motion).astype(np.uint8)
        else:
            bw = np.maximum(bw, motion).astype(np.uint8)

        # 5) slow background update, but freeze under motion (smear kept, not jarring)
        bg_alpha = float(os.getenv("BG_ALPHA", "0.01"))
        bg28 = np.where(motion==0, (1.0 - bg_alpha)*bg28 + bg_alpha*gray28, bg28)
        # -------------------------------------------------------------------------

        # Apply panel polarity ONCE here
        out = bw if BLACK_IS==1 else (1-bw)

        # Send cadence (unchanged; honor SMOOTH_ALPHA_DEFAULT if you use it elsewhere)
        now = time.time()
        changed = 0 if last_frame is None else int(np.sum(out != last_frame))
        min_int = SEND_MIN_INTERVAL; max_int = SEND_MAX_INTERVAL
        need = (last_frame is None) or (changed >= SEND_DELTA_MIN) or ((now - last_send) >= max_int)
        if need and (now - last_send) >= min_int:
            panels = pack_flipbytes(out)
            try:
                for addr, data in zip(PANEL_ADDRS_DEFAULT, panels):
                    # sender thread handles wrapping; here we inline small fast path:
                    pass
            except Exception:
                pass
            # use the queue helper
            try:
                _ = pack_flipbytes(out)
                _queue_send(out)
                last_send = now
                last_frame = out.copy()
                if SEND_LOG:
                    print(f"[SEND] changed={changed:3d}  pix={int(np.sum(bw)):3d}")
            except Exception as e:
                print(f"[SEND] error: {e}")

    print("[MAIN] stopping...")
    time.sleep(0.05)

if __name__ == "__main__":
    main()
