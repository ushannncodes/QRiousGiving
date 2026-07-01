"""Adapter exposing the API cam_v2.py expects (DFRobot_HuskyLens_UART,
DFRobot_HuskyLens_I2C, ALGORITHM_FACE_RECOGNITION, with a
request()/count_blocks()/blocks() polling loop) on top of DFRobot's actual
HuskylensV2 client, vendored in vendor/dfrobot_huskylensv2.py.

DFRobot's own client uses a different shape (getResult()/
getCachedResultByIndex()), so this module is a thin translation layer rather
than a copy of vendor code.
"""

import logging
import os
import sys
import time

log = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import dfrobot_huskylensv2 as _vendor  # noqa: E402
from dfrobot_huskylensv2 import (  # noqa: E402
    HuskylensV2_I2C,
    HuskylensV2_UART,
    ALGORITHM_FACE_RECOGNITION,
    ALGORITHM_HAND_RECOGNITION,
    ALGORITHM_POSE_RECOGNITION,
)


# ProtocolV2.wait() blocks for up to 8000ms per call waiting for a clean
# I2C response before giving up — fine for a single quick read, but with
# pose recognition's much larger landmark payload, an occasional I2C
# hiccup means cam_v2.py's whole poll loop stalls for up to 8 real
# seconds (observed live: cam_state.json's timestamp freezing for ~8s at
# a stretch, which blanks the attract display since it looks stale).
# Same retry/give-up behavior, just fails fast instead of hanging.
_WAIT_TIMEOUT_MS = 500


def _fast_wait(self, command):
    receiving = True
    self.receive_buffer = bytearray(1024)
    self.receive_index = _vendor.HEADER_0_INDEX
    start_ms = time.time_ns() // 1_000_000
    while receiving:
        now_ms = time.time_ns() // 1_000_000
        if now_ms - start_ms > _WAIT_TIMEOUT_MS:
            break
        c = self._read_from_huskyLens()
        if c is None:
            time.sleep(0.01)
            continue
        if self.husky_lens_protocol_receive(c):
            receiving = False
    if receiving:
        return False, [], []
    if command != self.receive_buffer[_vendor.COMMAND_INDEX]:
        return False, [], []
    retInt = []
    retStr = []
    if command != _vendor.COMMAND_RETURN_ARGS:
        return True, [], []

    totalIntArgs = self.receive_buffer[_vendor.CONTENT_INDEX]
    contentSize = self.receive_buffer[_vendor.CONTENT_SIZE_INDEX]
    contentEnd = _vendor.CONTENT_INDEX + contentSize
    retValue = self.receive_buffer[_vendor.CONTENT_INDEX + 1]
    offset = _vendor.CONTENT_INDEX + 2
    for _ in range(totalIntArgs):
        v = self.receive_buffer[offset] | (self.receive_buffer[offset + 1] << 8)
        retInt.append(v)
        offset += 2

    offset = _vendor.CONTENT_INDEX + 10
    while offset < contentEnd:
        length = self.receive_buffer[offset]
        if length == 0:
            break
        offset += 1
        if offset + length > contentEnd:
            break
        s = bytes(self.receive_buffer[offset:offset + length]).decode("utf-8")
        retStr.append(s)
        offset += length
    return retValue == 0, retInt, retStr


_vendor.ProtocolV2.wait = _fast_wait


def _safe_face_result_init(self, buf):
    # Per HuskyLens2_Protocol.md, a face block's 20-byte eye/nose/mouth
    # landmark payload is appended after the name/content fields and should
    # be included in the packet's declared length. In practice (firmware
    # quirk, or the chunked 32-byte I2C reads getting cut short under
    # timing pressure) that payload is sometimes missing/truncated, which
    # made the vendor's FaceResult.__init__ read past the buffer end and
    # raise "bytearray index out of range" — crashing the whole poll loop.
    # We only ever need the basic xCenter/yCenter/width/height bounding box
    # (already parsed by Result.__init__ below), so make the landmark
    # fields best-effort instead of fatal.
    _vendor.Result.__init__(self, buf)
    face_fields = [
        ("leye_x", 0), ("leye_y", 2),
        ("reye_x", 4), ("reye_y", 6),
        ("nose_x", 8), ("nose_y", 10),
        ("lmouth_x", 12), ("lmouth_y", 14),
        ("rmouth_x", 16), ("rmouth_y", 18),
    ]
    base = _vendor.CONTENT_INDEX + 12 + self.nameLength + self.contentLength
    for name, offset in face_fields:
        try:
            value = _vendor.read_u16(buf, base + offset)
        except IndexError:
            value = 0
        setattr(self, name, value)


_vendor.FaceResult.__init__ = _safe_face_result_init


# Same fields HandResult.__init__ parses (see vendor/dfrobot_huskylensv2.py),
# duplicated here so the best-effort override below doesn't need to reach
# into the vendor class's closure.
_HAND_FIELDS = [
    ("wrist_x", 0), ("wrist_y", 2),
    ("thumb_cmc_x", 4), ("thumb_cmc_y", 6),
    ("thumb_mcp_x", 8), ("thumb_mcp_y", 10),
    ("thumb_ip_x", 12), ("thumb_ip_y", 14),
    ("thumb_tip_x", 16), ("thumb_tip_y", 18),
    ("index_finger_mcp_x", 20), ("index_finger_mcp_y", 22),
    ("index_finger_pip_x", 24), ("index_finger_pip_y", 26),
    ("index_finger_dip_x", 28), ("index_finger_dip_y", 30),
    ("index_finger_tip_x", 32), ("index_finger_tip_y", 34),
    ("middle_finger_mcp_x", 36), ("middle_finger_mcp_y", 38),
    ("middle_finger_pip_x", 40), ("middle_finger_pip_y", 42),
    ("middle_finger_dip_x", 44), ("middle_finger_dip_y", 46),
    ("middle_finger_tip_x", 48), ("middle_finger_tip_y", 50),
    ("ring_finger_mcp_x", 52), ("ring_finger_mcp_y", 54),
    ("ring_finger_pip_x", 56), ("ring_finger_pip_y", 58),
    ("ring_finger_dip_x", 60), ("ring_finger_dip_y", 62),
    ("ring_finger_tip_x", 64), ("ring_finger_tip_y", 66),
    ("pinky_finger_mcp_x", 68), ("pinky_finger_mcp_y", 70),
    ("pinky_finger_pip_x", 72), ("pinky_finger_pip_y", 74),
    ("pinky_finger_dip_x", 76), ("pinky_finger_dip_y", 78),
    ("pinky_finger_tip_x", 80), ("pinky_finger_tip_y", 82),
]

# MediaPipe's standard 21-point hand landmark order (wrist first, then each
# finger base-to-tip) — matches HandResult's field order exactly, so callers
# that already speak MediaPipe-shaped landmarks (lm[0]=wrist, lm[5..8]=index,
# etc.) work unchanged against HuskyLens landmarks.
HAND_LANDMARK_NAMES = [
    "wrist",
    "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip",
    "index_finger_mcp", "index_finger_pip", "index_finger_dip", "index_finger_tip",
    "middle_finger_mcp", "middle_finger_pip", "middle_finger_dip", "middle_finger_tip",
    "ring_finger_mcp", "ring_finger_pip", "ring_finger_dip", "ring_finger_tip",
    "pinky_finger_mcp", "pinky_finger_pip", "pinky_finger_dip", "pinky_finger_tip",
]


def _safe_hand_result_init(self, buf):
    # Same truncated-payload issue as FaceResult above (seen live: a
    # "bytearray index out of range" mid-session) — best-effort instead of
    # fatal so one bad read doesn't kill the poll loop.
    _vendor.Result.__init__(self, buf)
    base = _vendor.CONTENT_INDEX + 12 + self.nameLength + self.contentLength
    for name, offset in _HAND_FIELDS:
        try:
            value = _vendor.read_u16(buf, base + offset)
        except IndexError:
            value = 0
        setattr(self, name, value)


_vendor.HandResult.__init__ = _safe_hand_result_init


# Same fields PoseResult.__init__ parses (see vendor/dfrobot_huskylensv2.py).
_POSE_FIELDS = [
    ("nose_x", 0), ("nose_y", 2),
    ("leye_x", 4), ("leye_y", 6),
    ("reye_x", 8), ("reye_y", 10),
    ("lear_x", 12), ("lear_y", 14),
    ("rear_x", 16), ("rear_y", 18),
    ("lshoulder_x", 20), ("lshoulder_y", 22),
    ("rshoulder_x", 24), ("rshoulder_y", 26),
    ("lelbow_x", 28), ("lelbow_y", 30),
    ("relbow_x", 32), ("relbow_y", 34),
    ("lwrist_x", 36), ("lwrist_y", 38),
    ("rwrist_x", 40), ("rwrist_y", 42),
    ("lhip_x", 44), ("lhip_y", 46),
    ("rhip_x", 48), ("rhip_y", 50),
    ("lknee_x", 52), ("lknee_y", 54),
    ("rknee_x", 56), ("rknee_y", 58),
    ("lankle_x", 60), ("lankle_y", 62),
    ("rankle_x", 64), ("rankle_y", 66),
]

POSE_LANDMARK_NAMES = [name[:-2] for name, off in _POSE_FIELDS if name.endswith("_x")]


def _safe_pose_result_init(self, buf):
    # Same truncated-payload issue as FaceResult/HandResult above —
    # best-effort instead of fatal so one bad read doesn't kill the poll loop.
    _vendor.Result.__init__(self, buf)
    base = _vendor.CONTENT_INDEX + 12 + self.nameLength + self.contentLength
    for name, offset in _POSE_FIELDS:
        try:
            value = _vendor.read_u16(buf, base + offset)
        except IndexError:
            value = 0
        setattr(self, name, value)


_vendor.PoseResult.__init__ = _safe_pose_result_init


class _Block:
    def __init__(self, result):
        self.width = result.width
        self.height = result.height
        self.x = result.xCenter - self.width // 2
        self.y = result.yCenter - self.height // 2


class _HandBlock(_Block):
    """Bbox plus the 21 hand landmarks, in MediaPipe's wrist-first order, as
    (x, y) pixel-coordinate pairs (not normalized — HuskyLens doesn't report
    its working resolution, so callers needing 0..1 space must pick their own
    reference scale)."""

    def __init__(self, result):
        super().__init__(result)
        self.landmarks = [
            (getattr(result, f"{name}_x"), getattr(result, f"{name}_y"))
            for name in HAND_LANDMARK_NAMES
        ]


class _PoseBlock(_Block):
    """Bbox plus named body landmarks (nose, l/rshoulder, l/rhip, etc.) as
    a dict of (x, y) pixel-coordinate pairs — raw sensor pixels, same
    caveat as _HandBlock (no reported working resolution)."""

    def __init__(self, result):
        super().__init__(result)
        self.landmarks = {
            name: (getattr(result, f"{name}_x"), getattr(result, f"{name}_y"))
            for name in POSE_LANDMARK_NAMES
        }


class _HuskyLensAdapter:
    def __init__(self):
        self._algo = ALGORITHM_FACE_RECOGNITION

    def begin(self):
        return self._hl.begin()

    def write_algo(self, algo, retries=14, settle=3.5):
        # No reliable way to confirm the switch actually happened from
        # software: switchAlgorithm()'s ack only means the command frame was
        # received, not that the new model finished loading (observed: acks
        # True on the first call, sensor's own screen stays on the old
        # algorithm indefinitely). A prior attempt tried checking
        # getResult(algo)'s response header (Result.algo) as "ground truth",
        # but that field is just an echo of the algo byte *we* put in the
        # request (see husky_lens_protocol_write_begin) — it always matches,
        # so that check was a no-op that made cam_v2.py stop retrying after
        # one attempt while the device was still on the wrong algorithm.
        #
        # Confirmed live (physical screen watched across repeated calls):
        # a single switchAlgorithm() + settle does NOT reliably apply the
        # switch, even after 20s. Re-issuing the command every ~4s across a
        # ~30s window does eventually get it to take. So: brute-force
        # re-issue on a generous budget instead of trusting any single ack.
        self._algo = algo
        talked_to_sensor = False
        for attempt in range(retries):
            try:
                self._hl.switchAlgorithm(algo)
                talked_to_sensor = True
            except Exception as e:
                log.warning("switchAlgorithm attempt %d/%d raised: %s", attempt + 1, retries, e)
            # The sensor briefly drops off the bus while it switches models
            # (observed: disappears from i2cdetect for a couple seconds) —
            # give it time to settle before the next request.
            time.sleep(settle)
        # Best-effort: we can't confirm the model actually loaded, only that
        # we got at least one command frame through without the bus itself
        # erroring out (total silence across every retry means something
        # more fundamental than a slow model swap, e.g. the sensor dropped
        # off entirely).
        return talked_to_sensor

    def request(self):
        return self._hl.getResult(self._algo) is not None

    def count_blocks(self):
        return self._hl.getCachedResultNum(self._algo)

    def blocks(self):
        n = self.count_blocks()
        if self._algo == ALGORITHM_HAND_RECOGNITION:
            wrapper = _HandBlock
        elif self._algo == ALGORITHM_POSE_RECOGNITION:
            wrapper = _PoseBlock
        else:
            wrapper = _Block
        return [
            wrapper(self._hl.getCachedResultByIndex(self._algo, i))
            for i in range(n)
        ]


class DFRobot_HuskyLens_I2C(_HuskyLensAdapter):
    def __init__(self, bus=1, addr=0x32):
        super().__init__()
        self._hl = HuskylensV2_I2C(bus_num=bus)
        self._hl.i2c_addr = addr


class DFRobot_HuskyLens_UART(_HuskyLensAdapter):
    def __init__(self, baud=9600, uart_addr="/dev/ttyS0"):
        super().__init__()
        self._hl = HuskylensV2_UART(tty_name=uart_addr, baudrate=baud)
