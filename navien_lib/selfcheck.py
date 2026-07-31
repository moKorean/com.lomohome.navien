"""Startup self-check for the Homey Python runtime.

This app needs two things from the runtime that a plain Homey app never exercises:
the `paho-mqtt` wheel declared in app.json's pythonPackages must import, and the
runtime must let the app open an outbound TLS WebSocket to AWS IoT. Neither can be
verified off-device, so both are probed once at startup and logged.

Every probe is read-only and failure is reported rather than raised — a failing probe
should show up in the log, not stop the app from starting.
"""

import platform
import socket
import sys


def _probe(name, fn):
    try:
        return f"{name}: {fn()}"
    except Exception as exc:
        return f"{name}: FAILED ({type(exc).__name__}: {exc})"


def _interpreter():
    return f"{platform.python_version()} on {platform.machine()} ({sys.platform})"


def _paho():
    import paho.mqtt.client as mqtt

    return f"paho-mqtt {getattr(mqtt, '__version__', 'version unknown')}"


def _ssl_context():
    from navien_lib.navien import tls

    cafile = tls.ca_file()
    ctx = tls.ssl_context()
    source = f"certifi ({cafile})" if cafile else "system default (no certifi!)"
    return f"TLS context ok (TLS {ctx.minimum_version.name}+), CA: {source}"


def _outbound_address():
    """The address the runtime would use to reach the internet. A bridge-looking
    address here can mean cloud hosts are unreachable."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 53))  # no packet sent; just picks the route
        return sock.getsockname()[0]


PROBES = (
    ("interpreter", _interpreter),
    ("paho-mqtt", _paho),
    ("tls-context", _ssl_context),
    ("outbound-address", _outbound_address),
)


def run(log) -> None:
    """Run every probe, passing each result line to `log`."""
    log("--- runtime self-check ---")
    for name, fn in PROBES:
        log(_probe(name, fn))
    log("--- end self-check ---")
