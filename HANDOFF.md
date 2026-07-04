# QRiousGiving Debug Handoff — 2026-07-02 (algorithm-switch investigation)

## TL;DR for tomorrow
The POSE↔HAND algorithm switch (`write_algo()`) is **genuinely unreliable at
the firmware/hardware level** — confirmed by direct live testing against the
sensor, not just a timing/race bug in our code. The fix in progress: stop
switching algorithms at runtime altogether by using the HuskyLens's
**multi-algorithm mode**, so `cam_v2.py` and `hi5_final.py` each just read
their own algorithm's results without ever calling `switchAlgorithm()`
during the live kiosk flow. Not yet implemented — plan below.

## What was fixed this session (already committed to working tree, not yet git-committed)
Run `git status --short` — 5 files touched: `kiosk/DFRobot_HuskyLens.py`,
`kiosk/attract_outline.py`, `kiosk/cam_v2.py`, `kiosk/hi5_final.py`,
`kiosk/run_kiosk.py`. In order of discovery:

1. **`run_kiosk.py` duplicate-instance footgun** — running `run_kiosk.py` a
   second time (e.g. testing in a second terminal) spawns a second
   `cam_v2.py`/`attract_outline.py` fighting the first over the same I2C bus
   and serial port. No code fix — just don't run two instances. Always
   `ps aux | grep -E "run_kiosk|cam_v2|attract_|hi5_final"` before starting
   a new one.

2. **`attract_outline.py` only draws when `cam_state.json` has
   `active=true`** — this is correct/expected, not a bug. If you don't see
   the silhouette, it's because no presence is currently detected, not
   because the pipeline is broken. Confirmed via `/tmp/cam_state.json`.

3. **`run_kiosk.py`'s `_ensure_cam_stopped()` / `_ensure_hi5_stopped()`
   didn't wait for the killed process to actually exit** — `cam_v2.py` and
   `hi5_final.py` do blocking I2C reads that can leave the process in
   uninterruptible D-state for a couple seconds after SIGKILL is sent (the
   signal is only delivered once the blocking syscall returns). The next
   script would start touching the I2C bus while the old process was still
   mid-transaction, corrupting/losing the algorithm switch. Fixed by adding
   `_wait_for_pattern_gone()` (polls `pgrep -f <pattern>` up to 5s) and
   calling it from both `_ensure_cam_stopped()` and `_ensure_hi5_stopped()`
   before returning. **This fix is real and worth keeping** regardless of
   the multi-algorithm work below — it prevents a genuine two-master I2C
   race.

4. **`write_algo()`'s "verification" was a dead end — do not resurrect it.**
   Tried checking `getResult(algo)`'s response header (`Result.algo`) as
   "ground truth" that the switch really took effect. Traced the protocol
   code (`husky_lens_protocol_write_begin`, `vendor/dfrobot_huskylensv2.py:488`)
   and confirmed that field is just an echo of the algo byte *we* put in the
   *request* — it always matches what we asked for, regardless of what the
   sensor is actually running. This made `write_algo()` return `True` (and
   `cam_v2.py`/`hi5_final.py` stop retrying) after a single attempt even
   when the physical screen was still on the wrong algorithm. There is no
   "get current running algorithm" query anywhere in the vendored client —
   only per-request algo tags.

5. **Confirmed live (watching the physical screen across ~5 manual trials)
   that a single `switchAlgorithm()` call, even acked `True`, does NOT
   reliably apply within 20s** — and that repeated re-issuing over ~24-36s
   sometimes works and sometimes doesn't (same code, same budget, different
   outcome on different trials). This is real hardware/firmware flakiness,
   matching the pre-existing note in this doc's "Remaining issues #1"
   section about I2C instability. `write_algo()` was bumped to 14 retries ×
   3.5s settle (~49s worst case) and made to always return `True` (best
   effort — no reliable failure signal exists), but even this padded budget
   failed at least once in testing. **Do not trust any retry count to fully
   solve this** — it's a probability-of-success knob, not a fix.

6. `hi5_final.py` (line ~510) and `cam_v2.py`'s `connect()` (line ~69) now
   check `write_algo()`'s return value and `fatal()`/`sys.exit(1)` if it's
   `False`, so `run_kiosk.py` respawns them instead of silently polling with
   the sensor in the wrong mode. Given point 5, `write_algo()` basically
   always returns `True` now (it only returns `False` if every single
   `switchAlgorithm()` call raised an exception, e.g. total bus dropout) —
   so this guards against a different, real failure (sensor totally gone),
   not "switch didn't take."

7. `attract_outline.py` now sends a blank/white frame immediately on
   startup (before the main loop), so a fresh `run_kiosk.py` start doesn't
   leave whatever was on the panel from the previous session (old QR code,
   old outline) lingering until someone shows up.

## The plan for tomorrow: multi-algorithm mode

Goal: never call `switchAlgorithm()` during the live kiosk flow at all.
`cam_v2.py` always asks for POSE results, `hi5_final.py` always asks for
HAND results — both against a sensor that's running both models
concurrently the whole time.

### Why this should work
- `vendor/dfrobot_huskylensv2.py` has `setMultiAlgorithm(algos: list)`
  (line ~687, `COMMAND_SET_MULTI_ALGORITHM = 0x0C`) — takes 2-3 algorithm
  IDs. Also `setMultiAlgorithmRatio(ratios: list)` (line ~710,
  `COMMAND_SET_MULTI_ALGORITHM_RATIO = 0x0D`) which may need to be set too
  (controls processing-time split between the concurrent algorithms —
  untested whether a sane default applies if you skip this).
- `ProtocolV2.__init__` (line ~320) already pre-allocates
  `self.result[algo]` for every algo 0-255 independently — `getResult(algo)`
  writes into `self.result[algo]["blocks"]` per-algo, so the underlying
  client already supports "ask for algo X's results" as an independent
  per-algo query, not a global "current mode" — this is exactly the shape
  multi-algorithm mode needs.

### Concrete steps
1. **Test `setMultiAlgorithm([ALGORITHM_POSE_RECOGNITION,
   ALGORITHM_HAND_RECOGNITION])` in isolation first**, watching the
   physical screen, before touching any kiosk code. Confirm:
   - Does the screen show both algorithms running (or some multi-mode
     indicator)?
   - Does `getResult(ALGORITHM_POSE_RECOGNITION)` return live pose blocks
     while a person is in frame, *without ever calling `switchAlgorithm()`*?
   - Does `getResult(ALGORITHM_HAND_RECOGNITION)` return live hand blocks
     the same way, concurrently?
   - Is there any settle delay needed after `setMultiAlgorithm()` itself
     (same drop-off-bus behavior as `switchAlgorithm()` is plausible —
     check `i2cdetect -y 1` during/after)?
   A minimal test script skeleton (adapt from
   `/tmp/claude-*/scratchpad/husky_switch_test*.py` used this session, now
   gone — rewrite fresh):
   ```python
   from DFRobot_HuskyLens import DFRobot_HuskyLens_I2C, ALGORITHM_POSE_RECOGNITION, ALGORITHM_HAND_RECOGNITION
   hl = DFRobot_HuskyLens_I2C(bus=1, addr=0x50); hl.begin()
   hl._hl.setMultiAlgorithm([ALGORITHM_POSE_RECOGNITION, ALGORITHM_HAND_RECOGNITION])
   time.sleep(3)
   hl._hl.getResult(ALGORITHM_POSE_RECOGNITION); print(hl._hl.getCachedResultNum(ALGORITHM_POSE_RECOGNITION))
   hl._hl.getResult(ALGORITHM_HAND_RECOGNITION); print(hl._hl.getCachedResultNum(ALGORITHM_HAND_RECOGNITION))
   ```
2. **If that works**, update `kiosk/DFRobot_HuskyLens.py`'s
   `_HuskyLensAdapter`:
   - Add a `write_multi_algo(algos: list)` method wrapping
     `self._hl.setMultiAlgorithm(algos)`, called once at startup instead of
     `write_algo()`.
   - `request()`, `count_blocks()`, `blocks()` currently key off
     `self._algo` (a single tracked "current" algorithm) — change them to
     take an explicit `algo` parameter instead, since both POSE and HAND
     results need to be queryable independently and concurrently. Check all
     callers (`cam_v2.py`, `hi5_final.py`, `attract_hand.py`) for the
     `hl.request()` / `hl.count_blocks()` / `hl.blocks()` call sites that'll
     need the new explicit-algo signature.
3. Update `cam_v2.py`: call `hl.write_multi_algo([ALGORITHM_POSE_RECOGNITION, ALGORITHM_HAND_RECOGNITION])`
   once at connect time instead of `hl.write_algo(ALGORITHM_POSE_RECOGNITION)`,
   then always query with `algo=ALGORITHM_POSE_RECOGNITION` explicitly.
4. Update `hi5_final.py` similarly — drop its own `write_algo()` call
   entirely (multi-algo setup already happened in `cam_v2.py` before it was
   spawned, and both processes are talking to the same physical sensor
   state), just query with `algo=ALGORITHM_HAND_RECOGNITION` explicitly.
   **Careful**: `run_kiosk.py` kills `cam_v2.py` before spawning
   `hi5_final.py` — if multi-algo mode is a *device-level* setting that
   persists independent of which process is talking to it (likely, since
   it's the sensor's own running state, not a per-connection thing), this
   should be fine. But verify: does `hi5_final.py` need to re-issue
   `setMultiAlgorithm()` itself in case the device resets/forgets on
   reconnect, or does it stay in multi-mode across separate I2C client
   connections? Test explicitly.
5. **Fallback if multi-algorithm mode doesn't actually run both models
   well** (e.g. degraded frame rate/accuracy per algorithm, or the firmware
   doesn't really support POSE+HAND together — check DFRobot's own docs/
   examples for which algorithm pairs are actually supported in multi
   mode): revert to the current switch-based approach (already committed,
   works probabilistically) and instead pursue the hardware angle — Remaining
   issue #1 in this doc (PSU/cabling/pull-ups) may be the real root cause of
   why even a "confirmed" switch sometimes silently fails.

## Existing sections below are from the prior (2026-07-01) simulator bring-up handoff.

## What's working
- **Flipdot simulator** running at http://127.0.0.1:5050 (code: `simulator/flipdot_simulator.py`)
- **Simulator bug fixed**: `grid()` method now maps panel addresses by value `(addr-1)*PANEL_H` instead of sorted position, preventing a phantom addr-0 frame from shifting content down by one panel
- **Full kiosk stack** wired to simulator via systemd service (`runkiosk.service`)
- **HuskyLens** connected on I2C bus 1, addr 0x50, using `DFRobot_HuskyLens_I2C`
- `ALGORITHM_HAND_RECOGNITION` (=8) and `ALGORITHM_POSE_RECOGNITION` (=9) both confirmed working in isolation
- `HAND_RECOGNITION` does detect hands — intermittently (blocks=1 seen live), needs steady flat palm facing camera

## How to start everything

```bash
# 1. Check HuskyLens is on I2C bus
i2cdetect -y 1   # should show 0x50

# 2. Start simulator (if not already running)
python3 /home/pi/QRiousGiving/simulator/flipdot_simulator.py &
# → open http://127.0.0.1:5050

# 3. Start flipdot API
FLIPDOT_SERIAL=/tmp/flipdot_vserial SERIAL_PORT=/tmp/flipdot_vserial \
  python3 /home/pi/QRiousGiving/api/flipdot-api.py &

# 4. Start kiosk (service already configured to use simulator)
sudo systemctl restart runkiosk.service
journalctl -u runkiosk.service -f   # follow logs
```

## Kiosk flow (expected)
```
cam_v2.py (pose detection) + attract_v2.py (silhouette)
  → person stands in front for ~8s
  → hi5_final.py (show open palm for 2s)
  → qr_works.py (QR code shown)
  → flipdot API triggers anim.py (countdown + random anim + THANK YOU)
  → back to attract/idle
```

## systemd service config
- Service file: `/etc/systemd/system/runkiosk.service`
- Override: `/etc/systemd/system/runkiosk.service.d/override.conf`
- Points to: `/home/pi/QRiousGiving/kiosk/run_kiosk.py` (NOT the Desktop version)
- Key env vars set in override:
  - `FLIPDOT_SERIAL=/tmp/flipdot_vserial`
  - `SERIAL_PORT=/tmp/flipdot_vserial`
  - `FLIPDOT_BAUD=57600`

## Remaining issues

### 1. HuskyLens I2C instability (highest priority — now has new PSU)
- Device drops off I2C bus mid-session, especially after algorithm switches
- Symptoms: repeated `[KIOSK] launching camera…` spam, `Could not connect to HuskyLens 2`
- New power supply installed — verify stability with: `watch -n1 i2cdetect -y 1`
- If still dropping: check SDA/SCL cable seating, add 4.7kΩ pull-up resistors on I2C lines

### 2. HuskyLens drops off bus during algorithm switch
- `write_algo()` in `DFRobot_HuskyLens.py` does `time.sleep(2.0)` to wait for settle
- If the device takes >2s to come back after switching POSE→HAND algorithm, reads fail
- Fix if needed: increase the sleep in `kiosk/DFRobot_HuskyLens.py` line ~265:
  ```python
  time.sleep(2.0)  # increase to 3.0 or 4.0 if still failing
  ```

### 3. Palm detection is intermittent
- `ALGORITHM_HAND_RECOGNITION` sees hand ~30% of frames even with hand held steady
- `HOLD_REQUIRED_SEC` default is 2.0s — requires 2 continuous seconds of open-palm
- `MISS_GRACE_SEC` default is 0.8s — gap tolerance between lost frames
- To make hi5 easier to trigger, loosen in `kiosk/hi5_final.py` env or service override:
  ```bash
  Environment=MISS_GRACE_SEC=1.5     # more forgiveness for dropped frames
  Environment=HOLD_REQUIRED_SEC=1.5  # shorter hold required
  ```

### 4. attract_v2.py silhouette not showing
- attract_v2 only draws when `cam_state.json` has `active=True` AND is <2s old
- If cam_v2 is crash-looping (HuskyLens instability), no state file is written → blank screen
- Fix the HuskyLens stability first; silhouette should work once cam_v2 stays alive

### 5. attract_v2.py exits immediately on kiosk restart
- When kiosk restarts with a stale `cam_state.json` showing `active=True`, it immediately
  triggers the 8s hold timer → spawns hi5 → kills attract before it does anything
- Not a code bug; just a side effect of stale state. Resolves itself after cam_v2 writes fresh state.

## Key files
| File | Purpose |
|------|---------|
| `kiosk/run_kiosk.py` | Main orchestrator FSM |
| `kiosk/cam_v2.py` | HuskyLens pose detection → writes `/tmp/cam_state.json` |
| `kiosk/attract_v2.py` | Idle silhouette display |
| `kiosk/hi5_final.py` | Palm hi-5 detection, chains to `qr_works.py` |
| `kiosk/qr_works.py` | QR code display |
| `kiosk/DFRobot_HuskyLens.py` | HuskyLens adapter (hand+pose landmarks) |
| `animations/anim.py` | Countdown + random anim + THANK YOU |
| `animations/rand_anim/` | Random animations (sunflower etc.) |
| `api/flipdot-api.py` | HTTP API that queues and runs anim.py |
| `simulator/flipdot_simulator.py` | Web-based flipdot display at :5050 |

## Quick diagnostics
```bash
# Is HuskyLens on bus?
i2cdetect -y 1

# What is kiosk doing?
journalctl -u runkiosk.service -n 50 --no-pager

# What does camera see?
cat /tmp/cam_state.json | python3 -m json.tool

# Is simulator receiving frames?
curl -s http://127.0.0.1:5050/api/state | python3 -c "import sys,json; print('frames:', json.load(sys.stdin)['frames'])"

# Is flipdot API alive?
curl -s http://127.0.0.1:8080/status

# Test hand detection directly (kill kiosk first)
sudo systemctl stop runkiosk.service
cd /home/pi/QRiousGiving/kiosk
python3 -c "
import sys, time; sys.path.insert(0,'.')
from DFRobot_HuskyLens import DFRobot_HuskyLens_I2C, ALGORITHM_HAND_RECOGNITION
hl = DFRobot_HuskyLens_I2C(bus=1, addr=0x50); hl.begin()
hl.write_algo(ALGORITHM_HAND_RECOGNITION)
for i in range(30):
    hl.request(); n=hl.count_blocks()
    print(f't={i*0.3:.1f}s blocks={n}', hl.blocks()[0].landmarks[0] if n else '')
    time.sleep(0.3)
"
```
