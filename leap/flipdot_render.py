#!/usr/bin/env python3
"""flipdot_render.py — shared Leap landmark -> 28x28 flipdot-grid logic.

Single source of truth for how raw Leap hand landmarks turn into the
28x28 lit/unlit grid, used by both leap_flipdot_preview.py (drives the
real panel) and leap_visualizer.py (browser preview). Kept as its own
module so the browser preview is guaranteed to show exactly what the
panel driver would draw, rather than a second reimplementation that can
drift out of sync with it.

Grid values are plain 1 (lit) / 0 (unlit) — hardware wire polarity
(WHITE_VAL) is a leap_flipdot_preview.py concern applied at serial-pack
time, not a rendering concern.

The hand is drawn as a filled silhouette, not a skeleton: the palm is a
scan-line-filled polygon (wrist + thumb base + the four knuckle MCPs),
and each finger is a tapered capsule stroke (thick at the knuckle,
narrow at the tip) stamped along its bone segments. Thickness comes
entirely from the strokes/fill — there is no post-hoc dilation pass.

Env vars (see leap_flipdot_preview.py's module docstring for full
descriptions):
  REFRESH_HZ, STALE_SEC, EASE_FACTOR, LINE_THICKNESS,
  X_RANGE_MM, Z_MIN_MM, Z_MAX_MM, GRID_ROTATE, MIRROR

GRID_ROTATE rotates the final grid counter-clockwise by 0/90/180/270
degrees, and MIRROR flips it horizontally (applied after rotation) —
display-orientation fixes applied after all the geometry above,
independent of PROJECT_AXES on the Beelink sender. Rotation alone can
never fix a left/right-swapped (mirrored) hand — rotation preserves
handedness, only a flip changes it — so a mirrored thumb needs MIRROR,
not more GRID_ROTATE. Use these once the raw capture (leap_visualizer.py's
left pane) already shows a correctly *shaped* hand but it's oriented
wrong on the panel; use PROJECT_AXES instead if the raw capture itself is
compressed/wrong-shaped (these can't fix a bad axis choice, only reorient
a good one). leap_visualizer.py's raw-capture pane applies the same
rotation + mirror so both panes turn together and stay visually
comparable.
"""

import os

GRID = 28

REFRESH_HZ = float(os.getenv("REFRESH_HZ", "18"))
STALE_SEC = float(os.getenv("STALE_SEC", "0.5"))
EASE_FACTOR = max(0.0, min(1.0, float(os.getenv("EASE_FACTOR", "0.65"))))
LINE_THICKNESS = float(os.getenv("LINE_THICKNESS", "1.1"))
# How fast the z window's center follows sustained changes in the hand's
# real position, per redraw tick (see HandRenderer) — deliberately much
# slower than EASE_FACTOR so one atypical frame (a hand mid-transition,
# a hand-type swap) can't skew the whole session, only a sustained shift.
Z_CENTER_EASE = max(0.0, min(1.0, float(os.getenv("Z_CENTER_EASE", "0.05"))))

GRID_ROTATE = int(os.getenv("GRID_ROTATE", "0")) % 360
if GRID_ROTATE not in (0, 90, 180, 270):
    raise ValueError(f"GRID_ROTATE must be 0, 90, 180, or 270, got {os.getenv('GRID_ROTATE')!r}")
_ROTATE_STEPS = GRID_ROTATE // 90

MIRROR = os.getenv("MIRROR", "0") not in ("0", "", "false", "False")

# Fixed-*size* calibration bounds. A deliberate departure from an
# auto-expanding range: expand-only bounds meant one wide gesture
# permanently shrank the hand for the rest of the session.
#
# X measured live off the real Beelink feed (current vertical mount,
# PROJECT_AXES=x,z): a single hand's per-frame spread was ~109mm median,
# centered close to 0 across multiple live captures (people naturally
# center themselves left-right in front of the panel). A single-hand-
# sized window (once ~190mm) comfortably filled ~80% of the grid for
# one hand, but clipped/hid one hand whenever both hands were up at
# once — real two-hand testing showed a combined span of 131-314mm
# (median 224mm) across both hands together, so X_RANGE_MM is sized for
# that (both hands visible) rather than a single hand filling the grid
# tightly; a lone hand will look smaller than the 75-85%-fill ideal as
# a direct consequence — that trade was chosen deliberately on user
# feedback once two-hand use came up. X uses a truly fixed, absolute
# window (not re-centered on the hand).
#
# Z (this mount's second axis) is different: per-frame *spread* was a
# consistent ~136mm median, but the *absolute* center drifted by well
# over 100mm between separate live captures (session to session, not
# frame to frame) — it isn't a stable depth-from-sensor value on this
# mount, so a fixed absolute window clipped the hand depending on
# exactly where someone happened to hold it. Z_MIN_MM/Z_MAX_MM are
# therefore an *offset window* (fixed size, ~180mm) re-centered on the
# hand's actual position each time it appears (see HandRenderer below)
# rather than absolute mm — X keeps real absolute left-right tracking,
# Z keeps a real absolute reading only within one continuous
# appearance, recentering after each stale reset.
X_RANGE_MM = float(os.getenv("X_RANGE_MM", "270"))
Z_MIN_MM = float(os.getenv("Z_MIN_MM", "-110"))
Z_MAX_MM = float(os.getenv("Z_MAX_MM", "110"))

# Finger taper: base radius (in grid cells) at the knuckle, narrowing to
# FINGER_TIP_RATIO * base at the fingertip. Thumb is drawn noticeably
# thicker than the other four fingers, matching a real hand.
FINGER_TIP_RATIO = 0.4
THUMB_RADIUS_SCALE = 1.15
_FINGER_BASE_R = LINE_THICKNESS
_FINGER_TIP_R = LINE_THICKNESS * FINGER_TIP_RATIO
_THUMB_BASE_R = LINE_THICKNESS * THUMB_RADIUS_SCALE
_THUMB_TIP_R = _THUMB_BASE_R * FINGER_TIP_RATIO

# 21-point wrist-first layout (see LEAP_HANDOFF.md): 0 wrist, 1-4 thumb
# (CMC, MCP, IP, TIP), 5-8/9-12/13-16/17-20 index/middle/ring/pinky
# (MCP, PIP, DIP, TIP).
_PALM_POLY = (0, 1, 2, 5, 9, 13, 17)

_FINGER_SPECS = [
    ((1, 2, 3, 4), _THUMB_BASE_R, _THUMB_TIP_R),
    ((5, 6, 7, 8), _FINGER_BASE_R, _FINGER_TIP_R),
    ((9, 10, 11, 12), _FINGER_BASE_R, _FINGER_TIP_R),
    ((13, 14, 15, 16), _FINGER_BASE_R, _FINGER_TIP_R),
    ((17, 18, 19, 20), _FINGER_BASE_R, _FINGER_TIP_R),
]


def _bounds_for(z_center):
    return (-X_RANGE_MM / 2, X_RANGE_MM / 2, z_center + Z_MIN_MM, z_center + Z_MAX_MM)


def _to_grid(x_mm, z_mm, bounds):
    """z increases away from the sensor, mapped so "closer" lands toward
    the bottom row (row 27) and "farther" toward the top (row 0)."""
    x_lo, x_hi, z_lo, z_hi = bounds
    col = int((x_mm - x_lo) / (x_hi - x_lo) * (GRID - 1))
    row = int((1 - (z_mm - z_lo) / (z_hi - z_lo)) * (GRID - 1))
    return max(0, min(GRID - 1, col)), max(0, min(GRID - 1, row))


def _bresenham(x0, y0, x1, y1):
    points = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return points


def _stamp_disk(frame, cx, cy, r):
    ri = int(r) + 1
    r2 = r * r
    for dy in range(-ri, ri + 1):
        y = cy + dy
        if not (0 <= y < GRID):
            continue
        row = frame[y]
        for dx in range(-ri, ri + 1):
            if dx * dx + dy * dy > r2:
                continue
            x = cx + dx
            if 0 <= x < GRID:
                row[x] = 1


def _stamp_thick_line(frame, x0, y0, x1, y1, r0, r1):
    """Bresenham centerline stamped with a disk at every point, radius
    interpolated from r0 (start) to r1 (end) — a tapered capsule."""
    pts = _bresenham(x0, y0, x1, y1)
    n = len(pts)
    for i, (px, py) in enumerate(pts):
        t = i / (n - 1) if n > 1 else 0.0
        _stamp_disk(frame, px, py, r0 + (r1 - r0) * t)


def _fill_polygon(frame, points):
    """Even-odd scan-line fill. points are (col, row) grid coords."""
    ys = [p[1] for p in points]
    y_lo, y_hi = max(0, min(ys)), min(GRID - 1, max(ys))
    n = len(points)
    for y in range(y_lo, y_hi + 1):
        xs = []
        for i in range(n):
            x0, y0 = points[i]
            x1, y1 = points[(i + 1) % n]
            if y0 == y1:
                continue
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0)
                xs.append(x0 + t * (x1 - x0))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            x_lo = max(0, int(round(xs[i])))
            x_hi = min(GRID - 1, int(round(xs[i + 1])))
            for x in range(x_lo, x_hi + 1):
                frame[y][x] = 1


def _rotate_ccw_90(frame):
    n = len(frame)
    return [[frame[col][n - 1 - row] for col in range(n)] for row in range(n)]


def _mirror_horizontal(frame):
    return [row[::-1] for row in frame]


def _apply_orientation(frame):
    for _ in range(_ROTATE_STEPS):
        frame = _rotate_ccw_90(frame)
    if MIRROR:
        frame = _mirror_horizontal(frame)
    return frame


def _draw_hand(frame, landmarks, bounds):
    grid_pts = [_to_grid(x, z, bounds) for x, z in landmarks]
    _fill_polygon(frame, [grid_pts[i] for i in _PALM_POLY])
    for bone, base_r, tip_r in _FINGER_SPECS:
        pts = [grid_pts[i] for i in bone]
        segs = len(pts) - 1
        for i in range(segs):
            r0 = base_r + (tip_r - base_r) * (i / segs)
            r1 = base_r + (tip_r - base_r) * ((i + 1) / segs)
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            _stamp_thick_line(frame, x0, y0, x1, y1, r0, r1)


def _landmarks_to_frame(hands_landmarks, bounds):
    frame = [[0] * GRID for _ in range(GRID)]
    for landmarks in hands_landmarks:
        _draw_hand(frame, landmarks, bounds)
    return _apply_orientation(frame)


def blank_frame():
    return [[0] * GRID for _ in range(GRID)]


class HandRenderer:
    """Stateful per-stream renderer: applies motion easing across
    successive landmark packets against fixed-*size* calibration bounds,
    mirroring exactly what the physical panel driver does. Easing state
    is kept per hand_type ("left"/"right") so up to two hands can be
    tracked and eased independently in the same frame.

    The z window tracks the hand's actual position with a slow-moving
    center (see Z_MIN_MM/Z_MAX_MM above) — the z axis's absolute value
    isn't stable session to session on this mount, only its span is.
    A single frame isn't trusted to set the center on its own (a hand
    caught mid-transition, or a hand-type swap, would skew the whole
    session) — instead the center eases toward the observed average at
    Z_CENTER_EASE per frame, much slower than EASE_FACTOR, so it settles
    on a sustained position over roughly a second rather than snapping
    to whatever landmark happened to arrive first."""

    def __init__(self):
        self.z_center = None
        self.eased = {}  # hand_type -> [[x, z], ...]

    def reset(self):
        """Call when the feed goes stale so reappearing hands don't ease
        in from a stale old position, and the z center starts fresh
        (instantly, from the next frame) rather than easing in from a
        stale old spot."""
        self.eased = {}
        self.z_center = None

    def update(self, hands):
        """hands: [{"hand_type": "left"/"right", "landmarks": [[x, z], ...]}, ...]
        or falsy/empty. Returns a GRIDxGRID frame of 1 (lit) / 0 (unlit)."""
        if not hands:
            self.reset()
            return blank_frame()
        all_z = [z for h in hands for _, z in h["landmarks"]]
        observed_center = sum(all_z) / len(all_z)
        if self.z_center is None:
            self.z_center = observed_center
        else:
            self.z_center += (observed_center - self.z_center) * Z_CENTER_EASE
        bounds = _bounds_for(self.z_center)
        eased_frames = []
        seen = set()
        for h in hands:
            key, landmarks = h["hand_type"], h["landmarks"]
            seen.add(key)
            prev = self.eased.get(key)
            if prev is None or len(prev) != len(landmarks):
                eased = [list(p) for p in landmarks]
            else:
                eased = prev
                for i, (tx, tz) in enumerate(landmarks):
                    eased[i][0] += (tx - eased[i][0]) * EASE_FACTOR
                    eased[i][1] += (tz - eased[i][1]) * EASE_FACTOR
            self.eased[key] = eased
            eased_frames.append(eased)
        # Drop hands no longer present so a re-appearing one doesn't ease
        # in from a stale old spot.
        for key in list(self.eased):
            if key not in seen:
                del self.eased[key]
        return _landmarks_to_frame(eased_frames, bounds)
