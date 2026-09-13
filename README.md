# steno

Listens to your calls, keeps the other side's words on screen when you drift,
and turns each meeting into notes and todos worth reading a week later.

It starts itself. When an app opens the microphone — Zoom, Discord, Slack, a
browser tab on Meet — it begins recording; when the mic is released it stops,
transcribes your side, writes a summary, and pulls out what you agreed to do.
You are not expected to press anything during a meeting, because you won't.

Steno runs with **no window open** — it has to, since a meeting rarely begins
with you opening a meeting app. The window is a view onto a listener that is
already running. Closing it puts it away; quitting stops the listening, and says
so.

Built for Linux with pipewire-pulse. The window is GTK4 and libadwaita.

## Install

On Arch, from the AUR:

```sh
paru -S steno-git
```

Steno only notices a meeting while it is running, and it does not start itself
at login until you ask: turn on **Start at login** in the window's menu (or
`steno autostart on`). Hyprland, sway and niri don't run autostart entries
unless the session was started through systemd (uwsm); there, start
`steno gui --background` from the compositor's config instead.

From a checkout:

```sh
make venv          # .venv on the system python, sharing its site-packages
make link          # `steno` on PATH, running from this checkout
make desktop       # launcher entry, so rofi lists it
make hypr          # start listening with the Hyprland session
make waybar        # recording indicator in the bar (backs up your config)
```

`make hypr` adds one `hl.exec_cmd` line to `hyprland.lua`, because Hyprland does
not read `~/.config/autostart`; on a desktop that does, `make autostart` (the
same as the menu toggle) is the portable equivalent. Both are idempotent, back up what they touch, and have an
`un` twin.

Or `make install` to build the Arch package properly. Note that `uv tool
install` does **not** work: it builds an isolated venv, and the window needs the
system `python-gobject`, which such a venv can never see. `make venv` builds one
that can — on `/usr/bin/python` explicitly, since `--system-site-packages`
shares the site-packages of whichever interpreter uv picks and uv left alone
picks its own. Moving the checkout is `make venv && make link`; nothing else
refers to it by path.

Then set it up once:

```sh
steno init         # writes ~/.config/steno/.env
```

Put a `DEEPGRAM_API_KEY` in that file. Summaries and questions run through
headless Claude Code (`claude -p`) on its own login, so `claude` must be on
PATH and logged in; no Anthropic key. Optionally
write `~/.config/steno/brief.md` — a short, curated page about who you are and
what you work on, used to resolve names and jargon that speech recognition
mangles. `steno brief --new` drafts one with Claude Code.

## Use

Day to day there is nothing to run. Open the window from rofi, or click the bar.

```
steno                      the window (same as `steno gui`)
steno gui --background     listen with no window (what autostart runs)
steno autostart [on|off]   start listening at login, for this user
steno show                 raise the window, starting it if needed
steno toggle               start or stop recording
steno pause 45m            stop detecting for a while; --off to resume
steno quit                 stop the listener
steno bar                  waybar JSON on stdout, one line per change
steno summarize [NAME]     (re)summarize a session; --all to catch up
steno realign [NAME]       re-time an old session against its audio (costs a pass)
steno delete [NAME]        move a meeting to the trash
steno todos                what you owe, across every meeting
steno status               pause state and unsummarized sessions
steno watch                print apps as they open the mic
steno brief [--new]        show or scaffold the standing context
```

In the window, the sidebar says what Steno is doing (listening, recording,
paused) and lists every meeting. A meeting in progress sits at the top under
**Now** and opens by itself: the far side's transcript, your notes, and a box to
ask Claude about what's being said. A past meeting is one page — its summary,
to-dos, your notes, a question box, and the full transcript, where **clicking
any line plays that moment back**. The ⋯ menu acts on the meeting you are
looking at: re-time it against its audio, drop the audio but keep the words, or
delete it. Deleting goes to your desktop trash, all of it together, recoverable
from there.

The layout follows the window: two columns when wide, one when narrower, and the
list and the meeting as separate pages when the window is narrow — a tall window
parked beside the call works.

`Ctrl+G` ask · `Ctrl+F` search · `Ctrl+R` record · `Ctrl+Space` play ·
`Ctrl+` `+`/`-` transcript size · `Ctrl+W` close (keeps listening) · `Ctrl+Q`
quit (stops listening) · `Ctrl+?` all of these

## The bar

Steno puts an icon in the system tray (any StatusNotifierItem host: waybar's
`tray`, KDE, XFCE, GNOME with the AppIndicator extension). Click opens the
window, middle-click starts or stops recording, and the right-click menu has
the rest, pause included. The icon changes while recording.

On waybar the custom module below says more (it counts the recording up), so
`STENO_TRAY=0` in `~/.config/steno/.env` turns the tray icon off.

`make waybar` inserts a `custom/steno` module into `~/.config/waybar/`, backing
up both files first and styling it from your own palette. Left-click opens the
window, right-click starts or stops recording, middle-click pauses for an hour.
`make unwaybar` removes it again.

The indicator is not decoration: with the window usually closed, it is where you
find out that something is being recorded.

## What it records, and what leaves the machine

Both sides of every detected call are recorded to
`~/.local/share/steno/sessions/<timestamp>/`, as `you.wav` and `them.wav`.

The far side is transcribed live, because reading back what *they* just said is
the point. Your own microphone is **recorded but never streamed** — it is
transcribed in one pass when the meeting ends, which is cheaper and more
accurate than streaming it.

Audio leaves the machine only to Deepgram, and transcript text only to Anthropic
(through `claude -p`) for the summary and for questions you ask. When a meeting
is about one of the projects in `~/projects` (`STENO_PROJECTS_DIR`), the
summary may read that directory, read-only, to get names and files right; so
whatever it reads is sent too. It can read nothing outside that directory. Nothing is uploaded or shared on the
tool's own initiative.

It records other people automatically. That is a deliberate choice, and getting
their consent is yours to handle — a notification with a Stop button appears
every time recording starts, and `steno pause`, the bar's middle click, and the
header's pause menu all exist to make stopping easy rather than an afterthought.

Meetings recorded before v0.7 place their lines in the audio by inference,
accurate to a second or two. `steno realign` re-times one against its own audio
and re-transcribes the far side in a single pass, which is both exact and
usually more accurate than the live stream was; it prints the cost first and
keeps the old transcript beside the new one. Newer recordings need none of this.

An hour of meeting is roughly 200 MB of audio. Set
`STENO_KEEP_AUDIO_DAYS=30` in your `.env` to drop the WAVs after a month and
keep the text; it is off by default, and it will never touch a session whose
transcript or summary is missing.

## Tuning detection

If a meeting app isn't recognized, find out what it calls itself:

```sh
steno watch      # then start a call
steno --allow zoom,chromium,my-app
steno --any-app  # anything that opens the mic counts
```

`--start-hold` (default 20s) is how long the mic must stay open before it counts
as a meeting — it exists so push-to-talk and notification blips don't trigger
one. `--end-grace` (default 90s) keeps a single session across a mid-call
reconnect.

## Layout

- **engine** — `detect.py` (who's holding the mic), `audio.py`, `transcribe.py`
  (Deepgram, streaming and batch), `summarize.py`, `notes.py`, `retention.py`,
  `playback.py`, `session.py` (the lifecycle that drives them). No GUI
  dependency, so `make test` runs headless.
- **`gui/`** — GTK4. `app.py` is the long-running listener and owns the engine;
  `window.py` is a view it can live without. `bridge.py` is the only place the
  engine's asyncio loop and GTK's main loop meet; everything crossing back goes
  through `GLib.idle_add`. `sidebar.py`, `meeting.py` and `live.py` are the three
  surfaces; `words.py` is what they say, testable headlessly. `player.py` is
  GStreamer, for playback.
- **`events.py`** — what the engine says. The engine never touches a widget.
- **`state.py` / `control.py`** — what the bar reads, and how a click reaches
  the running app (its own D-Bus actions; no socket, no pidfile).

`make help` lists everything. `PRODUCT.md` says what this is for and what it
refuses to be.
