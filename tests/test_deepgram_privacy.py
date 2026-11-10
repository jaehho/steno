"""Every Deepgram request opts out of model training, on any host."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx

from steno import transcribe
from steno.transcribe import HOST_ENV, listen_url, transcribe_file

SRC = Path(transcribe.__file__).parent


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)


def _batch_request(env: dict[str, str]) -> httpx.Request:
    """transcribe_file against a fake Deepgram; the request it sent."""
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": {"utterances": []}})

    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        return real_client(transport=httpx.MockTransport(reply), **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "you.wav"
        _silent_wav(wav)
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(transcribe.httpx, "AsyncClient",
                                  fake_client):
            asyncio.run(transcribe_file(wav, 0.0, "you", keyterms=["ACME"]))
    return seen[0]


class TestModelTrainingOptOut(unittest.TestCase):
    def test_stream_opts_out(self):
        q = parse_qs(urlparse(listen_url(["ACME"])).query)
        self.assertEqual(q["mip_opt_out"], ["true"])

    def test_batch_opts_out(self):
        request = _batch_request({"DEEPGRAM_API_KEY": "test"})
        self.assertEqual(request.url.params.get("mip_opt_out"), "true")


class TestUsageLogKeepsNoNames(unittest.TestCase):
    """The opt-out leaves keyterms in the usage log; redaction not."""

    def test_stream_redacts_usage(self):
        q = parse_qs(urlparse(listen_url(["Jane Doe"])).query)
        self.assertEqual(q["keyterm"], ["Jane Doe"])
        self.assertEqual(q["redact_usage"], ["true"])

    def test_batch_redacts_usage(self):
        request = _batch_request({"DEEPGRAM_API_KEY": "test"})
        self.assertEqual(request.url.params.get("keyterm"), "ACME")
        self.assertEqual(request.url.params.get("redact_usage"), "true")


class TestHost(unittest.TestCase):
    def test_global_host_by_default(self):
        with mock.patch.dict(os.environ, {HOST_ENV: ""}):
            self.assertEqual(urlparse(listen_url()).netloc,
                             "api.deepgram.com")

    def test_eu_host_for_both_calls(self):
        eu = "api.eu.deepgram.com"
        with mock.patch.dict(os.environ, {HOST_ENV: eu}):
            self.assertEqual(urlparse(listen_url()).netloc, eu)
        request = _batch_request({"DEEPGRAM_API_KEY": "test",
                                  HOST_ENV: eu})
        self.assertEqual(request.url.host, eu)
        self.assertEqual(request.url.params.get("mip_opt_out"), "true")


# An API address, global or regional. A link to Deepgram's console or
# docs in help text is not a request and may stay.
API_URL = re.compile(r"\w+://api\.(?:[\w-]+\.)*deepgram\.com")


class TestNoOtherDeepgramCall(unittest.TestCase):
    """A URL written by hand would skip the opt-out and the EU pin."""

    def test_no_module_writes_a_deepgram_url(self):
        writing = sorted(
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if API_URL.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(writing, [])

    def test_pattern_is_api_hosts_only(self):
        for url in ("https://api.deepgram.com/v1/listen",
                    "wss://api.eu.deepgram.com/v1/listen"):
            self.assertRegex(url, API_URL)
        for url in ("https://console.deepgram.com/",
                    "https://developers.deepgram.com/docs"):
            self.assertNotRegex(url, API_URL)


if __name__ == "__main__":
    unittest.main()
