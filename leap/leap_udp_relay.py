#!/usr/bin/env python3
"""leap_udp_relay.py — runs on the Raspberry Pi, standalone.

Fans out the single UDP stream from leap_sender.py (Beelink) to multiple
local listeners. Only one process can bind a given UDP port, so without
this, leap_flipdot_preview.py (driving the physical panel) and
leap_visualizer.py (browser preview) can't run at the same time against
the live feed — this lets both run continuously side by side, each
listening on its own local port instead of both fighting over 5111.

Usage: point leap_sender.py at this script's LISTEN_PORT (unchanged,
same port it already targets), then point leap_flipdot_preview.py and
leap_visualizer.py at one FANOUT_PORTS entry each via their own
LISTEN_PORT env var:

  python3 leap_udp_relay.py                          # listens :5111, fans to 5112 + 5113
  LISTEN_PORT=5112 python3 leap_flipdot_preview.py    # panel
  LISTEN_PORT=5113 python3 leap_visualizer.py         # browser preview

Env vars:
  LISTEN_PORT   port leap_sender.py actually sends to (default 5111 —
                must match the Beelink's RPI_HOST/RPI_PORT target,
                unchanged from before the relay existed)
  FANOUT_PORTS  comma-separated local ports to copy every packet to
                (default "5112,5113")
"""

import os
import socket

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
FANOUT_PORTS = [int(p) for p in os.getenv("FANOUT_PORTS", "5112,5113").split(",") if p.strip()]


def main():
    in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    in_sock.bind(("0.0.0.0", LISTEN_PORT))
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[leap_udp_relay] listening on :{LISTEN_PORT}, fanning out to {FANOUT_PORTS}")
    while True:
        data, _addr = in_sock.recvfrom(8192)
        for port in FANOUT_PORTS:
            out_sock.sendto(data, ("127.0.0.1", port))


if __name__ == "__main__":
    main()
