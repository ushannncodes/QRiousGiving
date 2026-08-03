#!/usr/bin/env python3
"""check_feed.py — is the Beelink's Leap feed actually reaching this Pi?

Run this BEFORE run_kiosk.py. With no feed the kiosk doesn't error, it just
silently never detects anyone (see CLAUDE.md), which looks identical to a
broken sensor, a wedged panel, or a bad gesture threshold — so rule the
network out first.

It binds LISTEN_PORT itself, so nothing else may hold it while this runs.
Only one process can bind a UDP port: stop run_kiosk.py first, and Ctrl-C
this before starting it.

What "healthy" looks like: packets arriving continuously at ~SEND_HZ whether
or not a hand is over the sensor. beelink/leap_sender.py deliberately keeps
sending {"hands": []} when the volume is empty rather than falling silent,
so *zero* packets means a network/sender problem, never "no hand right now".

`pose block: yes` matters: hi5_final.py refuses to guess a hi-5 from raw
landmarks, so an old sender without `pose` aborts the hi-5 stage on purpose.

Env:
  LISTEN_PORT ("5111"), CHECK_SECS ("10")
"""
import json
import os
import socket
import time

LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5111"))
CHECK_SECS = float(os.getenv("CHECK_SECS", "10"))


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", LISTEN_PORT))
    except OSError as e:
        print(f"can't bind :{LISTEN_PORT} ({e}) — something else is holding it.")
        print('  ps aux | grep -E "run_kiosk|attract_leap|hi5_final|leap_"')
        return
    s.settimeout(1.0)

    print(f"listening on :{LISTEN_PORT} for {CHECK_SECS:.0f}s — "
          f"wave a hand over the Leap partway through…")

    t0 = time.time()
    packets = with_hands = pose_seen = malformed = 0
    senders = set()
    while time.time() - t0 < CHECK_SECS:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        packets += 1
        senders.add(addr[0])
        try:
            hands = json.loads(data.decode("utf-8")).get("hands", [])
        except Exception:
            malformed += 1
            continue
        if hands:
            with_hands += 1
            if isinstance(hands[0].get("pose"), dict):
                pose_seen += 1
    s.close()

    elapsed = time.time() - t0
    print(f"\n  packets      {packets}  (~{packets / max(elapsed, 0.1):.0f}/s)")
    print(f"  from         {', '.join(sorted(senders)) or '—'}")
    print(f"  with hands   {with_hands}")
    print(f"  pose block   {'yes' if pose_seen else 'no'}")
    if malformed:
        print(f"  malformed    {malformed}")

    print()
    if packets == 0:
        print("NO FEED. The kiosk will run but never see anyone. Check, in order:")
        print("  1. leap_sender.py actually running on the Beelink?")
        print("  2. Is it aimed here? This Pi is the RPI_HOST it must target:")
        os.system("hostname -I")
        print("  3. Beelink on the same network (the Prolink hotspot, not venue WiFi)?")
        print("  4. Windows Firewall blocking outbound UDP for python.exe?")
    elif with_hands == 0:
        print("Feed is healthy but saw no hands — fine if you never waved, "
              "otherwise check the Leap is plugged in and Gemini is tracking.")
    elif not pose_seen:
        print("Hands but NO pose block — the Beelink is running an OLD "
              "leap_sender.py. hi5_final.py will abort the hi-5 stage on "
              "purpose. Re-copy beelink/leap_sender.py over (see CLAUDE.md).")
    else:
        print("Feed healthy — packets, hands, and pose all present. "
              "Ctrl-C is not needed; port released. Safe to start the kiosk.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
