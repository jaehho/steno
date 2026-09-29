"""Headless tests.

Nothing here opens audio, reaches the network, needs a display, or talks to the
user's task app — the engine is deliberately separable from both the GUI and the
services it calls, and these exist to keep it that way.
"""
from unittest import mock

from steno import asstlink

# A test run must never file tasks into the real inbox. Tests that mean to
# exercise the asst handshake re-enable asstlink under mock; the default is off.
mock.patch.object(asstlink, "available", lambda: False).start()
