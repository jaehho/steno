# steno

README.md covers install, use and how it works; PRODUCT.md is the design brief for the GTK window (impeccable) and its promises (recording state always visible, guidance pulled, never pushed).

- Tasks, gotchas and decisions live in GitHub issues (labels `gotcha`, `decision`). Search before changing behavior: `gh issue list --label gotcha --state all --search <term>` (or `--label decision`). Never add TODO.md, ISSUES.md or DESIGN_LOG.md.
- GUI checks run on GTK Broadway with `WAYLAND_DISPLAY` and `DISPLAY` unset; never present a window on the real display, it steals focus. Recipe: `start_broadway` in `tests/test_sidebar_menu.py`.
