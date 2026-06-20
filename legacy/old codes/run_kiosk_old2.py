#!/usr/bin/env python3
import os, time, json, signal, subprocess, sys, urllib.request, urllib.error

# ---------- Config (env-tweakable) ----------
CAM_SCRIPT = os.getenv("CAM_SCRIPT", "./cam_final.py")
HI5_SCRIPT = os.getenv("HI5_SCRIPT", "./hi5_final.py")

CAM_SIGNAL_PATH     = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
TRIGGER_HOLD_SEC    = float(os.getenv("TRIGGER_HOLD_SEC", "4.0"))   # presence to promote -> HI5

ACTIVE_STALE_SEC    = float(os.getenv("ACTIVE_STALE_SEC", "2.0"))   # heartbeat freshness from cam
API_STATUS_URL      = os.getenv("API_STATUS_URL", "http://127.0.0.1:8080/status")

SCAN_GRACE_SEC      = float(os.getenv("SCAN_GRACE_SEC", "30.0"))    # window to scan the QR (PRE-anim only)
ANIM_WAIT_TIMEOUT   = float(os.getenv("ANIM_WAIT_TIMEOUT", "300.0"))# safety cap
COOLDOWN_AFTER_ANIM = float(os.getenv("COOLDOWN_AFTER_ANIM", "1.0"))# small breather

WARMUP_SEC          = float(os.getenv("WARMUP_SEC", "2.0"))         # seconds of motion before hold
WARMUP_DELTA_MIN    = float(os.getenv("WARMUP_DELTA_MIN", "8"))     # min delta_ema to count as motion

POLL_SLEEP_CAM      = float(os.getenv("POLL_SLEEP_CAM", "0.05"))
POLL_SLEEP_WAIT     = float(os.getenv("POLL_SLEEP_WAIT", "0.10"))

# logger cadence in WAIT state
WAIT_LOG_EVERY      = float(os.getenv("WAIT_LOG_EVERY", "0.5"))

# ---------- helpers ----------
def _spawn_py(path):
    # Run as a child Python process so each script owns its resources cleanly.
    return subprocess.Popen([sys.executable, path])

def _grace_stop(p, grace=0.2):
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
    # cam writes {"active": bool, "delta_ema": float, "ts": unix}
    try:
        with open(CAM_SIGNAL_PATH, "r") as f:
            st = json.load(f)
        if (time.time() - float(st.get("ts", 0))) > ACTIVE_STALE_SEC:
            return {"active": False, "stale": True}
        return st
    except Exception:
        return {"active": False, "stale": True}

def _get_api_status():
    # flipdot-api /status -> {"running": bool, "queue": int, "last_started_ts": float, "last_done_ts": float}
    try:
        with urllib.request.urlopen(API_STATUS_URL, timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

# ---------- main FSM ----------
def main():
    STATE = "CAM"  # CAM -> HI5 -> WAIT_ANIM -> CAM
    cam = hi5 = None
    hold_t0 = warmup_t0 = None
    print("[KIOSK] boot…")

    # WAIT state bookkeeping (initialized on entry)
    pre_anim_mode = True           # True = grace applies; False = grace killed (override)
    dwell_elapsed = 0.0            # only used in pre-anim mode
    anim_started_seen = False
    last_started_seen = 0.0
    wait_t0 = 0.0
    last_log_t = 0.0

    while True:
        # ---------------- CAM ----------------
        if STATE == "CAM":
            # Ensure camera is running
            if cam is None or cam.poll() is not None:
                print("[KIOSK] launching camera…")
                cam = _spawn_py(CAM_SCRIPT)
                hold_t0 = warmup_t0 = None

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
                        warmup_t0 = hold_t0 = None
            else:
                warmup_t0 = hold_t0 = None

            time.sleep(POLL_SLEEP_CAM)

        # ---------------- HI5 (QR) ----------------
        elif STATE == "HI5":
            # Wait until HI5 exits (it paints QR then exits)
            if hi5.poll() is not None:
                print("[KIOSK] HI5 exited → WAIT for scan/anim (pre-anim grace starts)")
                STATE = "WAIT_ANIM"

                # Reset WAIT bookkeeping for a fresh cycle
                pre_anim_mode      = True         # grace applies again for this new QR cycle
                dwell_elapsed      = 0.0
                anim_started_seen  = False
                last_started_seen  = 0.0
                wait_t0            = time.time()  # used for dwell (pre-anim) and safety timers
                last_log_t         = 0.0

                # Print header once for the WAIT progress line
                print("[WAIT] phase      elapsed/target | started running q  done_ok        note")
            time.sleep(POLL_SLEEP_CAM)

        # ---------------- WAIT_ANIM ----------------
        elif STATE == "WAIT_ANIM":
            st = _get_api_status()
            now = time.time()

            # Defaults if API is missing
            q = 0
            running = False
            last_started = 0.0
            last_done = 0.0

            if st:
                q = int(st.get("queue", 0))
                running = bool(st.get("running", False))
                last_started = float(st.get("last_started_ts", 0.0))
                last_done    = float(st.get("last_done_ts", 0.0))

                # Detect any start (either currently running or a start timestamp recorded)
                if running or last_started > 0.0:
                    anim_started_seen = True
                    if last_started > 0.0:
                        last_started_seen = max(last_started_seen, last_started)

                # --------- Modes ---------
                if pre_anim_mode and not anim_started_seen:
                    # PRE-ANIM: grace counts up until SCAN_GRACE_SEC
                    dwell_elapsed = now - wait_t0
                    min_dwell_done = (dwell_elapsed >= SCAN_GRACE_SEC)

                    # Log (throttled)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        note = "min dwell" if not min_dwell_done else "no anim; can exit"
                        msg = (f"\r[WAIT] pre-anim  {dwell_elapsed:6.1f}/{SCAN_GRACE_SEC:6.1f} | "
                               f"{int(anim_started_seen):>7d} {int(running):>7d} {q:>2d}  "
                               f"{int(last_done >= last_started_seen > 0):>7d}    {note:>12s}")
                        sys.stdout.write(msg); sys.stdout.flush()
                        last_log_t = now

                    # Exit back to CAM if grace elapsed and still no animation started
                    if min_dwell_done:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] no scan/anim; min dwell elapsed → back to CAM")
                        STATE = "CAM"
                        continue

                    # Safety for stuck pre-anim wait
                    if (now - wait_t0) > ANIM_WAIT_TIMEOUT:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] WAIT pre-anim safety timeout → back to CAM")
                        STATE = "CAM"
                        continue

                else:
                    # Either an animation JUST started (kill the grace), or we're already in override
                    if pre_anim_mode and anim_started_seen:
                        # Kill grace the instant an animation starts
                        pre_anim_mode = False
                        dwell_elapsed = 0.0  # not used anymore

                    # OVERRIDE: animation owns the display until it's done and queue is empty
                    done_ok = (last_done >= last_started_seen > 0)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        note = ("anim running" if running
                                else ("queue draining" if q > 0
                                      else ("finished; exit" if done_ok else "waiting")))
                        msg = (f"\r[WAIT] override   {'--':>6}/{ '--':>6} | "
                               f"{int(anim_started_seen):>7d} {int(running):>7d} {q:>2d}  "
                               f"{int(done_ok):>7d}    {note:>12s}")
                        sys.stdout.write(msg); sys.stdout.flush()
                        last_log_t = now

                    if (not running) and q == 0 and done_ok:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] animations finished → back to CAM")
                        time.sleep(COOLDOWN_AFTER_ANIM)
                        STATE = "CAM"
                        continue

                    # Safety for override hanging too long without progress
                    if (now - last_started_seen) > ANIM_WAIT_TIMEOUT and anim_started_seen:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] override safety timeout → back to CAM")
                        STATE = "CAM"
                        continue

            else:
                # API unreachable
                if pre_anim_mode:
                    # In pre-anim, still honor grace then return
                    dwell_elapsed = now - wait_t0
                    min_dwell_done = (dwell_elapsed >= SCAN_GRACE_SEC)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        msg = (f"\r[WAIT] pre-anim  {dwell_elapsed:6.1f}/{SCAN_GRACE_SEC:6.1f} | API unreachable; holding…")
                        sys.stdout.write(msg); sys.stdout.flush()
                        last_log_t = now
                    if min_dwell_done:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] API unreachable; min dwell elapsed → back to CAM")
                        STATE = "CAM"
                        continue
                else:
                    # In override but API vanished — rely on safety timeout from last start we saw
                    if (time.time() - (last_started_seen or wait_t0)) > ANIM_WAIT_TIMEOUT:
                        sys.stdout.write("\n"); sys.stdout.flush()
                        print("[KIOSK] override + API down; safety timeout → back to CAM")
                        STATE = "CAM"
                        continue

            time.sleep(POLL_SLEEP_WAIT)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[KIOSK] bye")
