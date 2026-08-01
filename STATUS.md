# v2 (HuskyLens) bring-up status — 2026-06-20

Working notes from bringing the HuskyLens-based kiosk pipeline up on real
hardware. Point a fresh chat at this file to resume without re-deriving
everything.

## What's done (on `v2`, also mirrored onto `flip.simulator` and `main`)

- Repo restructured (`kiosk/`, `animations/`, `api/`, `assets/`, `legacy/`)
  — see `README.md`.
- HuskyLens adapter (`kiosk/DFRobot_HuskyLens.py` + vendored
  `kiosk/vendor/dfrobot_huskylensv2.py`) now supports face, hand, and pose
  recognition, with a best-effort parser for all three — the vendor client
  raises `bytearray index out of range` on a truncated/corrupted landmark
  payload otherwise, seen live on all three algorithms — plus a 2s settle
  delay after switching algorithms (the sensor drops off the I2C bus
  briefly while switching models).
- Fixed a real bug: `ProtocolV2.wait()` in the vendor client can block up
  to 8 seconds per call waiting for a clean I2C response. Pose
  recognition's larger payload made this stall `cam_v2.py`'s whole loop
  for up to 8s at a stretch, intermittently blanking the attract display.
  Shortened to 500ms.
- `cam_v2.py` switched from `ALGORITHM_FACE_RECOGNITION` to
  `ALGORITHM_POSE_RECOGNITION` — presence now triggers on a person's body
  generally, not specifically a recognized face (closer to v1's old
  "anything in front of the camera" behavior). Writes body landmarks
  (nose/shoulders/hips/etc.) to `/tmp/cam_state.json` instead of a face
  bbox.
- `attract_v2.py` rewritten: instead of 3 fixed-size generic person icons,
  it draws a live head+torso silhouette every frame from the actual
  tracked landmarks. Sized off the overall detection bbox height rather
  than literal shoulder width — real shoulder-width-to-height proportions
  render as an unreadable ~1px sliver at 28x28; bbox height is a much more
  stable reference.
- `hi5_final.py` ported off Picamera2 + MediaPipe Hands (no camera hardware
  left to read from — see "Explicitly out of scope" below) onto HuskyLens
  `ALGORITHM_HAND_RECOGNITION`. HuskyLens's 21-point hand landmark layout
  matches MediaPipe's exactly, so the existing open-palm geometry
  (`is_open_palm`, finger-angle checks) didn't need to change.
- Fixed a separate, pre-existing bug in `hi5_final.py`: it imported
  `flipdot_driver`, which lives in `legacy/` (not on `kiosk/`'s import
  path), so the `ImportError` fallback silently sent a completely
  different, incompatible packet format for everything past the very
  first frame. This likely never worked correctly even before this
  session.
- Added `simulator/flipdot_simulator.py` (on `flip.simulator` branch) — a
  virtual flipdot panel (pty-based virtual serial port + web renderer) for
  testing the whole pipeline without the physical panel. See `README.md`.
- Added `README.md`; fixed `requirements.txt` (was missing
  numpy/Pillow/qrcode/Flask/Flask-Cors/smbus2, listed unused `pinpong`,
  had a stale "clone separately" note for an already-vendored dependency).

## Verified

- Hand recognition: real palm-geometry detection (`reason=palm_geom`)
  against actual HuskyLens hardware, confirmed visually through the
  simulator — correct flipdot packet rendering, hold-to-fill progress
  tracking, idle-abort/reconnect.
- Pose recognition: real body landmarks tracked live (nose/shoulders/hips
  move correctly as the tracked person moves), confirmed through the
  simulator after the 8s-stall and silhouette-proportion fixes — the
  attract display now stays up continuously and renders a recognizable
  head+torso shape instead of flickering/disappearing.
- `qr_works.py`'s QR code renders correctly through the simulator (full
  3-finder-pattern QR, confirms protocol/panel-stacking parsing).

## Not yet verified — pick up here

- **Nothing this session was tested against the physical flipdot panel —
  only `simulator/flipdot_simulator.py`** (real HuskyLens hardware +
  simulated panel). Next: connect the real panel, rerun the same smoke
  tests (`qr_works.py`, `attract_v2.py` while standing in front of the
  camera, `hi5_final.py` palm fill) with `FLIPDOT_SERIAL=/dev/ttyS0`.
- `hi5_final.py`'s palm-fill never reached 100% in testing — detection
  flickers between open/closed (reached ~14% progress before resetting).
  `HYST_ALPHA`/`HYST_THRESH`/`DIST_MARGIN`/`MIN_HAND_AREA` are already
  flagged in the file as on-device tuning knobs; this is the next thing to
  tune.
- The full `run_kiosk.py` state-machine handoff (attract → hi-5 → QR →
  animations → back to attract) hasn't been re-run end-to-end since
  today's `cam_v2.py`/`attract_v2.py`/`hi5_final.py` rewrites — only the
  individual scripts were tested standalone against the simulator.
- `flipdot-api.py` + `anim.py` + `rand_anim/*.py` — untouched this
  session, same as before.
- HuskyLens occasionally returns a bit-corrupted landmark coordinate under
  I2C timing pressure — seen on face, hand, *and* pose results (e.g.
  `nose=(33194, 178)` instead of `~(440, 178)`). Each best-effort parser
  treats fully-truncated payloads as zero, and `attract_v2.py` clamps
  obviously-out-of-range values, but this is recurring transport-level
  flakiness worth watching, not a one-off.

## Explicitly out of scope so far

**Superseded 2026-08-01 — this section described a deployment gap that no
longer exists.** It used to say production ran a separate, older copy at
`/home/pi/Desktop` (still on v1's `cam_final.py`, crash-looping on a
`Picamera2()` IndexError after the camera was swapped for the HuskyLens),
with nothing here deployed to it.

Both systemd services now run **this repo**, via `.d/override.conf`
drop-ins: `runkiosk.service` → `/home/pi/QRiousGiving/kiosk/run_kiosk.py`,
`flipdot-api.service` → `/home/pi/QRiousGiving/api/flipdot-api.py`. The
stale `/home/pi/Desktop/*.py` copies still exist and are still what the
*base* unit files name, so only the drop-ins keep production on the repo —
if one is lost, the kiosk starts cleanly on year-old code. See "Starting
the kiosk itself" in `LEAP_HANDOFF.md`.

Note this whole file predates the Leap Motion switchover and describes the
retired HuskyLens pipeline. `LEAP_HANDOFF.md` is the current doc.
