"""AirMonitor driver export shim. Implementation in navien_lib/airmonitor/driver.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from navien_lib.airmonitor.driver import AirMonitorDriver


class Driver(AirMonitorDriver):
    """Navien AirMonitor driver."""


homey_export = Driver
