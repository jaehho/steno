"""The OpenRouter client: request shape, parsing, retries, streaming, tools."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from steno import claude
from steno.summarize import (
    list_projects,
    load_project,
    save_project,
    summarize_session,
)

SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}


def _sse(*events):
    """SSE bytes: dicts are json-encoded as data lines; strings (`:...` comments,
    `data: [DONE]`) pass through verbatim."""
    body = "".join(
        f"data: {json.dumps(e)}\n\n" if isinstance(e, dict) else f"{e}\n\n"
        for e in events)
    return httpx.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


def _completion(content, *, reasoning=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _resp(content, **kw):
    return httpx.Response(200, json=_completion(content, **kw))


def _tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class Keyed(unittest.TestCase):
    """A key in the environment, and no sleeping between retries."""

    def setUp(self):
        for p in (mock.patch.dict(os.environ, {"STENO_API_KEY": "sk-test"},
                                  clear=False),
                  mock.patch.object(claude, "_retry_sleep", mock.AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def serve(self, *responses, seen=None):
        """Serve responses in order, repeating the last; record requests to `seen`.

        Each response is rebuilt per request: a Response that has been read
        once cannot be streamed again.
        """
        specs = [(r.status_code, dict(r.headers), r.read()) for r in responses]

        def handler(request):
            if seen is not None:
                seen.append(json.loads(request.content))
            code, headers, body = specs.pop(0) if len(specs) > 1 else specs[0]
            return httpx.Response(code, headers=headers, content=body)

        return mock.patch.object(claude, "_transport", httpx.MockTransport(handler))


class TestRequestShape(Keyed):
    def test_model_messages_and_strict_schema(self):
        seen = []
        schema = {"type": "object", "required": ["a"],
                  "properties": {"a": {"type": ["string", "null"]}}}
        with self.serve(_resp('{"a": null}'), seen=seen):
            out = asyncio.run(claude.run_structured(
                "p", system="s", model="@preset/x", schema=schema, timeout=5))
        self.assertEqual(out, {"a": None})
        req = seen[0]
        self.assertEqual(req["model"], "@preset/x")
        self.assertEqual([m["role"] for m in req["messages"]], ["system", "user"])
        rf = req["response_format"]["json_schema"]
        self.assertEqual((rf["name"], rf["strict"]), ("steno", True))
        prop = rf["schema"]["properties"]["a"]
        self.assertNotIn("type", prop)
        self.assertEqual(prop["anyOf"], [{"type": "string"}, {"type": "null"}])


class TestStructured(Keyed):
    def test_string_content(self):
        with self.serve(_resp('{"a": "x"}')):
            self.assertEqual(asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5)), {"a": "x"})

    def test_parts_join_and_reasoning_ignored(self):
        body = _completion([{"type": "text", "text": '{"a": '},
                            {"type": "text", "text": '"x"}'}],
                           reasoning="thinking out loud")
        with self.serve(httpx.Response(200, json=body)):
            self.assertEqual(asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5)), {"a": "x"})

    def test_fenced_content_parses(self):
        with self.serve(_resp('```json\n{"a": "x"}\n```')):
            self.assertEqual(asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5)), {"a": "x"})


class TestRetries(Keyed):
    def test_429_then_success(self):
        with self.serve(httpx.Response(429, json={}), _resp('{"a": "x"}')):
            self.assertEqual(asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5)), {"a": "x"})

    def test_empty_content_is_retried_then_raises(self):
        with (self.serve(_resp("")),
              self.assertRaisesRegex(claude.ClaudeError, "after 3 attempts")):
            asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5))

    def test_persistent_500(self):
        with (self.serve(httpx.Response(500, json={})),
              self.assertRaisesRegex(claude.ClaudeError, "after 3 attempts")):
            asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5))

    def test_400_is_immediate(self):
        with (self.serve(httpx.Response(400, json={"error": {"message": "bad schema"}})),
              self.assertRaisesRegex(claude.ClaudeError, "bad schema")):
            asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, timeout=5))


class TestStream(Keyed):
    def test_deltas_and_reasoning_dropped(self):
        got = []
        with self.serve(_sse(
                ": OPENROUTER PROCESSING",
                {"choices": [{"delta": {"reasoning": "hmm"}}]},
                {"choices": [{"delta": {"content": "a"}}]},
                {"choices": [{"delta": {"content": "b"}}]},
                "data: [DONE]")):
            answer = asyncio.run(claude.stream_text(
                "q", system="s", model="m", on_delta=got.append, timeout=5))
        self.assertEqual((got, answer), (["a", "b"], "ab"))

    def test_midstream_death_retries_once_then_succeeds(self):
        got = []
        dead = _sse()   # a stream that dies before its first token
        good = _sse({"choices": [{"delta": {"content": "b"}}]}, "data: [DONE]")
        with self.serve(dead, good):
            answer = asyncio.run(claude.stream_text(
                "q", system="s", model="m", on_delta=got.append, timeout=5))
        self.assertEqual((got, answer), (["b"], "b"))

    def test_reasoning_content_is_dropped(self):
        got = []
        with self.serve(_sse(
                {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
                {"choices": [{"delta": {"content": "a"}}]},
                "data: [DONE]")):
            answer = asyncio.run(claude.stream_text(
                "q", system="s", model="m", on_delta=got.append, timeout=5))
        self.assertEqual((got, answer), (["a"], "a"))

    def test_partial_text_is_not_retried(self):
        got = []
        dead = _sse({"choices": [{"delta": {"content": "a"}}]})   # emits, then dies
        with (self.serve(dead), self.assertRaises(claude.ClaudeError)):
            asyncio.run(claude.stream_text(
                "q", system="s", model="m", on_delta=got.append, timeout=5))
        self.assertEqual(got, ["a"])

    def test_error_chunk_raises(self):
        got = []
        with (self.serve(_sse({"error": {"message": "over quota"}})),
              self.assertRaisesRegex(claude.ClaudeError, "over quota")):
            asyncio.run(claude.stream_text(
                "q", system="s", model="m", on_delta=got.append, timeout=5))


class TestToolLoop(Keyed):
    def test_tools_run_and_summarize_records_the_project(self):
        projects = Path(tempfile.mkdtemp())
        (projects / "thesis").mkdir()
        (projects / "thesis" / "README.md").write_text("# thesis\n\nA beamformer on an FPGA.\n")
        session = Path(tempfile.mkdtemp())
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "them", "text": "the beamformer"}) + "\n")
        seen = []
        calls = [
            [_tool_call("1", "list_dir", "{}")],
            [_tool_call("2", "read_file", '{"path": "thesis/README.md"}')],
        ]

        def handler(request):
            seen.append(json.loads(request.content))
            if calls:
                return _resp("", tool_calls=calls.pop(0))
            return _resp('{"summary": "# Beamformer Review\\n\\n## TL;DR\\n- x", "project": "thesis"}')

        with mock.patch.object(claude, "_transport", httpx.MockTransport(handler)):
            summary = asyncio.run(summarize_session(session, "m", None, projects))

        self.assertEqual((summary.title, summary.project), ("Beamformer Review", "thesis"))
        self.assertEqual(load_project(session), "thesis")
        tools1 = [m for m in seen[1]["messages"] if m["role"] == "tool"]
        tools2 = [m for m in seen[2]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools1], ["1"])
        self.assertEqual(tools1[0]["content"], "thesis/")
        self.assertEqual([m["tool_call_id"] for m in tools2], ["1", "2"])
        self.assertIn("beamformer", tools2[-1]["content"])
        self.assertIn(str(projects), seen[0]["messages"][0]["content"])

    def test_path_escape_is_an_error_string(self):
        projects = Path(tempfile.mkdtemp())
        seen = []
        calls = [[_tool_call("1", "read_file", '{"path": "../../etc/passwd"}')]]

        def handler(request):
            seen.append(json.loads(request.content))
            if calls:
                return _resp("", tool_calls=calls.pop(0))
            return _resp('{"a": "x"}')

        with mock.patch.object(claude, "_transport", httpx.MockTransport(handler)):
            out = asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, read_root=projects, timeout=5))
        self.assertEqual(out, {"a": "x"})
        tools = [m for m in seen[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(tools[0]["content"].startswith("error:"))

    def test_grep_does_not_follow_a_symlink_out_of_the_root(self):
        """The README promises it can read nothing outside the directory."""
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.env").write_text("API_TOKEN=hunter2\n")
        root = Path(tempfile.mkdtemp())
        (root / "notes.md").write_text("API_TOKEN is documented here\n")
        (root / ".env").symlink_to(outside / "secret.env")
        result = claude._run_tool(root, "grep", {"pattern": "API_TOKEN"})
        self.assertIn("notes.md", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn(".env", result)

    def test_the_tools_skip_a_projects_meeting_notes(self):
        """Steno's copies of earlier meetings are other people's speech,
        unredacted: the model neither lists, reads nor finds them."""
        root = Path(tempfile.mkdtemp())
        notes = root / "thesis" / "notes"
        (notes / "meetings").mkdir(parents=True)
        page = notes / "meetings" / "2026-10-06-1612-x.transcript.md"
        page.write_text("[00:01] Them: ring me on 555-0100\n")
        (notes / "ideas.md").write_text("ring the supplier\n")
        (notes / "alias").symlink_to(notes / "meetings")
        (root / "thesis" / "page.md").symlink_to(page)

        def tool(name, **args):
            call = {"id": "1", "function": {
                "name": name, "arguments": json.dumps(args)}}
            return claude._tool_result(root, call)["content"]

        self.assertEqual(tool("list_dir", path="thesis/notes"), "ideas.md")
        for path in ("thesis/notes/meetings", "thesis/notes/alias",
                     "thesis/notes/meetings/" + page.name):
            self.assertTrue(tool("list_dir", path=path).startswith("error:"))
            self.assertTrue(tool("read_file", path=path).startswith("error:"))
        found = tool("grep", pattern="ring")
        self.assertIn("ideas.md", found)
        self.assertNotIn("555", found)
        self.assertNotIn("page.md", found)

    def test_over_budget_forces_a_final_answer(self):
        projects = Path(tempfile.mkdtemp())
        seen = []
        n = [0]

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            if body.get("tools"):
                n[0] += 1
                return _resp("", tool_calls=[_tool_call(str(n[0]), "list_dir", "{}")])
            return _resp('{"a": "x"}')

        with mock.patch.object(claude, "_transport", httpx.MockTransport(handler)):
            out = asyncio.run(claude.run_structured(
                "p", system="s", model="m", schema=SCHEMA, read_root=projects, timeout=5))
        self.assertEqual(out, {"a": "x"})
        self.assertEqual(len(seen), claude.MAX_TOOL_ROUNDS + 1)
        self.assertNotIn("tools", seen[-1])

    def test_title_only_summary_is_retried_then_succeeds(self):
        projects = Path(tempfile.mkdtemp())
        session = Path(tempfile.mkdtemp())
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "them", "text": "hi"}) + "\n")
        replies = [
            '{"summary": "# Only a title", "project": null}',
            '{"summary": "# Real\\n\\n## TL;DR\\n- x", "project": null}',
        ]

        def handler(_request):
            return _resp(replies.pop(0) if len(replies) > 1 else replies[0])

        with mock.patch.object(claude, "_transport", httpx.MockTransport(handler)):
            summary = asyncio.run(summarize_session(session, "m", None, projects))
        self.assertEqual(summary.title, "Real")
        self.assertIn("## TL;DR", (session / "summary.md").read_text())

    def test_title_only_three_times_raises_without_writing(self):
        projects = Path(tempfile.mkdtemp())
        session = Path(tempfile.mkdtemp())
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "them", "text": "hi"}) + "\n")

        def handler(_request):
            return _resp('{"summary": "# Only a title", "project": null}')

        with (mock.patch.object(claude, "_transport", httpx.MockTransport(handler)),
              self.assertRaisesRegex(claude.ClaudeError, "only a title")):
            asyncio.run(summarize_session(session, "m", None, projects))
        self.assertFalse((session / "summary.md").exists())


class TestProjects(unittest.TestCase):
    def test_describes_each_directory_by_its_readme(self):
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "a" / "README.md").write_text("# a\n\n![badge](x)\nDoes a thing.\n")
        (root / "b").mkdir()
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("")
        self.assertEqual(list_projects(root), {"a": "Does a thing.", "b": ""})

    def test_missing_root(self):
        self.assertEqual(list_projects(Path("/nonexistent/steno")), {})

    def test_save_project_only_touches_that_key(self):
        """A manual link must not clobber title, cost, or anything else."""
        session = Path(tempfile.mkdtemp())
        (session / "meta.json").write_text(json.dumps({
            "title": "Beamformer Review",
            "project": None,
            "cost_usd": 0.12,
        }) + "\n")
        save_project(session, "thesis")
        meta = json.loads((session / "meta.json").read_text())
        self.assertEqual(meta["project"], "thesis")
        self.assertEqual(meta["title"], "Beamformer Review")
        self.assertEqual(meta["cost_usd"], 0.12)
        self.assertEqual(load_project(session), "thesis")

        save_project(session, None)
        self.assertIsNone(load_project(session))

    def test_save_project_creates_meta_when_missing(self):
        session = Path(tempfile.mkdtemp())
        save_project(session, "thesis")
        self.assertEqual(load_project(session), "thesis")


class TestEndpointConfig(unittest.TestCase):
    """STENO_MODEL / STENO_PROVIDER / STENO_BASE_URL are how the endpoint moves."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mimo = self.tmp / "mimo.env"
        self.mimo.write_text(
            "MIMO_API_KEY=tp-test\nMIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/anthropic\n")
        patcher = mock.patch.dict(os.environ, {
            "STENO_API_KEY": "",
            "STENO_API_KEY_FILE": str(self.mimo),
            "STENO_API_KEY_NAME": "MIMO_API_KEY",
            "STENO_PROVIDER": "mimo",
            "STENO_BASE_URL": "",
            "MIMO_ENV": str(self.mimo),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mimo_route_is_derived_from_the_anthropic_host(self):
        self.assertEqual(claude.base_url(),
                         "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions")

    def test_steno_base_url_wins(self):
        os.environ["STENO_BASE_URL"] = "https://example.test/v1/chat/completions"
        self.assertEqual(claude.base_url(), "https://example.test/v1/chat/completions")

    def test_openrouter_provider_keeps_its_own_url(self):
        os.environ["STENO_PROVIDER"] = "openrouter"
        self.assertEqual(claude.base_url(),
                         "https://openrouter.ai/api/v1/chat/completions")

    def test_key_comes_from_the_provider_file(self):
        self.assertEqual(claude.api_key(), "tp-test")
        self.assertIsNone(claude.check_ready())

    def test_steno_api_key_wins(self):
        os.environ["STENO_API_KEY"] = "sk-direct"
        self.assertEqual(claude.api_key(), "sk-direct")

    def test_default_model_follows_the_provider(self):
        self.assertEqual(claude.default_model(), "mimo-v2.6-flash")
        os.environ["STENO_PROVIDER"] = "openrouter"
        self.assertEqual(claude.default_model(), "@preset/cc-flash-med")

    def test_unknown_provider_raises(self):
        os.environ["STENO_PROVIDER"] = "nope"
        with self.assertRaisesRegex(claude.ClaudeError, "unknown STENO_PROVIDER"):
            claude.base_url()

    def test_presets_lead_with_the_providers_default(self):
        self.assertEqual(claude.presets(),
                         ["mimo-v2.6-flash", "mimo-v2.6-pro[1m]"])
        os.environ["STENO_PROVIDER"] = "openrouter"
        self.assertEqual(claude.presets(), ["@preset/cc-flash-med"])


class TestModelList(unittest.TestCase):
    """The provider's own list, which fills the Ask model menu."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {
            "STENO_API_KEY": "sk-test",
            "STENO_PROVIDER": "mimo",
            "STENO_BASE_URL": "https://example.test/v1/chat/completions",
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, response, seen=None):
        def handler(request):
            if seen is not None:
                seen.append(request)
            return response

        return mock.patch.object(claude, "_transport", httpx.MockTransport(handler))

    def test_the_list_sits_beside_the_chat_endpoint(self):
        self.assertEqual(claude.models_url("https://x.test/v1/chat/completions/"),
                         "https://x.test/v1/models")

    def test_an_endpoint_that_is_not_chat_completions_has_no_list(self):
        with self.assertRaises(ValueError):
            claude.models_url("https://x.test/v1/messages")

    def test_ids_are_sorted_and_not_repeated(self):
        body = {"object": "list", "data": [
            {"id": "b-2"}, {"id": "a-1", "owned_by": "x"}, {"id": "b-2"},
            {"id": " "}, {"id": 7}, {"name": "no id"}, "junk"]}
        self.assertEqual(claude.parse_models(body), ["a-1", "b-2"])

    def test_speech_models_are_left_out_of_a_list_for_chat(self):
        body = {"data": [{"id": i} for i in (
            "mimo-v2.5-pro", "mimo-v2.5-tts", "mimo-v2.5-tts-voiceclone",
            "mimo-v2.5-asr", "openai/whisper-1", "vendor/tts", "stats-1")]}
        self.assertEqual(claude.parse_models(body),
                         ["mimo-v2.5-pro", "openai/whisper-1", "stats-1"])

    def test_a_reply_that_is_not_a_list_lists_nothing(self):
        for body in ({}, {"data": None}, {"data": "x"}, [], "x", None):
            self.assertEqual(claude.parse_models(body), [], body)

    def test_it_asks_with_the_key_and_returns_the_ids(self):
        seen = []
        reply = httpx.Response(200, json={"data": [{"id": "m-2"}, {"id": "m-1"}]})
        with self.serve(reply, seen):
            got = asyncio.run(claude.list_models(timeout=5))
        self.assertEqual(got, ["m-1", "m-2"])
        (request,) = seen
        self.assertEqual(str(request.url), "https://example.test/v1/models")
        self.assertEqual(request.headers["authorization"], "Bearer sk-test")

    def test_a_refusal_is_a_claude_error(self):
        reply = httpx.Response(401, json={"error": {"message": "bad key"}})
        with (self.serve(reply), self.assertRaisesRegex(claude.ClaudeError, "bad key")):
            asyncio.run(claude.list_models(timeout=5))

    def test_a_reply_that_is_not_json_is_a_claude_error(self):
        with (self.serve(httpx.Response(200, content=b"<html>")),
              self.assertRaises(claude.ClaudeError)):
            asyncio.run(claude.list_models(timeout=5))

    def test_an_unreachable_host_is_a_claude_error(self):
        def refuse(request):
            msg = "no route"
            raise httpx.ConnectError(msg, request=request)

        with (mock.patch.object(claude, "_transport", httpx.MockTransport(refuse)),
              self.assertRaises(claude.ClaudeError)):
            asyncio.run(claude.list_models(timeout=5))

    def test_an_endpoint_with_no_list_is_a_claude_error(self):
        os.environ["STENO_BASE_URL"] = "https://example.test/v1/messages"
        with self.assertRaises(claude.ClaudeError):
            asyncio.run(claude.list_models(timeout=5))


if __name__ == "__main__":
    unittest.main()
