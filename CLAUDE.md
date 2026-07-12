# QRiousGiving

Interactive flipdot-display donation kiosk: presence detection → live silhouette →
palm hi-5 gesture → QR code for donation. Raspberry Pi + custom flipdot hardware.
See `README.md` for the full pipeline description and wire protocol.

## Two copies — figure out which one you're on

The repo is checked out in two places. Check the path (`pwd` / repo root):

- **`/Users/ushan/Desktop/QRiousGiving`** — dev copy on the Mac. No physical
  hardware attached; verify changes via `simulator/flipdot_simulator.py`, and
  never claim a hardware test passed. Nothing here is live until pushed,
  pulled on the Pi, and the service restarted.
- **`/home/pi/QRiousGiving`** — production on the Raspberry Pi (usually reached
  via VS Code Remote-SSH). Real HuskyLens and flipdot panel may be attached,
  and systemd services (`runkiosk.service`, `flipdot-api.service`) run this
  copy. Before testing here, stop the service
  (`sudo systemctl stop runkiosk.service`) so you don't fight it for the I2C
  bus and serial port; restart it when done. Ask before changing anything
  under `/etc/systemd/`.

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

- `kiosk/` — the live pipeline: `run_kiosk.py` (orchestrator/FSM), `cam_v2.py`,
  `attract_v2.py`, `hi5_final.py`, `qr_works.py`, `DFRobot_HuskyLens.py` + `vendor/`.
- `animations/`, `api/` — flipdot animation library and the Flask queue that
  serves it.
- `simulator/` — virtual flipdot panel + browser renderer, for testing without
  hardware (branch-specific, see README).
- `leap/` — Leap Motion hand tracking, the active new direction (see below).
- `legacy/` — dead code kept for reference only, not part of the live pipeline.
  Don't "fix" things here, and don't import from it into `kiosk/`.

## Branches

- `leapmotion` (current) is the active direction: Leap Motion hand tracking
  replacing/supplementing the HuskyLens. Treat it as current work, not a
  throwaway experiment.
- `main` / `v2` / `flip.simulator` are prior stages. Default to whatever branch
  is currently checked out unless told otherwise.

## Running

    pip install -r requirements.txt
    python3 kiosk/run_kiosk.py               # full state machine
    python3 api/flipdot-api.py               # animation queue API, separate process
    python3 simulator/flipdot_simulator.py   # virtual panel at :5050, no hardware needed

## Known gotchas

- **HuskyLens I2C is genuinely flaky at the firmware level** — algorithm-switch
  failures, bus drop-offs, and corrupted landmark reads are confirmed hardware
  behavior, not necessarily a code bug. Check `HANDOFF.md` before assuming a
  fresh bug.
- **Don't run `run_kiosk.py` twice** — a second instance fights the first over
  the same I2C bus/serial port. Check
  `ps aux | grep -E "run_kiosk|cam_v2|attract_|hi5_final"` first.
- Killed camera/hand-detection processes can sit in D-state for a couple seconds
  after SIGKILL (blocking I2C read mid-flight). `run_kiosk.py` already waits for
  this (`_wait_for_pattern_gone()`) — don't remove it to "speed things up."
- Repo history was previously purged of an accidentally-committed `/home/pi` home
  directory (`.ssh`, `.cloudflared` credentials). Never stage broadly from a Pi
  checkout — add specific project files only.

## Session handoff

At the end of a substantial work session, update `STATUS.md` and/or
`HANDOFF.md` yourself (what's done, what's verified, what's next) — this is the
established pattern for resuming cold in a future session. Don't wait to be asked.
