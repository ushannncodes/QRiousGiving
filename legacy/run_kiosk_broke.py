#!/usr/bin/env python3
import os, time, time, json, signal, subprocess, sys, urllib.request, urllib.error

# ---------- Config (env-tweakable) ----------
CAM_SCRIPT = os.getenv("CAM_SCRIPT", "./cam_final.py")
HI5_SCRIPT = os.getenv("HI5_SCRIPT", "./hi5_final.py")

CAM_SIGNAL_PATH     = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
TRIGGER_HOLD_SEC    = float(os.getenv("TRIGGER_HOLD_SEC", "4.0"))   # presence to promote -> HI5
# import os, time as _os
# _os.environ["TRIGGER_HOLD_SEC"] = str(TRIGGER_HOLD_SEC)

ACTIVE_STALE_SEC    = float(os.getenv("ACTIVE_STALE_SEC", "2.0"))   # heartbeat freshness
API_STATUS_URL      = os.getenv("API_STATUS_URL", "http://127.0.0.1:8080/status")

SCAN_GRACE_SEC      = float(os.getenv("SCAN_GRACE_SEC", "30.0"))    # window to scan the QR (MIN dwell)
ANIM_WAIT_TIMEOUT   = float(os.getenv("ANIM_WAIT_TIMEOUT", "300.0"))# safety cap
COOLDOWN_AFTER_ANIM = float(os.getenv("COOLDOWN_AFTER_ANIM", "1.0"))# small breather
WARMUP_SEC       = float(os.getenv("WARMUP_SEC", "2.0"))   # seconds of motion before hold
WARMUP_DELTA_MIN = float(os.getenv("WARMUP_DELTA_MIN", "8")) # min delta_ema to count as motion

POLL_SLEEP_CAM      = float(os.getenv("POLL_SLEEP_CAM", "0.05"))
POLL_SLEEP_WAIT     = float(os.getenv("POLL_SLEEP_WAIT", "0.10"))

# NEW: how often to update the WAIT logger line
WAIT_LOG_EVERY      = float(os.getenv("WAIT_LOG_EVERY", "0.5"))
FD_PAUSE_FLAG = os.getenv("FD_PAUSE_FLAG", "/tmp/fd_pause")


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
                        print(f"[KIOSK] trigger met at t={time.time():.3f} → pause cam + spawn HI5")
                        try:
                            open(FD_PAUSE_FLAG, "w").close()
                        except Exception:
                            pass
                        t_spawn = time.time()
                        hi5 = _spawn_py(HI5_SCRIPT)
                        print(f"[KIOSK] HI5 spawned at t={time.time():.3f} Δ={(time.time()-t_spawn):.3f}s")
                        STATE = "HI5"
                        warmup_t0 = None
                        hold_t0 = None

            time.sleep(POLL_SLEEP_CAM)

        elif STATE == "HI5":
            # Wait until hi5 exits (it should exit after painting QR)
            if hi5.poll() is not None:
                print("[KIOSK] HI5 exited → waiting for scan/anim (min dwell)")
                STATE = "WAIT_ANIM"
                anim_started_seen = False
                last_started_seen = 0.0
                wait_t0 = time.time()
                last_log_t = 0.0  # for logger cadence
                # print header once
                print("[WAIT] elapsed    / target   | anim_started running q  last_started_ok  note")
            time.sleep(POLL_SLEEP_CAM)

        elif STATE == "WAIT_ANIM":
            st = _get_api_status()
            now = time.time()
            elapsed = now - wait_t0
            min_dwell_done = (elapsed >= SCAN_GRACE_SEC)

            # defaults if API unreachable
            q = 0
            running = False
            last_started = 0.0
            last_done = 0.0

            if st is not None:
                q = int(st.get("queue", 0) or 0)
                running = bool(st.get("running"))
                last_started = float(st.get("last_started", 0.0) or 0.0)
                last_done = float(st.get("last_done", 0.0) or 0.0)

            anim_started_seen = False
            last_started_seen = 0.0
            # Any sign an animation started?
            if running or last_started > 0:
                anim_started_seen = True
                if last_started > 0:
                    last_started_seen = max(last_started_seen, last_started)

            # progress logger (throttled)

            if (now - last_log_t) >= WAIT_LOG_EVERY:
                started_ok = (last_started_seen > 0)
                done_ok = (last_done >= last_started_seen > 0)
                note = "waiting"
                if not anim_started_seen and not min_dwell_done:
                    note = "min dwell"
                elif not anim_started_seen and min_dwell_done:
                    note = "no anim; can exit"
                elif anim_started_seen and running:
                    note = "anim running"
                elif anim_started_seen and (not running) and (q>0):
                    note = "queue draining"
                elif anim_started_seen and done_ok and min_dwell_done:
                    note = "ready to exit"
                msg = (f"\r[WAIT] {elapsed:6.1f}s / {SCAN_GRACE_SEC:6.1f}s | "
                       f"{int(anim_started_seen):>12d} {int(running):>7d} {q:>2d}  "
                       f"{int(done_ok):>15d}  {note:>12s}")
                sys.stdout.write(msg); sys.stdout.flush()
                last_log_t = now

            if anim_started_seen:
                # REQUIRE BOTH min dwell and finished queue
                done_ok = (last_done >= last_started_seen > 0)
                if min_dwell_done and (not running) and (q == 0) and done_ok:
                    sys.stdout.write("\n"); sys.stdout.flush()
                    print("[KIOSK] min dwell hit AND anim finished → back to CAM")
                    print(f"[KIOSK] clearing pause flag t={time.time():.3f}")
                    try:
                        os.remove(FD_PAUSE_FLAG)
                    except FileNotFoundError:
                        pass
                    STATE = "CAM"
                    time.sleep(COOLDOWN_AFTER_ANIM)
                    continue
            else:
                # No animation started: leave right after min dwell
                if min_dwell_done:
                    sys.stdout.write("\n"); sys.stdout.flush()
                    print("[KIOSK] no scan/anim; min dwell elapsed → back to CAM")
                    print(f"[KIOSK] clearing pause flag t={time.time():.3f}")
                    try:
                        os.remove(FD_PAUSE_FLAG)
                    except FileNotFoundError:
                        pass
                    STATE = "CAM"
                    continue