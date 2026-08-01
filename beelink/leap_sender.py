#!/usr/bin/env python3
"""leap_sender.py â runs on the Beelink mini PC (Windows/Linux/Mac).

Reads hand tracking data from a Leap Motion Controller via Ultraleap's
Gemini tracking service + LeapC Python bindings, reshapes it into the same
21-point wrist-first landmark layout QRiousGiving already uses for
HuskyLens hand recognition (see kiosk/hi5_final.py's `_LM` /
`is_open_palm()`), and streams it over UDP to the Raspberry Pi as JSON.

Landmark order (matches MediaPipe / HuskyLens ALGORITHM_HAND_RECOGNITION):
  0  wrist
  1-4   thumb   (CMC, MCP, IP, TIP)
  5-8   index   (MCP, PIP, DIP, TIP)
  9-12  middle  (MCP, PIP, DIP, TIP)
  13-16 ring    (MCP, PIP, DIP, TIP)
  17-20 pinky   (MCP, PIP, DIP, TIP)

Each hand on the wire carries both `landmarks` (the 21 projected 2D points,
used for rendering the hand shadow) and `pose` (Gemini's own hand-pose
signals — grab_strength, per-digit is_extended, palm normal — used for
gesture *recognition*, see hand_to_pose()). Consumers that only render
(kiosk/attract_leap.py, leap/leap_flipdot_preview.py) can ignore `pose`
entirely; it's additive and doesn't change the landmark schema.

2D projection note:
  Which two of Leap's native (x, y, z) axes get sent as each landmark's
  2D [a, b] pair depends entirely on how the controller is physically
  mounted, and is controlled by PROJECT_AXES (see below) rather than
  hardcoded, since this has already changed once (desk-flat, lens-up ->
  mounted vertically on the panel's right edge, lens facing outward) and
  will probably change again. Leap's native axes, controller at rest:
    x = along the controller's long edge
    y = perpendicular to the lensed face (the "up" direction when lying
        flat lens-up)
    z = along the controller's short edge
  When you change the physical mount, don't guess the new axis pairing
  from geometry alone — run this with a few PROJECT_AXES candidates and
  watch leap_visualizer.py's raw-capture pane on the Pi while moving your
  hand through the gestures you actually care about (up/down, toward/away
  from the sensor); pick whichever pairing makes the plotted shape spread
  out like an actual hand instead of compressing into a blob.

Setup (once, on the Beelink):
  1. Install Ultraleap Gemini V5 tracking software for Windows
     (https://leapmotion.ultraleap.com/, "Leap Motion Controller" download,
     not "Leap Motion Controller 2"). Plug in the controller and confirm
     it tracks in the bundled Ultraleap Control Panel / Visualizer.
  2. pip install --break-system-packages leapc-python-api
     (or: git clone https://github.com/ultraleap/leapc-python-bindings,
     then pip install -e leapc-python-api, if the prebuilt wheel doesn't
     match your Python version â see that repo's README).
  3. Run this script: python leap_sender.py
     Env vars:
       RPI_HOST        IP of the Raspberry Pi on your LAN (required)
       RPI_PORT        UDP port on the Pi (default 5111)
       SEND_HZ         max send rate, throttled from Leap's own ~90-120Hz
                        frame rate (default 30)
       PREFERRED_HAND  "right", "left", or "any" (default "any" — sends
                        every currently tracked hand, up to 2; set to
                        "left"/"right" to only ever send that one hand)
       PROJECT_AXES    which two Leap axes become each landmark's [a, b],
                        comma-separated, each optionally "-"-prefixed to
                        flip its sign (default "x,z" â the original
                        desk-flat, lens-up mount). E.g. "x,y" or "-z,y".
"""

import json
import os
import socket
import time

import leap

RPI_HOST = os.environ["RPI_HOST"]  # no sane default â must point somewhere
RPI_PORT = int(os.getenv("RPI_PORT", "5111"))
SEND_HZ = float(os.getenv("SEND_HZ", "30"))
PREFERRED_HAND = os.getenv("PREFERRED_HAND", "any").lower()
MIN_INTERVAL = 1.0 / SEND_HZ

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _parse_axis(spec):
    sign = -1.0 if spec.startswith("-") else 1.0
    axis = spec.lstrip("-")
    if axis not in ("x", "y", "z"):
        raise ValueError(f"invalid axis {spec!r} in PROJECT_AXES — expected x, y, or z, optionally '-'-prefixed")
    return axis, sign


_axis_specs = [s.strip() for s in os.getenv("PROJECT_AXES", "x,z").split(",")]
if len(_axis_specs) != 2:
    raise ValueError(f"PROJECT_AXES must be two comma-separated axes, got {os.getenv('PROJECT_AXES')!r}")
_AXIS_A, _SIGN_A = _parse_axis(_axis_specs[0])
_AXIS_B, _SIGN_B = _parse_axis(_axis_specs[1])


def _pt(vec):
    """Project a Leap Vector(x, y, z) to the 2D plane we track finger
    geometry in, per PROJECT_AXES — see the module docstring."""
    return [getattr(vec, _AXIS_A) * _SIGN_A, getattr(vec, _AXIS_B) * _SIGN_B]


def _vec3(v):
    """[x, y, z] from a Leap vector, or None if it isn't one."""
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except Exception:
        return None


_POSE_PROBE_DONE = False


def _probe(obj, name):
    """getattr that degrades to None instead of raising.

    Which of Gemini's pose fields the LeapC Python bindings actually surface
    varies between binding versions (some wrap the cffi struct with an
    explicit property list, some pass everything through __getattr__). A
    field we can't read must not kill the sender — the Pi treats None as
    "this sub-test is unavailable, skip it".
    """
    try:
        val = getattr(obj, name)
    except Exception:
        return None
    return val


def hand_to_pose(hand):
    """Leap's own hand-pose signals, passed through raw for the Pi to threshold.

    These come straight out of Gemini's tracking model, which already knows
    whether the hand is open — `grab_strength` is 0.0 for a flat open hand
    and 1.0 for a closed fist, which is exactly the open-palm/high-five test
    kiosk/hi5_palm_debug.py needs. Far more reliable than re-deriving finger
    extension from the 2D landmarks below, since PROJECT_AXES discards one
    of the three axes and with it much of the finger-curl information.

    Thresholding deliberately happens on the Pi, not here: this file has to
    be hand-copied to the Beelink to take effect (see the module docstring),
    so anything you might want to *tune* belongs on the machine you can
    actually edit in place.
    """
    global _POSE_PROBE_DONE

    palm = _probe(hand, "palm")
    digits = [_probe(hand, n) for n in ("thumb", "index", "middle", "ring", "pinky")]
    extended = [_probe(d, "is_extended") if d is not None else None for d in digits]
    pose = {
        # 0.0 = flat open hand, 1.0 = fist. The primary open-palm signal.
        "grab": _probe(hand, "grab_strength"),
        "pinch": _probe(hand, "pinch_strength"),
        "grab_angle": _probe(hand, "grab_angle"),
        # [thumb, index, middle, ring, pinky]; entries may be None
        "extended": [None if e is None else bool(e) for e in extended],
        "palm_normal": _vec3(_probe(palm, "normal")) if palm is not None else None,
        "palm_dir": _vec3(_probe(palm, "direction")) if palm is not None else None,
        "confidence": _probe(hand, "confidence"),
    }
    pose = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
            for k, v in pose.items()}

    if not _POSE_PROBE_DONE:
        _POSE_PROBE_DONE = True
        got = [k for k, v in pose.items() if v is not None and v != [None] * 5]
        missing = [k for k in pose if k not in got]
        print(f"[leap_sender] pose fields available: {', '.join(got) or '(none!)'}")
        if missing:
            print(f"[leap_sender] pose fields NOT exposed by these bindings: {', '.join(missing)}")
        if pose["grab"] is None:
            print("[leap_sender] WARNING: grab_strength unavailable — the Pi's "
                  "open-palm test will fall back to the is_extended count alone")

    return pose


def hand_to_landmarks(hand):
    lm = [None] * 21
    lm[0] = _pt(hand.arm.next_joint)  # wrist

    finger_bases = {
        "thumb": 1,
        "index": 5,
        "middle": 9,
        "ring": 13,
        "pinky": 17,
    }
    for name, base in finger_bases.items():
        digit = getattr(hand, name)
        lm[base + 0] = _pt(digit.metacarpal.next_joint)   # MCP/CMC
        lm[base + 1] = _pt(digit.proximal.next_joint)     # PIP/MCP
        lm[base + 2] = _pt(digit.intermediate.next_joint) # DIP/IP
        lm[base + 3] = _pt(digit.distal.next_joint)       # TIP
    return lm


class SenderListener(leap.Listener):
    def __init__(self):
        self._last_sent = 0.0

    def on_connection_event(self, event):
        print("[leap_sender] connected to tracking service")

    def on_device_event(self, event):
        try:
            with event.device.open():
                info = event.device.get_info()
        except leap.LeapCannotOpenDeviceError:
            info = event.device.get_info()
        print(f"[leap_sender] device: {info.serial}")

    def on_tracking_event(self, event):
        now = time.time()
        if now - self._last_sent < MIN_INTERVAL:
            return  # throttle to SEND_HZ

        hands = list(event.hands)

        if hands and PREFERRED_HAND in ("left", "right"):
            wanted = "HandType.Left" if PREFERRED_HAND == "left" else "HandType.Right"
            hands = [h for h in hands if str(h.type) == wanted]

        # Keep sending when there are no hands, as {"hands": []}, rather than
        # falling silent. Silence is ambiguous on the Pi — it can't tell "the
        # hand left the tracking volume" from "the network hiccuped" or "the
        # Beelink died", so it has to wait out a staleness timeout before
        # believing the hand is gone, and during that wait it's still holding
        # the last hand it saw. That let a yanked-away open palm keep driving
        # the hi-5 fill for most of a second after it was physically gone
        # (see kiosk/hi5_palm_debug.py). An explicit empty list is positive
        # evidence of absence and lands within one frame.
        payload = {
            "ts": now,
            "hands": [
                {
                    "hand_type": "left" if str(h.type) == "HandType.Left" else "right",
                    "landmarks": hand_to_landmarks(h),
                    "pose": hand_to_pose(h),
                }
                for h in hands
            ],
        }
        msg = json.dumps(payload).encode("utf-8")
        sock.sendto(msg, (RPI_HOST, RPI_PORT))
        self._last_sent = now


def main():
    print(f"[leap_sender] sending to {RPI_HOST}:{RPI_PORT} at up to {SEND_HZ}Hz, PROJECT_AXES={_axis_specs}")
    listener = SenderListener()
    connection = leap.Connection()
    connection.add_listener(listener)

    with connection.open():
        connection.set_tracking_mode(leap.TrackingMode.Desktop)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[leap_sender] stopping")


if __name__ == "__main__":
    main()
