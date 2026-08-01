# QRiousGiving

Interactive flipdot-display donation kiosk: presence detection → live silhouette →
palm hi-5 gesture → QR code for donation. Raspberry Pi + custom flipdot hardware.
See `README.md` for the full pipeline description and wire protocol.

## Three machines — figure out which one you're on

Check the path (`pwd` / repo root):

- **`/Users/ushan/Desktop/QRiousGiving`** — dev copy on the Mac. No physical
  hardware attached; verify changes via `simulator/flipdot_simulator.py`, and
  never claim a hardware test passed. Nothing here is live until pushed,
  pulled on the Pi, and restarted there.
- **`/home/pi/QRiousGiving`** — production on the Raspberry Pi (usually reached
  via VS Code Remote-SSH). Real flipdot panel, HuskyLens, and the Leap UDP
  receivers live here; systemd services (`runkiosk.service`,
  `flipdot-api.service`) run this copy. Before testing here, stop the service
  (`sudo systemctl stop runkiosk.service`) so you don't fight it for the I2C
  bus and serial port; restart it when done. Ask before changing anything
  under `/etc/systemd/`.
- **Beelink mini PC (Windows)** — runs the Leap Motion Controller + Ultraleap
  Gemini tracking (Leap has no Pi/ARM driver, hence this machine) and streams
  hand landmarks to the Pi over UDP. **Not a git checkout**: it runs a
  hand-copied `leap_sender.py` at
  `C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main\`. Editing
  `beelink/leap_sender.py` in this repo changes nothing on the Beelink until
  the file is manually copied over — always remind the user of this step.

## User context

Technical and comfortable with Python/git, but hardware is newer territory —
explain hardware/protocol-level details (I2C, serial, firmware quirks) in more
depth than you would code-level ones.

## Stack

- Python 3. Flask (`api/flipdot-api.py`) for the animation queue API, pygame in
  some animations, otherwise no framework.
- Config is entirely env vars with defaults — every live script's module
  docstring lists its own env vars; there's no separate config file to search for.
- Logging via the `logging` module (not `print`) in the live kiosk pipeline.

## Structure

- `leap/` — the active direction: Pi-side Leap Motion scripts (`leap_udp_relay.py`,
  `leap_flipdot_preview.py`, `leap_visualizer.py`, shared `flipdot_render.py`).
  `leap/leapc-python-bindings/` is an untracked vendored clone — never stage it.
- `beelink/` — reference copy of the Beelink's sender script (see "Three machines").
- `kiosk/` — the kiosk pipeline: `run_kiosk.py` (orchestrator/FSM),
  `attract_leap.py` (Leap presence + hand-shadow, one process, replaces the
  old `cam_v2.py`+`attract_v2.py` pair), `hi5_final.py` (now Leap-fed),
  `qr_works.py`. `cam_v2.py`, `attract_v2.py`, `attract_outline.py`,
  `DFRobot_HuskyLens.py` + `vendor/` are the retired HuskyLens pipeline —
  still present, not wired in by default, kept as reference/fallback.
- `animations/`, `api/` — flipdot animation library and the Flask queue that
  serves it.
- `simulator/` — virtual flipdot panel + browser renderer, for testing without
  hardware.
- `legacy/` — dead code kept for reference only, not part of the live pipeline.
  Don't "fix" things here, and don't import from it into `kiosk/`.

## Branches

- `leapmotion` has been merged into `main`; Leap Motion hand tracking
  (Beelink → UDP → Pi → panel) is now wired into `run_kiosk.py` for both the
  attract/shadow stage and hi-5, fully replacing the HuskyLens by default
  (see "Structure" above and `LEAP_HANDOFF.md`).
- `v2` / `flip.simulator` are prior stages. Default to whatever branch is
  currently checked out unless told otherwise.

## Running

    pip install -r requirements.txt
    python3 kiosk/run_kiosk.py               # kiosk state machine (Leap-fed by default)
    python3 api/flipdot-api.py               # animation queue API, separate process
    python3 simulator/flipdot_simulator.py   # virtual panel at :5050, no hardware needed

`run_kiosk.py` needs a live Leap Motion UDP feed arriving on `LISTEN_PORT`
(default 5111) to do anything — that means the Beelink PC's `leap_sender.py`
needs to actually be running (see "Three machines" above and the "Full
relaunch cheat sheet" in `LEAP_HANDOFF.md`). No feed = no crash, just a
kiosk that silently never detects presence. For hardware-free testing, use
`leap/synthetic_leap_udp_sender.py` in place of the Beelink instead.

## Known gotchas

- **HuskyLens I2C is genuinely flaky at the firmware level** (relevant only if
  reviving the retired HuskyLens scripts) — algorithm-switch failures, bus
  drop-offs, and corrupted landmark reads are confirmed hardware behavior,
  not necessarily a code bug. Check `HANDOFF.md` before assuming a fresh bug.
- **Don't run `run_kiosk.py` twice** — a second instance fights the first over
  the same UDP port (5111) and serial port. Check
  `ps aux | grep -E "run_kiosk|attract_leap|hi5_final"` first.
- `run_kiosk.py` waits for a killed process to actually disappear before
  starting the next one (`_wait_for_pattern_gone()`) rather than assuming a
  signal was enough — originally guarded against HuskyLens I2C D-state
  lingering, now just generic "don't start the next stage early" insurance.
  Don't remove it to "speed things up."
- `attract_leap.py`'s entire presence signal now depends on the Beelink PC
  actually running `leap_sender.py` — if it's down, the kiosk doesn't error,
  it just never sees anyone (see "Running" above).
- Only one process can bind a UDP port — the panel driver and browser preview
  can't both listen on 5111; run them through `leap_udp_relay.py`
  (see `LEAP_HANDOFF.md`, including the browser hard-refresh gotcha).
- Leap orientation problems have three independent causes — hand *shape*
  (`PROJECT_AXES`, Beelink-side), *rotation* (`GRID_ROTATE`, Pi-side), and
  *handedness* (`MIRROR`, Pi-side). Diagnose in that order per `LEAP_HANDOFF.md`;
  don't fix a mirror problem with rotation.
- Repo history was previously purged of an accidentally-committed `/home/pi` home
  directory (`.ssh`, `.cloudflared` credentials). Never stage broadly from a Pi
  checkout — add specific project files only.

## Session handoff

At the end of a substantial work session, update the relevant handoff doc
yourself — `STATUS.md`/`HANDOFF.md` for the HuskyLens kiosk pipeline,
`LEAP_HANDOFF.md` for the Leap work (what's done, what's verified, what's
next). This is the established pattern for resuming cold in a future session.
Don't wait to be asked.
