"""AirOne device export shim. Implementation in lib/airone/device.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from lib.airone.device import AironeDevice_


class Device(AironeDevice_):
    """One paired Navien AirOne unit."""


homey_export = Device
