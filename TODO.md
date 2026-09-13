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
  - live: PipeWire's own canceller sits in front of the mic as a hidden
    WirePlumber smart filter, so recording the default mic records through it.
    The config lives in dotfiles (`audio` package), not here.
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

## v0.9 — done
- [x] **headless Claude Code instead of the API** (`claude.py`). The summary and
      the advisor run `claude -p` on the subscription login, with settings, MCP
      and hooks off; `ANTHROPIC_API_KEY` is stripped from its environment and
      `anthropic` is off the dependency list.
- [x] **meetings tied to a project.** The summary prompt lists `~/projects`
      (`STENO_PROJECTS_DIR`) with each README's first line; when the meeting is
      about one, Claude reads it with Read/Grep/Glob, allowed inside that root
      only, and `meta.json` records `project`. The archive row shows it.

## v0.10 — done
- [x] **one window, no tabs.** Live and Archive merged: a sidebar with a status
      card (what Steno is doing, why, and the one button that changes it) over
      the meeting list, and a meeting in progress as the top row under "Now".
      A past meeting is one page — summary, to-dos, notes, ask, transcript —
      with the player pinned to the bottom instead of hidden on a tab.
- [x] **adaptive.** `Adw.NavigationSplitView` + breakpoints: two columns when
      wide, stacked below 1000sp (`Adw.MultiLayoutView`), list and meeting as
      separate pages below 600sp. Needs libadwaita 1.6.
- [x] libadwaita's own surfaces recoloured from the palette; tracked-caps chrome
      replaced with sentence-case labels. Toasts for errors and a finished
      summary, instead of a tooltip nobody hovered.
- [x] asking about a past meeting (`Engine.ask(session_dir=)`), over the whole
      transcript with its own prompt.

## v0.11
- [x] **tray icon** (`gui/tray.py`): a StatusNotifierItem exported over Gio, its
      menu through libdbusmenu-glib. libayatana-appindicator is GTK3-only, and
      its GTK-free successor exports `org.gtk.Menus`, which waybar/KDE/XFCE
      don't read (upstream issues #87, #102; dbusmenu fallback is PR #103).
      Verified on waybar over D-Bus: registration, menu layout, pause/resume
      clicks flip the state. `STENO_TRAY=0` turns it off.
- [x] **own icons** in `src/steno/data/icons/hicolor` (package data): the app
      icon and one per tray state, handed to the host via `IconThemePath`, so
      the tray no longer depends on the host theme carrying stock names.
- [x] **start at login is opt-in, per user** (`autostart.py`): "Start at login"
      in the window menu, `steno autostart on|off`. The package no longer
      installs `/etc/xdg/autostart`; `steno.install` says how to turn it on, and
      a compositor line (`make hypr`) counts as on without being touched.
- [x] one public email: PKGBUILD maintainer is jaeho2025@gmail.com, as in the commits.
- [x] version 0.11.0 everywhere (`pyproject.toml`, `__version__`, PKGBUILD).

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
Installed via `make venv` + `make link` + `make desktop` + `make autostart`
(all no-sudo, all running from this checkout). `make venv` pins
`/usr/bin/python`: `--system-site-packages` shares the site-packages of
whichever interpreter uv picks, and left alone uv picks its own managed build,
which has neither `python-gobject` nor `numpy`. `uv tool install` is not viable
either — that venv is isolated and can never see the system `python-gobject`
the window needs; `make install` builds the Arch package instead.

- [x] pushed to GitHub: `jaehho/steno`, private.
- [x] `packaging/aur/steno-git/`: `-git` PKGBUILD + `.SRCINFO` (`make aur`),
      `pkgver()` from tags with an `r<count>.<hash>` fallback. Built from a
      mirror of the working tree, then in a clean chroot.
- [x] clean-chroot build: `check()` passes against the real packages (199
      tests); namcap's license-path error and implicit deps fixed.
- [x] public, tagged `v0.11.0`, `steno-git` published to the AUR. History was
      rewritten first to drop a real meeting's name from TODO.md.
- [ ] tag releases so the non-`-git` variant can pin a version.
- [x] the checkout moved to `~/projects/steno`. Only two things baked the old
      path — the `~/.local/bin/steno` wrapper and the venv's shebangs — so a
      move is `make venv && make link`. Everything else (desktop entry, the
      Hyprland line, the waybar module) goes through `steno` on PATH.
