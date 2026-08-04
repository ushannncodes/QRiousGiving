#!/usr/bin/env python3
"""
loading.py — a one-minute loading ring, then a random animation, then the thank-you card.

One cycle:
  1. An opener, either (LOADING_STYLE):
     'ring' — a full ring is drawn, then eaten away starting at 12 o'clock and
       travelling anti-clockwise, emptying over RING_SECONDS (60). Same radius
       and thickness as anim.py's 5-4-3-2-1 countdown ring. The seconds left
       count down 60..0 in the middle of it, in the same 5x7 font as the
       'text' opener.
     'text' — TEXT_MESSAGE scrolls right-to-left TEXT_PASSES times, in the same
       5x7 font, spacing and centred row as hi5_final.py's
       "I AM A FUTURE DONATION MACHINE". 3 passes of the default message
       is about 43s.
  2. Exactly one random script from rand_anim/ runs — the same picker anim.py
     uses, so the two never repeat each other's last pick.
  3. The static THANK YOU card holds for END_CARD_TOTAL_SEC (same as anim.py).
  4. Exit. One press of the website button = one cycle; presses that arrive
     mid-cycle queue up in flipdot-api.py and play back-to-back. Set LOOP=1 to
     instead repeat forever (note: that blocks the API's job queue).

Env vars:
  SERIAL_PORT         /dev/ttyS0  panel serial port
  BAUD_RATE           57600
  LOADING_STYLE       ring        'ring' or 'text' — which opener to play
  TEXT_MESSAGE        EVERY $ HELPS!
  TEXT_PASSES         3           how many times the message scrolls past
  SCROLL_STEP         1           columns per step (as hi5_final.py)
  SCROLL_DELAY        0.1         seconds per step (as hi5_final.py)
  FONT_SPACING        1           gap between glyphs (as hi5_final.py)
  RING_SECONDS        60          seconds for the circle to close
  RING_RADIUS         11          matches COUNTDOWN_RING_RADIUS in anim.py
  RING_THICKNESS      3           matches COUNTDOWN_RING_THICKNESS in anim.py
  RING_MODE           drain       'drain' starts full and erases it, 'fill' draws it in
  RING_HOLD_SEC       0.5         hold the full ring before the drain, and the empty panel after
  RING_DIGITS         1           0 = bare ring, no seconds-left number inside it
  END_CARD_TOTAL_SEC  3.0         thank-you hold (same default as anim.py)
  RAND_ANIM_DIR       ./rand_anim (read by anim.py's picker)
  FORCE_ANIM          -           force one rand_anim script by name/glob
  LOOP                0           1 = repeat cycles until stopped
  STOP_FILE           /tmp/flipdot_stop_loading   touch to end the loop cleanly
  REFRESH_SEC         0.5         re-send interval while a static frame is held
  INVERT              0           1 swaps dot/background if the panel reads inverted
"""

import logging
import math
import os
import signal
import time

import numpy as np
import serial

# Sibling module in this folder: panel packing, the THANK YOU card and the
# random-anim picker all live there. Importing keeps the ending identical to
# anim.py's instead of maintaining a second copy of the fonts.
import anim

log = logging.getLogger("loading")

HEIGHT, WIDTH = anim.HEIGHT, anim.WIDTH

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "57600"))

LOADING_STYLE = os.getenv("LOADING_STYLE", "ring").strip().lower()

TEXT_MESSAGE = os.getenv("TEXT_MESSAGE", "EVERY $ HELPS!")
TEXT_PASSES = int(os.getenv("TEXT_PASSES", "3"))
SCROLL_STEP = int(os.getenv("SCROLL_STEP", "1"))
SCROLL_DELAY = float(os.getenv("SCROLL_DELAY", "0.1"))
FONT_SPACING = int(os.getenv("FONT_SPACING", "1"))

RING_SECONDS = float(os.getenv("RING_SECONDS", "60"))
RING_RADIUS = float(os.getenv("RING_RADIUS", str(anim.COUNTDOWN_RING_RADIUS)))
RING_THICKNESS = int(os.getenv("RING_THICKNESS", str(anim.COUNTDOWN_RING_THICKNESS)))
RING_MODE = os.getenv("RING_MODE", "drain").strip().lower()
RING_HOLD_SEC = float(os.getenv("RING_HOLD_SEC", "0.5"))
RING_TICK_SEC = float(os.getenv("RING_TICK_SEC", "0.05"))  # how often to re-check for a changed frame
RING_DIGITS = os.getenv("RING_DIGITS", "1") not in ("0", "false", "False", "")

END_CARD_TOTAL_SEC = float(os.getenv("END_CARD_TOTAL_SEC", "3.0"))
REFRESH_SEC = float(os.getenv("REFRESH_SEC", "0.5"))

LOOP = os.getenv("LOOP", "0") not in ("0", "false", "False", "")
STOP_FILE = os.getenv("STOP_FILE", "/tmp/flipdot_stop_loading")

# anim.py's convention: 1 = white background, 0 = the drawn shape.
INVERT = os.getenv("INVERT", "0") not in ("0", "false", "False", "")
BG, INK = (0, 1) if INVERT else (1, 0)

_BRUSH = anim._make_brush(RING_THICKNESS)

_stop = False


def _request_stop(signum, _frame):
    """SIGTERM/SIGINT end the cycle at the next checkpoint instead of dying mid-frame."""
    global _stop
    _stop = True
    log.info("stop requested (signal %s)", signum)


def should_stop():
    return _stop or os.path.exists(STOP_FILE)


# Copied verbatim from kiosk/hi5_final.py rather than imported: that module is
# the live hi-5 stage (Leap UDP, ~40 env vars) and it reads WHITE_VAL with the
# opposite ink convention to this file, so importing it would drag in traps for
# 45 lines of glyphs.
FONT5x7 = {
    " ": ["00000","00000","00000","00000","00000","00000","00000"],
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","11110","10001","10001","10001","11110"],
    "C": ["01110","10001","10000","10000","10000","10001","01110"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","11110","10000","10000","10000","11111"],
    "F": ["11111","10000","11110","10000","10000","10000","10000"],
    "G": ["01110","10001","10000","10111","10001","10001","01111"],
    "H": ["10001","10001","11111","10001","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["00001","00001","00001","00001","10001","10001","01110"],
    "K": ["10001","10010","11100","10010","10010","10001","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10010","10001","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","01010","00100","00100","00100","01010","10001"],
    "Y": ["10001","01010","00100","00100","00100","00100","00100"],
    "Z": ["11111","00010","00100","00100","01000","10000","11111"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    "?": ["01110","10001","00010","00100","00100","00000","00100"],
    # Neither of these is in hi5_final.py's set; unknown chars fall back to "?".
    "!": ["00100","00100","00100","00100","00100","00000","00100"],
    "$": ["00100","01111","10100","01110","00101","11110","00100"],
    "0": ["01110","10001","10001","10001","10001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","11110","00001","00001","10001","01110"],
    "6": ["00110","01000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00010","11100"],
}
GLYPH_W, GLYPH_H = 5, 7


def text_strip(text):
    """The message rendered once into a (7 x msg_w) bitmap, as hi5_final.py builds it."""
    text = text.upper()
    msg_w = len(text) * (GLYPH_W + FONT_SPACING) - FONT_SPACING
    strip = np.zeros((GLYPH_H, msg_w), dtype=np.uint8)
    x = 0
    for ch in text:
        patt = FONT5x7.get(ch, FONT5x7["?"])
        for yy, row in enumerate(patt):
            for xx, c in enumerate(row):
                if c == "1":
                    strip[yy, x + xx] = 1
        x += GLYPH_W + FONT_SPACING
    return strip


def render_text_frame(strip, offset):
    """One scroller frame with the strip's left edge at column `offset`."""
    frame = np.full((HEIGHT, WIDTH), BG, dtype=np.uint8)
    y0 = max(0, (HEIGHT - GLYPH_H) // 2)   # row 10 on a 28-row panel, same as hi5_final.py
    for yy in range(GLYPH_H):
        fy = y0 + yy
        if not (0 <= fy < HEIGHT):
            continue
        for xx in range(strip.shape[1]):
            fx = offset + xx
            if 0 <= fx < WIDTH and strip[yy, xx]:
                frame[fy, fx] = INK
    return frame


def blit_strip_centered(frame, strip):
    """Drop a text_strip bitmap in the middle of the panel, centred on both axes."""
    h, w = strip.shape
    top = HEIGHT // 2 - h // 2
    left = WIDTH // 2 - w // 2
    for yy in range(h):
        fy = top + yy
        if not (0 <= fy < HEIGHT):
            continue
        for xx in range(w):
            fx = left + xx
            if 0 <= fx < WIDTH and strip[yy, xx]:
                frame[fy, fx] = INK
    return frame


def render_ring(frac_visible, label=None):
    """
    The ring with `frac_visible` of it drawn, 1.0 being the closed circle.

    This is anim.py's render_countdown_frame arc: same centre, radius, brush and
    angular step, so the shape is pixel-identical to the 5-4-3-2-1 countdown
    ring. The arc always starts at 12 o'clock, so shrinking it opens a gap there
    that grows anti-clockwise around the face.

    `label` is the seconds-left number, drawn in the middle in the same 5x7 font
    the text opener uses. Two digits are 11x7, well inside the ring's ~19px bore,
    so the number never touches the arc.
    """
    frame = np.full((HEIGHT, WIDTH), BG, dtype=np.uint8)
    cy, cx = HEIGHT // 2, WIDTH // 2
    r = RING_RADIUS

    arc_len = max(0.0, min(1.0, frac_visible)) * 2.0 * math.pi
    if arc_len > 0:
        a = -math.pi / 2.0
        a_end = a + arc_len
        step = (1.0 / max(6.0, r * 8.0)) * 2.0 * math.pi
        while a <= a_end + 1e-6:
            yy = int(round(cy + r * math.sin(a)))
            xx = int(round(cx + r * math.cos(a)))
            for dy, dx in _BRUSH:
                y, x = yy + dy, xx + dx
                if 0 <= y < HEIGHT and 0 <= x < WIDTH:
                    frame[y, x] = INK
            a += step

    if label:
        blit_strip_centered(frame, text_strip(label))
    return frame


def send(ser, frame):
    anim.send_to_panels(ser, anim.pack_flipbytes(frame))


def hold(ser, frame, seconds):
    """
    Keep a static frame up. Flipdots are bistable — a dot holds its position with
    no power — so this re-sends only every REFRESH_SEC to paper over a dropped
    serial byte, rather than redrawing at frame rate.
    """
    panels = anim.pack_flipbytes(frame)
    t_end = time.time() + seconds
    while True:
        anim.send_to_panels(ser, panels)
        remaining = t_end - time.time()
        if remaining <= 0 or should_stop():
            return
        time.sleep(min(REFRESH_SEC, remaining))


def seconds_label(remaining):
    """
    The number to show in the ring's middle: whole seconds left, 60 down to 0.

    Rounding up means each number gets a full second on the panel — 60 is up for
    the first second, 1 for the last — and 0 lands exactly as the ring empties,
    where the closing RING_HOLD_SEC keeps it visible for a beat.
    """
    if not RING_DIGITS:
        return None
    return str(int(math.ceil(max(0.0, remaining) - 1e-9)))


def play_ring(ser):
    """Empty (or close) the ring over RING_SECONDS. False if interrupted."""
    draining = RING_MODE == "drain"
    log.info("ring: %s over %.0fs, r=%g thickness=%d digits=%s",
             "draining" if draining else "filling", RING_SECONDS,
             RING_RADIUS, RING_THICKNESS, "on" if RING_DIGITS else "off")

    # Show the starting state first — draining, that's the complete ring, which
    # is the whole point; the clock only starts once it is up.
    start_frame = render_ring(1.0 if draining else 0.0, seconds_label(RING_SECONDS))
    hold(ser, start_frame, RING_HOLD_SEC)
    if should_stop():
        return False

    last = start_frame
    t0 = time.time()
    while True:
        if should_stop():
            return False
        elapsed = time.time() - t0
        progress = min(1.0, elapsed / RING_SECONDS)
        frame = render_ring(1.0 - progress if draining else progress,
                            seconds_label(RING_SECONDS - elapsed))
        # Send only when the dots actually change. The ring itself only has 73
        # distinct states over a 60s drain, and the number changes once a second,
        # so this pushes ~130 frames instead of clattering the whole panel at
        # frame rate to no effect.
        if not np.array_equal(frame, last):
            send(ser, frame)
            last = frame
        if progress >= 1.0:
            break
        time.sleep(RING_TICK_SEC)

    hold(ser, last, RING_HOLD_SEC)
    return not should_stop()


def play_text(ser):
    """Scroll TEXT_MESSAGE across the panel TEXT_PASSES times. False if interrupted."""
    strip = text_strip(TEXT_MESSAGE)
    msg_w = strip.shape[1]
    steps = len(range(WIDTH, -msg_w, -SCROLL_STEP))
    log.info(
        "text: %r, %d cols, %.1fs per pass x%d = %.1fs",
        TEXT_MESSAGE, msg_w, steps * SCROLL_DELAY, TEXT_PASSES,
        steps * SCROLL_DELAY * TEXT_PASSES,
    )

    for _ in range(TEXT_PASSES):
        # Each pass runs the strip from just off the right edge to fully past
        # the left, exactly as render_text_scroller_centered does — so a pass
        # always ends on an empty panel and the message never jumps mid-word.
        for offset in range(WIDTH, -msg_w, -SCROLL_STEP):
            if should_stop():
                return False
            send(ser, render_text_frame(strip, offset))
            time.sleep(SCROLL_DELAY)
    return True


def play_opener(ser):
    """The opener that runs before the random animation, in whichever style is set."""
    if LOADING_STYLE == "text":
        return play_text(ser)
    return play_ring(ser)


def play_cycle():
    """Opener → one random animation → thank-you card. False if interrupted."""
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        if not play_opener(ser):
            return False
    finally:
        # Released before the child starts — it opens the same port itself.
        ser.close()

    anim.run_one_rand_script(force_pattern=os.getenv("FORCE_ANIM", "").strip() or None)
    if should_stop():
        return False

    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        hold(ser, anim.render_end_card_static(), END_CARD_TOTAL_SEC)
    finally:
        ser.close()
    return True


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    if os.path.exists(STOP_FILE):
        # A leftover sentinel from a previous run would otherwise stop us instantly.
        os.remove(STOP_FILE)
        log.info("cleared stale stop file %s", STOP_FILE)

    cycle = 0
    while True:
        cycle += 1
        log.info("cycle %d starting", cycle)
        completed = play_cycle()
        if not LOOP or not completed or should_stop():
            break

    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    log.info("finished after %d cycle(s)", cycle)


if __name__ == "__main__":
    main()
