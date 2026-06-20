# anim_pixel_wave_ripple.py
import numpy as np
import time
import os
import serial
import math

# -----------------------------
# Serial / Panel settings (same as ref)
# -----------------------------
SERIAL_PORT  = os.getenv("SERIAL_PORT", "/dev/ttyS0")
BAUD_RATE    = int(os.getenv("BAUD_RATE", "57600"))
PANEL_ADDRS  = [1, 2, 3, 4]   # 4 stacked 7x28 panels = 28x28
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# -----------------------------
# Canvas / Timing
# -----------------------------
HEIGHT, WIDTH   = 28, 28

# Overall loop duration and frame rate
LOOP_SEC        = float(os.getenv("LOOP_SEC", "10"))   # full cycle that loops seamlessly
FPS             = float(os.getenv("FPS", "50"))
FRAME_INTERVAL  = 1.0 / max(FPS, 1.0)

# Wave params (tweak to taste)
# A ring is "ON/black" when (distance - speed*t) modulo wavelength is within ring_thickness.
WAVE_SPEED_PX_S   = float(os.getenv("WAVE_SPEED_PX_S", "6.5"))   # outward pixels/sec
WAVELENGTH_PX     = float(os.getenv("WAVELENGTH_PX", "6.0"))     # ring spacing (pixels)
RING_THICKNESS_PX = float(os.getenv("RING_THICKNESS_PX", "1.2")) # visual ring thickness

# Fade-to-center envelope during the last FADE_SEC of the loop:
# progressively clip outer rings so we land on a tight center pulse
FADE_SEC          = float(os.getenv("FADE_SEC", "4"))

# Optional soft center “heartbeat” at loop start/end (subtle radius bloom)
CENTER_PULSE_PX   = float(os.getenv("CENTER_PULSE_PX", "1.5"))   # base pulse radius
PULSE_EXTRA_PX    = float(os.getenv("PULSE_EXTRA_PX", "0.8"))    # extra at pulse peak
PULSE_SHAPE       = os.getenv("PULSE_SHAPE", "cos")               # "cos" or "sin"

# -----------------------------
# Helpers
# -----------------------------
def pack_flipbytes(frame28):
    """
    Pack a 28x28 array of {0,1} into 4 panel payloads (column-major, 7 rows/byte).
    0 = black (dot flipped), 1 = white (dot resting).
    """
    panels = []
    for p in range(4):
        y0 = p * 7
        data = bytearray()
        for x in range(28):
            byte = 0
            for dy in range(7):
                bit = int(frame28[y0 + dy, x]) & 1
                byte |= (bit << dy)
            data.append(byte)
        panels.append(data)
    return panels

def send_to_panels(panels):
    for addr, data in zip(PANEL_ADDRS, panels):
        packet = bytearray([0x80, 0x83, addr]) + data + bytearray([0x8F])
        ser.write(packet)
    ser.flush()

# Precompute distance-from-center map (float pixels)
CX = (WIDTH  - 1) / 2.0
CY = (HEIGHT - 1) / 2.0
yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
DIST = np.sqrt((xx - CX)**2 + (yy - CY)**2)

# Useful bounds
MAX_RADIUS = float(np.sqrt(CX**2 + CY**2))  # to the farthest corner-ish

def center_pulse_radius(t_norm):
    """
    A small breathing pulse around the center at the start of the loop.
    t_norm in [0,1) across LOOP_SEC.
    Peaks near start/end so the fade lands cleanly on a center bloom.
    """
    if PULSE_SHAPE == "cos":
        # Peak at t=0: cos(0)=1 -> smoothly down -> cos(pi)= -1 at mid-loop
        osc = 0.5 * (1.0 + math.cos(2.0 * math.pi * t_norm))
    else:
        # sin shifted to also peak near 0 and 1
        osc = 0.5 * (1.0 + math.sin(2.0 * math.pi * (t_norm - 0.25)))
    return CENTER_PULSE_PX + PULSE_EXTRA_PX * (osc**2)  # square for tighter peak

def render_frame(t_now):
    """
    Compute a rippling ring field at absolute time t_now (seconds).
    Uses a modulo on LOOP_SEC to keep motion seamless.
    Fades outer rings during final FADE_SEC to land on a center pulse.
    """
    # Phase within loop [0, LOOP_SEC)
    t_phase = (t_now % LOOP_SEC)
    t_norm  = t_phase / LOOP_SEC

    # Outward-travel phase for the rings
    # For each pixel, consider saw = (d - v*t) % wavelength. If saw is near 0, we're on a ring.
    v  = WAVE_SPEED_PX_S
    wl = max(WAVELENGTH_PX, 1e-3)
    th = RING_THICKNESS_PX

    # Envelope: progressively clip the visible max radius in the last FADE_SEC
    if FADE_SEC > 0:
        if t_phase >= (LOOP_SEC - FADE_SEC):
            # f goes 1 -> 0 over the fade window
            f = (LOOP_SEC - t_phase) / FADE_SEC
        else:
            f = 1.0
    else:
        f = 1.0
    r_clip = f * MAX_RADIUS

    # Center pulse radius
    r_pulse = center_pulse_radius(t_norm)

    # Start with white canvas (1 = white)
    frame = np.ones((HEIGHT, WIDTH), dtype=np.uint8)

    # 1) Draw dynamic ripple rings within the current clipped radius
    #    Ring where saw < th or > wl - th (two-sided to get symmetric thickness)
    if r_clip > 0.25:  # avoid extra work at very end of fade
        # Compute sawtooth field
        saw = (DIST - v * t_phase) % wl
        ring_mask = (saw < th) | (saw > (wl - th))
        # Clip outer rings during fade
        if f < 1.0:
            ring_mask &= (DIST <= r_clip + th)
        frame[ring_mask] = 0  # black dots for ring

    # 2) Overdraw the center pulse as a filled disc so the loop lands gracefully
    frame[DIST <= r_pulse] = 0

    return frame

# -----------------------------
# Main
# -----------------------------
try:
    t0 = time.time()
    while True:
        now = time.time()
        frame = render_frame(now - t0)

        panels = pack_flipbytes(frame)
        send_to_panels(panels)

        time.sleep(FRAME_INTERVAL)

except KeyboardInterrupt:
    print("\n[WAVE] stopped.")
finally:
    try:
        ser.close()
    except Exception:
        pass
