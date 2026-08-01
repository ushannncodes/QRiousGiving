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

**Currently running on the Pi** (background, via the relay setup below):
`leap_udp_relay.py`, `leap_flipdot_preview.py` (`LISTEN_PORT=5112,
GRID_ROTATE=180, MIRROR=1`), and `leap_visualizer.py` (`LISTEN_PORT=5113,
HTTP_PORT=8090, GRID_ROTATE=180, MIRROR=1`) — started to debug why the
panel was showing round blobs instead of a hand shape, then wrong
orientation, then a mirrored thumb, by comparing the raw capture against
the flipdot simulation live. **Status: orientation confirmed correct
on real hardware with `GRID_ROTATE=180, MIRROR=1`** (see "Where things
stand" below). Kill and restart per the cheat sheet below if a fresh
session needs to pick this back up.

**Browser-cache gotcha:** the flipdot pane is computed fresh server-side
on every request, but the raw-capture pane's rotate/mirror logic is JS
baked into the page when it *loads* — restarting `leap_visualizer.py`
does NOT push new JS to a browser tab that's already open. Hard-refresh
(`Ctrl+Shift+R`) after any Pi-side restart before trusting what you see,
or the two panes can look inconsistent for a reason that has nothing to
do with the actual code.

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
$env:RPI_HOST = "172.20.10.3"   # confirm this hasn't changed — check with `hostname -I` on the Pi
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
  confirmed working. **Not yet run against the real Beelink feed or
  physical panel** — do that before trusting it live.
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
- No systemd service / autostart — everything's being run manually in a
  terminal for now, on both the Pi and the Beelink.
- See "Wired into the kiosk" above for the current integration's open ends.
  The hi-5 gesture gate is no longer among them — it's confirmed on real
  hardware at its shipped defaults; see "Hi-5 detection rebuilt on Leap
  pose signals".
