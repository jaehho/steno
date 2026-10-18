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

Put a `DEEPGRAM_API_KEY` in that file. Summaries and questions are one
OpenAI-style chat-completions request to a model host, MiMo by default, with
its key in `~/.config/mimo.env` (`MIMO_API_KEY`). `STENO_PROVIDER=openrouter`
reads `~/.config/openrouter.env` instead; `STENO_MODEL`, `STENO_BASE_URL` and
`STENO_API_KEY` override the model, endpoint and key (the template `steno init`
writes lists them). `STENO_NO_ASST=1` (any non-empty value, `0` included)
stops Steno running `asst`, the task app your action items are filed in, so
a scratch run cannot touch your real lists. Optionally write
`~/.config/steno/brief.md` — a short, curated page about who you are and what
you work on, used to resolve names and jargon that speech recognition mangles.
`steno brief --new` drafts one with Claude Code.

## Use

Day to day there is nothing to run. Open the window from rofi, or click the bar.

```
steno                      the window (same as `steno gui`)
steno gui --background     listen with no window (what autostart runs)
steno autostart [on|off]   start listening at login, for this user
steno show                 raise the window, starting it if needed
steno toggle               start or stop recording (starts the listener if it is down)
steno pause 45m            stop detecting for a while; --off to resume
steno quit                 stop the listener
steno bar                  waybar JSON on stdout, one line per change
steno summarize [NAME]     (re)summarize a session; --all to catch up
steno summarize --uncited  redo the summaries that have no citations (lists, prices, --yes)
steno export [NAME] [-o F] print or save a meeting as markdown
steno realign [NAME]       re-time an old session against its audio (costs a pass)
steno delete [NAME]        move a meeting to the trash
steno archive NAME...      put meetings away: kept whole, left out of every --all
steno unarchive NAME...    bring them back
steno publish [NAME]       copy a meeting into its project's notes/meetings
steno publish --all        ... every meeting filed under a project (--dry-run first)
steno unpublish NAME...    take a meeting's pages out of its project again
steno todos                what you owe, across every meeting
steno status               pause state, archived and unsummarized sessions
steno watch                print apps as they open the mic
steno brief [--new]        show or scaffold the standing context
```

In the window, the sidebar says what Steno is doing (listening, recording,
paused) and lists every meeting. A meeting in progress sits at the top under
**Now** and opens by itself: the far side's transcript, your notes, and a box to
ask Claude about what's being said. A past meeting is one page — its summary,
to-dos, your notes, a question box, and the full transcript, where **clicking
any line plays that moment back**. A meeting that never got its summary says so
and has a **Summarize now** button. The sidebar's "N to-dos open" lists what you
still owe across every meeting; a row opens its meeting. The ⋯ menu acts on
the meeting you are looking at: summarize it again, re-time it against its
audio, drop the audio but keep the words, copy or save the meeting as
markdown, put it away (Archive, below), or delete it. Right-clicking a meeting in the list (or `Menu` /
`Shift+F10` on it) opens that meeting and shows the same menu. Deleting goes
to your desktop trash, all of it together, recoverable from there.

A summary says where each point was said. The model ends every bullet with
the transcript lines that support it; Steno checks those against the
transcript and keeps them in `summary.md` as a hidden comment at the end of
the line, `<!--cite 201-221,460-->`. The numbers are seconds into the meeting,
the ones the transcript's time column shows, so `steno realign` does not
strand them. GitHub and Obsidian show nothing; `steno export`, Copy meeting and
Save meeting write it as `[03:21, 07:40]`, which matches the stamps in the
transcript section. A bullet nothing supports gets none. A meeting summarized
before this has none until it is summarized again: **Summarize again** in the
⋯ menu (or in the menu of its row in the list), which asks first and says
what it costs, about a cent; `steno summarize NAME --force` for one; or
`steno summarize --uncited`, which lists every summary without citations and
what redoing them costs, and does it when you add `--yes`. The old summary
stays on the page, with a note by its heading, until the new one replaces it,
and stays for good if the model fails. Summarizing again keeps what is yours:
your notes, the meeting's project, and your ticks. The model is shown the
meeting's action items as they stand and told to keep their wording where the
transcript still supports them, so the same promise stays the same row. A
to-do the new summary words differently anyway (say "Send usage figures to
Alice by Friday" for "Send the usage figures by Friday") stays the same row,
with its tick and its task in asst, when the two read alike and differ only
in small words ("the", "to") or in a name, day or number one of them adds.
Any other word swapped (the "final" draft for the "first", the team for the
client) makes it a new one, so a new promise never arrives already ticked.

In the window a point that has citations ends in a small underlined number,
the count of places it was said. Click it (or press `Enter` on it) and the
lines it came from are marked in the transcript, the page scrolls to the first
of them unless it is already in view, and a bar above the player lists the
places: when, who, the first words, and `+2 more lines` for a stretch. The
arrows in the bar step from one place to the next, and a row plays the
recording from there. With the audio gone, or while a meeting is being
recorded, it scrolls and says why it does not play. The words of a to-do that
cites work the same way (its box still ticks). `Esc`, the bar's close button,
or clicking the point again puts the marks out; so does opening another
meeting. Points with no number are plain text you can select, and a summary
with no citations says so beside its heading, with a link to summarize it
again.

A meeting that no longer matters can be put away instead of deleted.
**Archive** in the ⋯ menu (or in the menu of its row in the list) sets
`archived` in its `meta.json` and touches nothing else, so the audio, the
words and the to-dos stay as they were. It asks nothing: the toast that
says so has an **Undo**. The meeting leaves the list and the search, and
the page moves to the next meeting, or to an empty page that says how many
are put away. While any meeting is, the last row of the list reads
**Archived (3)**; it shows them again, dimmed and in a section of their
own, and hides them on the next press. That is for this window only, a
window opens without them. With a search typed the number counts the
archived meetings that match it, so a search leaves nothing out, it only
asks. A meeting opened from there (or from the to-do list, or from a
notification) is a page like any other, with **Unarchive** in its menu
where Archive was. The meeting being recorded cannot be archived: its item
is greyed out.

`steno archive NAME...` does the same from a terminal, and an open window
follows when it is raised. Every command that goes through all meetings
then leaves it out (`steno summarize --all` and `--uncited`,
`steno realign --all`, `steno clean-echo --all`) and says how many it left;
a command given the meeting's name (`export`, `summarize NAME`, `delete`)
still reaches it. Its open to-dos stay open: they are counted in the
list's "N to-dos open" (hover it for how many of those are in archived
meetings), listed in it under the meeting's name, and listed by
`steno todos`, because a promise made in a meeting does not lapse when the
meeting is put away. The meeting count and the cost under the list still
include it, and `STENO_KEEP_AUDIO_DAYS` treats it like any other.
`steno status` counts the archived ones, and `steno unarchive NAME...`
brings them back.

A meeting that is about one of your projects is kept in it too. With the
meeting's project set (the summary picks one from `~/projects`, or
`STENO_PROJECTS_DIR`; **Set project...** in the ⋯ menu changes it), Steno
writes `notes/meetings/` inside that project:

```
notes/meetings/README.md                                     index, newest first
notes/meetings/2026-10-06-1612-rcnn-toolbox.md               summary, to-dos, your notes
notes/meetings/2026-10-06-1612-rcnn-toolbox.transcript.md    what was said, [mm:ss] You: ...
notes/meetings/.gitignore                                    `*`, made with the folder
```

so that whoever opens the project, you or an agent, finds the meetings it
had. Each page opens with a front matter block (title, date, project, the
session it came from) and a line saying Steno generated it. The pages are
written when the summary lands, when you set the project (the old project's
copy goes), and a minute after your last tick, edit to the notes or
re-timing, so that typing does not rewrite files every few seconds; opening
another meeting or quitting writes what is waiting at once. `steno summarize`
and `steno realign` do the same from a terminal. A meeting you delete is taken
out of its project first; one you archive stays there. **Show in project
notes** in the ⋯ menu opens the folder, for a meeting that has pages in one.

It keeps to its own folder. A file there that Steno did not write (it
lacks the generated-by line) is never overwritten or removed, only named, and
a project name that is not a plain directory under the projects directory, or
a `notes/` that is a link leading out of the project, is refused. A page is
written only when its bytes would change, so a synced folder is not churned.
The `.gitignore` is there because transcripts are other people's speech and
project repositories get pushed; delete it and Steno will not write it again,
and the meetings become committable. `STENO_PROJECT_NOTES=0` in your `.env`
turns all of it off. Nothing here uses the network or a model.

Meetings from before this are not copied until you ask. `steno publish --all
--dry-run` lists every file it would create or take away, without writing
any; read it, then `steno publish --all`. `--project P` limits that to one
project, `NAME` to one meeting, and `--force` publishes by hand with the
switch off. `--all` leaves the archived meetings out, as the other commands
that go through everything do. `steno unpublish NAME` takes a meeting's pages
back out (also with `--dry-run`), and none of this needs the app running.

The layout follows the window: two columns when wide, one when narrower, and the
list and the meeting as separate pages when the window is narrow — a tall window
parked beside the call works.

`Ctrl+G` ask · `Ctrl+F` search · `Ctrl+R` record · `Ctrl+Space` play ·
`Ctrl+` `+`/`-`/`0` transcript size · `Ctrl+W` close (keeps listening) · `Ctrl+Q`
quit (asks first, then stops listening) · `Ctrl+?` all of these

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

Audio leaves the machine only to Deepgram, and transcript text only to the
model host you configured (MiMo by default, or OpenRouter; see above) for the
summary and for questions you ask. Emails, phone numbers and the like are
scrubbed from the transcript on the way out. When a meeting is about one of the
projects in `~/projects` (`STENO_PROJECTS_DIR`), the summary may read that
directory, read-only, to get names and files right; so whatever it reads is
sent too. It can read nothing outside that directory. Nothing is uploaded or
shared on the tool's own initiative. The copies in a project's `notes/meetings`
are local files that carry the transcript with them: a repository ignores them
(the `.gitignore` there), a folder you sync, an Obsidian vault say, does not.

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
  (Deepgram, streaming and batch), `summarize.py`, `citations.py` (the
  markers above), `notes.py`, `projectnotes.py` (a meeting's pages in its
  project), `retention.py`, `playback.py`, `session.py` (the lifecycle that
  drives them). No GUI dependency, so `make test` runs headless.
- **`gui/`** — GTK4. `app.py` is the long-running listener and owns the engine;
  `window.py` is a view it can live without. `bridge.py` is the only place the
  engine's asyncio loop and GTK's main loop meet; everything crossing back goes
  through `GLib.idle_add`. `sidebar.py`, `meeting.py` and `live.py` are the three
  surfaces; `summary.py` is the summary's rows and the bar of places; `words.py`
  is what they say, testable headlessly; `republish.py` waits for a pause
  before it rewrites a meeting's project pages. `player.py` is GStreamer, for playback.
- **`events.py`** — what the engine says. The engine never touches a widget.
- **`state.py` / `control.py`** — what the bar reads, and how a click reaches
  the running app (its own D-Bus actions; no socket, no pidfile).

`make help` lists everything. `PRODUCT.md` says what this is for and what it
refuses to be.
