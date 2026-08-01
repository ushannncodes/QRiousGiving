#!/usr/bin/env python3
"""hourglass.py — the kiosk's idle animation: a draining hourglass.

This is what the panel shows whenever nobody's hand is in front of the
sensor. `kiosk/attract_leap.py` draws it in place of the blank panel it
used to show, and switches to the live hand shadow the moment a hand
appears. It lives in the same process for a hard reason: only one process
can bind the Leap UDP port (see LEAP_HANDOFF.md), so whatever draws the
idle art has to be the same thing watching for hands.

The animation
-------------
A mass of sand up top drains away over HOURGLASS_CYCLE_SEC (default 120s)
while a heap builds below, with loose grains visibly falling between the
two. Once drained it holds a beat, then the whole glass turns over
clockwise — eased in and out, so it starts and stops gently — and the next
run begins. Forever.

The glass is drawn as its two side curves, and only those: horizontal caps
across the top and bottom rows box the shape in. HOURGLASS_OUTLINE=0 drops
the outline entirely and lets the sand's own edges describe the shape.

The silhouette runs corner to corner, so a full heap blacks out the bottom
line entirely, and it tapers on a sine ease rather than a straight line:
straight interpolation is geometrically correct but reads as a bowtie
rather than glassware.

The turn lands on exactly 180 degrees, where a drained glass and a fresh
one are the same image — so the loop closes with nothing to hide, no fade
and no jump. That only holds because the outline is built symmetric under
point reflection; see _wall_paths().

Sand is placed by *cell count*, not by row: each frame fills the first N
cells of a fixed ordering. That gives the dithered leading edge — the
boundary row fills in as scattered grains before completing, so motion
reads as near-continuous instead of a 28-step staircase (one row every
~4s at the default cycle, which would read as a stutter). Two properties of
that ordering matter on flipdot hardware specifically:

  * it's deterministic, so a cell never flickers between frames; and
  * it's monotonic, so as the pile grows a placed grain is never removed.

Both keep the panel from flipping dots it doesn't need to. Flipdots are
electromechanical and audible, so an idle animation that runs all day is
exactly where needless flips are worth avoiding — see also the frame
dedupe in attract_leap.py, which skips sending an unchanged frame at all.

Orientation: this is authored directly in panel space, top row first, so
unlike the hand shadow it needs no GRID_ROTATE/MIRROR correction — those
exist to fix how the *Leap's* coordinates map onto the panel, not the
panel itself. If the sand runs upward on real hardware, set
HOURGLASS_FLIP_VERTICAL=1 rather than touching the geometry.

Env vars:
  HOURGLASS_CYCLE_SEC ("120")       — one full drain, top mass to bottom
  HOURGLASS_SETTLE_SEC ("0.25")     — drained pause before the glass turns
  HOURGLASS_ROTATE_SEC ("0.8")      — how long the turn itself takes
  HOURGLASS_ROTATE_STEPS ("14")     — distinct angles it moves through
  HOURGLASS_FLIP_VERTICAL ("0")     — "1" if the sand appears to fall upward
  HOURGLASS_OUTLINE ("1")           — "0" for sand only, no side curves
  HOURGLASS_STREAM ("1")            — "0" to drop the falling grains
  HOURGLASS_STREAM_GRAINS ("3")     — how many are in flight at once
  HOURGLASS_STREAM_FALL_SEC ("1.1") — how long one takes to fall

Preview it without the kiosk (prints frames as text, no hardware):
    python3 kiosk/hourglass.py
    python3 kiosk/hourglass.py --cycle 6 --fps 4

Play it on the real panel, without the rest of the kiosk (no UDP port
bound, no /tmp/cam_state.json written — just the animation, for tuning the
look and confirming which way up it lands):
    python3 kiosk/hourglass.py --panel
    python3 kiosk/hourglass.py --panel --cycle 10

Tuning the turnover, without sitting through a drain to see 0.8s of motion
(--turn-only replays just the turn, on a loop):
    python3 kiosk/hourglass.py --panel --turn-only
    python3 kiosk/hourglass.py --panel --turn-only --rotate 4      # slower
    python3 kiosk/hourglass.py --panel --turn-only --rotate 1.2    # snappier
    python3 kiosk/hourglass.py --panel --turn-only --steps 24      # smoother

--rotate/--steps override HOURGLASS_ROTATE_SEC/HOURGLASS_ROTATE_STEPS for
the run; both are printed at startup so it's clear what's being tested.
Once one feels right, set it as the env default (or edit the default here)
so kiosk/attract_leap.py picks it up — the flags only affect this preview.
"""

import math
import os
import time

GRID = 28

CYCLE_SEC = float(os.getenv("HOURGLASS_CYCLE_SEC", "120"))
SETTLE_SEC = float(os.getenv("HOURGLASS_SETTLE_SEC", "0.25"))
FLIP_VERTICAL = os.getenv("HOURGLASS_FLIP_VERTICAL", "0") == "1"
DRAW_STREAM = os.getenv("HOURGLASS_STREAM", "1") == "1"
# The two side curves, drawn as a permanent outline. Set "0" for sand only,
# with nothing marking an empty bulb.
DRAW_OUTLINE = os.getenv("HOURGLASS_OUTLINE", "1") == "1"
# The turnover between runs. Steps are quantised because a flipdot can only
# physically move so fast — a smooth 18Hz sweep would ask for thousands of
# disc flips a second and just smear. ~14 distinct angles reads as rotation
# and stays within what the panel can actually do.
ROTATE_SEC = float(os.getenv("HOURGLASS_ROTATE_SEC", "0.8"))
ROTATE_STEPS = int(os.getenv("HOURGLASS_ROTATE_STEPS", "14"))
STREAM_GRAINS = int(os.getenv("HOURGLASS_STREAM_GRAINS", "3"))
STREAM_FALL_SEC = float(os.getenv("HOURGLASS_STREAM_FALL_SEC", "1.1"))
# How steeply the heap mounds up — height lost per cell of distance from the
# centre. ~0.7 is roughly sand's real angle of repose (34 degrees). Lower is
# a flatter, wider spread; higher is a sharper peak.
REPOSE_SLOPE = float(os.getenv("HOURGLASS_REPOSE_SLOPE", "0.7"))
HEAP_JITTER = float(os.getenv("HOURGLASS_HEAP_JITTER", "0.45"))

# The sand's own extent, in panel rows/columns — there is no drawn glass, so
# these bounds are the silhouette: an upper mass narrowing downward into the
# waist, a lower one widening downward from it. Corner to corner, so the
# widest rows are the panel's own top and bottom edges at full width and a
# full heap blacks out the bottom line completely.
TOP_Y, BOT_Y = 0, GRID - 1
WAIST_TOP, WAIST_BOT = 13, 14
OUTER_X0, OUTER_X1 = 0, GRID - 1
WAIST_X0, WAIST_X1 = 13, 14


def _taper(t):
    """How far the wall has closed in, 0 at the widest row, 1 at the waist.

    A sine ease rather than a straight line: the wall holds wide near the
    top and then sweeps inward toward the neck, which is what gives the
    bulbs their curve. Straight-line interpolation makes two plain
    triangles — correct, but it reads as a bowtie rather than glassware.
    """
    return 1.0 - math.cos(t * math.pi / 2.0)


def _bounds():
    """Row -> (leftmost x, rightmost x) the sand may occupy on that row."""
    rows = {}
    span = WAIST_TOP - TOP_Y
    for y in range(TOP_Y, WAIST_TOP + 1):
        g = _taper((y - TOP_Y) / span)
        rows[y] = (round(OUTER_X0 + g * (WAIST_X0 - OUTER_X0)),
                   round(OUTER_X1 + g * (WAIST_X1 - OUTER_X1)))
    for y in range(WAIST_BOT, BOT_Y + 1):
        rows[y] = rows[TOP_Y + BOT_Y - y]  # mirror about the waist
    return rows


BOUNDS = _bounds()


def _line_cells(p0, p1):
    """Bresenham, as an ordered list of cells."""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    cells = []
    while True:
        cells.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return cells
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _wall_paths():
    """The two walls as ordered, 8-connected cell paths, top to bottom.

    Built symmetric *by construction* — the lower half is the mirror of the
    upper, and the right wall the mirror of the left — rather than by
    running Bresenham independently over each. That matters because the
    turnover ends on a 180 degree rotation and then hands straight back to
    the statically drawn frame: if the outline weren't invariant under that
    rotation the handover would pop. Bresenham's tie-breaking is not
    symmetric under point reflection, so drawing each wall separately left
    24 cells that didn't survive the round trip.
    """
    pts = [(BOUNDS[y][0], y) for y in range(TOP_Y, WAIST_TOP + 1)]
    upper = []
    for a, b in zip(pts, pts[1:]):
        for cell in _line_cells(a, b):
            if not upper or upper[-1] != cell:
                upper.append(cell)
    lower = [(x, TOP_Y + BOT_Y - y) for x, y in reversed(upper)]
    left = upper + [c for c in lower if c != upper[-1]]
    right = [(OUTER_X0 + OUTER_X1 - x, y) for x, y in left]
    return left, right


LEFT_WALL, RIGHT_WALL = _wall_paths()
OUTLINE_CELLS = frozenset(LEFT_WALL) | frozenset(RIGHT_WALL)


def _interior(y):
    """Columns the sand may occupy on row `y`.

    Inclusive of the edges when there's no outline, so the mass runs right
    out to its own silhouette. When the outline *is* drawn, its cells are
    excluded — they're already lit, so sand landing on them would be an
    invisible grain. That matters for pacing, not just tidiness: those
    hidden cells are the last ones the heap reaches, and counting them left
    the animation looking frozen for two seconds at the end of every drain
    while it dutifully placed sand nobody could see.
    """
    if y not in BOUNDS:
        return []
    x0, x1 = BOUNDS[y]
    xs = range(x0, x1 + 1)
    if DRAW_OUTLINE:
        return [x for x in xs if (x, y) not in OUTLINE_CELLS]
    return list(xs)


def _dither_order(n):
    """A fixed, spread-out visiting order for `n` cells in a row.

    Walking a stride coprime with n hits every cell exactly once while
    scattering consecutive grains apart, so a half-filled row looks like
    loose sand rather than a solid bar with a hard edge. Deterministic, so
    the same partial count always yields the same cells — no flicker.
    """
    if n <= 2:
        return list(range(n))
    stride = max(2, int(n * 0.618))
    while _gcd(stride, n) != 1:
        stride += 1
    return [(i * stride) % n for i in range(n)]


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def _bulb_cells(rows):
    """Cells of a bulb, in fill order, as a flat [(x, y), ...] list.

    `rows` is ordered from the mass's base outward, so filling a prefix of
    the result stacks sand from that base — the top bulb's remaining sand
    sits against the neck, its surface descending as it drains.
    """
    cells = []
    for y in rows:
        xs = _interior(y)
        cells.extend((xs[i], y) for i in _dither_order(len(xs)))
    return cells


def _heap_cells():
    """Bottom-bulb cells ordered so the heap grows as a cone, not a level.

    Sand lands in the middle and mounds up, and the mound spreads sideways
    as it gains height — the same thing happens in a real hourglass, where
    the heap sits at its angle of repose rather than lying flat.

    Filling in ascending order of

        depth-from-the-floor + distance-from-centre * REPOSE_SLOPE

    produces exactly that: the locus of equal cost is a cone, so a prefix of
    this ordering is always a cone of some height. It also keeps growing
    correctly once the cone reaches the walls — the bulb narrows going up,
    so the remaining cells are near the centre anyway and the pile simply
    continues to climb. Crucially it's still a fixed prefix ordering, so the
    heap stays monotonic and nothing flickers.

    The jitter roughens the surface by up to a cell so the growing edge
    reads as loose grains rather than a drawn triangle; it's derived from
    the coordinates, so it's stable frame to frame.
    """
    centre = (GRID - 1) / 2.0
    ranked = []
    for y in range(WAIST_BOT, BOT_Y + 1):
        for x in _interior(y):
            jitter = (((x * 7 + y * 13) % 11) / 11.0 - 0.5) * 2 * HEAP_JITTER
            cost = (BOT_Y - y) + abs(x - centre) * REPOSE_SLOPE + jitter
            ranked.append((cost, x, y))
    ranked.sort()
    return [(x, y) for _, x, y in ranked]


# Top mass drains from the neck upward; the heap mounds up from the floor.
# Both run right to the outermost row — with no caps drawn there's nothing
# for the sand to stop short of, so it uses the full height available.
TOP_CELLS = _bulb_cells(range(WAIST_TOP, TOP_Y - 1, -1))
BOTTOM_CELLS = _heap_cells()
GRAINS = min(len(TOP_CELLS), len(BOTTOM_CELLS))


def _blank():
    return [[0] * GRID for _ in range(GRID)]


def _line(frame, p0, p1):
    """Bresenham, so a transformed wall stays connected."""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= x0 < GRID and 0 <= y0 < GRID:
            frame[y0][x0] = 1
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _draw_walls(frame):
    """The two side curves, always lit — the only outline there is.

    Deliberately no horizontal caps at the top and bottom rows: the glass
    reads as two sweeping sides, and closing it off with straight lines
    boxes the whole thing in. The curves' outermost cells land on the
    panel's corner rows anyway, so they still run the full height.

    Being always lit, these cells never toggle, which makes them free on a
    flipdot — and the sand may occupy the same columns without any handover
    flicker, since they're already on.
    """
    for wall in (LEFT_WALL, RIGHT_WALL):
        for x, y in wall:
            frame[y][x] = 1


def _pile_top_row(cells, count):
    """Highest row (smallest y) reached by the first `count` cells."""
    return min((y for _, y in cells[:count]), default=BOT_Y)


def _draw_falling(frame, t, pile_top):
    """A few loose grains in flight between the waist and the heap.

    Deliberately decoupled from the rate sand is actually placed (one grain
    per ~0.5s is far too sparse to read as falling): this is a steady
    trickle of STREAM_GRAINS dots at staggered phases, each taking
    STREAM_FALL_SEC to cross the gap. It's the one part of the animation
    that isn't monotonic — a travelling dot necessarily lights a cell and
    then clears it — but at two or three dots that costs little, and the
    motion is the whole point of it.
    """
    drop = pile_top - WAIST_BOT
    if drop <= 0:
        return
    for i in range(STREAM_GRAINS):
        phase = ((t / STREAM_FALL_SEC) + i / STREAM_GRAINS) % 1.0
        y = WAIST_BOT + int(phase * drop)
        x = WAIST_X0 if i % 2 == 0 else WAIST_X1
        if WAIST_BOT <= y < pile_top:
            frame[y][x] = 1


def _draw_sand(frame, progress, t=0.0):
    """progress 0.0 = all sand up top, 1.0 = all of it fallen."""
    fallen = int(round(progress * GRAINS))
    remaining = GRAINS - fallen

    for x, y in TOP_CELLS[:remaining]:
        frame[y][x] = 1
    for x, y in BOTTOM_CELLS[:fallen]:
        frame[y][x] = 1

    if DRAW_STREAM and 0 < fallen < GRAINS:
        _draw_falling(frame, t, _pile_top_row(BOTTOM_CELLS, fallen))


def _flip_vertical(frame):
    return frame[::-1]


def _ease_in_out(u):
    """0..1 with a gentle start and finish — a raised cosine."""
    return (1.0 - math.cos(math.pi * max(0.0, min(1.0, u)))) / 2.0


def _rotate_frame(sand, degrees):
    """Turn the glass clockwise about the panel's centre.

    The sand is a filled area, so forward-mapping its cells is safe —
    shrinking a solid region can't open holes in it. The outline is *not*:
    it's one cell thick, so pushing its cells through the same transform
    drops neighbours and leaves a dashed line. It's therefore transformed as
    a polyline and re-drawn with Bresenham, which stays connected at any
    angle.

    The shape runs corner to corner, so at 45 degrees its diagonal would
    overhang the panel and the corners would simply be cut off. Scaling by
    1/(|cos|+|sin|) shrinks it exactly enough to stay inside at every
    angle — full size at 0 and 180, ~0.71 at 45 — which also reads as the
    glass tumbling in the hand rather than sliding around flat.
    """
    rad = math.radians(degrees)
    cos, sin = math.cos(rad), math.sin(rad)
    scale = 1.0 / (abs(cos) + abs(sin))
    c = (GRID - 1) / 2.0

    def xf(x, y):
        rx, ry = x - c, y - c
        # Screen y runs downward, so the standard rotation matrix turns the
        # image clockwise as seen on the panel.
        return (int(round(c + scale * (rx * cos - ry * sin))),
                int(round(c + scale * (rx * sin + ry * cos))))

    out = _blank()
    for y in range(GRID):
        row = sand[y]
        for x in range(GRID):
            if row[x]:
                nx, ny = xf(x, y)
                if 0 <= nx < GRID and 0 <= ny < GRID:
                    out[ny][nx] = 1

    if DRAW_OUTLINE:
        for wall in (LEFT_WALL, RIGHT_WALL):
            pts = [xf(x, y) for x, y in wall]
            for a, b in zip(pts, pts[1:]):
                _line(out, a, b)
    return out


def frame_at(elapsed):
    """The frame `elapsed` seconds into the animation. Loops on its own."""
    period = CYCLE_SEC + SETTLE_SEC + ROTATE_SEC
    t = elapsed % period
    frame = _blank()
    if DRAW_OUTLINE:
        _draw_walls(frame)

    if t < CYCLE_SEC:
        _draw_sand(frame, t / CYCLE_SEC, t)
        if FLIP_VERTICAL:
            frame = _flip_vertical(frame)
        return frame

    # Drained: hold for a beat, then turn the glass over.
    spin = t - CYCLE_SEC - SETTLE_SEC
    if spin <= 0:
        _draw_sand(frame, 1.0, t)
    else:
        # Sand only here — _rotate_frame re-draws the outline itself, from
        # geometry rather than from these cells.
        sand = _blank()
        _draw_sand(sand, 1.0, t)
        # Quantise the eased progress, not the raw time, so the turn still
        # starts and finishes gently — it just does so in discrete steps.
        eased = _ease_in_out(spin / ROTATE_SEC)
        step = round(eased * ROTATE_STEPS) / ROTATE_STEPS
        frame = _rotate_frame(sand, 180.0 * step)

    if FLIP_VERTICAL:
        frame = _flip_vertical(frame)
    return frame


def turn_only_clock(t):
    """Map preview time onto the turnover, so it can be watched on a loop.

    Tuning the turn otherwise means sitting through a full drain to see 2.5
    seconds of motion. This replays just the turn, holding the finished
    glass briefly before going round again.
    """
    base = CYCLE_SEC + SETTLE_SEC
    hold = max(0.4, ROTATE_SEC * 0.25)
    u = t % (ROTATE_SEC + hold)
    # Past the end of the turn, sit on the final frame — which is a fresh
    # glass, the same image the next run starts from.
    return base + min(u, ROTATE_SEC)


class Hourglass:
    """Wall-clock driver for the animation, with the restart-on-idle rule.

    attract_leap.py calls reset() every time a hand goes away, so each
    return to idle starts a fresh glass rather than resuming mid-drain.
    """

    def __init__(self):
        self._t0 = None

    def reset(self):
        self._t0 = None

    def frame(self, now=None):
        now = time.time() if now is None else now
        if self._t0 is None:
            self._t0 = now
        return frame_at(now - self._t0)


def _play_on_panel(fps, clock=lambda t: t):
    """Drive the physical panel directly — same wire protocol and polarity
    as attract_leap.py, so what you see here is what the kiosk shows."""
    import serial

    port = os.getenv("FLIPDOT_SERIAL", "/dev/ttyS0")
    baud = int(os.getenv("FLIPDOT_BAUD", "57600"))
    white = int(os.getenv("WHITE_VAL", "0"))  # attract_leap.py's default
    black = 1 - white

    ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
    print(f"panel on {port} @ {baud}, WHITE_VAL={white} — ctrl-C to stop")

    last = None
    t0 = time.time()
    try:
        while True:
            frame = frame_at(clock(time.time() - t0))
            if frame != last:  # don't re-send an unchanged frame
                for p in range(4):
                    off = p * 7
                    data = bytearray()
                    for x in range(GRID):
                        b = 0
                        for y in range(7):
                            b |= ((white if frame[off + y][x] else black) & 1) << y
                        data.append(b)
                    ser.write(bytearray([0x80, 0x83, p + 1]) + data + bytearray([0x8F]))
                ser.flush()
                last = [row[:] for row in frame]
            time.sleep(1.0 / fps)
    except KeyboardInterrupt:
        print("\nstopping, blanking panel")
        for p in range(4):
            ser.write(bytearray([0x80, 0x83, p + 1]) + bytes([black * 0x7F] * GRID)
                      + bytearray([0x8F]))
        ser.flush()
        ser.close()


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Preview or play the idle hourglass.")
    ap.add_argument("--panel", action="store_true",
                    help="drive the real flipdot panel instead of printing text")
    ap.add_argument("--cycle", type=float, help="override HOURGLASS_CYCLE_SEC")
    ap.add_argument("--rotate", type=float, metavar="SEC",
                    help="override HOURGLASS_ROTATE_SEC — how long the turn takes")
    ap.add_argument("--steps", type=int,
                    help="override HOURGLASS_ROTATE_STEPS — distinct angles in the turn")
    ap.add_argument("--turn-only", action="store_true",
                    help="loop just the turnover, skipping the drain, for tuning it")
    ap.add_argument("--fps", type=float, default=6.0)
    ap.add_argument("--cycles", type=float, default=2.0,
                    help="how many to play (text preview only)")
    args = ap.parse_args()

    global CYCLE_SEC, ROTATE_SEC, ROTATE_STEPS
    if args.cycle:
        CYCLE_SEC = args.cycle
    if args.rotate:
        ROTATE_SEC = args.rotate
    if args.steps:
        ROTATE_STEPS = args.steps

    print(f"cycle {CYCLE_SEC:g}s drain + {SETTLE_SEC:g}s hold + {ROTATE_SEC:g}s turn "
          f"({ROTATE_STEPS} steps, {ROTATE_SEC / ROTATE_STEPS * 1000:.0f}ms each)")
    print(f"outline {'on' if DRAW_OUTLINE else 'off'}, {GRAINS} grains per bulb "
          f"(one every {CYCLE_SEC / GRAINS:.2f}s)")
    if args.turn_only:
        print("turn-only: replaying just the turnover, ctrl-C to stop")

    clock = turn_only_clock if args.turn_only else (lambda t: t)

    if args.panel:
        _play_on_panel(max(args.fps, 6.0), clock)
        return

    total = ((ROTATE_SEC + max(0.4, ROTATE_SEC * 0.25)) * args.cycles
             if args.turn_only else (CYCLE_SEC + SETTLE_SEC + ROTATE_SEC) * args.cycles)
    t = 0.0
    while t < total:
        f = frame_at(clock(t))
        out = ["\x1b[H\x1b[J" + f"t = {t:5.1f}s"]
        for row in f:
            out.append("".join("##" if c else "  " for c in row))
        print("\n".join(out), flush=True)
        time.sleep(1.0 / args.fps)
        t += 1.0 / args.fps


if __name__ == "__main__":
    main()
