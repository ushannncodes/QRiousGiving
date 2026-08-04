# QRiousGiving

An interactive flipdot-display kiosk: it notices someone nearby, shows a
live tracking silhouette to draw them in, invites a hi-5/palm gesture, then
shows a QR code so they can donate.

## Hardware

- Raspberry Pi
- Flipdot display: 4 stacked 28x7 panels = 28x28, driven over serial
  (`FLIPDOT_SERIAL`, default `/dev/ttyS0`). Wire protocol, one packet per
  panel: `[0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F]`.
- Leap Motion Controller for hand tracking, for both presence/attract and
  the hi-5 gesture. It isn't Pi-attached: a separate Beelink Windows PC runs
  the sensor + Ultraleap tracking and streams 21-point hand landmarks to the
  Pi over UDP (see `LEAP_HANDOFF.md` for the full three-machine setup).
  Sensing range is short (~10-40cm) — the kiosk only notices someone once
  their hand is already close to the panel, not from across the room.
  This replaced a HuskyLens 2 AI camera (I2C), which itself replaced the
  original Pi Camera Module + MediaPipe setup (v1) — see `STATUS.md` for
  that hardware history. The HuskyLens-based scripts are kept in the repo
  (unused by default) as a fallback/reference, not deleted.

## Layout

- `kiosk/` — the live pipeline: `run_kiosk.py` (orchestrator/state machine),
  `attract_leap.py` (Leap Motion presence + live hand-shadow display, one
  process — see `leap/flipdot_render.py` for the shared rendering logic),
  `hi5_final.py` (palm-fill game, now fed by the same Leap UDP feed),
  `qr_works.py` (QR code display). `cam_v2.py`, `attract_v2.py`,
  `attract_outline.py`, `DFRobot_HuskyLens.py` + `vendor/` are the earlier
  HuskyLens-based sensor pipeline — no longer used by default, kept for
  reference.
- `animations/` — flipdot animations triggered through the API (`anim.py`,
  `loading.py`, `rand_anim/`).
- `api/` — `flipdot-api.py`, a small Flask service that queues/runs
  animations, triggered by buttons on the donation website.
- `assets/` — static assets (palm outline mask, images).
- `legacy/` — old/dead code kept for reference; not part of the live
  pipeline.
- `simulator/` — virtual flipdot panel for testing without the physical
  hardware (this branch only — see "Testing without hardware" below).

## Running

```
pip install -r requirements.txt
python3 kiosk/run_kiosk.py
```

`run_kiosk.py` drives the full state machine (Leap attract → hi-5 palm
game → QR code → back to attract), spawning/killing `attract_leap.py` and
`hi5_final.py` as needed. Both expect a live Leap Motion UDP feed on
`LISTEN_PORT` (default `5111`) — see `LEAP_HANDOFF.md` for the Beelink-side
setup required to actually produce that feed. Individual scripts can also
be run standalone for testing — each has a module docstring listing its env
vars.

The flipdot animation API runs separately:

```
python3 api/flipdot-api.py
```

Buttons on the Framer donation site POST to `/trigger` with a `sequence`
name and the `X-Trigger-Secret` header (matching `TRIGGER_SECRET` on the
Pi). `SCRIPTS` in `flipdot-api.py` maps each name to a script plus any env
it should run with:

| sequence | plays |
|----------|-------|
| `anim_py` | `animations/anim.py` — 5-4-3-2-1 countdown, random anim, THANK YOU |
| `loading_py` | `animations/loading.py` — 60s draining ring counting 60..0, random anim, THANK YOU |
| `loading_text_py` | same as above, with a scrolling message in place of the ring opener |

One press queues one run; a single worker thread plays them back-to-back so
only one script ever owns the serial port. The `loading_*` sequences are
listed in `EXCLUSIVE_JOBS`, so a press arriving while one is already running
is rejected with `409` rather than queued — that keeps the panel in sync
with whoever is standing in front of it instead of building a backlog.
Check `/status` for the running job and queue depth.

## Testing without hardware

The flipdot panel and the Leap Motion hardware (Beelink PC + controller)
aren't always available. `simulator/flipdot_simulator.py` opens a virtual
serial port standing in for the real panel, decodes the same wire protocol,
and renders the resulting 28x28 grid live in a browser:

```
python3 simulator/flipdot_simulator.py
```

`leap/synthetic_leap_udp_sender.py` stands in for the Beelink feed, sending
the same UDP wire schema with a procedurally animated hand — no Leap Motion
Controller or second PC needed:

```
export FLIPDOT_SERIAL=/tmp/flipdot_vserial   # attract_leap.py, hi5_final.py, qr_works.py
export SERIAL_PORT=/tmp/flipdot_vserial       # anim.py, rand_anim/*.py
python3 leap/synthetic_leap_udp_sender.py &
python3 kiosk/attract_leap.py
```

Then open the printed `http://127.0.0.1:5050` URL (or check your editor's
auto-forwarded ports if working over SSH/remote).

## Key env vars

- `FLIPDOT_SERIAL` / `SERIAL_PORT` — flipdot serial port (default `/dev/ttyS0`)
- `FLIPDOT_BAUD` — baud rate (default `57600`)
- `LISTEN_PORT` — UDP port `attract_leap.py`/`hi5_final.py` listen on for
  the Leap feed (default `5111`, must match `beelink/leap_sender.py`'s
  `RPI_PORT`)
- `CAM_SIGNAL_PATH` — presence/state file `attract_leap.py` writes and
  `run_kiosk.py` reads to trigger hi-5 (default `/tmp/cam_state.json`,
  same path/schema the old `cam_v2.py`/`attract_v2.py` pair used)

See each script's module docstring for the complete list.

## Current status

See `STATUS.md` for the latest hardware bring-up notes — what's verified on
real hardware vs. still pending.
