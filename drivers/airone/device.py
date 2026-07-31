"""AirOne device export shim. Implementation in navien_lib/airone/device.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from navien_lib.airone.device import AironeDevice_


class Device(AironeDevice_):
    """One paired Navien AirOne unit."""


homey_export = Device
