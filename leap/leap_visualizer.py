#!/usr/bin/env python3
"""leap_visualizer.py — runs on the Raspberry Pi, standalone.

A self-contained companion to leap_receiver.py: instead of a terminal
readout, this serves a live browser page with two side-by-side views —
the raw Leap Motion capture, and a simulated preview of exactly what
leap_flipdot_preview.py would draw on the physical panel — so you can
visually confirm both the tracking and the panel rendering look right
without needing the physical panel powered on. Doesn't import or modify
any existing QRiousGiving file; the flipdot-side rendering is shared with
leap_flipdot_preview.py via flipdot_render.py so this preview can't drift
out of sync with what the real panel does.

Usage:
  python3 leap_visualizer.py
  then open http://<pi-ip>:8090 in a browser on the same network.

Env vars:
  LISTEN_PORT   UDP port the Leap data arrives on (default 5111, must
                match leap_sender.py's RPI_PORT)
  HTTP_PORT     web page port (default 8090 — port 8080 on this Pi is
                already held by another process, see LEAP_HANDOFF.md)
  STALE_SEC     hide the hand / blank the flipdot preview if no packet
                arrives within this window (default 0.5, shared with
                flipdot_render.py so both views go stale together)
  GRID_ROTATE   0/90/180/270, see flipdot_render.py — rotates both the
                flipdot pane and this raw-capture pane counter-clockwise
                by the same amount, so they turn together
  MIRROR        "1" to horizontally flip both panes (applied after
                rotation) — fixes a left/right-swapped hand, which
                GRID_ROTATE alone can never do (rotation preserves
                handedness, only a flip changes it)

The flipdot-preview pane redraws at REFRESH_HZ (see flipdot_render.py,
default 18), same as the real panel, not at the raw ~30Hz packet rate —
otherwise the motion easing would look smoother here than it actually
does on the mechanical panel.

Only needs the standard library — nothing to pip install on the Pi.
"""

import json
import math
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flipdot_render import GRID_ROTATE, MIRROR, HandRenderer, REFRESH_HZ, STALE_SEC

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8090"))

# Same defaults as kiosk/hi5_final.py, used only to color the skeleton
# open/closed — purely visual here, doesn't affect the data itself.
ANGLE_PIP_THRESH_DEG = 130.0
ANGLE_DIP_THRESH_DEG = 118.0
DIST_MARGIN = 3.0

_lock = threading.Lock()
_latest = {"ts": 0.0, "hands": None}
_flipdot_lock = threading.Lock()
_flipdot_latest = {"ts": 0.0, "frame": None}


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle_deg(a, b, c):
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    num = bax * bcx + bay * bcy
    den = math.hypot(bax, bay) * math.hypot(bcx, bcy) + 1e-9
    return math.degrees(math.acos(max(-1.0, min(1.0, num / den))))


def _extended_finger(lm, mcp_i, pip_i, dip_i, tip_i):
    wrist = lm[0]
    mcp, pip, dip, tip = lm[mcp_i], lm[pip_i], lm[dip_i], lm[tip_i]
    dist_ok = _dist(tip, wrist) > _dist(pip, wrist) + DIST_MARGIN
    angle_ok = (_angle_deg(mcp, pip, dip) >= ANGLE_PIP_THRESH_DEG and
                _angle_deg(pip, dip, tip) >= ANGLE_DIP_THRESH_DEG)
    return dist_ok and angle_ok


def is_open_palm(lm):
    idx = _extended_finger(lm, 5, 6, 7, 8)
    mid = _extended_finger(lm, 9, 10, 11, 12)
    rng = _extended_finger(lm, 13, 14, 15, 16)
    pky = _extended_finger(lm, 17, 18, 19, 20)
    return sum([idx, mid, rng, pky]) >= 2


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    print(f"[leap_visualizer] UDP listener on :{LISTEN_PORT}")
    while True:
        data, _addr = sock.recvfrom(8192)
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        with _lock:
            _latest["ts"] = time.time()
            _latest["hands"] = payload.get("hands")


def flipdot_loop():
    """Mirrors leap_flipdot_preview.py's main loop timing: redraw the
    simulated panel frame at REFRESH_HZ, blank on stale, so this preview
    matches the real panel's motion feel, not just its geometry."""
    renderer = HandRenderer()
    min_interval = 1.0 / REFRESH_HZ
    while True:
        with _lock:
            hands = _latest["hands"]
            last_ts = _latest["ts"]
        stale = (time.time() - last_ts) > STALE_SEC if last_ts else True
        frame = None if (stale or not hands) else renderer.update(hands)
        if stale or not hands:
            renderer.reset()
        with _flipdot_lock:
            _flipdot_latest["ts"] = time.time()
            _flipdot_latest["frame"] = frame
        time.sleep(min_interval)


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Leap Motion — live view</title>
<style>
  body { background:#111; color:#eee; font-family:monospace; text-align:center; margin:0; padding:20px; }
  #status { font-size:18px; margin-bottom:10px; }
  h2 { font-size:14px; font-weight:normal; color:#999; margin:0 0 8px; }
  .panes { display:flex; flex-wrap:wrap; justify-content:center; gap:24px; }
  .pane canvas { background:#000; border:1px solid #444; }
  #flipdot { background:#eee; border:1px solid #999; }
  .open { color:#7CFC00; }
  .closed { color:#FF6347; }
  .none { color:#888; }
</style>
</head>
<body>
  <div id="status">waiting for data…</div>
  <div class="panes">
    <div class="pane">
      <h2>raw Leap capture</h2>
      <canvas id="raw" width="480" height="480"></canvas>
    </div>
    <div class="pane">
      <h2>flipdot panel preview (28×28, simulated)</h2>
      <canvas id="flipdot" width="480" height="480"></canvas>
    </div>
  </div>
<script>
const FINGERS = [
  [0,1,2,3,4],
  [0,5,6,7,8],
  [0,9,10,11,12],
  [0,13,14,15,16],
  [0,17,18,19,20],
];
const GRID = 28;
const GRID_ROTATE = __GRID_ROTATE__;  // degrees CCW, from flipdot_render.py — kept in sync with the flipdot pane
const MIRROR = __MIRROR__;  // horizontal flip, applied after rotation — from flipdot_render.py

// Rotates a canvas-space point counter-clockwise around the canvas center,
// by GRID_ROTATE, so the raw-capture pane turns the same way the flipdot
// grid does (which is rotated server-side in flipdot_render.py).
function rotateCCW(px, py, canvas, steps) {
  const ccx = canvas.width / 2, ccy = canvas.height / 2;
  let dx = px - ccx, dy = py - ccy;
  for (let i = 0; i < steps; i++) {
    const ndx = dy, ndy = -dx;
    dx = ndx; dy = ndy;
  }
  return [ccx + dx, ccy + dy];
}

function drawRaw(data) {
  const canvas = document.getElementById('raw');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!data.hands || !data.hands.length) return;

  // Fit all hands into one shared bounding box so two hands stay
  // positioned relative to each other, instead of each auto-centering
  // on its own and hiding their real relative position/scale.
  const allPts = data.hands.flatMap(h => h.landmarks);
  const xs = allPts.map(p => p[0]), ys = allPts.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 40;
  const scale = Math.min(
    (canvas.width - pad*2) / Math.max(1, maxX - minX),
    (canvas.height - pad*2) / Math.max(1, maxY - minY)
  );
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const rotateSteps = GRID_ROTATE / 90;
  const proj = ([x, y]) => {
    let px = canvas.width/2 + (x - cx) * scale;
    let py = canvas.height/2 - (y - cy) * scale; // flip so "away from sensor" is up
    if (rotateSteps) [px, py] = rotateCCW(px, py, canvas, rotateSteps);
    if (MIRROR) px = canvas.width - px;
    return [px, py];
  };

  for (const hand of data.hands) {
    const lm = hand.landmarks;
    ctx.strokeStyle = hand.open_palm ? '#7CFC00' : '#FF6347';
    ctx.lineWidth = 3;
    for (const finger of FINGERS) {
      ctx.beginPath();
      finger.forEach((idx, i) => {
        const [px, py] = proj(lm[idx]);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }
    ctx.fillStyle = '#fff';
    lm.forEach(p => {
      const [px, py] = proj(p);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI*2);
      ctx.fill();
    });
  }
}

function drawFlipdot(frame) {
  const canvas = document.getElementById('flipdot');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const cell = canvas.width / GRID;
  const r = cell * 0.42;
  for (let row = 0; row < GRID; row++) {
    for (let col = 0; col < GRID; col++) {
      const lit = frame && frame[row] && frame[row][col];
      ctx.beginPath();
      ctx.arc(col*cell + cell/2, row*cell + cell/2, r, 0, Math.PI*2);
      // Matches WHITE_VAL=0 on the real panel: hand = dark dots (a
      // shadow), background = light dots — not white-on-black.
      ctx.fillStyle = lit ? '#222' : '#ccc';
      ctx.fill();
    }
  }
}

async function tick() {
  try {
    const res = await fetch('/latest');
    const data = await res.json();
    const statusEl = document.getElementById('status');

    if (!data.hands || !data.hands.length) {
      statusEl.textContent = 'no hand detected';
      statusEl.className = 'none';
    } else {
      const ageMs = (Date.now()/1000 - data.ts) * 1000;
      const parts = data.hands.map(h => `${h.hand_type} ${h.open_palm ? 'OPEN' : 'closed'}`);
      statusEl.textContent = `${parts.join(' · ')} — latency ${ageMs.toFixed(0)}ms`;
      statusEl.className = data.hands.some(h => h.open_palm) ? 'open' : 'closed';
    }
    drawRaw(data);
    drawFlipdot(data.flipdot_frame);
  } catch (e) {
    console.error(e);
  }
}
setInterval(tick, 33);
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__GRID_ROTATE__", str(GRID_ROTATE)).replace("__MIRROR__", "true" if MIRROR else "false")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def do_GET(self):
        if self.path == "/latest":
            with _lock:
                ts = _latest["ts"]
                hands = _latest["hands"]
            stale = (time.time() - ts) > STALE_SEC if ts else True
            with _flipdot_lock:
                flipdot_frame = _flipdot_latest["frame"]
            if stale or not hands:
                body = json.dumps({"ts": ts, "hands": None, "flipdot_frame": None})
            else:
                hands_out = [
                    {
                        "hand_type": h["hand_type"],
                        "landmarks": h["landmarks"],
                        "open_palm": is_open_palm(h["landmarks"]),
                    }
                    for h in hands
                ]
                body = json.dumps({
                    "ts": ts,
                    "hands": hands_out,
                    "flipdot_frame": flipdot_frame,
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=flipdot_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[leap_visualizer] open http://<this-pi-ip>:{HTTP_PORT} in a browser")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[leap_visualizer] stopping")


if __name__ == "__main__":
    main()
