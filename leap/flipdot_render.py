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

Env vars (see leap_flipdot_preview.py's module docstring for full
descriptions):
  REFRESH_HZ, STALE_SEC, EASE_FACTOR, LINE_THICKNESS,
  X_RANGE_MM, Z_MIN_MM, Z_MAX_MM, RANGE_MARGIN_MM, GRID_ROTATE, MIRROR

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

REFRESH_HZ = float(os.getenv("REFRESH_HZ", "6"))
STALE_SEC = float(os.getenv("STALE_SEC", "0.5"))
EASE_FACTOR = max(0.0, min(1.0, float(os.getenv("EASE_FACTOR", "0.35"))))
LINE_THICKNESS = int(os.getenv("LINE_THICKNESS", "1"))

GRID_ROTATE = int(os.getenv("GRID_ROTATE", "0")) % 360
if GRID_ROTATE not in (0, 90, 180, 270):
    raise ValueError(f"GRID_ROTATE must be 0, 90, 180, or 270, got {os.getenv('GRID_ROTATE')!r}")
_ROTATE_STEPS = GRID_ROTATE // 90

MIRROR = os.getenv("MIRROR", "0") not in ("0", "", "false", "False")

X_RANGE_MM = float(os.getenv("X_RANGE_MM", "300"))
Z_MIN_MM = float(os.getenv("Z_MIN_MM", "80"))
Z_MAX_MM = float(os.getenv("Z_MAX_MM", "380"))
RANGE_MARGIN_MM = float(os.getenv("RANGE_MARGIN_MM", "20"))

_FINGERS = [
    [0, 1, 2, 3, 4],
    [0, 5, 6, 7, 8],
    [0, 9, 10, 11, 12],
    [0, 13, 14, 15, 16],
    [0, 17, 18, 19, 20],
]


def _initial_bounds():
    return (-X_RANGE_MM / 2, X_RANGE_MM / 2, Z_MIN_MM, Z_MAX_MM)


def _expand_bounds(bounds, landmarks):
    x_lo, x_hi, z_lo, z_hi = bounds
    xs = [p[0] for p in landmarks]
    zs = [p[1] for p in landmarks]
    x_lo = min(x_lo, min(xs) - RANGE_MARGIN_MM)
    x_hi = max(x_hi, max(xs) + RANGE_MARGIN_MM)
    z_lo = min(z_lo, min(zs) - RANGE_MARGIN_MM)
    z_hi = max(z_hi, max(zs) + RANGE_MARGIN_MM)
    return (x_lo, x_hi, z_lo, z_hi)


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


def _dilate(frame, passes):
    for _ in range(max(0, passes)):
        src = frame
        frame = [row[:] for row in src]
        for y in range(GRID):
            for x in range(GRID):
                if not src[y][x]:
                    continue
                if x + 1 < GRID:
                    frame[y][x + 1] = 1
                if x - 1 >= 0:
                    frame[y][x - 1] = 1
                if y + 1 < GRID:
                    frame[y + 1][x] = 1
                if y - 1 >= 0:
                    frame[y - 1][x] = 1
    return frame


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


def _landmarks_to_frame(landmarks, bounds):
    frame = [[0] * GRID for _ in range(GRID)]
    grid_pts = [_to_grid(x, z, bounds) for x, z in landmarks]
    for finger in _FINGERS:
        for a, b in zip(finger, finger[1:]):
            (x0, y0), (x1, y1) = grid_pts[a], grid_pts[b]
            for px, py in _bresenham(x0, y0, x1, y1):
                frame[py][px] = 1
    for px, py in grid_pts:
        frame[py][px] = 1
    frame = _dilate(frame, LINE_THICKNESS)
    return _apply_orientation(frame)


def blank_frame():
    return [[0] * GRID for _ in range(GRID)]


class HandRenderer:
    """Stateful per-stream renderer: applies motion easing and
    auto-expanding calibration bounds across successive landmark
    packets, mirroring exactly what the physical panel driver does."""

    def __init__(self):
        self.bounds = _initial_bounds()
        self.eased = None

    def reset(self):
        """Call when the feed goes stale so reappearing hands don't ease
        in from a stale old position."""
        self.eased = None

    def update(self, landmarks):
        """landmarks: raw [[x, z], ...] or falsy. Returns a GRIDxGRID
        frame of 1 (lit) / 0 (unlit)."""
        if not landmarks:
            self.reset()
            return blank_frame()
        self.bounds = _expand_bounds(self.bounds, landmarks)
        if self.eased is None or len(self.eased) != len(landmarks):
            self.eased = [list(p) for p in landmarks]
        else:
            for i, (tx, tz) in enumerate(landmarks):
                self.eased[i][0] += (tx - self.eased[i][0]) * EASE_FACTOR
                self.eased[i][1] += (tz - self.eased[i][1]) * EASE_FACTOR
        return _landmarks_to_frame(self.eased, self.bounds)
