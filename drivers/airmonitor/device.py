"""AirMonitor device export shim. Implementation in navien_lib/airmonitor/device.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from navien_lib.airmonitor.device import AirMonitorDevice_


class Device(AirMonitorDevice_):
    """One paired Navien AirMonitor."""


homey_export = Device
