"""A TLS context that actually has CA certificates to verify against.

The Homey Python runtime ships without a system CA bundle, so
`ssl.create_default_context()` there trusts nothing and every HTTPS call fails with
CERTIFICATE_VERIFY_FAILED. `certifi` (declared in app.json's pythonPackages) provides
the CA bundle; this builds one context from it that both the REST client and the MQTT
client use.

Falls back to the plain default context if certifi somehow isn't present, so the
import never takes the app down — the caller just sees the same verify error it would
have anyway.
"""

import ssl


def ca_file() -> str | None:
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def ssl_context() -> ssl.SSLContext:
    cafile = ca_file()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()
