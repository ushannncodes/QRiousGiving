#!/usr/bin/env python3
import os, time, json, signal, subprocess, sys, urllib.request, urllib.error

# ---------- TTY-aware logger (so journald shows elapsed/target lines) ----------
IS_TTY = sys.stdout.isatty()
def _log_line(s: str):
    if IS_TTY:
        sys.stdout.write("\r" + s); sys.stdout.flush()
    else:
        print(s)

# ---------- Config (env-tweakable) ----------
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CAM_SCRIPT     = os.getenv("CAM_SCRIPT",     os.path.join(SCRIPT_DIR, "cam_v2.py"))
HI5_SCRIPT     = os.getenv("HI5_SCRIPT",     os.path.join(SCRIPT_DIR, "hi5_final.py"))
ATTRACT_SCRIPT = os.getenv("ATTRACT_SCRIPT", os.path.join(SCRIPT_DIR, "attract_outline.py"))

CAM_SIGNAL_PATH     = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
TRIGGER_HOLD_SEC    = float(os.getenv("TRIGGER_HOLD_SEC", "20.0"))   # presence to promote -> HI5

ACTIVE_STALE_SEC    = float(os.getenv("ACTIVE_STALE_SEC", "2.0"))   # heartbeat freshness from cam
API_STATUS_URL      = os.getenv("API_STATUS_URL", "http://127.0.0.1:8080/status")

# PRE-ANIM grace window only; killed the instant any animation is detected (active or pending)
SCAN_GRACE_SEC      = float(os.getenv("SCAN_GRACE_SEC", "120.0"))

ANIM_WAIT_TIMEOUT   = float(os.getenv("ANIM_WAIT_TIMEOUT", "300.0"))# safety cap
COOLDOWN_AFTER_ANIM = float(os.getenv("COOLDOWN_AFTER_ANIM", "1.0"))# small breather

WARMUP_SEC          = float(os.getenv("WARMUP_SEC", "1.0"))         # seconds of motion before hold
WARMUP_DELTA_MIN    = float(os.getenv("WARMUP_DELTA_MIN", "0"))     # cam_v2 always writes delta_ema=0; gate is the active flag

POLL_SLEEP_KIOSK    = float(os.getenv("POLL_SLEEP_CAM", "0.05"))
POLL_SLEEP_WAIT     = float(os.getenv("POLL_SLEEP_WAIT", "0.10"))
WAIT_LOG_EVERY      = float(os.getenv("WAIT_LOG_EVERY", "0.5"))

# NEW: HI-5 idle timeout + progress logging
HI5_IDLE_TIMEOUT_SEC = float(os.getenv("HI5_IDLE_TIMEOUT_SEC", "120.0"))
HI5_IDLE_LOG_EVERY   = float(os.getenv("HI5_IDLE_LOG_EVERY", "0.5"))

# ---------- helpers ----------
def _spawn_py(path):
    # Start unbuffered; optionally skip site for faster start
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    argv = [sys.executable, "-u"]
    if env.get("FAST_START", "0") == "1":
        argv.append("-S")
        env.setdefault("PYTHONNOUSERSITE", "1")
    argv.append(path)
    return subprocess.Popen(argv, env=env)

def _grace_stop(p, grace=0.2):
    if not p:
        return
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

def _is_alive(p):
    return (p is not None) and (p.poll() is None)

def _hard_kill(pattern):
    try:
        subprocess.run(["pkill", "-f", pattern],
                       check=False,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _ensure_cam_stopped(cam_proc):
    if _is_alive(cam_proc):
        _grace_stop(cam_proc)
    _hard_kill(r"/cam_v2\.py")
    return None

def _ensure_attract_stopped(attract_proc):
    if _is_alive(attract_proc):
        _grace_stop(attract_proc)
    _hard_kill(r"/attract_[a-z0-9_]+\.py")
    return None

def _ensure_hi5_stopped(hi5_proc):
    if _is_alive(hi5_proc):
        _grace_stop(hi5_proc)
    _hard_kill(r"/hi5_final\.py")
    return None

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
    # States: RUN_KIOSK → HI5 → WAIT_ANIM → RUN_KIOSK
    STATE = "RUN_KIOSK"
    cam = hi5 = attract = None
    hold_t0 = warmup_t0 = None
    print("[KIOSK] boot…")
    # Remove stale cam_state.json so an old active=True can't instantly trigger hi5
    try:
        os.remove(CAM_SIGNAL_PATH)
    except FileNotFoundError:
        pass

    # WAIT state bookkeeping (set on entry)
    pre_anim_mode = True           # True = grace applies; False = grace killed (override)
    dwell_elapsed = 0.0            # only used during pre-anim
    anim_started_seen = False
    last_started_seen = 0.0
    wait_t0 = 0.0
    last_log_t = 0.0

    # HI-5 idle timer + log throttle
    hi5_t0 = None
    hi5_last_log_t = 0.0

    while True:
        # ---------------- RUN_KIOSK (camera-driven scanning) ----------------
        if STATE == "RUN_KIOSK":
            # Ensure camera and attract are running
            if cam is None or cam.poll() is not None:
                print("[KIOSK] launching camera…")
                cam = _spawn_py(CAM_SCRIPT)
                hold_t0 = warmup_t0 = None
            if attract is None or attract.poll() is not None:
                print("[KIOSK] launching attract…")
                attract = _spawn_py(ATTRACT_SCRIPT)

            st = _read_cam_state()
            # Pre-empt CAM if an animation is active/pending while we're in RUN_KIOSK
            st_api = _get_api_status()
            if st_api:
                q            = int(st_api.get("queue", 0))
                running      = bool(st_api.get("running", False))
                last_started = float(st_api.get("last_started_ts", 0.0))
                last_done    = float(st_api.get("last_done_ts", 0.0))

                if running or q > 0 or (last_started > last_done):
                    print("[KIOSK] animation detected during RUN_KIOSK → killing CAM+attract and handing to anim")
                    cam     = _ensure_cam_stopped(cam)
                    attract = _ensure_attract_stopped(attract)
                    # Enter WAIT in override mode (grace killed)
                    STATE = "WAIT_ANIM"
                    pre_anim_mode      = False
                    dwell_elapsed      = 0.0
                    anim_started_seen  = True
                    last_started_seen  = max(last_started, 0.0)
                    wait_t0            = time.time()
                    last_log_t         = 0.0
                    print("[WAIT] phase      elapsed/target | started running q  done_ok        note")
                    time.sleep(POLL_SLEEP_WAIT)
                    continue

            now = time.time()
            active = bool(st.get("active"))
            delta_ema = float(st.get("delta_ema", 0.0))

            if active and (delta_ema >= WARMUP_DELTA_MIN):
                # start or continue warmup
                warmup_t0 = warmup_t0 or now
                if (now - warmup_t0) >= WARMUP_SEC:
                    hold_t0 = hold_t0 or now
                    if (now - hold_t0) >= TRIGGER_HOLD_SEC:
                        print("[KIOSK] trigger met → stopping CAM+attract + spawning HI5")
                        t_kill  = time.time()
                        cam     = _ensure_cam_stopped(cam)
                        attract = _ensure_attract_stopped(attract)
                        print(f"[KIOSK] CAM+attract stop done in {time.time()-t_kill:.3f}s → spawning HI5…")
                        t_spawn = time.time()
                        hi5 = _spawn_py(HI5_SCRIPT)
                        print(f"[KIOSK] spawned HI5 pid={hi5.pid} in {time.time()-t_spawn:.3f}s")
                        hi5_t0 = time.time()     # start HI5 idle timer
                        hi5_last_log_t = 0.0     # reset log throttle
                        STATE = "HI5"
                        warmup_t0 = hold_t0 = None
            else:
                warmup_t0 = hold_t0 = None

            time.sleep(POLL_SLEEP_KIOSK)

        # ---------------- HI5 (QR screen) ----------------
        elif STATE == "HI5":
            # Make absolutely sure camera and attract are not running while QR/anim flow is active
            cam     = _ensure_cam_stopped(cam)
            attract = _ensure_attract_stopped(attract)

            # If an animation is triggered while HI5 is playing, kill HI5 immediately
            st = _get_api_status()
            if st:
                q = int(st.get("queue", 0))
                running = bool(st.get("running", False))
                last_started = float(st.get("last_started_ts", 0.0))
                last_done    = float(st.get("last_done_ts", 0.0))

                if running or q > 0 or (last_started > last_done):
                    print("[KIOSK] animation queued/started during HI5 → killing HI5 and handing to anim")
                    hi5 = _ensure_hi5_stopped(hi5)
                    hi5_t0 = None
                    hi5_last_log_t = 0.0
                    print()  # finish any in-place HI5 progress line
                    # Enter WAIT in override mode (grace killed)
                    STATE = "WAIT_ANIM"
                    pre_anim_mode      = False
                    dwell_elapsed      = 0.0
                    anim_started_seen  = True
                    last_started_seen  = max(last_started, 0.0)
                    wait_t0            = time.time()
                    last_log_t         = 0.0
                    print("[WAIT] phase      elapsed/target | started running q  done_ok        note")
                    time.sleep(POLL_SLEEP_WAIT)
                    continue

            # ---- Progress log: show how much of the idle window has elapsed ----
            now = time.time()
            if hi5_t0 and (now - hi5_last_log_t) >= HI5_IDLE_LOG_EVERY:
                elapsed   = now - hi5_t0
                remaining = max(0.0, HI5_IDLE_TIMEOUT_SEC - elapsed)
                msg = (f"[HI5] idle  {elapsed:6.1f}/{HI5_IDLE_TIMEOUT_SEC:6.1f}s "
                       f"(remaining {remaining:5.1f}s)")
                _log_line(msg)  # TTY-safe single-line updater
                hi5_last_log_t = now

            # ---- Idle timeout: return to RUN_KIOSK if no interaction ----
            if hi5 and _is_alive(hi5) and hi5_t0 and (time.time() - hi5_t0) >= HI5_IDLE_TIMEOUT_SEC:
                print("\n[KIOSK] HI5 idle timeout → killing HI5 and returning to RUN_KIOSK")
                hi5 = _ensure_hi5_stopped(hi5)
                hi5_t0 = None
                hi5_last_log_t = 0.0
                STATE = "RUN_KIOSK"
                time.sleep(POLL_SLEEP_KIOSK)
                continue

            # Otherwise, wait for HI5 to exit normally → then start pre-anim grace
            if hi5 and hi5.poll() is not None:
                print("\n[KIOSK] HI5 exited → WAIT for scan/anim (pre-anim grace starts)")
                hi5_t0 = None
                hi5_last_log_t = 0.0
                STATE = "WAIT_ANIM"
                pre_anim_mode      = True
                dwell_elapsed      = 0.0
                anim_started_seen  = False
                last_started_seen  = 0.0
                wait_t0            = time.time()
                last_log_t         = 0.0
                print("[WAIT] phase      elapsed/target | started running q  done_ok        note")

            time.sleep(POLL_SLEEP_KIOSK)

        # ---------------- WAIT_ANIM ----------------
        elif STATE == "WAIT_ANIM":
            st = _get_api_status()
            now = time.time()

            # defaults if API is missing
            q = 0
            running = False
            last_started = 0.0
            last_done = 0.0

            if st:
                q = int(st.get("queue", 0))
                running = bool(st.get("running", False))
                last_started = float(st.get("last_started_ts", 0.0))
                last_done    = float(st.get("last_done_ts", 0.0))

                # Detect start or pending work
                if running or q > 0 or (last_started > last_done):
                    anim_started_seen = True
                    if last_started > 0.0:
                        last_started_seen = max(last_started_seen, last_started)
                    # Once anything is seen, KILL grace and keep CAM/HI5 dead
                    pre_anim_mode = False
                    cam     = _ensure_cam_stopped(cam)
                    attract = _ensure_attract_stopped(attract)
                    hi5     = _ensure_hi5_stopped(hi5)

                # -------- modes --------
                if pre_anim_mode and not anim_started_seen:
                    # PRE-ANIM: grace counts up until SCAN_GRACE_SEC
                    dwell_elapsed = now - wait_t0
                    min_dwell_done = (dwell_elapsed >= SCAN_GRACE_SEC)

                    # Log (throttled)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        note = "min dwell" if not min_dwell_done else "no anim; can exit"
                        msg = (f"[WAIT] pre-anim  {dwell_elapsed:6.1f}/{SCAN_GRACE_SEC:6.1f} | "
                               f"{int(anim_started_seen):>7d} {int(running):>7d} {q:>2d}  "
                               f"{int(last_done >= last_started_seen > 0):>7d}    {note:>12s}")
                        _log_line(msg)
                        last_log_t = now

                    # Exit back to RUN_KIOSK if grace elapsed and still no animation started
                    if min_dwell_done:
                        print()  # finish the progress line
                        print("[KIOSK] no scan/anim; min dwell elapsed → back to RUN_KIOSK")
                        STATE = "RUN_KIOSK"
                        continue

                    # Safety for stuck pre-anim wait
                    if (now - wait_t0) > ANIM_WAIT_TIMEOUT:
                        print()
                        print("[KIOSK] WAIT pre-anim safety timeout → back to RUN_KIOSK")
                        STATE = "RUN_KIOSK"
                        continue

                else:
                    # OVERRIDE: animation owns the display until done and queue is empty
                    done_ok = (last_done >= last_started_seen > 0)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        note = ("anim running" if running
                                else ("queue draining" if q > 0
                                      else ("finished; exit" if done_ok else "waiting")))
                        msg = (f"[WAIT] override   {'--':>6}/{ '--':>6} | "
                               f"{int(anim_started_seen):>7d} {int(running):>7d} {q:>2d}  "
                               f"{int(done_ok):>7d}    {note:>12s}")
                        _log_line(msg)
                        last_log_t = now

                    if (not running) and q == 0 and done_ok:
                        print()
                        print("[KIOSK] animations finished → back to RUN_KIOSK")
                        time.sleep(COOLDOWN_AFTER_ANIM)
                        STATE = "RUN_KIOSK"
                        continue

                    # Safety for override hanging too long without progress
                    if anim_started_seen and (time.time() - max(last_started_seen or 0.0, wait_t0)) > ANIM_WAIT_TIMEOUT:
                        print()
                        print("[KIOSK] override safety timeout → back to RUN_KIOSK")
                        STATE = "RUN_KIOSK"
                        continue

            else:
                # API unreachable
                if pre_anim_mode:
                    dwell_elapsed = now - wait_t0
                    min_dwell_done = (dwell_elapsed >= SCAN_GRACE_SEC)
                    if (now - last_log_t) >= WAIT_LOG_EVERY:
                        msg = (f"[WAIT] pre-anim  {dwell_elapsed:6.1f}/{SCAN_GRACE_SEC:6.1f} | API unreachable; holding…")
                        _log_line(msg)
                        last_log_t = now
                    if min_dwell_done:
                        print()
                        print("[KIOSK] API unreachable; min dwell elapsed → back to RUN_KIOSK")
                        STATE = "RUN_KIOSK"
                        continue
                else:
                    # In override but API vanished — rely on safety timeout
                    if (time.time() - max(last_started_seen or 0.0, wait_t0)) > ANIM_WAIT_TIMEOUT:
                        print()
                        print("[KIOSK] override + API down; safety timeout → back to RUN_KIOSK")
                        STATE = "RUN_KIOSK"
                        continue

            time.sleep(POLL_SLEEP_WAIT)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[KIOSK] bye")
