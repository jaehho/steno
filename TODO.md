# steno

See `PRODUCT.md` for what this is and what it refuses to be.

## v0.5 — done
- [x] dropped auto-triggered advisor responses. Guidance is pulled, never pushed.
- [x] end-of-meeting summary (`summarize.py`) -> `summary.md` + `meta.json` title.
- [x] automatic meeting detection (`detect.py`): an app holding an **uncorked
      PulseAudio source-output** is the signal. Our own parec self-tags via
      `--client-name` so it can never detect itself.

## v0.6 — done
- [x] **the app is the pipeline.** No separate daemon process: a long-running
      `Adw.Application` owns detection, capture, and finalization. `session.py`
      is the engine, emits `events.py`, imports no GUI toolkit, and is tested
      headlessly.
- [x] `gui/bridge.py`: the engine gets its own thread and asyncio loop; every
      event crosses back through `GLib.idle_add`. The only place the two loops
      meet.
- [x] GTK4 + libadwaita window, palette borrowed from `~/.config/theme/palette`.
      **Live** (far-side transcript, notes, ask) and **Archive** (summary,
      todos, notes, transcript).
- [x] notes and todos (`notes.py`). `notes.md` is never rewritten by the tool;
      `todos.json` keys checkmarks by text so they survive a re-summarize, and
      completed items are never dropped.
- [x] Textual TUI deleted; `textual`, `sounddevice`, `soundfile`, `numpy` off
      the dependency list.

## v0.7 — done
- [x] **renamed to `steno`.** `migrate_legacy()` renames the old config and data
      directories on first run, once, and only onto paths that don't exist.
- [x] **it runs without a window.** The app `hold()`s, so closing the window
      hides it and the listener keeps going; quitting is an explicit, separate
      gesture that confirms if a meeting is live. `--background` starts it with
      no window at all, which is what autostart uses.
- [x] **waybar indicator** (`state.py` + `steno bar`). The app publishes state
      to `$XDG_RUNTIME_DIR/steno/state.json`; the bar renders it and reports
      `off` if the writing process is gone. `packaging/install-waybar.py` wires
      it in idempotently, with backups, in the user's own palette.
- [x] **control from outside** (`control.py`): `steno show|toggle|pause|quit`
      drive the running app through the D-Bus actions every GApplication already
      exports. No socket, no pidfile.
- [x] **notifications** — recording starts with a Stop button attached; the
      summary arrives with a click-through to the meeting. With the window
      usually closed, this is how the user knows.
- [x] **playback is back** (`playback.py` + `gui/player.py`). Click any line in
      the archive to hear it, with the played line highlighted as it goes.
      GStreamer directly, because GTK's media backend isn't packaged on Arch.
      Seek rewinds by the estimated length of the utterance, since a record's
      timestamp is when it was *finalized*, not when it was said.
- [x] **playback sync.** Three separate errors: `meta.json` had no `started_at`
      on any existing session, so time zero was the *first spoken line* — off by
      the whole quiet head of the recording (149s on one meeting, 295s on
      another). `playback.derived_start()` now infers it from the audio
      (`mtime - duration`) and `repair_started_at()` caches it. New recordings
      store Deepgram's own stream offset per line (`offset`), which is exact and
      survives a reconnect, so nothing is estimated. And `started_at` is stamped
      immediately before capture rather than before two `pactl` subprocesses.
- [x] cross-session search in the archive (`Ctrl+F`), substring over titles,
      summaries, notes and transcripts.
- [x] retention (`retention.py`): `STENO_KEEP_AUDIO_DAYS` drops old WAVs and
      keeps the text. Off by default, and never touches a session missing a
      transcript or a summary.
- [x] Deepgram KeepAlive every 8s, so a quiet meeting doesn't drop the socket.
- [x] `claude-opus-5`. All current models are natively 1M context.
- [x] launcher: `dev.jaeho.Steno.desktop` with Record/Quit actions, plus a
      `NoDisplay` autostart entry that starts it in the background.

- [x] **`realign.py`** — re-times a pre-v0.7 session against its own audio and
      re-transcribes the far side in one pass. Prints the cost before spending
      it, keeps the previous transcript as `transcript.before-realign.jsonl`,
      and marks the session so it is not redone.
- [x] **deleting a meeting**, from the archive's ⋯ menu or `steno delete`. Goes
      to the freedesktop trash — implemented in `retention.py` rather than via
      `Gio` so it stays headless and testable. A failed trash is reported as a
      failure and **never** silently upgraded to a permanent delete. There is
      also "delete the audio, keep the text" for reclaiming space.

## v0.8 — done
- [x] **echo cancellation** (`echo.py`). On speakers the mic hears the far side
      too, so `you.wav` was a second copy of `them.wav` and every You/Them
      attribution downstream was a guess — measured at 411 of 482 "you" lines
      also appearing on the them track.
  - live: `make echo-cancel` asks PipeWire for a filtered twin of the mic
    (`monitor.mode`, so nothing has to change where it plays), and `capture_mic`
    records from it when it exists. `STENO_ECHO_CANCEL=0` opts out.
  - after the fact: `steno clean-echo` re-derives the echo path from the two
    tracks it already has — GCC-PHAT for the bulk delay, then a partitioned
    frequency-domain adaptive filter and a spectral suppressor for the part a
    linear filter cannot reach. Writes `you.clean.wav`, never over the original,
    and writes nothing at all when there was nothing to cancel. Measured on one
    session: bleed down 12.5 dB, the near voice down 0.6 dB.
  - `track_path()` prefers a cleaned track, so playback and `realign` both use
    it without being told.
- [x] **playback is both sides at once.** `gui/player.py` is an `audiomixer`
      with one branch per track instead of `playbin3` on one file, so a meeting
      plays as a conversation and the transcript highlight follows one timeline
      rather than every other line. A dropdown solos either side.

## Next
- [ ] the live canceller is wired and unverified: with headphones plugged in
      the mic hears no speaker output, so there is nothing to cancel and no way
      to measure it. Check the bleed number on the next meeting held on
      speakers.
- [ ] search should say *where* it matched, and jump to the line. Right now it
      only filters the sidebar, which is enough to find a meeting and not enough
      to find a moment.
- [ ] per-app / per-calendar-event briefs; right now there is one standing brief.
- [ ] cost meter — minutes transcribed and estimated spend.
- [ ] `Heard` events for the mic while the window is open, so the live transcript
      shows both sides when someone is actually watching.
- [ ] Deepgram keyterm boost from the brief's proper nouns (`&keyterm=`).
- [ ] diarization on the far side (`diarize=true`) + a learned name map, so
      summaries can say who owns what.
- [ ] redact the transcript before it goes to Claude (PII scrubber).
- [ ] sqlite FTS5 if the archive ever gets big enough that reading every
      transcript to filter a sidebar is slow. Not before.

## Deployment
Installed via `make link` + `make desktop` + `make autostart` (all no-sudo, all
running from this checkout). `uv tool install` is not viable — its venv is
isolated and can never see the system `python-gobject` the window needs;
`make install` builds the Arch package instead, which needs `python-anthropic`
from the AUR.

- [ ] push to GitHub, then convert `packaging/PKGBUILD` to a `-git` variant with
      a `pkgver()` reading git tags, and publish to the AUR.
- [ ] tag releases so the non-`-git` variant can pin a version.
- [ ] the checkout is still at `~/projects/meeting-copilot`. `mv` it and re-run
      `make link`; nothing in the tree depends on the directory name.
