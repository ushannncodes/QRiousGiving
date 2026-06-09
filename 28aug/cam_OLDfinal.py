#!/usr/bin/env python3
import sys
sys.path.append('/usr/lib/python3.11/dist-packages')

import os, cv2, time, signal, numpy as np, serial, threading, queue, json
from picamera2 import Picamera2
import mediapipe as mp

import faulthandler, traceback, sys
faulthandler.enable()


# ======= DEFAULTS =======
SERIAL_PORT_DEFAULT = "/dev/serial0"
BAUD_RATE_DEFAULT   = 57600
PANEL_ADDRS_DEFAULT = [1, 2, 3, 4]

RESOLUTION_DEFAULT   = (640, 480)    # more source detail helps at distance
INFER_DEFAULT        = (224, 168)    # stable landmarks
DELTA_THRESHOLD_DEF  = 0.010
MOTION_FALLBACK_DEF  = True
MOTION_GAIN_DEFAULT  = 1.3
MIN_INTERVAL_DEFAULT = 0.015
NO_INTERVAL_DEFAULT  = True
MIRROR_DEFAULT       = True

GUI_DEFAULT   = (os.getenv("GUI", "").strip() == "1") or bool(os.environ.get("DISPLAY"))
ASCII_DEFAULT = not GUI_DEFAULT if os.getenv("ASCII") is None else (os.getenv("ASCII","").strip() == "1")

ASCII_RATE_DEFAULT   = 4.0
MAX_HANDS_DEFAULT    = 2

cv2.setUseOptimized(True)
cv2.setNumThreads(1)

# ---- logging knobs ----
LOG_INTERACTION   = int(os.getenv("LOG_INTERACTION", "1"))              # 1=on, 0=off
INTERACT_LOG_PATH="/home/pi/Desktop/cam_interaction.log"


# ======= Low-res sharpness tunables =======
SUPER_DEFAULT        = 5
BLUR_SIGMA_DEFAULT   = 1.2
BIN_THRESH_DEFAULT   = 0.50
MORPH_RADIUS_DEFAULT = 0
DILATE_ITERS_DEFAULT = 0
ERODE_ITERS_DEFAULT  = 0
USE_DITHER_DEFAULT   = True

# ======= Auto-ROI (panel) detection =======
AUTO_ROI_DEFAULT     = True
ROI_BOX_DEFAULT      = (0.25, 0.10, 0.50, 0.70)  # fallback
ROI_MARGIN_DEFAULT   = 0.04
RECALIB_KEY          = ord('r')

# ---- Auto-ROI on presence knobs ----
AUTO_ROI_MODE            = os.getenv("AUTO_ROI_MODE", "on_presence")  # "on_start" or "on_presence"
ROI_ARM_SEC              = float(os.getenv("ROI_ARM_SEC", "0.8"))     # wait this long after start before calibrating
PRESENCE_HOLD_FOR_CALIB  = float(os.getenv("PRESENCE_HOLD_FOR_CALIB", "1.0"))  # need stable presence for this long
CALIB_TRIES              = int(os.getenv("CALIB_TRIES", "18"))        # samples taken during calibration
CALIB_SLEEP              = float(os.getenv("CALIB_SLEEP", "0.05"))    # spacing between samples


# ======= Auto-ZOOM (keep subject large at 28×28) =======
AUTO_ZOOM_DEFAULT     = True
ZOOM_PAD_DEFAULT      = 0.20
ZOOM_MIN_AREA_DEFAULT = 1800
ZOOM_SMOOTH_DEFAULT   = 0.35

# ======= Head/shoulders geometry (no mushroom) =======
USE_GEOMETRIC_HEAD_DEFAULT = True
HEAD_OVAL_SCALE    = 1.10    # head width vs ear distance
NECK_RATIO         = 0.42    # neck width vs head width
NECK_LEN_RATIO     = 0.30    # neck length vs head height
SHOULDER_SPREAD    = 1.10    # shoulders vs measured shoulder span
SHOULDER_DEPTH     = 0.28    # shoulder thickness vs spread
ROUND_SHOULDERS    = True

# ======= Shared state (RESTORED) =======
latest_frame = None
frame_lock   = threading.Lock()
running      = True
current_roi  = ROI_BOX_DEFAULT
zoom_box_hi  = None

# ======= Heartbeat env knobs (used inside main loop) =======
CAM_SIGNAL_PATH   = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
ACTIVE_PIXELS_MIN = int(os.getenv("ACTIVE_PIXELS_MIN", "60"))
EMA_ALPHA         = float(os.getenv("EMA_ALPHA", "0.85"))
HEARTBEAT_EVERY   = float(os.getenv("HEARTBEAT_EVERY", "0.25"))

# Extra robustness for presence:
PRESENT_MIN_BLACK = int(os.getenv("PRESENT_MIN_BLACK", "120"))   # min 1-bits to count as "there's a person"
PRESENT_MAX_BLACK = int(os.getenv("PRESENT_MAX_BLACK", "620"))   # max (28*28=784)
DELTA_MIN         = float(os.getenv("DELTA_MIN", "0.02"))        # min fraction of pixels changing per step
ARM_AFTER_SEC     = float(os.getenv("ARM_AFTER_SEC", "1.5"))     # ignore early frames
CLEAR_GRACE_SEC = float(os.getenv("CLEAR_GRACE_SEC", "2.0"))  # delay before resetting to 0s



# ---- presence mode ----
# human  = require pose or hands landmarks
# pixels = use pixel/motion gates only (old way)
# hybrid = human landmarks OR (pixel range + motion)
PRESENCE_MODE = os.getenv("PRESENCE_MODE", "human")


# ---------- Serial sender ----------
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
            time.sleep(0.003)  # backoff only on error

# ---------- Pack 28x28 -> 4 panel payloads ----------
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

# ---------- Landmark raster helpers ----------
_HAND_BONES = [
    (0,1),(1,2),(2,3),(3,4),
    (0,5),(5,6),(6,7),(7,8),
    (0,9),(9,10),(10,11),(11,12),
    (0,13),(13,14),(14,15),(15,16),
    (0,17),(17,18),(18,19),(19,20)
]

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

def _draw_hand_tapered(hand_lm, W, H, canvas):
    pts = np.array([[hand_lm.landmark[i].x * W, hand_lm.landmark[i].y * H] for i in range(21)], dtype=np.float32)
    palm_px = max(8.0, float(np.linalg.norm(pts[5] - pts[17])))
    base = max(2.0, 0.14 * palm_px)
    # tight palm
    palm_order = [0,5,9,13,17]
    cv2.fillConvexPoly(canvas, pts[palm_order].astype(np.int32), 255)
    # fingers (taper)
    fingers = {"index":[5,6,7,8], "middle":[9,10,11,12], "ring":[13,14,15,16],
               "pinky":[17,18,19,20], "thumb":[1,2,3,4]}
    taper = [1.00,0.80,0.60,0.50]; thumb_taper=[0.90,0.70,0.55,0.45]
    for name, chain in fingers.items():
        tlist = thumb_taper if name=="thumb" else taper
        for seg in range(3):
            a,b = chain[seg], chain[seg+1]
            ax, ay = int(pts[a][0]), int(pts[a][1])
            bx, by = int(pts[b][0]), int(pts[b][1])
            rad = max(1, int(round(base * tlist[seg])))
            cv2.line(canvas, (ax,ay), (bx,by), 255, rad, cv2.LINE_AA)
        tx, ty = int(pts[chain[-1]][0]), int(pts[chain[-1]][1])
        tip_r = max(1, int(round(base * tlist[-1])))
        cv2.circle(canvas, (tx,ty), tip_r, 255, -1, cv2.LINE_AA)

def _draw_pose_convex(pr_landmarks, W, H, canvas):
    pts = []
    for lm in pr_landmarks:
        if getattr(lm, "visibility", 1.0) < 0.5: 
            continue
        pts.append([int(lm.x*W), int(lm.y*H)])
    if len(pts) >= 3:
        hull = cv2.convexHull(np.array(pts, dtype=np.int32))
        cv2.fillConvexPoly(canvas, hull, 255)

# ---- NEW: Human-looking head + neck + shoulders from Pose ----
def _draw_head_shoulders_from_pose(landmarks, W, H, canvas):
    idx = mp.solutions.pose.PoseLandmark
    need = ["LEFT_EAR","RIGHT_EAR","LEFT_EYE","RIGHT_EYE","NOSE","LEFT_SHOULDER","RIGHT_SHOULDER"]
    pts = {}
    for name in need:
        lm = landmarks[getattr(idx, name).value]
        if getattr(lm, "visibility", 1.0) < 0.5:
            return  # not reliable -> skip this frame
        pts[name] = np.array([lm.x*W, lm.y*H], dtype=np.float32)

    left_ear, right_ear = pts["LEFT_EAR"], pts["RIGHT_EAR"]
    left_eye, right_eye = pts["LEFT_EYE"], pts["RIGHT_EYE"]
    nose = pts["NOSE"]
    ls, rs = pts["LEFT_SHOULDER"], pts["RIGHT_SHOULDER"]

    ear_mid = 0.5*(left_ear + right_ear)
    eye_mid = 0.5*(left_eye + right_eye)
    head_dir = eye_mid - ear_mid
    head_w = max(8.0, np.linalg.norm(left_ear - right_ear)) * HEAD_OVAL_SCALE
    head_h = head_w * 1.25
    angle = np.degrees(np.arctan2(head_dir[1], head_dir[0]))

    # center slightly above eyes toward the nose
    center = eye_mid + 0.20*(eye_mid - ear_mid)
    c = tuple(np.round(center).astype(int))
    axes = (int(round(head_w*0.5)), int(round(head_h*0.5)))
    cv2.ellipse(canvas, c, axes, angle, 0, 360, 255, -1, cv2.LINE_AA)

    # Neck
    neck_w = head_w * NECK_RATIO
    neck_len = head_h * NECK_LEN_RATIO
    nx = int(round(neck_w*0.5))
    ny = int(round(neck_len))
    cx, cy = c
    neck_top_y = cy + int(head_h*0.45)
    neck_poly = np.array([
        (cx-nx, neck_top_y),
        (cx+nx, neck_top_y),
        (cx+nx, neck_top_y+ny),
        (cx-nx, neck_top_y+ny)
    ], dtype=np.int32)
    cv2.fillConvexPoly(canvas, neck_poly, 255)

    # Shoulders (shallow trapezoid)
    shoulder_vec = rs - ls
    shoulder_w = float(np.linalg.norm(shoulder_vec))
    if shoulder_w < 4: 
        return
    spread = SHOULDER_SPREAD * shoulder_w
    depth  = SHOULDER_DEPTH  * spread
    axis = shoulder_vec / shoulder_w
    normal = np.array([-axis[1], axis[0]], dtype=np.float32)
    left_ext  = ls - axis * (0.05*spread)
    right_ext = rs + axis * (0.05*spread)
    topL, topR = left_ext, right_ext
    botL, botR = left_ext + normal*depth, right_ext + normal*depth
    shp = np.array([topL, topR, botR, botL], dtype=np.int32)
    cv2.fillConvexPoly(canvas, shp, 255)
    if ROUND_SHOULDERS:
        r = max(1, int(round(0.06*spread)))
        cv2.circle(canvas, tuple(np.round(topL).astype(int)), r, 255, -1, cv2.LINE_AA)
        cv2.circle(canvas, tuple(np.round(topR).astype(int)), r, 255, -1, cv2.LINE_AA)

# ---------- Auto-ROI (panel) detection ----------
def _norm_box(x0, y0, x1, y1, w, h):
    x0 = max(0, min(w-1, x0)); x1 = max(0, min(w-1, x1))
    y0 = max(0, min(h-1, y0)); y1 = max(0, min(h-1, y1))
    if x1 <= x0 or y1 <= y0: return None
    return (x0/w, y0/h, (x1-x0)/w, (y1-y0)/h)

def detect_panel_roi_bgr(bgr):
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 7, 50, 50)
    gray = cv2.equalizeHist(gray)
    edges = cv2.Canny(gray, 60, 160)
    edges = cv2.dilate(edges, np.ones((3,3), np.uint8), iterations=1)

    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_score = None, 0.0
    img_area = W*H
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 0.03*img_area: continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02*peri, True)
        if len(approx)!=4 or not cv2.isContourConvex(approx): continue
        x,y,w,h = cv2.boundingRect(approx)
        rect_area = float(w*h)+1e-6
        rectangularity = float(area)/rect_area
        ar = w/float(h+1e-6)
        aspect_score = 1.0 - min(abs(ar-1.0),0.5)*2.0
        score = rectangularity*(area/img_area)*aspect_score
        if score>best_score:
            best_score = score; best=(x,y,x+w,y+h)
    if best is None: return None
    x0,y0,x1,y1 = best
    mx = int(ROI_MARGIN_DEFAULT*(x1-x0)); my = int(ROI_MARGIN_DEFAULT*(y1-y0))
    return _norm_box(x0-mx, y0-my, x1+mx, y1+my, W, H)

def calibrate_roi(get_frame_bgr, tries=18, sleep_s=0.05):
    boxes=[]
    for _ in range(tries):
        bgr=get_frame_bgr()
        if bgr is None:
            time.sleep(sleep_s); continue
        box=detect_panel_roi_bgr(bgr)
        if box is not None: boxes.append(box)
        time.sleep(sleep_s)
    if not boxes:
        print("[ROI] Auto-detect FAILED, using fallback ROI_BOX_DEFAULT.")
        return ROI_BOX_DEFAULT
    med = np.median(np.array(boxes,dtype=np.float32), axis=0)
    x,y,w,h = [float(v) for v in med.tolist()]
    x = min(max(0.0,x),1.0); y = min(max(0.0,y),1.0)
    w = min(max(0.05,w), 1.0-x); h = min(max(0.05,h), 1.0-y)
    print(f"[ROI] Auto-detect OK: x={x:.3f} y={y:.3f} w={w:.3f} h={h:.3f}")
    return (x,y,w,h)

# ---------- Auto-ZOOM helpers ----------
def _square_clamp(x0,y0,x1,y1,W,H):
    w = x1-x0; h = y1-y0
    if w<=0 or h<=0: return 0,0,W,H
    if w>h:
        d=(w-h)//2; y0-=d; y1+=d
    else:
        d=(h-w)//2; x0-=d; x1+=d
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(W, x1); y1 = min(H, y1)
    return x0,y0,x1,y1

def _ema_box(prev, curr, alpha):
    if prev is None: return curr
    px0,py0,px1,py1 = prev; cx0,cy0,cx1,cy1 = curr
    x0 = int(round(alpha*px0 + (1-alpha)*cx0))
    y0 = int(round(alpha*py0 + (1-alpha)*cy0))
    x1 = int(round(alpha*px1 + (1-alpha)*cx1))
    y1 = int(round(alpha*py1 + (1-alpha)*cy1))
    return (x0,y0,x1,y1)

# ---------- Vision pipeline ----------
def to_flipdot_matrix(
    rgb_small, pose, hands, mirror=True,
    motion_prev=None, motion_gain=1.0, return_debug=False,
    # detail params
    SUPER=SUPER_DEFAULT, BLUR_SIGMA=BLUR_SIGMA_DEFAULT, BIN_THRESH=BIN_THRESH_DEFAULT,
    MORPH_RADIUS=MORPH_RADIUS_DEFAULT, DILATE_ITERS=DILATE_ITERS_DEFAULT,
    ERODE_ITERS=ERODE_ITERS_DEFAULT, USE_DITHER=USE_DITHER_DEFAULT,
    # framing params
    ROI_BOX=(0,0,1,1), overlay_roi=False,
    # auto-zoom params
    AUTO_ZOOM=AUTO_ZOOM_DEFAULT, ZOOM_PAD=ZOOM_PAD_DEFAULT,
    ZOOM_MIN_AREA=ZOOM_MIN_AREA_DEFAULT, ZOOM_SMOOTH=ZOOM_SMOOTH_DEFAULT,
    zoom_box_state=None
):
    # ----- ROI crop -----
    h, w = rgb_small.shape[:2]
    rx, ry, rw, rh = ROI_BOX
    x0, y0 = int(rx*w), int(ry*h)
    x1, y1 = int((rx+rw)*w), int((ry+rh)*h)
    roi = rgb_small[y0:y1, x0:x1]
    H, W = roi.shape[:2]

    # ----- supersampled canvas -----
    SS = max(1, int(SUPER))
    Hi, Wi = 28*SS, 28*SS
    sil_hi = np.zeros((Hi, Wi), dtype=np.uint8)
    sx, sy = Wi/float(W), Hi/float(H)

    # ----- landmarks -----
    pr = pose.process(roi)
    hr = hands.process(roi)

    got_any = False
    present_human = False

    if pr.pose_landmarks:
        canvas = np.zeros_like(sil_hi)
        _draw_pose_convex(pr.pose_landmarks.landmark, int(W*sx), int(H*sy), canvas)
        sil_hi = np.maximum(sil_hi, canvas); got_any = True
        present_human = True     

    if hr.multi_hand_landmarks:
        for hlm in hr.multi_hand_landmarks:
            canvas = np.zeros_like(sil_hi)
            _draw_hand_tapered(hlm, int(W*sx), int(H*sy), canvas)
            sil_hi = np.maximum(sil_hi, canvas); got_any = True
        present_human = True  

    # --- geometric head/shoulders from pose (crisp human shape) ---
    if USE_GEOMETRIC_HEAD_DEFAULT and pr.pose_landmarks:
        _draw_head_shoulders_from_pose(pr.pose_landmarks.landmark, int(W*sx), int(H*sy), sil_hi)
        got_any = True

    # ----- motion fallback -----
    if not got_any and motion_prev is not None:
        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        diff = cv2.absdiff(gray, motion_prev)
        _, mot = cv2.threshold(diff, 12, 255, cv2.THRESH_BINARY)
        mot_hi = cv2.resize(mot, (Wi,Hi), interpolation=cv2.INTER_LINEAR)
        sil_hi = np.maximum(sil_hi, mot_hi)
        motion_next = gray
    else:
        motion_next = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)

    # ----- clean at supersampled scale -----
    if BLUR_SIGMA > 0:
        k = max(1, int(2*BLUR_SIGMA*3)|1)
        sil_f = cv2.GaussianBlur(sil_hi.astype(np.float32)/255.0, (k,k), BLUR_SIGMA)
    else:
        sil_f = sil_hi.astype(np.float32)/255.0
    _, bin_hi = cv2.threshold(sil_f, BIN_THRESH, 1.0, cv2.THRESH_BINARY)
    if MORPH_RADIUS > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*MORPH_RADIUS+1, 2*MORPH_RADIUS+1))
        if DILATE_ITERS: bin_hi = cv2.dilate(bin_hi, k, iterations=DILATE_ITERS)
        if ERODE_ITERS:  bin_hi = cv2.erode (bin_hi, k, iterations=ERODE_ITERS)

    # ----- AUTO-ZOOM (in supersampled coords) -----
    zoom_box_out = zoom_box_state
    if AUTO_ZOOM:
        ys, xs = np.where(bin_hi > 0.5)
        if len(xs) > 0:
            x0z, x1z = int(xs.min()), int(xs.max())
            y0z, y1z = int(ys.min()), int(ys.max())
            pad_x = int(ZOOM_PAD * (x1z - x0z + 1))
            pad_y = int(ZOOM_PAD * (y1z - y0z + 1))
            x0z -= pad_x; x1z += pad_x; y0z -= pad_y; y1z += pad_y
            x0z,y0z,x1z,y1z = _square_clamp(x0z,y0z,x1z,y1z, Wi, Hi)
            area = max(1,(x1z-x0z)) * max(1,(y1z-y0z))
            if area >= ZOOM_MIN_AREA_DEFAULT:
                if zoom_box_state is not None:
                    x0z,y0z,x1z,y1z = _ema_box(zoom_box_state,(x0z,y0z,x1z,y1z), ZOOM_SMOOTH_DEFAULT)
                zoom_box_out = (x0z,y0z,x1z,y1z)
                crop = bin_hi[y0z:y1z, x0z:x1z]
                lo = cv2.resize(crop.astype(np.float32), (28,28), interpolation=cv2.INTER_AREA)
            else:
                lo = cv2.resize(bin_hi.astype(np.float32), (28,28), interpolation=cv2.INTER_AREA)
        else:
            lo = cv2.resize(bin_hi.astype(np.float32), (28,28), interpolation=cv2.INTER_AREA)
    else:
        lo = cv2.resize(bin_hi.astype(np.float32), (28,28), interpolation=cv2.INTER_AREA)

    # ----- dither / binarize -----
    if USE_DITHER_DEFAULT:
        T = np.tile(_BAYER_8, (28//8+1, 28//8+1))[:28,:28]
        bw = (lo > T).astype(np.uint8)
    else:
        bw = (lo > 0.5).astype(np.uint8)

    bw = 1 - bw
    if mirror:
        bw = np.fliplr(bw)

    if not return_debug:
        return bw, motion_next, zoom_box_out, present_human

    # Debug panel
    roi_bgr = cv2.cvtColor(roi, cv2.COLOR_RGB2BGR)
    if overlay_roi:
        full_dbg = cv2.cvtColor(rgb_small.copy(), cv2.COLOR_RGB2BGR)
        cv2.rectangle(full_dbg, (x0,y0), (x1,y1), (0,0,255), 2)
        cam_vis = cv2.resize(full_dbg, (224,224), interpolation=cv2.INTER_LINEAR)
    else:
        cam_vis = cv2.resize(roi_bgr, (224,224), interpolation=cv2.INTER_LINEAR)

    sil_vis = cv2.resize((bin_hi*255).astype(np.uint8), (224,224), interpolation=cv2.INTER_NEAREST)
    sil_vis = cv2.cvtColor(sil_vis, cv2.COLOR_GRAY2BGR)
    dot_vis = cv2.resize(bw*255, (224,224), interpolation=cv2.INTER_NEAREST)
    dot_vis = cv2.cvtColor(dot_vis, cv2.COLOR_GRAY2BGR)

    # draw zoom box on the silhouette pane
    if zoom_box_out is not None:
        x0z,y0z,x1z,y1z = zoom_box_out
        zx0 = int(x0z * (224.0/Wi)); zy0 = int(y0z * (224.0/Hi))
        zx1 = int(x1z * (224.0/Wi)); zy1 = int(y1z * (224.0/Hi))
        cv2.rectangle(sil_vis, (zx0,zy0), (zx1,zy1), (0,0,255), 2)

    debug_panel = np.hstack([cam_vis, sil_vis, dot_vis])
    return bw, motion_next, zoom_box_out,present_human, debug_panel

# ---------- Signals ----------
def handle_sigint(signum, frame):
    global running
    running = False

# ---------- Main ----------
def main():
    global running, current_roi, zoom_box_hi

    serial_port  = SERIAL_PORT_DEFAULT
    baud_rate    = BAUD_RATE_DEFAULT
    panel_addrs  = PANEL_ADDRS_DEFAULT
    cw, ch       = RESOLUTION_DEFAULT
    iw, ih       = INFER_DEFAULT
    mirror       = MIRROR_DEFAULT
    delta_th     = DELTA_THRESHOLD_DEF
    motion_fallback = MOTION_FALLBACK_DEF
    motion_gain  = MOTION_GAIN_DEFAULT
    min_interval = MIN_INTERVAL_DEFAULT
    no_interval  = NO_INTERVAL_DEFAULT
    use_gui      = GUI_DEFAULT
    use_ascii    = ASCII_DEFAULT
    ascii_rate   = ASCII_RATE_DEFAULT
    print(f"[DBG] DISPLAY={os.environ.get('DISPLAY')} use_gui={use_gui} use_ascii={use_ascii}")

    picam2 = Picamera2()
    cfg = picam2.create_video_configuration(main={"format": "YUV420", "size": (cw, ch)})
    picam2.configure(cfg)
    try:
        picam2.set_controls({"FrameRate": 60})
    except Exception:
        pass
    picam2.start(); time.sleep(0.6)
    print(f"[CAM] {cw}x{ch} | infer {iw}x{ih}")

    pkt_queue = queue.Queue(maxsize=2)
    threading.Thread(target=serial_sender, args=(serial_port, baud_rate, panel_addrs, pkt_queue), daemon=True).start()
    threading.Thread(target=camera_loop, args=(picam2,), daemon=True).start()

    # MediaPipe objects
    mp_pose = mp.solutions.pose
    mp_hands = mp.solutions.hands
    pose = mp_pose.Pose(model_complexity=0, min_detection_confidence=0.5, min_tracking_confidence=0.5, smooth_landmarks=False)
    hands = mp_hands.Hands(static_image_mode=False, max_num_hands=MAX_HANDS_DEFAULT,
                           min_detection_confidence=0.6, min_tracking_confidence=0.6)

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    # --- Auto-ROI setup ---
    def _get_latest_bgr():
        with frame_lock:
            f = None if latest_frame is None else latest_frame.copy()
        if f is None: return None
        try: return cv2.cvtColor(f, cv2.COLOR_YUV2BGR_I420)
        except: return None

    roi_calibrated = False
    if AUTO_ROI_DEFAULT:
        t0 = time.time()
        while latest_frame is None and (time.time()-t0) < 2.0:
            time.sleep(0.02)
        if AUTO_ROI_MODE == "on_start":
            # old behavior (calibrate immediately)
            current_roi   = calibrate_roi(_get_latest_bgr, tries=CALIB_TRIES, sleep_s=CALIB_SLEEP) \
                            if latest_frame is not None else ROI_BOX_DEFAULT
            roi_calibrated = True
        else:
            # on_presence: start with fallback until a person appears
            current_roi   = ROI_BOX_DEFAULT
    else:
        roi_calibrated = True  # ROI stays as configured


    last_send = 0.0
    prev_bw = None
    motion_prev = None
    last_ascii_print = 0.0

    # heartbeat memory (module-local)
    hb_prev_bw = None
    present_ema = 0.0
    present_since = None
    last_hb = 0.0
    start_time = time.time()
    last_logged_sec = -1
    presence_hold_start = None          

    # grace-reset state (for 2s hold before zeroing)
    not_present_since = None
    last_active_for   = 0.0

    if use_gui:
        win_name = "Flipdot Debug  |  Full/ROI | Silhouette(+ZoomBox) | 28x28"
        try:
            cv2.startWindowThread()
            cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win_name, 1024, 340)
            cv2.moveWindow(win_name, 60, 60)
            print(f"[DBG] window ready: {win_name}")

        except Exception as _e:
            print(f"[DBG] namedWindow failed: {_e}")

    try:
        while running:
            try:
                # ---- grab latest frame ----
                with frame_lock:
                    frame = None if latest_frame is None else latest_frame.copy()
                if frame is None:
                    time.sleep(0.001)
                    continue

                # ---- convert + resize ----
                rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
                rgb_small = cv2.resize(rgb, (iw, ih), interpolation=cv2.INTER_AREA)

                # ---- vision -> 28x28 + flags ----
                if use_gui:
                    bw, motion_prev, zoom_box_hi, present_human, debug_panel = to_flipdot_matrix(
                        rgb_small, pose, hands, mirror,
                        motion_prev=motion_prev if motion_fallback else None,
                        motion_gain=motion_gain, return_debug=True,
                        ROI_BOX=current_roi, overlay_roi=True,
                        zoom_box_state=zoom_box_hi
                    )
                else:
                    bw, motion_prev, zoom_box_hi, present_human = to_flipdot_matrix(
                        rgb_small, pose, hands, mirror,
                        motion_prev=motion_prev if motion_fallback else None,
                        motion_gain=motion_gain, return_debug=False,
                        ROI_BOX=current_roi,
                        zoom_box_state=zoom_box_hi
                    )

                # --- presence detection + heartbeat (runs AFTER bw exists) ---
                _now   = time.time()
                _armed = (_now - start_time) >= ARM_AFTER_SEC

                _black = int(np.sum(bw))
                if hb_prev_bw is None:
                    hb_prev_bw = bw.copy()
                _delta_frac = float(np.mean(np.abs(bw - hb_prev_bw)))
                hb_prev_bw  = bw.copy()

                _rangeOK  = (PRESENT_MIN_BLACK <= _black <= PRESENT_MAX_BLACK)
                _motionOK = (_delta_frac >= DELTA_MIN)

                if PRESENCE_MODE == "human":
                    _raw_present = bool(_armed and present_human)
                elif PRESENCE_MODE == "pixels":
                    _raw_present = bool(_armed and _rangeOK and _motionOK)
                else:  # hybrid
                    _raw_present = bool(_armed and (present_human or (_rangeOK and _motionOK)))

                # EMA debounce
                present_ema = EMA_ALPHA*present_ema + (1.0-EMA_ALPHA)*(1.0 if _raw_present else 0.0)
                _present    = (present_ema >= 0.6)

                # --- Auto-ROI on presence (one-shot) ---
                if AUTO_ROI_DEFAULT and (AUTO_ROI_MODE == "on_presence") and not roi_calibrated:
                    if (_now - start_time) >= ROI_ARM_SEC:
                        present_for_calib = bool(present_human or (_rangeOK and _motionOK))
                        if present_for_calib:
                            if presence_hold_start is None:
                                presence_hold_start = _now
                            if (_now - presence_hold_start) >= PRESENCE_HOLD_FOR_CALIB:
                                new_roi = calibrate_roi(_get_latest_bgr, tries=CALIB_TRIES, sleep_s=CALIB_SLEEP)
                                current_roi    = new_roi
                                zoom_box_hi    = None
                                roi_calibrated = True
                                print(f"[ROI] Calibrated on presence: {new_roi}")
                        else:
                            presence_hold_start = None
                # --- end Auto-ROI on presence ---

                # ---- graceful timing with hold-before-reset ----
                did_reset_now = False  # init flag once per loop

                if _present:
                    not_present_since = None
                    if present_since is None:
                        present_since = _now
                    last_active_for = _now - present_since
                    _active_for     = last_active_for
                else:
                    if not_present_since is None:
                        not_present_since = _now
                    if (_now - not_present_since) < CLEAR_GRACE_SEC:
                        _active_for = last_active_for
                    else:
                        present_since     = None
                        not_present_since = None
                        last_active_for   = 0.0
                        _active_for       = 0.0
                        did_reset_now     = True
                # ---- end graceful timing ----

                # --- cleared logger (only after grace reset) ---
                if LOG_INTERACTION and did_reset_now and last_logged_sec != -1:
                    print("[PRESENCE] cleared", flush=True)
                    try:
                        with open(INTERACT_LOG_PATH, "a") as _lf:
                            _lf.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [PRESENCE] cleared\n")
                    except Exception:
                        pass
                    last_logged_sec = -1

                # --- interaction logger (once/sec while present) ---
                if LOG_INTERACTION:
                    _sec = int(_active_for)
                    if _present and _sec >= (last_logged_sec + 1):
                        _msg = f"[PRESENCE] active_for={_active_for:.2f}s black={_black} delta={_delta_frac:.3f}"
                        print(_msg, flush=True)
                        try:
                            with open(INTERACT_LOG_PATH, "a") as _lf:
                                _lf.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + _msg + "\n")
                        except Exception:
                            pass
                        last_logged_sec = _sec

                # --- heartbeat JSON ---
                if (_now - last_hb) >= HEARTBEAT_EVERY:
                    _hb = {
                        "ts": _now,
                        "present": bool(_present),
                        "active_for": float(_active_for),
                        "black_pixels": int(_black),
                        "delta_frac": float(_delta_frac),
                        "present_ema": float(present_ema)
                    }
                    try:
                        with open(CAM_SIGNAL_PATH, "w") as _f:
                            json.dump(_hb, _f)
                    except Exception:
                        pass
                    last_hb = _now

                # --- send to flipdot ---
                do_send = (prev_bw is None)
                if not do_send:
                    delta = float(np.mean(np.abs(bw - prev_bw)))
                    if delta > delta_th:
                        do_send = True if no_interval else (time.time() - last_send) >= min_interval

                if do_send:
                    pkt = pack_flipbytes(bw)
                    try:
                        try:
                            pkt_queue.put(pkt, timeout=0.001)
                        except queue.Full:
                            try:
                                _ = pkt_queue.get_nowait()
                            except queue.Empty:
                                pass
                            pkt_queue.put_nowait(pkt)
                        prev_bw   = bw
                        last_send = time.time()
                    except Exception as e:
                        print(f"[Q] enqueue err: {e}")

                # --- previews ---
                if use_ascii and (time.time() - last_ascii_print) >= (1.0 / max(ascii_rate, 1.0)):
                    os.system("clear")
                    print("[Preview] '#'=black, '.'=white")
                    print("\n".join("".join('#' if bw[y, x] else '.' for x in range(28)) for y in range(28)))
                    print("(Ctrl+C to quit)")
                    if LOG_INTERACTION:
                        print(f"[STATUS] present={_present} active_for={_active_for:.2f}s black={_black} delta={_delta_frac:.3f}")
                    last_ascii_print = time.time()

                if use_gui:
                    # snapshot once per second in case window is hidden
                    try:
                        _snap_last
                    except NameError:
                        _snap_last = 0.0
                    if (_now - _snap_last) >= 1.0:
                        cv2.imwrite("/home/pi/Desktop/flipdot_debug.jpg", debug_panel)
                        _snap_last = _now

                    try:
                        cv2.imshow(win_name, debug_panel)
                        key = cv2.waitKey(1) & 0xFF
                    except Exception as e:
                        print(f"[DBG] imshow/waitKey error: {e}")
                        key = 255

                    if key == ord('q'):
                        running = False
                    elif key == RECALIB_KEY:
                        print("[ROI] Recalibrating… hold still")
                        current_roi = calibrate_roi(_get_latest_bgr)
                        zoom_box_hi = None

                time.sleep(0.0005)

            except Exception as loop_err:
                print("[LOOP] Uncaught error:")
                import traceback
                traceback.print_exc()
                time.sleep(0.05)  # backoff to avoid spam

    except KeyboardInterrupt:
        pass

   
    finally:
        print("\n[SHUTDOWN] Stopping…")
        try:
            pose.close(); hands.close()
        except:
            pass
        try:
            picam2.stop()
        except:
            pass
        if use_gui:
            try:
                cv2.destroyAllWindows()
            except:
                pass
        print("[SHUTDOWN] Done.")




        