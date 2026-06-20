# QRiousGiving

An interactive flipdot-display kiosk: it notices someone nearby, shows a
live tracking silhouette to draw them in, invites a hi-5/palm gesture, then
shows a QR code so they can donate.

## Hardware

- Raspberry Pi
- Flipdot display: 4 stacked 28x7 panels = 28x28, driven over serial
  (`FLIPDOT_SERIAL`, default `/dev/ttyS0`). Wire protocol, one packet per
  panel: `[0x80, 0x83, <panel addr>, <28 column bytes>, 0x8F]`.
- HuskyLens 2 AI camera (I2C, default address `0x50`) for presence and pose
  detection. This replaced the original Pi Camera Module + MediaPipe setup
  (v1) — see `STATUS.md` for the hardware swap and bring-up history.

## Layout

- `kiosk/` — the live pipeline: `run_kiosk.py` (orchestrator/state machine),
  `cam_v2.py` (HuskyLens presence sensor), `attract_v2.py` (live silhouette
  display), `hi5_final.py` (palm-fill game), `qr_works.py` (QR code
  display), `DFRobot_HuskyLens.py` + `vendor/` (HuskyLens client adapter).
- `animations/` — flipdot animations triggered through the API (`anim.py`,
  `rand_anim/`).
- `api/` — `flipdot-api.py`, a small Flask service that queues/runs
  animations.
- `assets/` — static assets (palm outline mask, images).
- `legacy/` — old/dead code kept for reference; not part of the live
  pipeline.

## Running

```
pip install -r requirements.txt
python3 kiosk/run_kiosk.py
```

`run_kiosk.py` drives the full state machine (camera attract → hi-5 palm
game → QR code → back to attract), spawning/killing `cam_v2.py`,
`attract_v2.py`, and `hi5_final.py` as needed. Individual scripts can also
be run standalone for testing — each has a module docstring listing its env
vars.

The flipdot animation API runs separately:

```
python3 api/flipdot-api.py
```

## Key env vars

- `FLIPDOT_SERIAL` / `SERIAL_PORT` — flipdot serial port (default `/dev/ttyS0`)
- `FLIPDOT_BAUD` — baud rate (default `57600`)
- `HUSKYLENS_I2C_BUS` / `HUSKYLENS_I2C_ADDR` — HuskyLens I2C bus/address
  (default `1` / `0x50`)
- `CAM_SIGNAL_PATH` — shared state file between `cam_v2.py` and
  `attract_v2.py` (default `/tmp/cam_state.json`)

See each script's module docstring for the complete list.

## Current status

See `STATUS.md` for the latest hardware bring-up notes — what's verified on
real hardware vs. still pending.
