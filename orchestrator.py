#!/usr/bin/env python3
import os, sys, time, json, subprocess, signal

CAM = os.path.abspath("./cam_final.py")
HI5 = os.path.abspath("./hi5_final.py")

# knobs (override via env)
CAM_SIGNAL_PATH   = os.getenv("CAM_SIGNAL_PATH", "/tmp/cam_state.json")
INTERACT_REQUIRED = float(os.getenv("INTERACT_REQUIRED_SEC", "7.0"))  # tweakable
STALE_AFTER       = float(os.getenv("STALE_AFTER_SEC", "2.0"))        # how long until a heartbeat is considered stale
POLL              = float(os.getenv("POLL_SEC", "0.1"))



def start_cam():
    env = os.environ.copy()
    env["CAM_SIGNAL_PATH"] = CAM_SIGNAL_PATH
    # keep your cam free to change; orchestrator doesn’t depend on CLI args
    return subprocess.Popen([sys.executable, CAM], env=env)

def wait_for_presence():
    last = None
    while True:
        time.sleep(POLL)
        try:
            with open(CAM_SIGNAL_PATH, "r") as f:
                hb = json.load(f)
        except Exception:
            continue
        now = time.time()
        ts = hb.get("ts", 0.0)
        if (now - ts) > STALE_AFTER:
            # cam paused or crashed; let caller decide what to do
            continue
        active_for = float(hb.get("active_for", 0.0))
        if active_for >= INTERACT_REQUIRED:
            return

def stop_proc(p: subprocess.Popen, grace=0.3):
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGINT)
    except Exception:
        pass
    t0 = time.time()
    while (time.time() - t0) < grace:
        if p.poll() is not None:
            return
        time.sleep(0.05)
    try:
        p.terminate()
    except Exception:
        pass

def main():
    while True:
        cam = start_cam()
        try:
            wait_for_presence()  # blocks until N seconds detected
        except KeyboardInterrupt:
            stop_proc(cam); return
        # we got enough interaction; stop cam and run hi5
        stop_proc(cam)
        env = os.environ.copy()
        # pass through serial/baud etc if you prefer:
        # env.setdefault("FLIPDOT_SERIAL", "/dev/ttyS0")
        # env.setdefault("FLIPDOT_BAUD", "57600")
        subprocess.run([sys.executable, HI5], env=env)
        # after hi5 exits, loop back to camera
        # (if you want a one-shot, just `return` here)

if __name__ == "__main__":
    main()
