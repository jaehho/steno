#!/usr/bin/env python
"""Give the microphone an echo-cancelled twin, for meetings held on speakers.

On headphones the two recorded tracks are independent: the mic hears you, the
sink monitor hears them. On speakers the mic hears *both*, so the near-side
track is a muddy copy of the far side and every attribution downstream — who
said what, whose todo it is — is guesswork.

This asks PipeWire for a second, filtered source: the same microphone with
whatever is currently coming out of the speakers subtracted from it, using the
WebRTC canceller PipeWire already ships. `monitor.mode` makes it take the
default sink's monitor as its reference, which is the whole point — no
application has to be told to play into a special device, and nothing about the
existing setup changes. The real microphone stays exactly where it was; this
only adds a source next to it, which Steno then prefers.

    python packaging/install-echo-cancel.py            # install
    python packaging/install-echo-cancel.py --remove   # take it back out
    python packaging/install-echo-cancel.py --dry-run  # show what would change

PipeWire reads `pipewire.conf.d` at startup, so this restarts the user's own
audio services (never the system's, never sudo). That is a second of silence.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from steno.echo import (
    AEC_LIBRARY,
    AEC_SOURCE_DESCRIPTION,
    AEC_SOURCE_NAME,
)

CONFIG = (
    Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
    / "99-steno-echo-cancel.conf"
)
SERVICES = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")

BODY = f"""\
# Written by steno (packaging/install-echo-cancel.py). Safe to delete.
#
# A copy of the default microphone with the default sink's output subtracted
# from it, so a meeting held on speakers does not record the far side twice.
# `monitor.mode` takes the reference from the sink's monitor, so no application
# has to play into a special device and the real microphone is left alone.
context.modules = [
    {{ name = libpipewire-module-echo-cancel
        args = {{
            monitor.mode = true
            library.name = {AEC_LIBRARY}
            aec.args = {{
                # Cancellation only. Gain control and noise suppression are the
                # kind of thing that sounds better and transcribes worse, and
                # this audio exists to be transcribed.
                webrtc.gain_control = false
                webrtc.noise_suppression = false
                webrtc.high_pass_filter = true
            }}
            capture.props = {{
                node.name = "steno_echo_cancel.capture"
                # Follows whatever the default microphone is, rather than
                # pinning today's device into a config file.
                node.passive = true
            }}
            source.props = {{
                node.name = "{AEC_SOURCE_NAME}"
                node.description = "{AEC_SOURCE_DESCRIPTION}"
            }}
        }}
    }}
]
"""


def aec_library_present() -> bool:
    return any(
        (Path(root) / f"{AEC_LIBRARY}.so").is_file()
        for root in ("/usr/lib/spa-0.2", "/usr/lib64/spa-0.2", "/usr/local/lib/spa-0.2")
    )


def restart_audio(dry_run: bool = False) -> None:
    """Reload the user's own audio stack. No sudo, no system units."""
    if dry_run:
        print(f"--- would restart {' '.join(SERVICES)}")
        return
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", *SERVICES],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"could not restart pipewire ({e}); log out and back in")
        return
    # The nodes appear a moment after the daemon does.
    for _ in range(20):
        time.sleep(0.25)
        if source_exists():
            return


def source_exists() -> bool:
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(AEC_SOURCE_NAME in row for row in out.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true", help="take the source back out")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    if args.remove:
        if not CONFIG.is_file():
            print("no echo-cancelled source installed")
            return
        if args.dry_run:
            print(f"--- would delete {CONFIG}")
            return
        CONFIG.unlink()
        print(f"removed {CONFIG}")
        restart_audio()
        print("microphone is back to unfiltered")
        return

    if not aec_library_present():
        raise SystemExit(
            f"{AEC_LIBRARY}.so is missing — install pipewire's WebRTC echo "
            "canceller (Arch: it ships in `pipewire`; check `pacman -Ql pipewire "
            "| grep aec`)"
        )

    if CONFIG.is_file() and CONFIG.read_text() == BODY:
        print("pipewire config already up to date")
        if source_exists():
            print(f"source present: {AEC_SOURCE_NAME}")
            return
        restart_audio(args.dry_run)
    else:
        if args.dry_run:
            print(f"--- would write {CONFIG}")
            print(BODY)
            restart_audio(dry_run=True)
            return
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG.is_file():
            copy = CONFIG.with_suffix(f".conf.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(CONFIG, copy)
            print(f"backed up {copy.name}")
        CONFIG.write_text(BODY)
        print(f"wrote {CONFIG}")
        restart_audio()

    if source_exists():
        print(f"source present: {AEC_SOURCE_NAME}")
        print("steno will record the microphone through it from the next meeting")
    else:
        print(
            "the source did not appear. `pactl list short sources` to look, and "
            "`journalctl --user -u pipewire -n 50` for why"
        )


if __name__ == "__main__":
    main()
