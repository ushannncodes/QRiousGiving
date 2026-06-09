#!/usr/bin/env python3
import os, time, json, threading, queue, subprocess
from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------------- Config ----------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))
TRIGGER_SECRET = os.getenv("TRIGGER_SECRET")  # optional; set both server & client

# Where anim.py lives (adjust if needed)
ANIM_SCRIPT = os.getenv("ANIM_SCRIPT", "./anim.py")

# ---------------- Job System ----------------
job_q = queue.Queue()
stop_evt = threading.Event()

# State visible to /status
job_state = {
    "running": False,
    "queue": 0,
    "last_started_ts": 0.0,
    "last_done_ts": 0.0,
}

def run_anim_py():
    """
    Launch anim.py as a child process. This keeps serial ownership clean and avoids
    fighting with any open handles in this API process.
    """
    try:
        # If anim.py needs env vars (e.g., serial port), pass them through:
        env = os.environ.copy()
        # Example:
        # env["FLIPDOT_SERIAL"] = "/dev/ttyS0"
        # env["FLIPDOT_BAUD"] = "57600"

        subprocess.run([os.sys.executable, ANIM_SCRIPT], check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"[anim] exited with non-zero status: {e.returncode}")
    except Exception as e:
        print(f"[anim] error: {e}")

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

        # Update state for observers
        job_state["running"] = True
        job_state["queue"] = job_q.qsize()
        job_state["last_started_ts"] = time.time()

        try:
            if job == "anim_py":
                run_anim_py()
            elif job == "countdown_fireworks":
                animation_countdown_and_fireworks()
            else:
                print(f"[worker] unknown job: {job!r}")
        except Exception as e:
            print(f"[worker] job error: {e}")
        finally:
            job_q.task_done()
            job_state["running"] = False
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
    st = dict(job_state)
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

    job_q.put(sequence)
    job_state["queue"] = job_q.qsize()
    return jsonify({"ok": True, "queued": sequence, "queue_length": job_q.qsize()})

if __name__ == "__main__":
    try:
        app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        stop_evt.set()
