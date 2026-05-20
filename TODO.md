# meeting-copilot

## v0 (MVP) — done
- [x] dual audio capture via parec (default mic + default sink monitor)
- [x] two Deepgram WebSocket streams, speaker-tagged
- [x] textual TUI: split transcript + advisor pane
- [x] Ctrl+G → Claude advisor with rolling context

## v0.1 — done
- [x] show interim (gray) transcripts that get replaced by finals
- [x] stream advisor output token-by-token
- [x] auto-trigger advisor when a question is detected from "them"
- [x] persist session transcript (jsonl + txt) and audio (wav per speaker)

## v0.2 — done
- [x] load `brief.md` from cwd as standing context for the advisor (lines starting with `@` attach file/dir manifests; cached system block)
- [x] proper package layout under `src/meeting_copilot/` (audio, transcribe, brief, app, cli modules)
- [x] XDG paths: config at `~/.config/meeting-copilot/.env`, sessions at `~/.local/share/meeting-copilot/sessions/`
- [x] `copilot init` subcommand to bootstrap config dir + .env template
- [x] `copilot --brief PATH` flag to override cwd brief.md lookup
- [x] `copilot --version`
- [x] PKGBUILD for local Arch install (`paru -Bi`); deps come from official repos + AUR

## v0.3 — done
- [x] brief status in TUI header (sub_title shows `brief: ~Xk tok · Nd old` or `no brief`)
- [x] `copilot brief` subcommand: prints brief + char/token/age
- [x] `copilot brief --new`: scaffolds a **tight curated** `brief.md` via Claude Code (`claude --model opus`, uses subscription not API). The prompt instructs Claude Code to distill, not dump — no whole-file inlining.
- [x] split model defaults: `CLAUDE_MODEL` (opus 4.7) for Ctrl+G, `CLAUDE_MODEL_AUTO` (sonnet 4.6) for auto-triggered question responses; 1M context tier enabled via `betas=["context-1m-2025-08-07"]` on `client.beta.messages.stream` (the `[1m]` suffix is a display-only convention, not a valid API model ID)
- [x] brief.md is a curated terse markdown file the user maintains by hand (or regenerates via `--new`). The Anthropic API has no filesystem access, so `@path` resolution / `<!-- snapshot -->` blocks / `--refresh` were all explored and dropped — keeping every token earning its keep matters more than auto-syncing project files, especially for the Sonnet auto-trigger path.

## v0.4 — done
- [x] `copilot history` subcommand: TUI browser for past sessions. Left pane lists sessions (newest first, with date/duration/line-count); right pane shows the transcript color-coded by speaker; click any line to seek audio; space play/pause; ←/→ ±5s; `d` deletes the highlighted session (confirm modal). Current playback line is highlighted and auto-scrolled into view. Audio is stereo (you=L, them=R) via sounddevice + soundfile + numpy. Adds `portaudio` as a system dep.

## Next
- [ ] Deepgram keyterm boost from `keyterms.txt` (`&keyterm=...`)
- [ ] Deepgram KeepAlive ping every 8s (idle-close mitigation)
- [ ] speaker diarization on the "them" stream (`diarize=true`)
- [ ] end-of-session summary written to `summary.md` on quit
- [ ] adjustable system-prompt presets (standup / customer call / 1:1)
- [ ] global hotkey via evdev so it works without TUI focus
- [ ] sounddevice fallback if parec missing
- [ ] device picker UI (instead of always default sink/source)
- [ ] redact transcript before sending to Claude (PII scrubber)
- [ ] history browser: search/filter sessions; in-transcript text search; auto-scroll transcript with playback (follow mode)

## Deployment
Currently installed via `uv tool install /home/jaeho/projects/meeting-copilot` (reinstall with `--reinstall` after edits). PKGBUILD exists but unused day-to-day.

- [ ] push repo to GitHub (prereq for everything below)
- [ ] convert `PKGBUILD` to a `meeting-copilot-git` variant: `source=("git+https://github.com/...git")`, add `pkgver()` reading from git tags
- [ ] publish to AUR so other Arch machines can `paru -S meeting-copilot-git`
- [ ] README with install paths (uv tool, AUR, manual) + brief.md syntax
- [ ] tag releases (`v0.2.0`...) so the non-`-git` PKGBUILD variant can pin to a stable version
- [ ] cross-platform audio (macOS BlackHole, Windows WASAPI loopback) — only if the audience grows beyond Linux
