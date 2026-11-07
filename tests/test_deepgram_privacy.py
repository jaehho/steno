"""Every Deepgram request opts out of model training, on any host."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx

from steno import transcribe
from steno.transcribe import HOST_ENV, listen_url, transcribe_file


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)


def _batch_request(env: dict[str, str]) -> httpx.Request:
    """Run transcribe_file against a fake Deepgram; return its request."""
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
            asyncio.run(transcribe_file(wav, 0.0, "you"))
    return seen[0]


class TestModelTrainingOptOut(unittest.TestCase):
    def test_stream_opts_out(self):
        q = parse_qs(urlparse(listen_url(["ACME"])).query)
        self.assertEqual(q["mip_opt_out"], ["true"])

    def test_batch_opts_out(self):
        request = _batch_request({"DEEPGRAM_API_KEY": "test"})
        self.assertEqual(request.url.params.get("mip_opt_out"), "true")


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


if __name__ == "__main__":
    unittest.main()
