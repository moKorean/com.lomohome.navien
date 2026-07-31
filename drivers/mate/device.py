"""Mat device export shim. Implementation in navien_lib/mate/device.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from navien_lib.mate.device import MateDevice_


class Device(MateDevice_):
    """One paired Navien sleep mat."""


homey_export = Device
