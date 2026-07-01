# QRiousGiving Debug Handoff — 2026-07-01

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
