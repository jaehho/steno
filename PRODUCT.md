# Product

## Register

product

## Users

One person: the author — an EE finishing a joint BS/MS, on Arch + Hyprland,
comfortable in a terminal but not wanting to be in one during a call. Not a
team, not a customer.

Context of use: a video call on a laptop — an interview, a research meeting, a
call with a collaborator. The user is talking, thinking, and being asked
questions in real time; attention is the scarce resource and it is already
spent. They cannot take good notes and be present at the same time, and they
drift — losing the thread of a question that was asked ten seconds ago.

Meetings are frequent but not constant, and they start without ceremony: a
browser tab opens and the call is already running. Whatever the tool needs from
the user at that moment, it will not get. So it starts itself.

Afterward — that evening, or the next week before a follow-up — the same person
comes back cold, wanting to know what was decided and what they promised to do.

## Product Purpose

Steno listens to a call, keeps the far side's words on screen so a
drifting mind can catch up without interrupting anyone, answers a question about
what's being said when asked, and turns the whole thing into notes and todos
that are worth reading a week later.

It exists because the two halves of the job are in direct conflict. Being
present in a conversation and recording it accurately are the same attention,
spent twice. A person who takes good notes is not listening well, and a person
listening well has no notes. The tool takes the mechanical half.

Success looks like: finish a call having been fully in it, never having touched
the app — and find the transcript, a summary, and a short list of what you
agreed to do already waiting. Then, a week later, open it and remember the
meeting in under a minute.

The failure it is built against is the recording that is never listened to. An
hour of audio nobody replays is worse than no recording — it is the *illusion*
of having captured something. Every meeting must reduce to something readable,
or the capture was pointless.

## Brand Personality

Quiet, attentive, unhurried. A good notetaker in the corner of the room: it
misses nothing and interrupts nobody. It never asks how it's doing, never
celebrates, never announces itself while someone is talking.

The voice is spare and factual — what was said, what was decided, what you owe.
It does not editorialize about the meeting, flatter the user's performance, or
soften what it heard. When speech was too garbled to read, it says so instead of
guessing prettily.

Crucially: calm is **not** passive. The tool is doing constant, real work —
listening, transcribing, summarizing — and it should feel capable and awake, not
inert. Calm means it holds all of that without making it the user's problem.

Emotional goal: the relief of being fully present in a conversation because
something trustworthy is keeping the record.

## Anti-references

- **The AI meeting-assistant SaaS (Otter, Fireflies, Fathom):** no "AI insights"
  panel, no engagement scores, no sentiment charts, no chirpy bot that posts a
  summary as if it attended. It does not have opinions about the meeting. It
  also never joins a call as a visible participant — the recording is the
  user's, on the user's machine, not a guest in the room.
- **The realtime "answer coach" (interview-cheat overlays):** the tool does not
  feed answers unprompted. That was tried — auto-triggered responses — and cut,
  because a suggestion arriving mid-sentence competes with the conversation
  instead of supporting it. Guidance is pulled, never pushed.
- **The IDE / cockpit:** no dockable panels, no dense multi-pane layout, no
  status bar of counters. Whatever is on screen during a call must be readable
  in a one-second glance by someone mid-sentence.
- (Adjacent, also avoid:) the note-taking app as filing system — folders, tags,
  backlinks, an organizational scheme to maintain. Meetings arrive on their own
  and are found by when they happened and what they were about.

## Design Principles

1. **The user's attention is spent.** Anything the tool needs during a call it
   must not need. It starts itself, records without confirmation, and asks
   nothing. Any interaction available live must be optional, one gesture, and
   safely ignorable — because it will be ignored.
2. **Glanceable beats complete.** The live surface is read in one second by
   someone who is talking. Large type, one column, the far side's most recent
   words. Completeness is the archive's job, not the live view's.
3. **Every meeting reduces.** Capture is never the deliverable. A session that
   ends without a summary and a todo list has failed, and the tool should treat
   that as an error worth surfacing — not a file quietly left on disk.
4. **Pulled, never pushed.** Guidance appears because it was asked for. The tool
   never speaks into a conversation on its own initiative.
5. **Honest about what it heard.** Transcripts are machine-made and wrong in
   specific ways. Show interim text as provisional, mark low confidence, and let
   a summary say "this passage was unintelligible" rather than inventing a
   plausible sentence. Trust survives visible gaps; it does not survive
   confident fiction.
6. **Recording is visible and stoppable.** It captures a room automatically,
   including other people's voices. There is always an obvious indication that
   it is running and an obvious way to stop it — never buried, never more than
   one gesture away.

## Accessibility & Inclusion

Personal single-user tool, so no external WCAG mandate — but the baseline holds:
body text ≥ 4.5:1 contrast, large text ≥ 3:1; never encode state by color alone
(recording, paused, and idle each carry text or an icon, not just a hue); honor
`prefers-reduced-motion`. The live transcript is the accessibility surface of
this tool in the literal sense — it is captioning — so it gets the most generous
type size and line spacing in the app, and its size is user-adjustable.

The window is used in a dim room during a call, often beside a video grid, so it
favors a dark comfortable surface over a bright field, and must never flash,
animate, or pop focus while a meeting is running.

## Ethical note

The tool records other people, automatically, and sends the far side's audio to
a third-party transcription service. That is a deliberate, uncomfortable choice
made by and for one user on their own machine, and it constrains the design:
recording state is always visible, pausing is always one gesture, audio and
transcripts stay local except for the transcription and summary calls, and
nothing is ever shared or uploaded on the tool's own initiative. Consent is the
user's responsibility, and the tool should make it easy to honor rather than
easy to forget.
