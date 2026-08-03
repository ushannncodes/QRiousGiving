# Leap Motion → Flipdot Handoff — 2026-07-11

## TL;DR
Leap Motion Controller (original hardware) is now streaming live hand
tracking from a Beelink mini PC, over UDP, to the Raspberry Pi, and
rendering as a live skeleton on the physical flipdot panel. Confirmed
working end to end. This doc exists so a fresh chat can pick up the "make
it more fun / snappy" work without re-deriving the setup.

**Update (2026-08-01): this is now wired into the kiosk.** `kiosk/run_kiosk.py`
uses a new `kiosk/attract_leap.py` (presence + hand-shadow, one process,
reusing `flipdot_render.py`'s `HandRenderer`) for the attract stage, and
`kiosk/hi5_final.py`'s hand-gesture detection now reads this same UDP feed
instead of the HuskyLens. HuskyLens is no longer the kiosk's default sensor
for either stage — the old `cam_v2.py`/`attract_v2.py`/`attract_outline.py`
scripts are still in the repo but unused by default, kept as reference. See
"Wired into the kiosk" below for what changed and what's still untested,
and "Hi-5 detection rebuilt on Leap pose signals" for the gesture gate,
which is confirmed working against a real hand on real hardware.
The rest of this document (below) describes the standalone `leap/` demo
scripts this integration is built on top of — `leap_flipdot_preview.py`,
`leap_visualizer.py`, etc. are unchanged and still useful for isolating
rendering/orientation issues from kiosk-integration issues.

**Currently running on the Pi** (checked 2026-08-01): only
`flipdot-api.service`. The kiosk itself (`runkiosk.service`) is stopped,
and none of the `leap/` debug scripts are up. Earlier sessions left
`leap_udp_relay.py` + `leap_flipdot_preview.py` (`LISTEN_PORT=5112,
GRID_ROTATE=180, MIRROR=1`) + `leap_visualizer.py` (`LISTEN_PORT=5113,
HTTP_PORT=8090, GRID_ROTATE=180, MIRROR=1`) running to debug why the
panel was showing round blobs instead of a hand shape, then wrong
orientation, then a mirrored thumb, by comparing the raw capture against
the flipdot simulation live. **Status: orientation confirmed correct
on real hardware with `GRID_ROTATE=180, MIRROR=1`** (see "Where things
stand" below), so that relay setup is only worth restarting (per the
cheat sheet below) if orientation or rendering needs debugging again.

**Browser-cache gotcha:** the flipdot pane is computed fresh server-side
on every request, but the raw-capture pane's rotate/mirror logic is JS
baked into the page when it *loads* — restarting `leap_visualizer.py`
does NOT push new JS to a browser tab that's already open. Hard-refresh
(`Ctrl+Shift+R`) after any Pi-side restart before trusting what you see,
or the two panes can look inconsistent for a reason that has nothing to
do with the actual code.

**Update (2026-08-03): the Pi now has a static IP and the Beelink no longer
prompts for it.** A new Prolink DL-7203E LTE mobile hotspot (SIM-based,
carried with the physical setup so the network is the same everywhere the
kiosk goes) replaced whatever WiFi the Pi used before. The Pi's `wlan0` is
now pinned to `192.168.1.102` (survives reboot), and `beelink/startup_leap.bat`
hardcodes `RPI_HOST=192.168.1.102` instead of asking for it interactively at
launch. See "Network setup" below for the full details — this makes the old
`$env:RPI_HOST = "172.20.10.3"` line further down stale; ignore it in favor
of the static value.

**Where things stand on orientation (confirmed 2026-08-01):**
- `PROJECT_AXES=x,z` (on the Beelink) gives a correctly *shaped* hand —
  fingers spread out and distinguishable, not a collapsed blob. This was
  discovered by comparing `x,y` (which gave an edge-on/profile-collapsed
  fan — one axis carried no spread info for a palm facing the sensor)
  against `x,z` in `leap_visualizer.py`'s raw pane.
- `GRID_ROTATE=180` (on the Pi, in `flipdot_render.py`) fixes the display
  being upside down from the physical mount — `90` was tried first and
  left the hand upside down, `180` is the value that's actually correct.
- `MIRROR=1` (on the Pi, same module) fixes a left/right-swapped
  hand (thumb on the wrong side) — rotation alone can never fix this,
  since rotation preserves handedness and only a flip changes it.
- **Confirmed correct on real hardware** with `GRID_ROTATE=180,
  MIRROR=1` — no longer just a best guess.

## Why this exists / architecture

The original Leap Motion Controller works with Ultraleap's Gemini V5
tracking software on Windows, but **not** on Raspberry Pi directly. So the
Beelink mini PC (Windows) runs the actual sensor + tracking software, and
relays hand landmark data to the Pi over the network, where it's rendered.

```
Leap Motion Controller
        │ USB
        ▼
  Beelink (Windows, Gemini V5 tracking service)
        │ leap_sender.py — UDP, JSON landmarks, ~30Hz
        ▼
  Raspberry Pi
        │ leap_flipdot_preview.py — draws skeleton, ~6Hz
        ▼
  Physical flipdot panel (28x28)
```

`leap_receiver.py` (terminal readout) and `leap_visualizer.py` (browser
view) are lower-friction ways to check the feed is alive without needing
the physical panel powered on — useful for debugging without walking over
to the hardware. `leap_visualizer.py` now shows two canvases side by
side: the raw Leap capture, and a simulated preview of exactly what
`leap_flipdot_preview.py` would draw on the real panel — both driven off
`flipdot_render.py`, the module the two scripts share so the browser
preview can't silently drift from what the physical panel actually does.

## Landmark schema (shared across all four scripts)

21-point wrist-first layout, matching MediaPipe / HuskyLens
`ALGORITHM_HAND_RECOGNITION` (same layout `hi5_final.py` already uses):

```
0      wrist
1-4    thumb   (CMC, MCP, IP, TIP)
5-8    index   (MCP, PIP, DIP, TIP)
9-12   middle  (MCP, PIP, DIP, TIP)
13-16  ring    (MCP, PIP, DIP, TIP)
17-20  pinky   (MCP, PIP, DIP, TIP)
```

Each landmark is `[a, b]`, projected from Leap's native `(x, y, z)` mm
coordinates via `beelink/leap_sender.py`'s `_pt()` — which two axes (and
signs) get used is controlled by the `PROJECT_AXES` env var (default
`x,z`, the original desk-flat/lens-up mount), not hardcoded, since the
controller has already been remounted once (now vertical, on the panel's
right edge, lens facing outward) and the right axis pair depends entirely
on physical mounting. When the mount changes, don't guess geometrically —
try a few `PROJECT_AXES` values and watch `leap_visualizer.py`'s raw pane
while moving a hand around; see "Full relaunch cheat sheet" below.

UDP packet (Beelink → Pi), JSON:
```json
{"ts": 1752221845.0, "hands": [
  {"hand_type": "right",
   "landmarks": [[x, y], ...],
   "pose": {"grab": 0.02, "pinch": 0.05, "grab_angle": 0.31,
            "extended": [true, true, true, true, true],
            "palm_normal": [x, y, z], "palm_dir": [x, y, z],
            "confidence": 1.0}}
]}
```

### Empty-hands heartbeat (added 2026-08-01)

The sender now transmits `{"hands": []}` when the tracking volume is empty,
instead of falling silent as it used to. Silence is ambiguous on the Pi —
it can't distinguish "the hand left" from "the network hiccuped" or "the
Beelink died" — so a receiver had to wait out a staleness timeout while
still holding the last hand it saw. An explicit empty list is positive
evidence of absence and lands within one frame.

Consumers must therefore treat `hands` as *possibly present but empty*, not
just present/absent. `bool(payload.get("hands"))` is the right test and is
already what `attract_leap.py` and `hi5_palm_debug.py` use, so this was
backward-compatible for them; new consumers should follow suit. A staleness
timeout is still needed, but now genuinely means "the feed is down".

### `pose` — Gemini's own hand-pose signals (added 2026-08-01)

`landmarks` is for *rendering*; `pose` is for *gesture recognition*. Any
field the installed LeapC bindings don't expose arrives as `null` rather
than being omitted, and `leap_sender.py` prints which ones resolved on the
first tracked hand.

`grab` is the important one: **0.0 = flat open hand, 1.0 = closed fist**.
It's what `kiosk/hi5_palm_debug.py` now uses as its primary open-palm test.

Why this exists: the old open-palm test re-derived finger extension from
the 2D `landmarks`, with a bounding-box-area fallback. Both are wrong on a
Leap feed — `PROJECT_AXES` discards an axis (taking most of the finger-curl
information with it), and the fallback's `MIN_HAND_AREA=5000` was tuned in
HuskyLens sensor *pixels* while Leap landmarks are *millimetres*, so 5000
became ≈ a 70×70mm box, smaller than a fist. Result: the hi-5 fill
triggered on any hand in view regardless of shape. Don't reintroduce a
landmark-geometry or bbox-area open-palm test; threshold `pose.grab`.

`pose` is purely additive — render-only consumers (`kiosk/attract_leap.py`,
`leap/leap_flipdot_preview.py`, `leap_visualizer.py`) ignore it and are
unaffected. But **`kiosk/hi5_palm_debug.py` requires it**, and the Beelink
runs a hand-copied sender, so that script exits with instructions if
packets arrive without a `pose` block. `kiosk/hi5_final.py` still uses the
old landmark/bbox test and still has the over-triggering bug — port the
`pose` logic across once the thresholds are tuned on hardware.

## Files (all in this repo's working folder, none touch existing kiosk code)

| File | Runs on | Purpose |
|---|---|---|
| `beelink/leap_sender.py` | Beelink | Reads Leap frames via `leap` bindings, reshapes to the 21-point schema, sends UDP. Tracked here for reference, but **not synced automatically** — the Beelink runs its own copy at `C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main\leap_sender.py`; copy this file over by hand after editing it here. |
| `beelink/startup_leap.bat` | Beelink | Sets env vars (`RPI_HOST` hardcoded to `192.168.1.102`, `PROJECT_AXES`, `LEAPSDK_INSTALL_LOCATION`), activates the `leapenv` venv, and runs `leap_sender.py` — this is what the "STart up LEAP" Task Scheduler task actually launches. Same manual-copy caveat as `leap_sender.py` — live copy is at `C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main\startup_leap.bat`, copy by hand after editing here. See "Autostart on the Beelink" below. |
| `leap_receiver.py` | Pi | Terminal readout — hand type, open/closed, latency, packet rate |
| `leap_visualizer.py` | Pi | Browser view (`http://<pi-ip>:8090`) — raw capture + simulated flipdot preview, side by side |
| `leap_flipdot_preview.py` | Pi | Draws the skeleton directly on the physical panel — **confirmed working** |
| `flipdot_render.py` | Pi | Shared landmark → 28×28 grid rendering (easing, auto-ranging bounds, dilation), imported by both `leap_flipdot_preview.py` and `leap_visualizer.py` — not run directly |
| `leap_udp_relay.py` | Pi | Fans the single UDP feed out to multiple local ports so the panel driver and browser preview can run at the same time — see "Running the panel + browser preview at the same time" below |

## Environment setup — what it actually took (keep this, it's the annoying part)

**Beelink:**
- Gemini install: the SDK-only zip (`LeapDeveloperKit_5.0.0-preview`) does
  **not** include the pre-compiled `leapc_cffi` module — needed the full
  Gemini tracking software instead. Ultraleap's official download portal
  (`central.leapmotion.com`) wasn't reachable for us; installed via the
  **Ultraleap Gemini listing on Steam** instead, which worked.
- Steam installs Gemini's SDK to a non-default path:
  `C:\Program Files (x86)\Steam\steamapps\common\Ultraleap Gemini\LeapSDK`
  (not `C:\Program Files\Ultraleap\LeapSDK`, which is what the bindings
  look for by default) — needed `$env:LEAPSDK_INSTALL_LOCATION` set to the
  Steam path every session.
- Python version matters: Ultraleap's pre-compiled `leapc_cffi` module
  currently only ships for **Python 3.8** on Windows. The Beelink's main
  Python was 3.14 — installed 3.8.10 side by side via the `py` launcher,
  and built a dedicated venv (`leapenv`) for this project rather than
  touching the main install.
- `leapc-python-api` is **not on PyPI** — it only installs from a local
  clone of `github.com/ultraleap/leapc-python-bindings`
  (`pip install -e leapc-python-api` from inside that folder).
- PowerShell blocked the venv activation script by default
  (`running scripts is disabled on this system`) — fixed once per account
  with `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`.

**Pi:**
- Port 8080 is already in use by something else (`python`, PID varies) —
  `leap_visualizer.py` now defaults to `HTTP_PORT=8090` instead. If 8090
  ever collides too, check `sudo lsof -i :<port>` first.
- `leap_flipdot_preview.py` needs `pyserial`, already a repo dependency
  (`requirements.txt`), nothing extra to install there.

## Full relaunch cheat sheet

**Beelink** (PowerShell), every new session:
```powershell
cd "C:\Users\Creative Machine 02"
leapenv\Scripts\activate
$env:LEAPSDK_INSTALL_LOCATION = "C:\Program Files (x86)\Steam\steamapps\common\Ultraleap Gemini\LeapSDK"
$env:RPI_HOST = "192.168.1.102" # static now — see "Network setup" below, shouldn't change
$env:PROJECT_AXES = "x,z"       # current mount (vertical, right edge, lens outward) — gives correct SHAPE, see note below
cd "C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main"
python leap_sender.py
```
`PROJECT_AXES` picks which two of Leap's native axes get sent, each
optionally `-`-prefixed to flip sign (default `x,z`, the old desk-flat
mount — wrong for the current vertical mount). To try another candidate,
`Ctrl+C` and rerun with a different value in the same shell, e.g.
`$env:PROJECT_AXES = "z,y"; python leap_sender.py` — no need to redo the
other env vars. Watch `leap_visualizer.py`'s raw-capture pane on the Pi
while moving a hand around to judge each candidate; once one reads as a
real, well-spread hand, make it the new default in both this file and
`beelink/leap_sender.py`'s docstring.

**Pi**, in whichever combination is useful for the session — any single
script works alone with its default `LISTEN_PORT=5111` (same port
`leap_sender.py` targets):
```bash
cd ~/QRiousGiving/leap
python3 leap_flipdot_preview.py          # draws on the real panel
python3 leap_visualizer.py               # http://<pi-ip>:8090, no panel needed
python3 leap_receiver.py                 # terminal-only readout
```
Start whichever Pi-side script *before* starting `leap_sender.py` on the
Beelink, so it's already listening.

**Running the panel + browser preview at the same time:** only one
process can bind UDP 5111, so the two scripts above can't both use the
default port simultaneously. `leap_udp_relay.py` sits on 5111 (where
`leap_sender.py` sends, unchanged) and fans every packet out to two
local ports, so both consumers can run continuously side by side:
```bash
cd ~/QRiousGiving/leap
python3 leap_udp_relay.py                                                        # :5111 -> :5112, :5113
LISTEN_PORT=5112 GRID_ROTATE=180 MIRROR=1 python3 leap_flipdot_preview.py          # panel
LISTEN_PORT=5113 HTTP_PORT=8090 GRID_ROTATE=180 MIRROR=1 python3 leap_visualizer.py  # http://<pi-ip>:8090
```
Start the relay first, then the two consumers, then `leap_sender.py` on
the Beelink last. `GRID_ROTATE`/`MIRROR` must match between the two
consumers, or the panel and its browser preview will visually disagree —
see "Where things stand on orientation" above. After restarting
`leap_visualizer.py`, hard-refresh the browser tab (see "Browser-cache
gotcha" above) before judging whether it looks right.

### Starting the kiosk itself

The block above starts the standalone `leap/` *debug* scripts. To run the
actual kiosk, start `leap_sender.py` on the Beelink as above, then on the
Pi:

```bash
sudo systemctl start flipdot-api.service   # usually already running
sudo systemctl start runkiosk.service
journalctl -u runkiosk.service -f
```

Both units are pointed at this repo by `.d/override.conf` drop-ins, which
also supply `SERIAL_PORT=/dev/ttyS0`, `FLIPDOT_BAUD=57600` and
`DEBUG_LOG=1`. **The base unit files still name stale standalone copies at
`/home/pi/Desktop/run_kiosk.py` and `/home/pi/Desktop/flipdot-api.py`
(last touched Aug/Sep 2025)** — only the drop-ins keep production on the
repo. If a drop-in is ever lost, the kiosk will start cleanly and run
year-old code, with nothing obviously wrong in the logs. Confirm what's
actually loaded with `systemctl status runkiosk.service`, which prints the
resolved binary path.

To run it in a terminal instead — for Ctrl+C and env-var tweaking — stop
the service first, or the two fight over `/dev/ttyS0` and UDP 5111, and
replicate the env the drop-in supplies:

```bash
sudo systemctl stop runkiosk.service
ps aux | grep -E "run_kiosk|attract_leap|hi5_final" | grep -v grep   # expect nothing
cd ~/QRiousGiving/kiosk
SERIAL_PORT=/dev/ttyS0 FLIPDOT_SERIAL=/dev/ttyS0 FLIPDOT_BAUD=57600 \
  DEBUG_LOG=1 python3 -u run_kiosk.py
```

## Tunable knobs, for the "make it more fun" pass

All via env vars, no code edits needed for quick experiments:

- `leap_flipdot_preview.py` / `flipdot_render.py`: `REFRESH_HZ` (default
  18 now — raised from 6 once the serial link's real ceiling, ~45Hz at
  57600 baud for 4 panels, was checked; still mechanical, so don't push
  much past this without testing the physical panel), `EASE_FACTOR`
  (motion smoothing between redraws, default 0.65, raised from 0.35 for
  snappier tracking), `LINE_THICKNESS` (finger stroke *radius* in grid
  cells now, not dilation passes — default 1.6, thumb drawn at 1.3x),
  `X_RANGE_MM`/`Z_MIN_MM`/`Z_MAX_MM` (fixed bounds now, not
  auto-expanding — tuned so a hand at normal hover distance fills
  ~75-85% of the grid; see "Rendering rewrite" below), `GRID_ROTATE`
  (0/90/180/270, display rotation), `MIRROR` (1 to horizontally flip,
  fixes handedness), `WHITE_VAL`.
- `beelink/leap_sender.py`: `SEND_HZ`, `PREFERRED_HAND`, `PROJECT_AXES`
  (which raw Leap axes become the 2D landmark pair — fixes hand *shape*,
  not display orientation; see "Where things stand on orientation").

## Rendering rewrite — filled silhouette (this session)

`flipdot_render.py` no longer draws a stick-figure skeleton — it draws a
filled hand silhouette, closer to the BikoArtz flipdot hand-shadow
reference than the old line-based version:

- **Palm**: scan-line-filled polygon over `(wrist, thumb CMC, thumb MCP,
  index/middle/ring/pinky MCPs)` — `_fill_polygon()` in
  `flipdot_render.py`.
- **Fingers**: each drawn as a tapered capsule (`_stamp_thick_line()` +
  `_stamp_disk()`) — thick at the knuckle (`LINE_THICKNESS`, thumb at
  1.3x), narrowing to `FINGER_TIP_RATIO` (0.45) of that at the tip. The
  old generic `_dilate()` pixel-dilation pass is gone entirely —
  thickness now comes only from the strokes and the palm fill.
- **Bounds are fixed, not auto-expanding.** The old `_expand_bounds()`
  only ever grew the mapped range, so one wide gesture permanently
  shrank the displayed hand for the rest of the session. `X_RANGE_MM`
  (200) / `Z_MIN_MM` (100) / `Z_MAX_MM` (320) are now a fixed mapping,
  tuned against a synthetic hand model (see below) so a hand at normal
  hover distance fills ~75-85% of the 28x28 grid. Panel *position* still
  tracks the hand's real x/z, unaffected by this — only the scale is
  now fixed instead of creeping.
- `REFRESH_HZ` raised 6 → 18 (the 4-panel serial write is ~128
  bytes/frame at 57600 baud, ceiling ~45Hz, so 18Hz leaves headroom) and
  `EASE_FACTOR` raised 0.35 → 0.65 for snappier tracking.

**Not yet verified on the real panel or with the real Beelink feed** —
verified so far only against a throwaway synthetic-hand script (adult-hand
proportions, not `synthetic_hand_test.py`, which writes a different/older
JSON schema — see below) feeding `HandRenderer` directly and eyeballing
the ASCII-rendered grid. Confirmed there: open spread hand reads as a
solid palm with 5 separated tapering fingers filling ~78% of the grid in
both axes; a hand curled to a real fist's degree of bend collapses to a
compact blob. **Next session should confirm this holds with the actual
Beelink feed** via `leap_visualizer.py` before trusting it on the
physical panel, and re-tune `X_RANGE_MM`/`Z_MIN_MM`/`Z_MAX_MM` /
`LINE_THICKNESS` against a real hand if the fill percentage or stroke
weight looks off — the current numbers are a best estimate from hand
proportions, not a real-hardware measurement.

**`synthetic_hand_test.py` note:** this file (and `leap/leap_sender.py`,
`leap/attract_leap_shadow.py` in this same folder) are leftover from an
earlier prototype stage — they use a different JSON-over-file schema
(`/tmp/leap_state.json`, `{"palm": ..., "fingers": {"thumb": {"joints":
...}}}`) than the current UDP/`flipdot_render.py` schema
(`{"hand_type", "landmarks": [[x, z], ...]}`, 21 flat points). They
predate the `beelink/leap_sender.py` rewrite and aren't wired to
`leap_flipdot_preview.py`/`leap_visualizer.py` at all. Don't assume
`synthetic_hand_test.py` exercises the current pipeline without checking
the schema match first — it currently doesn't.

## Wired into the kiosk (2026-08-01)

`run_kiosk.py` now uses this pipeline as its only sensor, for both stages:

- **Attract stage**: `kiosk/attract_leap.py` (new) replaces `cam_v2.py` +
  `attract_v2.py`/`attract_outline.py` with a single process — presence
  detection and hand-shadow rendering merged into one, since (unlike
  HuskyLens's I2C bus) nothing about UDP requires split ownership across
  two processes. It reuses `flipdot_render.py`'s `HandRenderer` directly
  (same rendering as `leap_flipdot_preview.py`) and writes
  `/tmp/cam_state.json` in the same schema `cam_v2.py` used to, so
  `run_kiosk.py`'s trigger logic needed no changes.
- **Hi-5 stage**: `kiosk/hi5_final.py`'s hand-open detection now reads this
  same UDP feed instead of polling the HuskyLens over I2C — only the
  landmark *source* changed; the palm-fill rendering, hold timer, and QR
  chaining are untouched.
- Verified end-to-end against `simulator/flipdot_simulator.py` and a new
  `leap/synthetic_leap_udp_sender.py` (procedurally animated hand over the
  real UDP schema, not to be confused with the older, incompatible
  `synthetic_hand_test.py`) — full `RUN_KIOSK → HI5 → WAIT_ANIM` cycle
  confirmed working. **Since confirmed on the real Beelink feed and
  physical panel too**: a real hand drives attract → trigger → hi-5 → QR
  end to end (see "Hi-5 detection rebuilt on Leap pose signals"), and the
  idle animation was confirmed on the panel separately (see "Idle
  hourglass animation"). The retuning caveats immediately below are still
  open — "it runs on hardware" is not "the timings feel right".
- `DIST_MARGIN` (default `3`) and `MIN_HAND_AREA` (default `5000`) in
  `hi5_final.py` were tuned for HuskyLens's pixel-space landmarks; Leap's
  are real-world millimeters, so these are almost certainly wrong now —
  retune against a real hand before relying on the bbox-area fallback path.
- `run_kiosk.py`'s `WARMUP_SEC`/`TRIGGER_HOLD_SEC` (1.0s/10.0s) were tuned
  for HuskyLens's room-scale approach detection. Leap only sees a hand once
  it's already close to the panel, so these multi-second windows will
  likely feel sluggish for that near-field interaction — retune live.
- **Found, not fixed** (pre-existing, predates this session, confirmed via
  `git show HEAD:kiosk/hi5_final.py` — not introduced by the Leap swap):
  `hi5_final.py`'s presence-grace logic (`PRESENCE_GRACE_SEC` handling,
  around the `presence_now = presence_now or (...)` line) re-stamps its own
  timestamp anchor using the grace-extended value, not just the raw signal —
  so once presence is ever true even once (including the very first grace
  window at cold start), the grace window perpetually renews itself and
  `IDLE_ABORT_SEC` can never actually fire. Affects both sensor backends
  identically; out of scope for the Leap integration, flagged here for
  whoever picks it up next.

## Hi-5 detection rebuilt on Leap pose signals (2026-08-01)

The hi-5 gesture used to fill to 100% for essentially any hand in view, and
kept filling for ~0.85s after the hand was pulled away. Three causes, all
fixed:

1. **The bbox fallback fired for every hand.** `MIN_HAND_AREA=5000` was
   tuned in HuskyLens sensor pixels; Leap landmarks are millimetres, so it
   meant "bigger than ~70×70mm" — smaller than a fist. It bypassed the palm
   test entirely. Removed, along with the landmark-geometry test it fell
   back from (a 2D projection can't see finger curl reliably).
2. **`MISS_GRACE_SEC` equalled `HOLD_REQUIRED_SEC`** (both 1.5s), so a
   single detected frame kept the fill counting for the entire hold. Now
   0.35s, with a startup warning if it's ever set ≥ the hold again.
3. **A stale pose was re-detected every loop pass.** The Pi held the last
   hand it saw and re-evaluated that frozen pose for `UDP_STALE_SEC` after
   the hand left, *then* started the grace window. A pose now counts only
   on the packet it arrived in, and the sender's new empty-hands heartbeat
   cancels the grace outright.

Detection is now `pose.grab` + `is_extended` + `pinch` (see the schema
section above). `kiosk/hi5_palm_debug.py` is the tuning harness — same
detection code, no intro text, loops back on success instead of chaining
to the QR script.

**Confirmed on real hardware 2026-08-01** — a real high-five against the
real Leap + flipdot panel, with the updated `leap_sender.py` running on the
Beelink, triggers the fill and chains through to the QR. **The shipped
defaults were what ran**: `MAX_GRAB_STRENGTH=0.20`,
`MIN_EXTENDED_FINGERS=4`, `MAX_PINCH_STRENGTH=0.40`, `MISS_GRACE_SEC=0.35`,
`HOLD_REQUIRED_SEC=1.5`, `PALM_FACING_AXIS` unset — no env overrides, no
local edits. So 0.20 is a *confirmed working* threshold, not a guess.
(Commit 31226d1's message predates this and still says "not yet confirmed";
this section supersedes it.)

Before that, the same behaviour was verified against
`leap/synthetic_leap_udp_sender.py` (which now sends `pose` blocks, plus a
`SYNTH_MODE=cycle` that alternates absence/open-palm to drive the whole
kiosk unattended), with panel output to a pty. Those cases are the ones
awkward to stage by hand, and are worth rerunning after any change here:

| Case | Result | How |
|---|---|---|
| Open palm held past the hold | fills, chains to `qr_works.py` | real hand + synthetic |
| Palm held 0.7s of 1.5s then withdrawn | stops at ~38%, no trigger | synthetic |
| Closed fist held 2.4s | 0%, never triggers | synthetic |
| Hands with no `pose` block | logs ABORT, exits cleanly to the kiosk | synthetic |
| Full FSM: attract → trigger → hi-5 → QR | all milestones hit | real hand + synthetic |

Optional next tightening, only if false triggers show up in real use: set
`PALM_FACING_AXIS` to require a palm actually facing the panel rather than
held edge-on. It's mount-dependent, so read the axis off
`hi5_palm_debug.py`'s logged `palm_n=` values rather than deriving it.

## QR stage — the live donation target (2026-08-01)

`kiosk/qr_works.py` now points at **`qrgiving.framer.ai`**, replacing the
old `bit.ly/qriousgiving` short link. Overridable via the `QR_TEXT` env
var. Verified end to end: it generates at version 2 (25x25, same as the
old link) and decodes back to the new URL.

The one thing not to undo: **the URL is scheme-less on purpose.** The
panel is 28x28 and `generate_qr_image()` has a hard-coded 25x25 copy
loop, so a version-2 code is the ceiling. Adding `https://` pushes it to
version 3 (29x29), which that loop crops into something that still looks
like a QR code but no longer scans — a silent failure, not an error. Any
future URL change should be checked by decoding the generated image back,
not by eyeballing the panel. Longer URLs that don't fit can drop to
`ERROR_CORRECT_L` to get back into version 2. The full reasoning is in
the comment above `QR_TEXT`.

## Idle hourglass animation (2026-08-01)

`attract_leap.py` used to blank the panel when no hand was present. It now
draws `kiosk/hourglass.py` instead — sand draining from a top mass into a
heap below over 60s, with loose grains visibly falling between them, a beat
fully drained, then again, forever. This is what the panel shows the vast
majority of the time. A hand appearing cuts straight to the live shadow;
each return to idle restarts full rather than resuming mid-drain.

**The glass is its two side curves**, and only those — horizontal caps
across the top and bottom rows box the shape in. `HOURGLASS_OUTLINE=0`
drops the outline and lets the sand's own edges carry the shape. The
silhouette runs corner to corner, so a full heap blacks out the bottom
line, and it tapers on a sine ease rather than a straight line (straight
interpolation is geometrically correct but reads as a bowtie).

**The turnover is an eased clockwise rotation**, not a cut. It lands on
exactly 180 degrees, where a drained glass and a fresh one are the same
image, so the loop closes seamlessly — verified at 0 differing cells. Two
things make that work and are easy to break:

- The outline is built **symmetric under point reflection** (lower half
  mirrored from the upper, right wall from the left) rather than by running
  Bresenham independently over each wall. Bresenham's tie-breaking isn't
  symmetric under reflection, and drawing the walls separately left 24
  cells that didn't survive the round trip — i.e. a visible pop at the loop.
- During the turn the sand is forward-mapped as a bitmap but the outline is
  transformed as *geometry* and redrawn with Bresenham. Forward-mapping a
  one-cell-thick diagonal line while shrinking it drops neighbours and
  leaves it dashed; that's fine for a filled area, not for a stroke.

The shape runs corner to corner, so it's scaled by `1/(|cos|+|sin|)` during
the turn — full size at 0 and 180, ~0.71 at 45 — or the corners would
overhang the panel and be cut off. It also reads as tumbling rather than
sliding. An earlier version outlined the glass and sold the
turnover with an on-its-side flip beat; with nothing drawn to rotate, that
beat read as a glitch and was replaced by a plain drained pause.

It lives inside `attract_leap.py`'s process, not as a separate stage
`run_kiosk.py` switches to, because **only one process can bind the Leap UDP
port** — whatever draws the idle art has to be the same thing watching for
hands. The FSM is untouched.

Two details that are load-bearing on flipdot hardware specifically, and
shouldn't be "simplified" away:

- **The heap grows as a cone, not a rising level.** Bottom-bulb cells are
  ordered by `depth-from-the-floor + distance-from-centre * REPOSE_SLOPE`,
  whose equal-cost locus is a cone — so sand mounds up in the middle and
  spreads sideways as it gains height, at roughly sand's real angle of
  repose. Still a fixed prefix ordering, so the heap stays monotonic.
- **Sand is placed by cell count, not by row.** A 60s cycle across 28 rows
  would step once every ~2.1s, which reads as a stutter; by grain it's one
  every ~0.4s, and the boundary row fills as scattered grains (the dither).
  That ordering is deterministic and monotonic *on purpose* — a placed grain
  never moves, so the two masses never flicker.
- **`attract_leap.py` skips sending a frame identical to the last one.**
  Cheap insurance, though the falling grains mean far fewer frames repeat
  now than when the sand alone moved.

One full period is 120s drain + 0.25s hold + 0.8s turn. Cost is ~4420
dot-flips, i.e. ~2190/minute, of which the turn itself is ~1750 in one
0.8s burst.

Sand cells that the outline already covers are excluded from the grain
ordering (`_interior`). Without that, the last ~2 seconds of every drain
were spent placing grains hidden underneath the outline — the panel looked
frozen while the animation thought it was still working. Combined with the
shorter hold, the dead time before the turn went from 3.6s to 0.5s. The falling grains are most of the rest; `HOURGLASS_STREAM=0`
quietens things considerably if the clicking is ever unwelcome.

`HOURGLASS_ROTATE_SEC` (0.8) and `HOURGLASS_ROTATE_STEPS` (14) are the turn
knobs — duration and smoothness, independently. At the current pairing each
step gets 57ms, which is on the fast side of what the discs can settle in;
if the turn ever looks smeared rather than crisp, drop to `--steps 8` for
~100ms per step at the same duration. Tune either with

    python3 kiosk/hourglass.py --panel --turn-only --rotate 1.2

which replays just the turnover on a loop instead of making you sit
through a full drain to see it. Flipdots click, so if the
install ends up somewhere quiet, `HOURGLASS_STREAM_GRAINS` (default 3)
trims it proportionally and `HOURGLASS_STREAM=0` removes the grains
entirely, leaving the masses drifting silently.

Verified by decoding the panel bytes back off a pty: the idle animation
runs, a hand cuts to the shadow, and idle resumes with a full top mass
(measured: drained to 10%, then back to 100%). The full FSM
(attract → trigger → hi-5 → QR) still passes.

**Confirmed on the real panel 2026-08-01** — drain, coning heap, falling
grains and the turnover all watched on the hardware and correct, at the
shipped defaults (120s drain, 0.25s hold, 0.8s turn in 14 steps, outline
on). Orientation came out right way up as authored, so
`HOURGLASS_FLIP_VERTICAL` stays off; it's there if the panel is ever
remounted. Preview without hardware:
`python3 kiosk/hourglass.py --cycle 6 --fps 4`.

## Stale HI-5 left on the panel after an abort (2026-08-03)

**Symptom:** logs said `[KIOSK] HI5 exited → WAIT for scan/anim` and then sat
on `[WAIT] pre-anim 34.9/120.0`, but the panel still physically showed the
HI-5 outline — no hourglass — for the whole two minutes.

**Not a bug in one place; three things stacked:**

1. Flipdots are bistable. The dots hold their last position with no power and
   no refresh, so the last frame written stays *mechanically* on the panel
   until some other process overwrites it. There is no "off" to fall back to.
2. `hi5_final.py`'s abort path exited without drawing anything — it logged,
   closed the serial port and left its half-drawn HI-5 outline on the dots.
3. Nothing writes to the panel during `WAIT_ANIM`. The hourglass belongs to
   `attract_leap.py`, which only runs in `RUN_KIOSK`, and `WAIT_ANIM`'s
   pre-anim grace is `SCAN_GRACE_SEC` = **120s** long.

Worse, `run_kiosk.py` couldn't tell the two HI-5 exits apart — success (which
chains into `qr_works.py`) and idle-abort both exited 0 — so an abort, where
by definition nobody is standing there to scan anything, still bought the full
two-minute hold.

**Both fixed:**

- `hi5_final.py` now exits **3** (`EXIT_IDLE_ABORT`) on both abort paths (the
  `IDLE_ABORT_SEC` no-presence one and the no-`pose`-block/old-Beelink-sender
  one). Plain 0 still means "ran to completion" and still earns the grace.
  `run_kiosk.py` reads the code (`HI5_EXIT_IDLE_ABORT`, env-overridable) and
  on 3 goes straight back to `RUN_KIOSK`, skipping the grace entirely.
- `hi5_final.py`'s `draw_idle_handoff_frame()` draws `hourglass.py`'s **t=0**
  frame before exiting, so the panel never sits on an ended stage's frame.
  t=0 is deliberate: `attract_leap.py` calls `idle.reset()` on each return to
  idle, so a full glass is exactly the frame it draws a moment later —
  seamless handoff rather than a blank.

**Polarity trap worth knowing** (it bit nothing here only because it was
checked): `hi5_final.py` and `attract_leap.py` read the *same* `WHITE_VAL` env
var with **opposite** meanings — ink is `BLACK_VAL` in the former, `WHITE_VAL`
in the latter — and they only agree while both are left at their opposite
defaults (`1` and `0`). Setting `WHITE_VAL` globally inverts one of them. So
`draw_idle_handoff_frame()` deliberately does *not* reuse `hi5_final.py`'s
own constants; it maps ink with `attract_leap.py`'s rule and default, and was
verified bit-identical to what `attract_leap.py` packs with `WHITE_VAL` unset,
`0`, and `1`.

**Verified** (2026-08-03, on the Pi, service was already stopped):
`IDLE_ABORT_SEC=3` run of `hi5_final.py` with no UDP feed → exits 3, hourglass
reaches the panel, no handoff-frame warning. `run_kiosk.py` driven with stub
attract/HI-5 scripts → rc=3 logs `HI5 aborted (rc=3, no presence) → skipping
scan grace` with zero `[WAIT]` lines and relaunches attract immediately; rc=0
logs `HI5 exited (rc=0)` and still serves the full pre-anim grace before
returning. Not yet watched end-to-end on the real panel with a live Beelink
feed — that's the one thing left to eyeball.

## Stage timings retuned + the QR-truncation trap (2026-08-03)

`HI5_IDLE_TIMEOUT_SEC` **120s → 100s** (`run_kiosk.py`). That's the outer cap on
the hi-5 stage: how long someone can stand there without completing the palm
hold. `SCAN_GRACE_SEC` stays **120s** — deliberately different, because that
one is the QR scanning window and a real visitor needs the time.

Briefly set to 60s, then raised to 100s once the intro was measured: the
`MESSAGES` scroll in `hi5_final.py` runs **~38s** (20.7s for "I AM A FUTURE
DONATION MACHINE", 10.5s for "TO LEARN MORE", 5.1s for "HI-5", plus the big
"HI" and palm settle) *before* palm detection starts, and that comes out of
this same budget. 60s left only ~22s of real interaction time; 100s leaves
~62s. If this cap ever needs to come down again, shorten the intro first —
`SCROLL_DELAY=0.05` halves the scroll, or drop a line from `MESSAGES`.

**Worth knowing: `SCAN_GRACE_SEC` *is* the QR display duration.** `qr_works.py`
draws the QR and exits in well under a second; it doesn't hold anything. The
QR stays up only because flipdots are bistable and nothing else writes to the
panel during `WAIT_ANIM` — the same physics as the stale-HI-5 bug above. So
"how long is the QR up for" is tuned by `SCAN_GRACE_SEC` and nothing else.

**The trap the 60s change exposed:** `HI5_IDLE_TIMEOUT_SEC` counts from when
`run_kiosk.py` *spawned* HI5, and `hi5_final.py` stays alive through the
"SCAN ME" card (3s) and `qr_works.py` (~0.6s) — `qr_works.py` runs as its
child. So completing the palm hold within ~3.6s of the cap got the visitor's
QR killed a moment after they earned it. This was latent at 120s; halving it
made the window far easier to hit.

**Fix — a commit marker.** `hi5_final.py` touches `HI5_COMMIT_PATH`
(`/tmp/hi5_committed`) the instant the palm hold completes. Past that point
it's the QR flow, not an idle stage, so `run_kiosk.py` stops enforcing the cap
(logs `[HI5] committed → QR flow running; idle cap suspended`). `run_kiosk.py`
deletes the marker before every spawn, so a leftover from one visitor can't
disarm the cap for the next.

**Verified** (2026-08-03, stubs, cap forced to 4–5s): commit at 4.5s with a 5s
cap → survived 6 more seconds and exited cleanly into the grace, where before
it would have been killed at 5s. Never-commits stub → still capped and killed
as intended. Real-hardware run still pending (no Leap feed at the time).

**New: `kiosk/check_feed.py`.** Run it before `run_kiosk.py` to confirm the
Beelink's UDP feed is actually arriving. No feed = the kiosk silently never
detects anyone, which looks exactly like a broken sensor or a bad threshold,
so this rules the network out first. It reports packet rate, hands, and
whether the `pose` block is present (its absence = old sender on the Beelink,
which aborts the hi-5 stage by design). It binds 5111 itself, so nothing else
may hold the port while it runs.

## Not yet done / open ends

- **Orientation is now confirmed**: `PROJECT_AXES=x,z` + `GRID_ROTATE=180`
  + `MIRROR=1` fixes shape, rotation, and handedness, verified on real
  hardware 2026-08-01. If it ever looks off again (e.g. after a remount),
  work through it in this order: is the raw pane's *shape* wrong (fix
  `PROJECT_AXES`) → is the raw pane shaped right but rotated wrong (fix
  `GRID_ROTATE`) → is it rotated right but left/right-swapped (fix
  `MIRROR`). Don't skip straight to guessing `PROJECT_AXES` sign flips for
  what's actually a rotation/mirror problem, or vice versa — they fix
  different, non-overlapping things.
- Calibration bounds are now fixed (not auto-expanding) and the hand is
  a filled silhouette, not a skeleton — see "Rendering rewrite" above.
  Still needs real-hardware confirmation of fill % and stroke weight.
- Motion easing (`EASE_FACTOR`) and a filled, reactive-looking hand
  shape are done. Still no reaction to open/closed palm state (e.g. a
  distinct look on hi-5/spread vs. fist beyond the shape itself) — that's
  the remaining "make it more fun" surface area for the next session.
  This got much cheaper to build: `pose.grab` is now on the wire for every
  hand (0.0 open → 1.0 fist), so `attract_leap.py` can drive a look off it
  directly without any shape analysis. It currently ignores `pose`.
- `leap_receiver.py`'s `/tmp/leap_state.json` output is a separate,
  still-unused debug artifact — `attract_leap.py`/`hi5_final.py` read the
  UDP feed directly, not this file.
- **The Pi side does have systemd services** (this line used to say it
  didn't): `flipdot-api.service` is enabled and running, `runkiosk.service`
  exists but is currently stopped. Both are pointed at this repo by
  `.d/override.conf` drop-ins — see "Starting the kiosk itself" in the
  cheat sheet. **The Beelink now autostarts too** (this line, from the
  2026-08-01 audit, said it was still fully manual — that was true when it
  was written, but a Task Scheduler task was set up ~10 minutes later that
  same evening; see "Autostart on the Beelink" below for what's actually
  running there and its one remaining caveat: it requires a logged-in
  session, so it's not fully unattended).
- See "Wired into the kiosk" above for the current integration's open ends.
  The hi-5 gesture gate is no longer among them — it's confirmed on real
  hardware at its shipped defaults; see "Hi-5 detection rebuilt on Leap
  pose signals".

## Network setup (2026-08-03)

The kiosk now runs on a dedicated, portable LTE hotspot instead of whatever
WiFi happened to be available at a venue — this is what makes a hardcoded
`RPI_HOST` on the Beelink sane instead of something that breaks the moment
the setup moves.

- **Prolink DL-7203E** (SIM-based LTE mobile router, SSID `Prolink_E8EC`) is
  carried with the physical setup everywhere it goes, so the Pi and Beelink
  always join the *same* network regardless of venue — the network doesn't
  depend on venue WiFi at all.
- **Pi (`wlan0`) has a static IP**, set via NetworkManager (Raspberry Pi OS
  Bookworm default network stack):
  ```bash
  sudo nmcli connection modify "Prolink_E8EC" \
    ipv4.addresses 192.168.1.102/24 \
    ipv4.gateway 192.168.1.1 \
    ipv4.dns "192.168.1.1 8.8.8.8" \
    ipv4.method manual
  sudo nmcli connection up "Prolink_E8EC"
  ```
  Confirmed via `ip -4 addr show wlan0` showing `valid_lft forever` (not a
  DHCP lease), and confirmed to survive a reboot.
- **Router's DHCP pool narrowed** from `192.168.1.100`–`200` to
  `192.168.1.110`–`200` (Advanced Settings → Router, on the Prolink's admin
  page at `192.168.1.1`) so `.102` can never be dynamically handed out to
  another device — this firmware has no MAC-based DHCP reservation feature,
  so shrinking the pool below the Pi's static IP is the practical
  equivalent. Confirmed working: two other devices reconnecting afterward
  picked up `.110`/`.111`, not `.102`.
- **`beelink/startup_leap.bat`** hardcodes `RPI_HOST=192.168.1.102` instead
  of prompting for it (`set /p RPI_HOST=...`, previously) — see the Files
  table above and "Autostart on the Beelink" below.

## Autostart on the Beelink

**What's actually running:** a Windows Task Scheduler task named
**"STart up LEAP"** (General tab: triggers "At log on of any user", runs
under the `Creative Machine 02` account, **"Run only when user is logged
on"**) launches `beelink/startup_leap.bat`, which sets env vars and runs
`leap_sender.py`. This requires the Beelink to actually reach a logged-in
desktop session before the sensor feed starts — it is not a fully
unattended/headless boot.

**Not currently used — a drafted, untested alternative:**
`beelink/leap_sender_autostart.ps1` + `beelink/install_autostart_task.ps1`
were written to make `leap_sender.py` start with no login and no clicking
at all — a Scheduled Task firing "At startup" (before any user signs in),
running as SYSTEM, wrapping `leap_sender.py` in a reachability-check +
restart loop. These files exist in the repo but **were never installed on
the Beelink** — Task Scheduler only shows "STart up LEAP" (the `.bat`
approach above), no task named "QRiousGiving LeapSender" (what
`install_autostart_task.ps1` would register). Worth revisiting if the
kiosk ever needs to survive an unattended power-cycle with nobody around to
log in, but treat it as unverified until then:

1. Copy both scripts to
   `C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main\`.
2. Open PowerShell **as Administrator**, `cd` there, run
   `.\install_autostart_task.ps1` once.
3. Test without rebooting: `Start-ScheduledTask -TaskName "QRiousGiving LeapSender"`,
   then tail the log: `Get-Content .\leap_sender_autostart.log -Tail 20 -Wait`.
4. Reboot to confirm it comes up unattended.

To remove: `schtasks /Delete /TN "QRiousGiving LeapSender" /F`.

**Unverified assumption (if this path is ever picked up):** this only works
fully unattended if Ultraleap's Gemini tracking service itself starts
without a logged-in session. Check `services.msc` for an Ultraleap/Leap
Motion tracking service set to "Automatic" startup type. If Gemini turns
out to only run as a tray app tied to a signed-in desktop session, the
fallback is Windows auto-login (`netplwiz` → uncheck "users must enter a
password" — or the `AutoAdminLogon` registry keys under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon` for a
password-less account) so the desktop session exists before the task's
script runs. Also note its `$env:RPI_HOST = "172.20.10.3"` default is
stale — update to `192.168.1.102` if this ever gets installed for real.
