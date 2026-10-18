"""Headless tests.

Nothing here opens audio, reaches the network, needs a display, or talks to the
user's task app — the engine is deliberately separable from both the GUI and the
services it calls, and these exist to keep it that way.
"""
import os
from unittest import mock

from steno import asstlink

# Second layer, below the patch: the session bus asst talks to. Even if a
# run loads the test modules without this package (gotcha #13 recurred that
# way), `asst` cannot reach the user's asstd; it fails to connect and exits.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/steno-tests"

# The real `available`, for the test of the STENO_NO_ASST switch itself.
real_available = asstlink.available

# A test run must never file tasks into the real inbox. Tests that mean to
# exercise the asst handshake re-enable asstlink under mock; the default is off.
mock.patch.object(asstlink, "available", lambda: False).start()
