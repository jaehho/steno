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

From a checkout, which is how steno runs on the author's machine:

```sh
make install       # `steno` on PATH and a launcher entry, both in ~/.local; builds .venv if missing
make hypr          # start listening with the Hyprland session
make waybar        # recording indicator in the bar (backs up your config)
```

`make hypr` adds one `hl.exec_cmd` line to `hyprland.lua`, because Hyprland does
not read `~/.config/autostart`; on a desktop that does, `make autostart` (the
same as the menu toggle) is the portable equivalent. Both are idempotent, back up what they touch, and have an
`un` twin.

`make install` needs no sudo and installs no package: the launcher runs this
checkout, so `git pull` is the update (`make venv` again after a
dependency change). `make uninstall` removes the launcher,
the entry and the icon and leaves your config, sessions and notes. `make
package` builds the Arch package locally without installing it.

Note that `uv tool install` does **not** work: it builds an isolated venv, and
the window needs the system `python-gobject`, which such a venv can never see.
`make venv` builds one that can — on `/usr/bin/python` explicitly, since
`--system-site-packages` shares the site-packages of whichever interpreter uv
picks and uv left alone picks its own. Moving the checkout is `make venv &&
make install`; nothing else refers to it by path.

Then set it up once:

```sh
steno init         # writes ~/.config/steno/.env
```

Put a `DEEPGRAM_API_KEY` in that file. Summaries and questions are one
OpenAI-style chat-completions request to a model host, MiMo by default, with
its key in `~/.config/mimo.env` (`MIMO_API_KEY`). `STENO_PROVIDER=openrouter`
reads `~/.config/openrouter.env` instead; `STENO_MODEL`, `STENO_BASE_URL` and
`STENO_API_KEY` override the model, endpoint and key (the template `steno init`
writes lists them). `STENO_ASK_MODEL` does the same for the Ask box alone and
wins over the model picked in the window (below); summaries keep
`STENO_MODEL`. `STENO_NO_ASST=1` (any non-empty value, `0` included)
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
steno archive NAME...      put meetings away: kept whole, left out of every --all,
                           their open to-dos closed
steno unarchive NAME...    bring them back, to-dos reopened
steno project-notes [NAME]  copy a meeting into its project's notes/meetings
steno project-notes --all   ... every meeting filed under a project (--dry-run first)
steno project-notes --remove NAME...  take a meeting's pages out again
steno todos                what you owe, across every meeting not put away
steno todos --all          ... and the ones you ticked
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
and has a **Summarize now** button. A meeting's to-dos sit in two groups: **For
you**, which also holds any item nobody has claimed, and **Waiting on them**,
what someone else promised, kept as a reminder. Only the first is counted, in
the page's "N to do" and in the sidebar's "N to-dos open", which lists what you
still owe across every meeting; a row opens its meeting. The ⋯ menu acts on
the meeting you are looking at: summarize it again, re-time it against its
audio, join it with the meeting before, drop the audio but keep the words,
copy or save the meeting as markdown, put it away (Archive, below), or
delete it. Right-clicking a meeting in the list (or `Menu` /
`Shift+F10` on it) opens that meeting and shows the same menu. Deleting goes
to your desktop trash, all of it together, recoverable from there.

If a crash mid-call (the meeting app dies, or you re-join from another
client) left one conversation as two meetings, open the later half and choose
**Join with the meeting before this…**. The earlier folder keeps its name and
holds both transcripts and both recordings on one timeline; the later folder
goes to the trash; the summary is written again so its citations match.

The Ask box answers with the model named under it, the summary's model until
you choose another. Click the name for the choices, a row for each model: the
summary's model, tagged *default*, then the provider's presets, the models it
lists at its `/models` endpoint, and **Custom…** for an id you type. The
*default* row follows the summary's model: choose it and the Ask box changes
with it when `STENO_MODEL` or the provider's default does. Any other row, or an
id you type, pins that model: it answers every question until you choose again,
whatever the default becomes. (Pinning the model that is the default today is
typing its id under Custom…; the menu shows that as the default row, because
the two answer alike.) The list is fetched once per run, the first time the
menu is opened and off the window's thread; if the provider will not give it,
the presets are all there is and nothing says why. Speech models (`tts`, `asr`)
are left out of it. The choice is kept in `gui.json` under
`$XDG_STATE_HOME/steno` (`~/.local/state/steno`) and applies from the next
question; summaries do not change. `STENO_ASK_MODEL` overrides it, and the menu
then says that instead of offering a choice that would be ignored. Each answer
says under it which model gave it (*answered by …*), so a surprising one can be
explained; a question already on its way keeps the model it was sent with. A
question that got no answer (an id the provider does not know, say) goes back
into the box and the message names the model, so you can pick another and press
Enter. An answer that stops partway keeps the text that came and says *Cut off*
under it, not *answered by …*.

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
`archived` in its `meta.json`, so the audio and the words stay as they
were, and closes its open to-dos (below). It asks nothing: the toast that
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
still reaches it. The meeting count and the cost under the list still
include it, and `STENO_KEEP_AUDIO_DAYS` treats it like any other.
`steno status` counts the archived ones, and `steno unarchive NAME...`
brings them back.

**Its open to-dos are closed.** A meeting put away is one you are done
with, so what it still held open is not counted in the page's "N to do" or
the list's "N to-dos open", is not in that list, and is not in
`steno todos`, which says how many meetings it left out. Archiving takes
the meeting's open tasks out of asst (`asst rm`; for a list tied to a
GitHub repo asst closes the issue as not planned) and marks those rows
`dropped` in `todos.json`, keeping their words and their asst source (a
row keeps its task id only until asst has let the task go). The
toast says what happened: "Archived “Sync”. Removed 3 open tasks from
asst." `steno archive` prints the same. A to-do you had ticked is left
alone, and so is its finished task. The asst calls run after the meeting
has left the list, so a slow asst never holds the page. The page of a
meeting put away still shows its to-dos, greyed, with a line saying they
were closed. **Unarchive**, `steno unarchive` and the toast's Undo open
them again and file yours in asst once more. That is by their source, so a
task asst still holds is found and not doubled; one it had removed comes
back as a new task (a new issue, on a list tied to a repo). If asst cannot
be reached or refuses a task, the archive still happens, the toast and
`steno archive` (which exits 1) say how many tasks are still open there,
and those rows stay open; `steno archive NAME` again retries them. So
does a removal cut short (the process killed, a Ctrl-C): a closed row that
still names its task is what is left to take out, and **Delete** takes
that task out too. Quitting the window waits for a removal in progress. A
to-do that never reached asst is closed here alone, so a meeting needs no
asst to be put away. Archiving does not rewrite the pages the meeting has
in a project (below).

**Retrying asst.** Unarchive reopens what archiving closed and files it;
it never takes a task out of asst, so it does not retry a removal asst
refused. Archiving again does, and in the window that is Unarchive and then
Archive (the toast and the meeting's page say so; the page also names the
`steno archive` command, which needs only the one step). The other way
round, when asst would not take a task Unarchive reopened (it is open here
only, and the toast and `steno unarchive` say so), unarchiving again does
nothing, since there is nothing left to reopen: archive it and unarchive it
once more.

A meeting put away before to-dos were closed with it keeps its open
to-dos, and their tasks in asst: nothing closes them on its own, since
that takes tasks out of asst. Its page says "Some are still open".
`steno archive NAME` closes them (it says "already archived" first), as
does Unarchive and then Archive in the window.

A meeting about one of your projects is also kept in that project, as
pages of its own; see Project notes, below.

The layout follows the window: two columns when wide, one when narrower, and the
list and the meeting as separate pages when the window is narrow — a tall window
parked beside the call works.

`Ctrl+G` ask · `Ctrl+F` search · `Ctrl+R` record · `Ctrl+Space` play ·
`Ctrl+` `+`/`-`/`0` transcript size · `Ctrl+W` close (keeps listening) · `Ctrl+Q`
quit (asks first, then stops listening) · `Ctrl+?` all of these

## Project notes

A meeting that is about one of your projects is kept in it too, so that
whoever opens the project, you or an agent working in it, finds the meetings
it had. There is nothing to turn on: once a meeting has a project, Steno
writes this inside it.

```
<project>/notes/meetings/
    README.md                                    the meetings, newest first
    2026-10-06-1612-rcnn-toolbox.md              summary, to-dos, your notes
    2026-10-06-1612-rcnn-toolbox.transcript.md   what was said, [mm:ss] you: ...
    .gitignore                                   `*`, made with the folder
```

The project is the one the summary picked from `~/projects` (or
`STENO_PROJECTS_DIR`); **Set project...** in the ⋯ menu changes it. Each page
opens with a front matter block (title, date, time, length, project, the
session it came from) and a line saying Steno generated it. The summary page
is what `steno export` makes without the words: the summary with its
citations written as `[03:21, 07:40]`, your to-dos as a checklist in the
state they are in now, your notes, and a link to the transcript page. A
to-do closed because the meeting was put away is not on the checklist as an
empty box: it follows it under *Closed when the meeting was put away*, as it
does in `steno export`. The
transcript page is `[mm:ss] who: text` for each line, with the far side's
names where Steno has them, as the export has it. `README.md` is the index, a
line per meeting (date, title, length, open to-dos) under a note on what the
folder is, for an agent that lands in it.

**When.** A page is written when the summary lands, when you set the project
(the old project's pages are taken out), and a minute after the last change
to a meeting that is already there: a tick, a saved note, a tick made on your
phone, a re-timing. The wait is so that typing a note does not rewrite a file
every few seconds; opening another meeting, or quitting, writes what is
waiting at once. A tick or an edit never puts a meeting into a project that
was not there already. `steno summarize` and `steno realign` do the same
from a terminal. A meeting you delete is taken out of its project first;
one you archive stays there. If the pages cannot be taken out (the folder is
not writable, say), the meeting is not deleted, because the transcript would
stay behind with nothing to say where it is: the window shows why, `steno
delete` prints it and exits 1, and deleting again after the fix goes through.
**Show in project notes** in the ⋯ menu opens the folder, for a meeting that
has pages in one.

**What it keeps to.**

- It stays in the project. The project has to be a plain directory directly
  under the projects directory, and Steno never makes one; it makes
  `notes/meetings/` (and `notes/`, if there is none). A `notes` or `meetings`
  that is a link leading out of the project is refused, not followed.
- It touches only its own files. A file in that folder without the
  generated-by line is yours: it is never overwritten or removed, only named
  (in the toast, in the command's output, in the log). Delete that line from a
  page to take it over; Steno leaves it alone from then on. A page that keeps
  the line is Steno's and is rewritten when the meeting changes, so edit
  nothing there, and an edit that comes back from another device is lost the
  same way.
- It writes only what changed. A page is compared byte for byte and written
  when it differs, whole: to a hidden temporary file in the same folder, then
  renamed over the old one. Writing the same meeting twice leaves every
  modification time alone, and nobody reads half a page.
- It runs no git, and sends nothing anywhere: no network, no model. The pages
  are made from files Steno already has.

**Git.** Transcripts are other people's speech, and project repositories get
pushed. So the folder is made with a `.gitignore` of `*`, which ignores the
meetings and itself: git sees nothing in the folder, and `git status` stays
clean. Steno stages and commits nothing. To keep the meetings in the
repository, delete the `.gitignore`; Steno writes it once, with the folder,
and does not write it back. (An earlier Steno also wrote `!.gitignore`
there, which left the folder showing in `git status` as untracked. That
file is rewritten, once, the next time Steno writes in the folder; a
`.gitignore` you have changed is left as it is.)

If `notes/meetings` already holds files of yours when Steno first writes in
it, a `*` would hide them from git too, and `git add` would refuse the next one
you write there. So that folder gets a `.gitignore` of `*.transcript.md`
alone: the transcripts stay out of git, your files stay visible, and the
summary pages and the index show up as new files for you to commit or ignore.

**A synced folder.** The `.gitignore` is for git alone; a sync tool does not
read it. If the project's `notes/` is synced (an Obsidian vault with a sync
plugin, a Nextcloud or Syncthing folder), the meetings go with it,
transcripts included, to every device and server it syncs to, and a vault
lists and searches them with the rest. Steno cannot tell, and does not look.
To keep them out, use the sync tool's own ignore setting for
`notes/meetings`, or turn project notes off. Steno does what it can to be a
quiet neighbour in such a folder: a sync sees one new page per meeting, and
an update only when a tick or a note really changes a page. A tool that
uploads hidden files may see the temporary file come and go.

**The switch.** `STENO_PROJECT_NOTES=0` in `~/.config/steno/.env` turns all
of it off: the window, `steno summarize` and `steno realign` write nothing,
and `steno project-notes` refuses unless given `--force`. Unset or empty is
on, and anything but a plain yes (`1`, `true`, `yes`, `on`) reads as off, so
a typo fails towards writing nothing. It does not take back what is already
written; `steno project-notes --remove` does, and works with the switch off.

**Meetings from before.** They are not copied until you ask. `steno
project-notes --all --dry-run` lists every file it would create, update or
take away, and writes none; read it, then run `steno project-notes --all`:

```
$ steno project-notes --all --dry-run
20261012-100000  Weekly sync  [rcnn]
  create  rcnn/notes/meetings/.gitignore
  create  rcnn/notes/meetings/2026-10-12-1000-weekly-sync.md
  create  rcnn/notes/meetings/2026-10-12-1000-weekly-sync.transcript.md
  create  rcnn/notes/meetings/README.md
20261006-161200  RCNN Toolbox  [rcnn]
  create  rcnn/notes/meetings/2026-10-06-1612-rcnn-toolbox.md
  create  rcnn/notes/meetings/2026-10-06-1612-rcnn-toolbox.transcript.md
dry run, nothing written: 2 meetings looked at, 0 already up to date; 6 to create
```

`steno project-notes` alone does the newest meeting, `NAME` one, `--project P`
only those filed under P. `--all` leaves the archived meetings out, as the other
commands that go through everything do, and also takes pages away from a
meeting that has since left its project. A file Steno would not touch is
listed as `skip`, with why. `steno project-notes --remove NAME...` takes
meetings' pages back out (also with `--dry-run`), and the folder with them if
nothing else is in it. None of this needs the app running.

**For an agent.** Steno never edits a project's own files, so no project
needs a line about the folder. The pointer is global: one line in your own
agent instructions (`~/.claude/CLAUDE.md`, for Claude Code) covers every
project:

```
- Meetings steno recorded about a project are in its `notes/meetings/` (generated, read-only; start at `README.md` there).
```

The line is true of a project on the machine that recorded its meetings; a
clone elsewhere has none, since they were never committed. An agent that
reads the pages sends what it reads to its own provider; that is what they
are for. The model that writes a summary is not shown them (see What it
records, below), so a summary is not written from another meeting's pages,
nor sends them on.

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
sent too. It can read nothing outside that directory, and nothing in a
project's `notes/meetings`, where Steno keeps its copies of earlier
meetings (the folder it leads to, if `notes` or `meetings` is a link to
another folder in the project). Nothing is uploaded or shared on the tool's
own initiative.

Project notes (above) add local files and nothing else: no upload, no
network, no model call, no git. What they change is where the transcript
lives. It is now also a file in the project of each meeting, as heard, without
the scrubbing above (that is for what is sent to the model host, not for what
is kept or exported). Whatever reads that folder reads it: an editor, a
backup, a sync client, an agent you run in the project, which sends what it
reads to its own provider. Steno keeps the transcripts out of git with a
`.gitignore`, the summaries too in a folder it made; it cannot keep either out
of a sync (an Obsidian vault, say), which carries the meetings to every device
it syncs to.
`STENO_PROJECT_NOTES=0` turns the copies off.

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
  is what they say, testable headlessly; `rewrite.py` waits for a pause
  before it rewrites a meeting's project pages. `player.py` is GStreamer, for playback.
- **`events.py`** — what the engine says. The engine never touches a widget.
- **`state.py` / `control.py`** — what the bar reads, and how a click reaches
  the running app (its own D-Bus actions; no socket, no pidfile).

`make help` lists everything. `PRODUCT.md` says what this is for and what it
refuses to be.
