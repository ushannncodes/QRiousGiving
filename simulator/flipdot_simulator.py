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

Rendering backend defaults to a high-fidelity web view (open the printed
http://127.0.0.1:5050 URL in a browser — works great through VSCode's
auto port-forwarding over SSH/remote too). Force a different backend with
--gui (pygame window) or --curses (terminal).

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
import signal
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
            panels = dict(self.panels)
            was_dirty = self.dirty
            self.dirty = False
            received = self.frames_received
        grid = [[False] * DISPLAY_W for _ in range(DISPLAY_H)]
        for addr, data in panels.items():
            # Use the address value directly as the row slot so that any
            # stray frame with an unexpected address (e.g. addr 0 injected
            # by the pty on open) doesn't shift the entire display down.
            y_off = (addr - 1) * PANEL_H
            if y_off < 0 or y_off + PANEL_H > DISPLAY_H:
                continue  # ignore out-of-range addresses
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
                # Pack two pixel rows into each terminal row via half-block
                # glyphs. Terminal cells are roughly twice as tall as they
                # are wide, so one char per column here keeps the 28x28
                # grid square instead of needing 28 separate terminal rows
                # (which is taller than most terminal panels and gets
                # clipped at the bottom).
                attr = (on_pair | curses.A_BOLD) if on_pair else 0
                for y in range(0, DISPLAY_H, 2):
                    top_row = grid[y]
                    bottom_row = grid[y + 1] if y + 1 < DISPLAY_H else [False] * DISPLAY_W
                    for x in range(DISPLAY_W):
                        top, bottom = top_row[x], bottom_row[x]
                        if top and bottom:
                            symbol = "█"
                        elif top:
                            symbol = "▀"
                        elif bottom:
                            symbol = "▄"
                        else:
                            symbol = " "
                        try:
                            stdscr.addstr(y // 2 + 2, x, symbol, attr if (top or bottom) else 0)
                        except curses.error:
                            pass
                stdscr.refresh()
            time.sleep(0.05)

    curses.wrapper(_main)


WEB_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>flipdot simulator</title>
<style>
  html, body {
    margin: 0; height: 100%;
    background: radial-gradient(circle at 50% 0%, #d9d9d9, #b9b9b9);
    display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .frame {
    background: linear-gradient(155deg, #e7cfa0, #cba669);
    border-radius: 6px;
    padding: 26px;
    box-shadow: 0 25px 60px rgba(0,0,0,0.35), 0 2px 0 rgba(255,255,255,0.4) inset;
  }
  .mat {
    background: #fafaf8;
    padding: 28px 28px 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.25) inset;
  }
  .led {
    width: 6px; height: 6px; border-radius: 50%;
    background: #222; margin: 0 auto 14px;
  }
  canvas { display: block; }
  .caption {
    text-align: center; margin-top: 10px;
    font-size: 12px; letter-spacing: 0.06em; color: #8a8a86;
    text-transform: uppercase;
  }
</style>
</head>
<body>
  <div class="frame">
    <div class="mat">
      <div class="led"></div>
      <canvas id="panel"></canvas>
      <div class="caption" id="caption">0 frames received</div>
    </div>
  </div>
<script>
const COLS = {cols}, ROWS = {rows};
const DOT = 17, GAP = 1, CELL = DOT + GAP;
const canvas = document.getElementById("panel");
canvas.width = COLS * CELL;
canvas.height = ROWS * CELL;
const ctx = canvas.getContext("2d");

function drawDot(x, y, on) {
  const cx = x * CELL + CELL / 2;
  const cy = y * CELL + CELL / 2;
  const r = DOT / 2;
  const grad = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.3, 1, cx, cy, r);
  if (on) {
    grad.addColorStop(0, "#3a3a3a");
    grad.addColorStop(1, "#0a0a0a");
  } else {
    grad.addColorStop(0, "#ffffff");
    grad.addColorStop(1, "#d9d6cd");
  }
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = grad;
  ctx.fill();
}

function render(grid) {
  ctx.fillStyle = "#161616";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (let y = 0; y < ROWS; y++) {
    for (let x = 0; x < COLS; x++) {
      drawDot(x, y, grid[y][x]);
    }
  }
}

async function poll() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    render(data.grid);
    document.getElementById("caption").textContent = data.frames + " frames received";
  } catch (e) {
    // server not reachable yet; keep retrying
  }
  setTimeout(poll, 100);
}
poll();
</script>
</body>
</html>
"""


def run_web(state: DisplayState, stop_evt: threading.Event, host: str, port: int):
    import logging

    from flask import Flask, jsonify, Response

    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    page = WEB_PAGE.replace("{cols}", str(DISPLAY_W)).replace("{rows}", str(DISPLAY_H))

    @app.route("/")
    def index():
        return Response(page, mimetype="text/html")

    @app.route("/api/state")
    def api_state():
        grid, _dirty, received = state.grid()
        return jsonify({"grid": grid, "frames": received})

    print(f"flipdot simulator: open http://{host}:{port} in a browser")
    app.run(host=host, port=port, debug=False, use_reloader=False)


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
    ap.add_argument("--web", action="store_true", help="force the web renderer (default)")
    ap.add_argument("--gui", action="store_true", help="force the pygame renderer")
    ap.add_argument("--curses", action="store_true", help="force the curses terminal renderer")
    ap.add_argument("--host", default="127.0.0.1", help="web renderer bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5050, help="web renderer port (default 5050)")
    ap.add_argument("--baud", type=int, default=57600, help="reported baud rate (cosmetic; pty ignores it)")
    args = ap.parse_args()

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

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

    use_web = args.web or not (args.gui or args.curses)
    use_gui = args.gui

    try:
        if use_web:
            try:
                run_web(state, stop_evt, args.host, args.port)
            except ImportError:
                print("flask not available, falling back to pygame/curses")
                use_web = False
                use_gui = use_gui or bool(os.environ.get("DISPLAY"))
        if not use_web and use_gui:
            try:
                run_pygame(state, stop_evt)
            except ImportError:
                print("pygame not available, falling back to curses")
                use_gui = False
        if not use_web and not use_gui:
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
