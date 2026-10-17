"""pytest entry: load tests/__init__ even under --import-mode=importlib.

That package keeps the suite away from the user's real asst inbox
(gotcha #13); a run that skips it files fixture to-dos into the phone.
"""
import tests  # noqa: F401
