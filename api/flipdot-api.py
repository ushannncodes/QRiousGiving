#!/usr/bin/env python3
import os, time, json, threading, queue, subprocess
from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------------- Config ----------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))
TRIGGER_SECRET = os.getenv("TRIGGER_SECRET")  # optional; set both server & client

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Where the animation scripts live (adjust if needed)
ANIM_SCRIPT = os.getenv("ANIM_SCRIPT", os.path.join(SCRIPT_DIR, "..", "animations", "anim.py"))
LOADING_SCRIPT = os.getenv("LOADING_SCRIPT", os.path.join(SCRIPT_DIR, "..", "animations", "loading.py"))

# sequence name (as sent by the website) -> (script, extra env for that script)
SCRIPTS = {
    "anim_py": (ANIM_SCRIPT, {}),
    "loading_py": (LOADING_SCRIPT, {}),
    "loading_text_py": (LOADING_SCRIPT, {"LOADING_STYLE": "text"}),
}

# ---------------- Job System ----------------
job_q = queue.Queue()
stop_evt = threading.Event()

# Sequences that hold the panel for the better part of a minute. Only one of
# these may be in flight at a time — a press arriving while one runs is dropped
# rather than queued, so the panel stays in sync with whoever is standing in
# front of it. They exclude each other as a group, not just themselves: the two
# loading variants are the same opener, so a second is never wanted either way.
EXCLUSIVE_JOBS = {"loading_py", "loading_text_py"}

# Guards job_state and pending_jobs: the worker thread and Flask's request
# threads both touch them.
state_lock = threading.Lock()

# job name -> how many of that job are queued but not yet started
pending_jobs = {}

# State visible to /status
job_state = {
    "running": False,
    "current": None,
    "queue": 0,
    "last_started_ts": 0.0,
    "last_done_ts": 0.0,
}


def _exclusive_in_flight_locked():
    """Name of the exclusive job running or waiting, else None. Hold state_lock."""
    if job_state["current"] in EXCLUSIVE_JOBS:
        return job_state["current"]
    for name in EXCLUSIVE_JOBS:
        if pending_jobs.get(name, 0) > 0:
            return name
    return None

def run_script(path, extra_env=None):
    """
    Launch an animation script as a child process. This keeps serial ownership clean
    and avoids fighting with any open handles in this API process.
    """
    if not os.path.exists(path):
        print(f"[anim] script not found: {path}")
        return
    try:
        # If the script needs env vars (e.g., serial port), pass them through:
        env = os.environ.copy()
        # Example:
        # env["FLIPDOT_SERIAL"] = "/dev/ttyS0"
        # env["FLIPDOT_BAUD"] = "57600"
        env.update(extra_env or {})

        subprocess.run([os.sys.executable, path], check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"[anim] {os.path.basename(path)} exited with non-zero status: {e.returncode}")
    except Exception as e:
        print(f"[anim] {os.path.basename(path)} error: {e}")

def animation_countdown_and_fireworks():
    """
    Example placeholder job (if you want a non-anim.py fallback).
    Implement your own flipdot drawing here if desired.
    """
    print("[demo] countdown + fireworks (placeholder)")
    time.sleep(3.0)

def worker():
    while not stop_evt.is_set():
        try:
            job = job_q.get(timeout=0.25)
        except queue.Empty:
            job_state["queue"] = 0
            continue

        # Update state for observers. Leaving the queue and becoming "current"
        # has to be one atomic step, or an exclusive job is briefly invisible to
        # /trigger and a second copy slips through.
        with state_lock:
            if pending_jobs.get(job, 0) > 0:
                pending_jobs[job] -= 1
            job_state["running"] = True
            job_state["current"] = job
            job_state["queue"] = job_q.qsize()
            job_state["last_started_ts"] = time.time()

        try:
            if job in SCRIPTS:
                run_script(*SCRIPTS[job])
            elif job == "countdown_fireworks":
                animation_countdown_and_fireworks()
            else:
                print(f"[worker] unknown job: {job!r}")
        except Exception as e:
            print(f"[worker] job error: {e}")
        finally:
            job_q.task_done()
            with state_lock:
                job_state["running"] = False
                job_state["current"] = None
                job_state["queue"] = job_q.qsize()
                job_state["last_done_ts"] = time.time()

# Start worker
t = threading.Thread(target=worker, daemon=True)
t.start()

# ---------------- HTTP API ----------------
app = Flask(__name__)
CORS(app)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/status", methods=["GET"])
def status():
    # shallow copy so we can extend later
    with state_lock:
        st = dict(job_state)
        st["pending"] = {k: v for k, v in pending_jobs.items() if v > 0}
    return jsonify(st)

@app.route("/trigger", methods=["POST"])
def trigger():
    # Optional shared secret
    if TRIGGER_SECRET and request.headers.get("X-Trigger-Secret") != TRIGGER_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if job_q.qsize() > 20:
        return jsonify({"ok": False, "error": "Busy"}), 429

    body = request.get_json(silent=True) or {}
    sequence = body.get("sequence", "anim_py")  # default is anim.py job

    # Check and enqueue under one lock: two presses landing in different request
    # threads must not both see an idle display and both get queued.
    with state_lock:
        blocker = _exclusive_in_flight_locked() if sequence in EXCLUSIVE_JOBS else None
        if blocker:
            depth = job_q.qsize()
            rejected = True
        else:
            pending_jobs[sequence] = pending_jobs.get(sequence, 0) + 1
            job_q.put(sequence)
            depth = job_q.qsize()
            job_state["queue"] = depth
            rejected = False

    if rejected:
        print(f"[trigger] {sequence} blocked by {blocker} already in flight; press ignored")
        return jsonify({
            "ok": False,
            "error": "Already running",
            "sequence": sequence,
            "running": blocker,
            "queue_length": depth,
        }), 409

    return jsonify({"ok": True, "queued": sequence, "queue_length": depth})

if __name__ == "__main__":
    try:
        app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        stop_evt.set()
