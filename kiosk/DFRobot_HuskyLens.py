"""Adapter exposing the API cam_v2.py expects (DFRobot_HuskyLens_UART,
DFRobot_HuskyLens_I2C, ALGORITHM_FACE_RECOGNITION, with a
request()/count_blocks()/blocks() polling loop) on top of DFRobot's actual
HuskylensV2 client, vendored in vendor/dfrobot_huskylensv2.py.

DFRobot's own client uses a different shape (getResult()/
getCachedResultByIndex()), so this module is a thin translation layer rather
than a copy of vendor code.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from dfrobot_huskylensv2 import (  # noqa: E402
    HuskylensV2_I2C,
    HuskylensV2_UART,
    ALGORITHM_FACE_RECOGNITION,
)


class _Block:
    def __init__(self, result):
        self.width = result.width
        self.height = result.height
        self.x = result.xCenter - self.width // 2
        self.y = result.yCenter - self.height // 2


class _HuskyLensAdapter:
    def __init__(self):
        self._algo = ALGORITHM_FACE_RECOGNITION

    def begin(self):
        return self._hl.begin()

    def write_algo(self, algo):
        self._algo = algo
        return self._hl.switchAlgorithm(algo)

    def request(self):
        return self._hl.getResult(self._algo) is not None

    def count_blocks(self):
        return self._hl.getCachedResultNum(self._algo)

    def blocks(self):
        n = self.count_blocks()
        return [
            _Block(self._hl.getCachedResultByIndex(self._algo, i))
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
