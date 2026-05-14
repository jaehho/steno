# meeting-copilot

## v0 (MVP) — done
- [x] dual audio capture via parec (default mic + default sink monitor)
- [x] two Deepgram WebSocket streams, speaker-tagged
- [x] textual TUI: split transcript + advisor pane
- [x] Ctrl+G → Claude advisor with rolling context

## Next
- [ ] show interim (gray) transcripts that get replaced by finals
- [ ] auto-trigger advisor when a question is detected from "them"
- [ ] adjustable system-prompt presets (standup / customer call / 1:1)
- [ ] persist session transcript + advisor turns to JSON
- [ ] global hotkey via evdev so it works without TUI focus
- [ ] sounddevice fallback if parec missing
- [ ] device picker UI (instead of always default sink/source)
- [ ] redact transcript before sending to Claude (PII scrubber)
