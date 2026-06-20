# v2 (HuskyLens) bring-up status — 2026-06-20

Working notes from restructuring the repo and bringing the HuskyLens-based kiosk
pipeline up on real hardware. Point a fresh chat at this file to resume without
re-deriving everything.

## What's done (all on `v2`, pushed to `origin/v2`)

- Repo restructured: `kiosk/` (`run_kiosk.py`, `cam_v2.py`, `attract_v2.py`,
  `hi5_final.py`, `qr_works.py`), `animations/` (`anim.py`, `rand_anim/`),
  `api/` (`flipdot-api.py`), `assets/` (`palm_combo.json`, `sunflower.png`),
  `legacy/` (everything dead/unreachable from the live pipeline — kept, not
  deleted, in case old experiments are worth revisiting).
- `cam_v2.py` imported `from DFRobot_HuskyLens import (...)`, a module that
  didn't exist anywhere on this Pi. Added `kiosk/DFRobot_HuskyLens.py` (adapter)
  + `kiosk/vendor/dfrobot_huskylensv2.py` (DFRobot's real client, vendored,
  MIT licensed).
- Fixed three real hardware-integration bugs found by actually running it on
  the device:
  - I2C address default was `0x32`; `i2cdetect -y 1` showed the sensor at
    `0x50`. Fixed in `cam_v2.py`.
  - `FaceResult` parsing crashed (`bytearray index out of range`) once a face
    was actually detected — the vendor client expects a 20-byte eye/nose/mouth
    landmark payload that isn't reliably present on this firmware/transport.
    Patched in `kiosk/DFRobot_HuskyLens.py` to degrade gracefully (we don't use
    those fields anyway, only the xCenter/yCenter/width/height bbox).
  - `attract_v2.py` used a different env var (`FLIPDOT_PORT`) and default
    (`/dev/ttyUSB0`) than the rest of the pipeline (`FLIPDOT_SERIAL` →
    `/dev/ttyS0`, the only serial device that exists on this Pi). Aligned it.

## Verified on real hardware

- `cam_v2.py` alone: connects over I2C, detects presence, writes
  `/tmp/cam_state.json` correctly, survives sustained use (50+ sec, multiple
  detections) without crashing.
- `cam_v2.py` + `attract_v2.py` together under `run_kiosk.py`: both start and
  stay running without crash-looping.

## Not yet verified — pick up here

- `attract_v2.py`'s actual silhouette draw on the flipdot panel. **The panel
  was not physically connected during this session** — `/dev/ttyS0` opens
  fine regardless (UART doesn't need an ACK), so no error, but nothing was
  visually confirmed. Next: connect the panel, run `python3 kiosk/run_kiosk.py`,
  stand in front of the camera, watch for a silhouette that scales with face
  distance.
- The full state-machine handoff: hold presence for `TRIGGER_HOLD_SEC` (8s,
  env-tweakable) to trigger `hi5_final.py`, then `hi5_final.py`'s own
  palm-fill game on real hardware.
- `flipdot-api.py` + `anim.py` + `rand_anim/*.py` on real hardware — untouched
  this session.

## Explicitly out of scope so far

Production at `/home/pi/Desktop` (systemd services `runkiosk.service`,
`flipdot-api.service`) is a **separate, older copy** of this code, still on
v1's `cam_final.py`, and was crash-looping (`Picamera2()` IndexError — no Pi
camera module found, since the hardware was swapped to the HuskyLens) before
any of this work started. None of the above has been deployed there yet —
that's a distinct step for whenever the v2 pipeline above is fully verified.
Check `journalctl -u runkiosk.service` and `/etc/systemd/system/*.service` to
re-orient if picking that up later.
