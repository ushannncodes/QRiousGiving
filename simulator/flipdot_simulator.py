#!/usr/bin/env python3
"""flipdot_simulator.py — virtual flipdot panel for testing without hardware.

Opens a pseudo-terminal (pty) that stands in for the real /dev/ttyS0 the
flipdot driver talks to. Decodes the same wire protocol the real panel
firmware expects (used by attract_v2.py, anim.py, hi5_final.py, qr_works.py,
rand_anim/*.py) and renders the resulting 28x28 dot grid live.

Usage:
    python3 simulator/flipdot_simulator.py

This prints a serial port path (and creates a stable symlink at
/tmp/flipdot_vserial). Point your flipdot code at it instead of the real
hardware, e.g. in another terminal:

    export FLIPDOT_SERIAL=/tmp/flipdot_vserial   # attract_v2.py, hi5_final.py, qr_works.py
    export SERIAL_PORT=/tmp/flipdot_vserial       # anim.py, rand_anim/*.py
    python3 kiosk/attract_v2.py

Rendering backend is auto-selected: a pygame window if a display is
available, otherwise a curses terminal view. Force one with --gui or
--curses.

Wire protocol (see attract_v2.py / qr_works.py _send_frame / build_packet):
    [0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F]
Each column byte packs 7 rows (bit 0 = top row of that panel), 4 panels
stacked top-to-bottom (by ascending addr) make a 28x28 display.
Bit value 1 = white/unflipped dot, bit value 0 = black/flipped dot.
"""

import argparse
import os
import pty
import select
import sys
import threading
import time

DISPLAY_W = 28
DISPLAY_H = 28
PANEL_H = 7
NUM_PANELS = 4
SYMLINK_PATH = "/tmp/flipdot_vserial"

FRAME_LEN = 32  # 0x80 0x83 addr + 28 data bytes + 0x8F


class ProtocolParser:
    """Resyncing parser for the [0x80,0x83,addr,<28 bytes>,0x8F] frames.

    Data bytes only ever use 7 bits (0x00-0x7F), so 0x80 can never appear
    inside a frame's payload — scanning for the 0x80 0x83 header is safe.
    """

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        frames = []
        while True:
            idx = self.buf.find(b"\x80\x83")
            if idx == -1:
                if len(self.buf) > 1:
                    del self.buf[:-1]
                break
            if idx > 0:
                del self.buf[:idx]
            if len(self.buf) < FRAME_LEN:
                break
            addr = self.buf[2]
            data_bytes = bytes(self.buf[3:31])
            terminator = self.buf[31]
            if terminator == 0x8F:
                frames.append((addr, data_bytes))
                del self.buf[:FRAME_LEN]
            else:
                del self.buf[:2]  # false header, resync
        return frames


class DisplayState:
    def __init__(self):
        self.lock = threading.Lock()
        self.panels = {}  # addr -> 28 bytes
        self.dirty = True
        self.frames_received = 0

    def apply(self, addr: int, data: bytes):
        with self.lock:
            self.panels[addr] = data
            self.dirty = True
            self.frames_received += 1

    def grid(self):
        """Return 28x28 grid of bools (True = flipped/black dot)."""
        with self.lock:
            addrs = sorted(self.panels)[:NUM_PANELS]
            panels = {addr: self.panels[addr] for addr in addrs}
            was_dirty = self.dirty
            self.dirty = False
            received = self.frames_received
        grid = [[False] * DISPLAY_W for _ in range(DISPLAY_H)]
        for slot, addr in enumerate(sorted(panels)):
            data = panels[addr]
            y_off = slot * PANEL_H
            for x, col_byte in enumerate(data):
                for y in range(PANEL_H):
                    bit = (col_byte >> y) & 1
                    grid[y_off + y][x] = (bit == 0)  # 0 = flipped/black
        return grid, was_dirty, received


def reader_thread(master_fd, state: DisplayState, stop_evt: threading.Event):
    parser = ProtocolParser()
    while not stop_evt.is_set():
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            # Transient EIO/hangup while no client has the slave open yet
            # (or it just closed) — keep polling, don't give up.
            time.sleep(0.05)
            continue
        if not chunk:
            continue
        for addr, data in parser.feed(chunk):
            state.apply(addr, data)


def run_pygame(state: DisplayState, stop_evt: threading.Event):
    import pygame

    dot = 18
    gap = 2
    cell = dot + gap
    margin = 10
    w = DISPLAY_W * cell + margin * 2
    h = DISPLAY_H * cell + margin * 2

    pygame.init()
    screen = pygame.display.set_mode((w, h))
    pygame.display.set_caption("flipdot simulator")
    bg = (15, 15, 15)
    off_color = (40, 40, 40)
    on_color = (255, 190, 30)

    clock = pygame.time.Clock()
    grid = [[False] * DISPLAY_W for _ in range(DISPLAY_H)]
    while not stop_evt.is_set():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                stop_evt.set()

        new_grid, dirty, received = state.grid()
        if dirty:
            grid = new_grid

        screen.fill(bg)
        for y in range(DISPLAY_H):
            for x in range(DISPLAY_W):
                color = on_color if grid[y][x] else off_color
                cx = margin + x * cell + dot // 2
                cy = margin + y * cell + dot // 2
                pygame.draw.circle(screen, color, (cx, cy), dot // 2)
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


def run_curses(state: DisplayState, stop_evt: threading.Event):
    import curses

    def _main(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        on_pair = None
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_YELLOW, curses.COLOR_BLACK)
            on_pair = curses.color_pair(1)

        grid = [[False] * DISPLAY_W for _ in range(DISPLAY_H)]
        while not stop_evt.is_set():
            try:
                ch = stdscr.getch()
                if ch in (ord("q"), 27):
                    stop_evt.set()
                    break
            except Exception:
                pass

            new_grid, dirty, received = state.grid()
            if dirty:
                grid = new_grid
                stdscr.erase()
                stdscr.addstr(0, 0, f"flipdot simulator — {received} frames received (q to quit)")
                for y in range(DISPLAY_H):
                    for x in range(DISPLAY_W):
                        ch_attr = (on_pair | curses.A_BOLD) if (on_pair and grid[y][x]) else 0
                        symbol = "██" if grid[y][x] else "··"
                        try:
                            stdscr.addstr(y + 2, x * 2, symbol, ch_attr)
                        except curses.error:
                            pass
                stdscr.refresh()
            time.sleep(0.05)

    curses.wrapper(_main)


def run_plain(state: DisplayState, stop_evt: threading.Event):
    print("flipdot simulator — no TTY/display detected, printing frames as they arrive (Ctrl-C to quit)")
    last_received = -1
    while not stop_evt.is_set():
        grid, dirty, received = state.grid()
        if dirty and received != last_received:
            last_received = received
            print(f"\n--- frame {received} ---")
            for row in grid:
                print("".join("#" if v else "." for v in row))
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gui", action="store_true", help="force the pygame renderer")
    ap.add_argument("--curses", action="store_true", help="force the curses terminal renderer")
    ap.add_argument("--baud", type=int, default=57600, help="reported baud rate (cosmetic; pty ignores it)")
    args = ap.parse_args()

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    # Keep slave_fd open for the simulator's whole lifetime. If we close it
    # immediately, the master sees a hangup the instant no client has the
    # port open yet (e.g. before the flipdot script starts) and reads can
    # raise EIO; holding it open avoids that race entirely.

    try:
        if os.path.islink(SYMLINK_PATH) or os.path.exists(SYMLINK_PATH):
            os.remove(SYMLINK_PATH)
        os.symlink(slave_path, SYMLINK_PATH)
        port_hint = SYMLINK_PATH
    except OSError:
        port_hint = slave_path

    print("flipdot simulator")
    print(f"  virtual serial port: {slave_path}")
    print(f"  stable symlink:      {port_hint}")
    print("  point your flipdot code at it, e.g.:")
    print(f"    export FLIPDOT_SERIAL={port_hint}")
    print(f"    export SERIAL_PORT={port_hint}")
    print()

    state = DisplayState()
    stop_evt = threading.Event()
    reader = threading.Thread(target=reader_thread, args=(master_fd, state, stop_evt), daemon=True)
    reader.start()

    use_gui = args.gui or (not args.curses and bool(os.environ.get("DISPLAY")))

    try:
        if use_gui:
            try:
                run_pygame(state, stop_evt)
            except ImportError:
                print("pygame not available, falling back to curses")
                use_gui = False
        if not use_gui:
            if args.curses or sys.stdout.isatty():
                run_curses(state, stop_evt)
            else:
                run_plain(state, stop_evt)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        reader.join(timeout=1.0)
        os.close(master_fd)
        os.close(slave_fd)
        try:
            os.remove(SYMLINK_PATH)
        except OSError:
            pass
        print("flipdot simulator: exited cleanly")


if __name__ == "__main__":
    main()
