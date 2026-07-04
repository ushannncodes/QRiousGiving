#!/usr/bin/env python3
"""leap_receiver.py — Pi-side UDP listener for Leap Motion hand data.

Receives the compact hand/finger JSON frames leap_sender.py streams from
the PC/Mac that has the real Leap Motion tracking service running (Leap
Gen 1 has no official ARM/Linux driver, so tracking can't happen on the Pi
itself — see leap_sender.py's docstring), and writes them to
/tmp/leap_state.json for attract_leap_shadow.py to read, in the exact same
schema synthetic_hand_test.py uses. This process does no smoothing or
projection itself — it's a dumb, fast relay; all of that lives in the
drawer so it can be retuned without touching the network path.

UDP (not a TCP/websocket reconnect dance) because we only ever care about
the *latest* hand position — an occasional dropped packet on the LAN is
imperceptible at the frame rates involved, and there's no state to
resynchronize.

Usage:
    python3 leap/leap_receiver.py
    # in another terminal/machine:
    python3 leap/leap_sender.py --pi-host <this-pi's-ip>

Env vars (all optional):
  LEAP_SIGNAL_PATH  default /tmp/leap_state.json
  LEAP_UDP_BIND     default 0.0.0.0
  LEAP_UDP_PORT     default 5566
"""

import json
import os
import socket
import time
import logging
import signal as _signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SIGNAL_PATH = os.getenv("LEAP_SIGNAL_PATH", "/tmp/leap_state.json")
BIND_ADDR   = os.getenv("LEAP_UDP_BIND", "0.0.0.0")
UDP_PORT    = int(os.getenv("LEAP_UDP_PORT", "5566"))

STATS_INTERVAL = 5.0


def _write_state(hands, now):
    tmp = SIGNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": now, "hands": hands}, f)
    os.replace(tmp, SIGNAL_PATH)


def main():
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_ADDR, UDP_PORT))
    sock.settimeout(1.0)
    log.info("leap_receiver: listening on %s:%d, writing %s", BIND_ADDR, UDP_PORT, SIGNAL_PATH)

    pkt_count = 0
    last_stats = time.time()
    last_sender = None

    while running:
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            if time.time() - last_stats >= STATS_INTERVAL:
                if pkt_count == 0:
                    log.warning("leap_receiver: no packets in the last %.0fs — is leap_sender.py running "
                                "and pointed at this Pi's IP:%d?", STATS_INTERVAL, UDP_PORT)
                last_stats = time.time()
                pkt_count = 0
            continue

        now = time.time()
        try:
            msg = json.loads(data)
            hands = msg.get("hands", [])
        except Exception as e:
            log.warning("leap_receiver: dropped unparseable packet from %s: %s", addr, e)
            continue

        _write_state(hands, now)
        pkt_count += 1
        if addr[0] != last_sender:
            log.info("leap_receiver: now receiving from %s", addr[0])
            last_sender = addr[0]

        if time.time() - last_stats >= STATS_INTERVAL:
            log.info("leap_receiver: %.1f pkts/sec from %s", pkt_count / STATS_INTERVAL, last_sender)
            pkt_count = 0
            last_stats = time.time()

    _write_state([], time.time())
    sock.close()
    log.info("leap_receiver: exited cleanly")


if __name__ == "__main__":
    main()
