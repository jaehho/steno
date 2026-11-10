"""A meeting in a room: one microphone, several people.

Record pressed with no call app open, and nothing said through the
speakers, is a room. Its recording stays on the laptop until the user
says everyone agreed. Then the mic pass asks for voices (one voice alone
is a voice memo), the summary sees `voice 1`, `voice 2` rather than
everyone as `you`, names the voices it can from first-person
introductions and from who the calendar and earlier meetings expected,
and every item it makes waits for the user to say whose it is before any
reaches asst.

Deepgram is an httpx.MockTransport, the model a mock, asst
tests/fakeasst.py: nothing leaves the machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import httpx

from steno import claude, realign, room, session, speakers, transcribe
from steno.events import NotesStatus
from steno.notes import (
    decide_owner,
    held_count,
    load_todos,
    open_count,
    restore_dropped_todos,
    sync_todos,
)
from steno.notesjob import NotesState
from steno.summarize import (
    format_for_model,
    load_meta,
    summarize_session,
)
from tests.fakeasst import FakeAsst

# Any zone and day will do; these are nobody's.
ZONE = ZoneInfo("Asia/Tokyo")
# A Wednesday, two minutes after a weekly slot opens.
STARTED = datetime(2030, 1, 16, 14, 2, tzinfo=ZONE).timestamp()

THREE_VOICES = {"results": {"utterances": [
    {"start": 0.5, "end": 2.0, "speaker": 0,
     "transcript": "Sam, can you send the slides?"},
    {"start": 2.4, "end": 4.0, "speaker": 1,
     "transcript": "Yes, I'll send them by Friday."},
    {"start": 4.5, "end": 6.0, "speaker": 2,
     "transcript": "And I will redo the chart."},
]}}

ONE_VOICE = {"results": {"utterances": [
    {"start": 0.5, "end": 2.0, "speaker": 0,
     "transcript": "Note to self: I will book the room."},
    {"start": 3.0, "end": 4.0, "speaker": 0,
     "transcript": "And send the slides."},
]}}

WEEKLY = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly-meeting
DTSTART;TZID=Asia/Tokyo:20300102T140000
DTEND;TZID=Asia/Tokyo:20300102T153000
RRULE:FREQ=WEEKLY;BYDAY=WE
SUMMARY:Group meeting
ORGANIZER;CN=Alice Moreau:mailto:alice@example.org
ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.org
ATTENDEE;CN=Carol;PARTSTAT=DECLINED:mailto:carol@example.org
ATTENDEE;CN=Room 2.14;CUTYPE=ROOM:mailto:room@example.org
ATTENDEE;CN=sam@example.org:mailto:sam@example.org
END:VEVENT
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20300116
SUMMARY:Holiday
ATTENDEE;CN=Eve:mailto:eve@example.org
END:VEVENT
END:VCALENDAR
"""

SUMMARY = """\
# Group meeting

## Action items
**You**
- Send the slides by Friday
**Them**
- voice 3: redo the chart
"""

# What the room prompt asks for when no line shows which voice is the
# user's: every promise under Them, each with its voice.
SUMMARY_NO_USER = """\
# Group meeting

## Action items
**Them**
- voice 2: send the slides by Friday
- voice 3: redo the chart
"""


def write_wav(path: Path, seconds: float = 1.0) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * int(seconds * 16000))


def make_session(root: Path, name: str, *, manual: bool, far: list[str],
                 meta: dict | None = None) -> Path:
    """A finished recording: meta.json, both tracks, and the far side's
    streamed lines (none in a room)."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({
        "started_at": STARTED, "app": "manual" if manual else "ZOOM",
        "manual": manual, **(meta or {}),
    }))
    rows = [{"ts": STARTED + 3 + i, "speaker": "them", "text": text,
             "offset": 3.0 + i}
            for i, text in enumerate(far)]
    (folder / "transcript.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    write_wav(folder / "you.wav")
    write_wav(folder / "them.wav")
    return folder


class Deepgram:
    """A fake prerecorded endpoint: what was asked, and a canned reply."""

    def __init__(self, test: unittest.TestCase, reply: dict) -> None:
        self.asked: list[httpx.Request] = []
        real = httpx.AsyncClient

        def answer(request: httpx.Request) -> httpx.Response:
            self.asked.append(request)
            return httpx.Response(200, json=reply)

        transport = httpx.MockTransport(answer)
        patch = mock.patch.object(
            transcribe.httpx, "AsyncClient",
            side_effect=lambda **kw: real(transport=transport, **kw),
        )
        patch.start()
        test.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test"})
        env.start()
        test.addCleanup(env.stop)

    def voices_asked(self, i: int = 0) -> str | None:
        return self.asked[i].url.params.get("diarize_model")


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sessions"
        self.root.mkdir()
        self.scratch = Path(tmp.name)


class TestMicPass(Scratch):
    def mic_pass(self, folder: Path) -> list[dict]:
        engine = session.Engine(
            emit=lambda _e: None, root=self.root,
            brief_path=self.root / "brief.md", model="m",
        )
        rows, ok = asyncio.run(engine._mic_pass(folder, STARTED, []))
        self.assertTrue(ok)
        return rows

    def test_a_room_asks_for_voices_and_keeps_them_apart(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "room", manual=True, far=[])
        rows = self.mic_pass(folder)
        self.assertEqual(dg.voices_asked(), "latest")
        self.assertEqual([r["who"] for r in rows], ["mic_0", "mic_1", "mic_2"])
        self.assertIs(load_meta(folder)["room"], True)

    def test_a_call_leaves_the_microphone_one_person(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "call", manual=False,
                              far=["Can you hear me?"])
        rows = self.mic_pass(folder)
        self.assertIsNone(dg.voices_asked())
        self.assertNotIn("who", rows[0])
        self.assertNotIn("room", load_meta(folder))

    def test_record_pressed_during_a_call_is_still_a_call(self):
        """A call app steno did not detect: the far side spoke."""
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "undetected", manual=True,
                              far=["Hello from the other side"])
        self.mic_pass(folder)
        self.assertIsNone(dg.voices_asked())

    def test_meta_json_can_say_so_by_hand(self):
        """A hybrid meeting, or a room with music playing."""
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "hybrid", manual=False,
                              far=["Remote hello"], meta={"room": True})
        self.mic_pass(folder)
        self.assertEqual(dg.voices_asked(), "latest")

    def test_one_voice_is_a_voice_memo_not_a_room(self):
        """Its lines are the user's, and its to-dos are filed as a call's."""
        Deepgram(self, ONE_VOICE)
        folder = make_session(self.root, "memo", manual=True, far=[])
        rows = self.mic_pass(folder)
        self.assertEqual([r.get("who") for r in rows], [None, None])
        self.assertIs(load_meta(folder)["room"], False)
        with FakeAsst() as asst:
            sync_todos(folder, SUMMARY)
            self.assertEqual(len(asst.tasks()), 1)

    def test_a_room_said_by_hand_stays_one_with_one_voice(self):
        Deepgram(self, ONE_VOICE)
        folder = make_session(self.root, "small", manual=True, far=[],
                              meta={"room": True})
        rows = self.mic_pass(folder)
        self.assertEqual({r.get("who") for r in rows}, {"mic_0"})
        self.assertIs(load_meta(folder)["room"], True)


class TestKeptUntilAgreed(Scratch):
    """Nothing of a room leaves the laptop until the user says everyone
    in it agreed, which asking for its notes is."""

    def setUp(self):
        super().setUp()
        self.events: list = []
        self.engine = session.Engine(
            emit=self.events.append, root=self.root,
            brief_path=self.root / "brief.md", model="m",
        )
        env = mock.patch.dict(os.environ, {
            "STENO_PROJECTS_DIR": str(self.scratch / "projects"),
        })
        env.start()
        self.addCleanup(env.stop)
        model = mock.patch.object(claude, "run_structured", mock.AsyncMock(
            return_value=({"summary": SUMMARY, "project": None,
                           "voices": []}, "fake-model"),
        ))
        self.model = model.start()
        self.addCleanup(model.stop)

    def end(self, folder: Path, *, manual: bool) -> None:
        rec = session.Recording(
            session_dir=folder, started_at=STARTED,
            app="manual" if manual else "ZOOM", manual=manual,
        )
        asyncio.run(self.engine._run_notes_job(folder, rec=rec))

    def states(self) -> list[NotesState]:
        return [e.state for e in self.events if isinstance(e, NotesStatus)
                and e.state is not NotesState.RUNNING]

    def test_a_room_is_kept_until_the_user_asks(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "room", manual=True, far=[])
        self.end(folder, manual=True)
        self.assertEqual(dg.asked, [])
        self.model.assert_not_called()
        self.assertEqual(self.states(), [NotesState.HELD])
        self.assertTrue(load_meta(folder)["mic_pending"])
        self.assertTrue(room.awaits_agreement(load_meta(folder), []))

        asyncio.run(self.engine.summarize_now(folder))
        self.assertEqual(dg.voices_asked(), "latest")
        self.model.assert_called()
        self.assertTrue((folder / "summary.md").is_file())
        self.assertNotIn("mic_pending", load_meta(folder))
        self.assertEqual(self.states(), [NotesState.HELD, NotesState.WRITTEN])

    def test_a_call_goes_on_as_before(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "call", manual=False,
                              far=["Can you hear me?"],
                              meta={"mic_pending": True})
        self.end(folder, manual=False)
        self.assertEqual(len(dg.asked), 1)
        self.assertEqual(self.states(), [NotesState.WRITTEN])


class TestLabels(unittest.TestCase):
    ROWS = (
        {"ts": 1.0, "speaker": "you", "who": "mic_0", "text": "a"},
        {"ts": 2.0, "speaker": "you", "who": "mic_1", "text": "b"},
        {"ts": 3.0, "speaker": "them", "who": "speaker_0", "text": "c"},
        {"ts": 4.0, "speaker": "you", "text": "d"},
    )

    def test_room_voices_are_never_you_to_the_model(self):
        lines = format_for_model(list(self.ROWS), redact=False).splitlines()
        self.assertEqual(
            [line.split("] ", 1)[1] for line in lines],
            ["voice 1: a", "voice 2: b", "them: c", "you: d"],
        )

    def test_a_named_voice_and_the_users_own(self):
        names = {"mic_0": "Alice", "mic_1": speakers.USER, "speaker_0": "Bob"}
        self.assertEqual(
            [speakers.tag(r, names) for r in self.ROWS],
            ["Alice", "You", "Bob", "You"],
        )
        self.assertEqual(speakers.tag(self.ROWS[0], {}), "Voice 1")

    def test_a_far_side_name_never_lands_on_a_room_voice(self):
        names = {"speaker_0": "Bob"}
        self.assertEqual(speakers.name_of(self.ROWS[0], names), "")


class TestIntroductions(Scratch):
    """Who a room voice is, from what it says. In a room "This is Sam" is
    someone introducing Sam, so it names nobody; "I'm Sam" names the
    voice that says it."""

    ROWS = (
        {"ts": 1, "speaker": "you", "who": "mic_0",
         "text": "This is Sam, who joins us this week."},
        {"ts": 2, "speaker": "you", "who": "mic_1",
         "text": "Hi everyone, glad to be here."},
        {"ts": 3, "speaker": "you", "who": "mic_2",
         "text": "And Sam here will share a desk with me."},
        {"ts": 4, "speaker": "you", "who": "mic_0",
         "text": "Sam, can you send the slides by Friday?"},
    )

    def test_an_introduction_of_someone_else_names_no_room_voice(self):
        folder = self.scratch / "intro"
        names = speakers.learn_from(folder, list(self.ROWS), "Sam")
        self.assertEqual(names, {})
        self.assertEqual([speakers.tag(r, names) for r in self.ROWS],
                         ["Voice 1", "Voice 2", "Voice 3", "Voice 1"])

    def test_i_am_names_the_voice_that_says_it(self):
        folder = self.scratch / "self"
        rows = [*self.ROWS, {"ts": 5, "speaker": "you", "who": "mic_1",
                             "text": "Thanks. I'm Sam, I started on Monday."}]
        names = speakers.learn_from(folder, rows, "Sam")
        self.assertEqual(names, {"mic_1": speakers.USER})

    def test_a_call_still_reads_this_is_as_the_speaker(self):
        """On a call the far side says who is on the line."""
        folder = self.scratch / "call"
        rows = [{"ts": 1, "speaker": "them", "who": "speaker_0",
                 "text": "This is Alice from the vendor."}]
        self.assertEqual(speakers.learn_from(folder, rows, "Sam"),
                         {"speaker_0": "Alice"})

    def test_the_summary_of_a_room_learns_the_same_way(self):
        folder = make_session(self.root, "20300116-140200", manual=True,
                              far=[], meta={"room": True})
        (folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in self.ROWS)
        )
        reply = {"summary": SUMMARY, "project": None, "voices": []}
        fake = mock.AsyncMock(return_value=(reply, "fake-model"))
        projects = self.scratch / "projects"
        projects.mkdir()
        with (
            mock.patch.dict(os.environ, {"STENO_USER_NAME": "Sam"}),
            mock.patch.object(claude, "run_structured", fake),
        ):
            asyncio.run(summarize_session(folder, "m", projects=projects))
        self.assertEqual(speakers.load_names(folder), {})
        self.assertIn("voice 1: This is Sam", fake.call_args.args[0])
        self.assertIn("names the voice that answers",
                      fake.call_args.kwargs["system"])


class TestCalendar(Scratch):
    def setUp(self):
        super().setUp()
        self.cal = self.scratch / "calendars" / "work"
        self.cal.mkdir(parents=True)
        (self.cal / "weekly.ics").write_text(WEEKLY)

    def at(self, hour: int, minute: int = 0) -> list[str]:
        moment = datetime(2030, 1, 16, hour, minute, tzinfo=ZONE)
        return room.calendar_attendees([self.cal.parent], moment)

    def test_a_later_week_of_a_weekly_meeting(self):
        """Declined people, rooms and bare addresses are no guests."""
        self.assertEqual(self.at(14, 2), ["Bob", "Alice Moreau"])

    def test_early_by_a_few_minutes_is_the_same_meeting(self):
        self.assertEqual(self.at(13, 50), ["Bob", "Alice Moreau"])

    def test_after_it_ends_nobody_is_expected(self):
        """The all-day event that day names no meeting's guests."""
        self.assertEqual(self.at(16, 0), [])


class TestRoomSummary(Scratch):
    """The model is a mock; what it is sent and what is kept is checked."""

    def setUp(self):
        super().setUp()
        cal = self.scratch / "calendars"
        cal.mkdir()
        (cal / "weekly.ics").write_text(WEEKLY)
        earlier = make_session(self.root, "20300109-140100", manual=False,
                               far=["hi"], meta={"project": "alpha"})
        (earlier / "speakers.json").write_text(
            json.dumps({"speaker_0": "Dana", "speaker_1": "Bob"})
        )
        other = make_session(self.root, "20291220-090000", manual=False,
                             far=["hi"], meta={"project": "beta"})
        (other / "speakers.json").write_text(json.dumps({"speaker_0": "Olga"}))
        self.folder = make_session(self.root, "20300116-140200",
                                   manual=True, far=[],
                                   meta={"project": "alpha"})
        Deepgram(self, THREE_VOICES)
        rows = asyncio.run(transcribe.transcribe_file(
            self.folder / "you.wav", STARTED, "you", voices_as="mic"
        ))
        (self.folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        env = mock.patch.dict(os.environ, {
            "STENO_USER_NAME": "Sam", "STENO_CALENDAR_DIR": str(cal),
        })
        env.start()
        self.addCleanup(env.stop)

    def summarize(self, voices: list[dict]) -> mock.AsyncMock:
        reply = {"summary": SUMMARY, "project": None, "voices": voices}
        fake = mock.AsyncMock(return_value=(reply, "fake-model"))
        projects = self.scratch / "projects"
        projects.mkdir(exist_ok=True)
        with mock.patch.object(claude, "run_structured", fake):
            asyncio.run(summarize_session(
                self.folder, "m", projects=projects
            ))
        return fake

    def test_the_model_sees_voices_and_who_was_expected(self):
        fake = self.summarize([])
        transcript = fake.call_args.args[0]
        system = fake.call_args.kwargs["system"]
        self.assertIn("voice 1: Sam, can you send", transcript)
        self.assertNotIn("] you:", transcript)
        self.assertIn(
            "People who may be in the room: Bob, Alice Moreau, Dana.",
            system,
        )
        self.assertIn("The user is Sam.", system)
        self.assertIn("voices", fake.call_args.kwargs["schema"]["required"])
        self.assertIs(load_meta(self.folder)["room"], True)

    def test_no_project_no_names_from_earlier_meetings(self):
        meta = json.loads((self.folder / "meta.json").read_text())
        meta.pop("project")
        (self.folder / "meta.json").write_text(json.dumps(meta))
        system = self.summarize([]).call_args.kwargs["system"]
        self.assertIn("People who may be in the room: Bob, Alice Moreau.",
                      system)

    def test_only_expected_names_on_heard_voices_are_kept(self):
        self.summarize([
            {"voice": "voice 2", "name": "Sam"},     # the user
            {"voice": "Voice 1", "name": "bob"},     # listed, any case
            {"voice": "voice 3", "name": "Zed"},     # nobody expected
            {"voice": "voice 9", "name": "Dana"},    # no such voice
        ])
        self.assertEqual(
            speakers.load_names(self.folder),
            {"mic_0": "Bob", "mic_1": speakers.USER},
        )

    def test_a_name_the_user_gave_is_not_overwritten(self):
        speakers.save_names(self.folder, {"mic_0": "Alice Moreau"})
        self.summarize([{"voice": "voice 1", "name": "Bob"}])
        self.assertEqual(speakers.load_names(self.folder)["mic_0"],
                         "Alice Moreau")

    def test_a_call_gets_no_room_prompt(self):
        call = make_session(self.root, "20300116-170000", manual=False,
                            far=["Hello"])
        reply = {"summary": SUMMARY, "project": None}
        fake = mock.AsyncMock(return_value=(reply, "fake-model"))
        projects = self.scratch / "projects"
        projects.mkdir(exist_ok=True)
        with mock.patch.object(claude, "run_structured", fake):
            asyncio.run(summarize_session(call, "m", projects=projects))
        self.assertNotIn("A room, not a call", fake.call_args.kwargs["system"])
        self.assertNotIn("voices", fake.call_args.kwargs["schema"]["required"])


class TestHeldTodos(Scratch):
    """The items a room summary gives the user wait for their word."""

    def room(self) -> Path:
        return make_session(self.root, "20300116-140200", manual=True,
                            far=[], meta={"room": True})

    def test_a_room_files_nothing_until_the_user_says(self):
        folder = self.room()
        with FakeAsst() as asst:
            todos = sync_todos(folder, SUMMARY)
            self.assertEqual(asst.tasks(), [])
        mine = next(t for t in todos if t.owner == "you")
        self.assertTrue(mine.held)
        self.assertEqual(mine.uid, "")
        # Not yours yet, so the list of meetings counts it as to sort.
        self.assertEqual((open_count(folder), held_count(folder)), (0, 2))

    def test_every_room_item_waits_whoever_the_summary_gave_it_to(self):
        """With the user's voice never named, their own promise is under
        Them: it waits to be sorted, and counts as nobody's meanwhile."""
        folder = self.room()
        with FakeAsst() as asst:
            todos = sync_todos(folder, SUMMARY_NO_USER)
            self.assertEqual(asst.tasks(), [])
        self.assertEqual([(t.owner, t.held) for t in todos],
                         [("them", True), ("them", True)])
        self.assertEqual((open_count(folder), held_count(folder)), (0, 2))
        with FakeAsst() as asst:
            decide_owner(folder, [todos[0].key], mine=True)
            self.assertEqual(len(asst.tasks()), 1)
        self.assertEqual((open_count(folder), held_count(folder)), (1, 1))

    def test_unarchiving_counts_a_held_row_as_waiting_not_failed(self):
        folder = self.room()
        sync_todos(folder, SUMMARY)
        rows = json.loads((folder / "todos.json").read_text())
        for row in rows:
            row["dropped"] = True
        (folder / "todos.json").write_text(json.dumps(rows))
        with FakeAsst():
            restored = restore_dropped_todos(folder)
        self.assertEqual((restored.filed, restored.failed), (0, 0))

    def test_mine_files_it_and_a_later_summary_keeps_it_filed(self):
        folder = self.room()
        with FakeAsst() as asst:
            todos = sync_todos(folder, SUMMARY)
            key = next(t.key for t in todos if t.owner == "you")
            decide_owner(folder, [key], mine=True)
            self.assertEqual(len(asst.tasks()), 1)
            sync_todos(folder, SUMMARY)
            self.assertEqual(len(asst.tasks()), 1)
        row = next(t for t in load_todos(folder) if t.key == key)
        self.assertFalse(row.held)
        self.assertTrue(row.uid)

    def test_not_mine_is_never_filed_whatever_a_later_summary_says(self):
        folder = self.room()
        with FakeAsst() as asst:
            todos = sync_todos(folder, SUMMARY)
            key = next(t.key for t in todos if t.owner == "you")
            decide_owner(folder, [key], mine=False)
            sync_todos(folder, SUMMARY)
            again = sync_todos(folder, SUMMARY.replace(
                "Send the slides by Friday",
                "Send the slides by Friday please",
            ))
            self.assertEqual(asst.tasks(), [])
        row = next(t for t in again if t.text.startswith("Send"))
        self.assertEqual(row.owner, "them")
        self.assertFalse(row.held)

    def test_a_call_still_files_at_once(self):
        folder = make_session(self.root, "20300116-170000", manual=False,
                              far=["Hello"])
        with FakeAsst() as asst:
            todos = sync_todos(folder, SUMMARY)
            self.assertEqual(len(asst.tasks()), 1)
        self.assertFalse(any(t.held for t in todos))


class TestWords(unittest.TestCase):
    """What the window and the notification say about a room."""

    def test_a_kept_room_offers_to_send_it(self):
        from steno.gui import words  # noqa: PLC0415 - no widget, no display

        text, offer = words.summary_missing(0, room_kept=True)
        self.assertIn("nothing has gone to Deepgram", text)
        self.assertTrue(offer)
        self.assertFalse(words.summary_missing(0)[1])

    def test_counts_say_what_waits(self):
        from steno.gui import words  # noqa: PLC0415 - no widget, no display

        self.assertEqual(words.session_badge(2, 3), "2 to do · 3 to sort")
        self.assertEqual(words.session_badge(0, 1), "1 to sort")
        self.assertEqual(words.summary_ready_body(0),
                         "Summary and todos are ready.")
        self.assertIn("its 3 to-dos are yours", words.summary_ready_body(3))


class TestRealign(Scratch):
    def test_both_sides_keep_their_voices_and_old_names_step_aside(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "room", manual=True, far=[],
                              meta={"room": True})
        speakers.save_names(folder, {"mic_0": "Bob"})
        asyncio.run(realign.realign_session(folder, started_at=STARTED))
        self.assertEqual([dg.voices_asked(i) for i in range(2)],
                         ["latest", "latest"])
        whos = {json.loads(x).get("who") for x in
                (folder / "transcript.jsonl").read_text().splitlines()}
        self.assertEqual(whos, {"mic_0", "mic_1", "mic_2",
                                "speaker_0", "speaker_1", "speaker_2"})
        self.assertFalse((folder / "speakers.json").exists())
        self.assertTrue((folder / "speakers.before-realign.json").is_file())

    def test_a_call_keeps_the_microphone_one_voice(self):
        dg = Deepgram(self, THREE_VOICES)
        folder = make_session(self.root, "call", manual=False,
                              far=["Hello"])
        asyncio.run(realign.realign_session(folder, started_at=STARTED))
        asked = {r.url.params.get("diarize_model") for r in dg.asked}
        self.assertEqual(asked, {"latest", None})


if __name__ == "__main__":
    unittest.main()
