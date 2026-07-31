"""Mat driver export shim. Implementation in navien_lib/mate/driver.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from navien_lib.mate.driver import MateDriver


class Driver(MateDriver):
    """Navien sleep-mat driver."""


homey_export = Driver
