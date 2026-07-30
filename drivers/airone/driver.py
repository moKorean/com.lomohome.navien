"""AirOne driver export shim.

The implementation lives in lib/airone/driver.py so it stays importable in tests and
a future per-model driver can subclass it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from lib.airone.driver import AironeDriver


class Driver(AironeDriver):
    """Navien AirOne driver."""


homey_export = Driver
