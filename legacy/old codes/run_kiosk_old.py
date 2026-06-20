#!/usr/bin/env python3
import os, time, json, signal, subprocess, sys, urllib.request, urllib.error

# ---------- Config (env-tweakable) ----------
CAM_SCRIPT = os.getenv("CAM_SCRIPT", "./cam-final6.py")
HI5_SCRIPT = os.getenv("HI5_SCRIPT", "./hi5_final.py")

CAM_SIGNAL_PATH     = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
TRIGGER_HOLD_SEC    = float(os.getenv("TRIGGER_HOLD_SEC", "4.0"))   # presence to promote -> HI5
import os as _os
_os.environ["TRIGGER_HOLD_SEC"] = str(TRIGGER_HOLD_SEC)

ACTIVE_STALE_SEC    = float(os.getenv("ACTIVE_STALE_SEC", "2.0"))   # heartbeat freshness
API_STATUS_URL      = os.getenv("API_STATUS_URL", "http://127.0.0.1:8080/status")

SCAN_GRACE_SEC      = float(os.getenv("SCAN_GRACE_SEC", "60.0"))    # window to scan the QR
ANIM_WAIT_TIMEOUT   = float(os.getenv("ANIM_WAIT_TIMEOUT", "300.0"))# safety cap
COOLDOWN_AFTER_ANIM = float(os.getenv("COOLDOWN_AFTER_ANIM", "1.0"))# small breather
WARMUP_SEC       = float(os.getenv("WARMUP_SEC", "2.0"))   # seconds of motion before hold
WARMUP_DELTA_MIN = float(os.getenv("WARMUP_DELTA_MIN", "8")) # min delta_ema to count as motion


POLL_SLEEP_CAM      = float(os.getenv("POLL_SLEEP_CAM", "0.05"))
POLL_SLEEP_WAIT     = float(os.getenv("POLL_SLEEP_WAIT", "0.10"))

def _spawn_py(path):
    # Run as a child Python process so each script owns its resources cleanly.
    return subprocess.Popen([sys.executable, path])

def _grace_stop(p, grace=2.0):
    if not p: return
    try: p.send_signal(signal.SIGINT)
    except Exception: pass
    t0 = time.time()
    while p.poll() is None and (time.time() - t0) < grace:
        time.sleep(0.05)
    if p.poll() is None:
        try: p.terminate()
        except Exception: pass
        t1 = time.time()
        while p.poll() is None and (time.time() - t1) < grace:
            time.sleep(0.05)
    if p.poll() is None:
        try: p.kill()
        except Exception: pass

def _read_cam_state():
    try:
        with open(CAM_SIGNAL_PATH, "r") as f:
            st = json.load(f)
        # stale = heartbeat older than ACTIVE_STALE_SEC
        if (time.time() - float(st.get("ts", 0))) > ACTIVE_STALE_SEC:
            return {"active": False, "stale": True}
        return st
    except Exception:
        return {"active": False, "stale": True}

def _get_api_status():
    try:
        with urllib.request.urlopen(API_STATUS_URL, timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def main():
    STATE = "CAM"  # CAM -> HI5 -> WAIT_ANIM -> CAM
    cam = hi5 = None
    hold_t0 = None
    warmup_t0 = None
    print("[KIOSK] boot…")

    while True:
        if STATE == "CAM":
            # Ensure cam is running
            if cam is None or cam.poll() is not None:
                print("[KIOSK] launching camera…")
                cam = _spawn_py(CAM_SCRIPT)
                hold_t0 = None

            # st = _read_cam_state()
            # if st.get("active"):
            #     hold_t0 = hold_t0 or time.time()
            #     if time.time() - hold_t0 >= TRIGGER_HOLD_SEC:
            #         print("[KIOSK] trigger met → switch to HI5")
            #         _grace_stop(cam); cam = None
            #         hi5 = _spawn_py(HI5_SCRIPT)
            #         STATE = "HI5"
            # else:
            #     hold_t0 = None

            st = _read_cam_state()
            now = time.time()
            active = bool(st.get("active"))
            delta_ema = float(st.get("delta_ema", 0.0))

            if active and (delta_ema >= WARMUP_DELTA_MIN):
                # start or continue warmup
                warmup_t0 = warmup_t0 or now
                warmup_elapsed = now - warmup_t0

                if warmup_elapsed >= WARMUP_SEC:
                    # warmup satisfied, now count main hold
                    hold_t0 = hold_t0 or now
                    hold_elapsed = now - hold_t0

                    if hold_elapsed >= TRIGGER_HOLD_SEC:
                        print("[KIOSK] trigger met → switch to HI5")
                        _grace_stop(cam); cam = None
                        hi5 = _spawn_py(HI5_SCRIPT)
                        STATE = "HI5"
                        warmup_t0 = None
                        hold_t0 = None
            else:
                warmup_t0 = None
                hold_t0 = None



            time.sleep(POLL_SLEEP_CAM)

        elif STATE == "HI5":
            # Wait until hi5 exits (it should exit after painting QR)
            if hi5.poll() is not None:
                print("[KIOSK] HI5 exited → waiting for scan (grace window)")
                STATE = "WAIT_ANIM"
                anim_started_seen = False
                wait_t0 = time.time()
                scan_deadline = wait_t0 + SCAN_GRACE_SEC
            time.sleep(POLL_SLEEP_CAM)

        elif STATE == "WAIT_ANIM":
            st = _get_api_status()
            now = time.time()
            if st:
                q = int(st.get("queue", 0))
                running = bool(st.get("running", False))
                last_started = float(st.get("last_started_ts", 0.0))
                last_done    = float(st.get("last_done_ts", 0.0))

                # Any sign an animation started?
                if running or last_started > 0:
                    anim_started_seen = True

                if anim_started_seen:
                    # Wait for all jobs to complete
                    if (not running) and q == 0 and last_done > 0:
                        print("[KIOSK] anims done & queue empty → back to CAM")
                        time.sleep(COOLDOWN_AFTER_ANIM)
                        STATE = "CAM"
                        continue
                else:
                    # No scan yet; did the grace window expire?
                    if now >= scan_deadline:
                        print("[KIOSK] no scan within grace window → back to CAM")
                        STATE = "CAM"
                        continue

            # Safety timeout (e.g. API unreachable)
            if (now - wait_t0) > ANIM_WAIT_TIMEOUT:
                print("[KIOSK] WAIT_ANIM timeout → back to CAM")
                STATE = "CAM"

            time.sleep(POLL_SLEEP_WAIT)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[KIOSK] bye")
