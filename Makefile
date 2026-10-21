# steno - listens to your meetings, hands you notes and todos.

.PHONY: help run listen test lint sources watch venv link unlink desktop undesktop \
	hypr unhypr autostart unautostart waybar unwaybar \
	install uninstall package aur release status clean

PREFIX  ?= $(HOME)/.local
DATADIR ?= $(or $(XDG_DATA_HOME),$(HOME)/.local/share)

help:            ## this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	  | sed 's/:.*## /\t/' | expand -t14

run:             ## the window, from the source tree
	uv run steno gui

listen:          ## background listener, no window (what autostart runs)
	uv run steno gui --background

test:            ## headless suite - no audio, no network, no display
	uv run python -m unittest discover -s tests -t . -v

lint:            ## ruff
	uv run ruff check src tests packaging

venv:            ## (re)create .venv - needed after moving the checkout
	@rm -rf .venv
	@# The system interpreter, explicitly: `--system-site-packages` shares the
	@# site-packages of whichever python uv picks, and uv prefers its own
	@# managed build, which has neither python-gobject nor numpy.
	@uv venv --python /usr/bin/python --system-site-packages
	@uv sync
	@.venv/bin/python -c 'import gi; \
	  gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); \
	  gi.require_version("Gst", "1.0"); \
	  from gi.repository import Adw, Gst, Gtk; \
	  print("ok: window and playback are importable")'

link:            ## put `steno` on PATH, running from this checkout (no sudo)
	@mkdir -p $(PREFIX)/bin
	@# Written beside the destination and renamed over it: a redirect would
	@# follow a symlink there and empty what it points at.
	@t=$$(mktemp $(PREFIX)/bin/.steno.XXXXXX) && \
	trap 'rm -f "$$t"' EXIT && \
	printf '#!/bin/sh\n# steno, run from the working checkout.\n# Not `uv tool install`: that venv is isolated and can never see the system\n# python-gobject the GTK window needs. This one is --system-site-packages.\nexec "%s/.venv/bin/steno" "$$@"\n' "$(CURDIR)" > "$$t" && \
	chmod 755 "$$t" && \
	mv -f "$$t" $(PREFIX)/bin/steno
	@echo "linked $(PREFIX)/bin/steno -> $(CURDIR)"

unlink:          ## remove that wrapper
	rm -f $(PREFIX)/bin/steno

desktop:         ## launcher entry and icon, so rofi and friends can find it
	@# Exec= gets the absolute launcher path: a desktop launcher may not
	@# have $(PREFIX)/bin on its PATH.
	@mkdir -p $(DATADIR)/applications
	@t=$$(mktemp $(DATADIR)/applications/.steno.XXXXXX) && \
	trap 'rm -f "$$t"' EXIT && \
	sed 's#^Exec=steno #Exec=$(PREFIX)/bin/steno #' \
		packaging/dev.jaeho.Steno.desktop > "$$t" && \
	chmod 644 "$$t" && \
	mv -f "$$t" $(DATADIR)/applications/dev.jaeho.Steno.desktop
	install -Dm644 src/steno/data/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg \
		$(DATADIR)/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg
	@update-desktop-database $(DATADIR)/applications 2>/dev/null || true
	@# Without a refreshed theme cache the bar and the notification daemon
	@# miss the SVG and fall back to their error placeholders (waybar's
	@# circle-with-slash, swaync's stock paper image). `-t`: a user hicolor
	@# often has no index.theme of its own.
	@gtk-update-icon-cache -f -t $(DATADIR)/icons/hicolor 2>/dev/null || true
	@echo "installed; rofi should list Steno"

undesktop:       ## remove the launcher entry and icon
	rm -f $(DATADIR)/applications/dev.jaeho.Steno.desktop \
		$(DATADIR)/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg

hypr:            ## start listening with the Hyprland session (this machine)
	uv run python packaging/install-hypr.py

unhypr:          ## stop starting with the session
	uv run python packaging/install-hypr.py --remove

autostart:       ## start at login (same as the window menu's toggle)
	uv run steno autostart on

unautostart:     ## stop starting at login
	uv run steno autostart off
	rm -f $(HOME)/.config/autostart/dev.jaeho.MeetingCopilot.desktop

waybar:          ## add the recording indicator to waybar (backs up your config)
	uv run python packaging/install-waybar.py

unwaybar:        ## take the indicator back out
	uv run python packaging/install-waybar.py --remove

install:         ## live launcher and entry in ~/.local for this user (no sudo)
	@# The venv first: a failed build must not leave a launcher that points
	@# at nothing.
	@[ -x .venv/bin/steno ] || { echo "no .venv yet, building it"; $(MAKE) venv; }
	@$(MAKE) --no-print-directory link desktop

uninstall: unlink undesktop  ## remove them (config, sessions and notes stay)

package:         ## build the Arch package without installing
	cd packaging && makepkg -f

aur:             ## regenerate the AUR package's .SRCINFO
	cd packaging/aur/steno-git && makepkg --printsrcinfo > .SRCINFO

release:         ## tag v$(VERSION) so a non-`-git` package can pin a version
	@# The tag is the only thing a pinned PKGBUILD can point at. One per
	@# pyproject version, annotated so `git describe` is readable in --version.
	@V=$$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml); \
	if [ -z "$$V" ]; then echo "no version in pyproject.toml"; exit 1; fi; \
	if git rev-parse -q --verify "refs/tags/v$$V" >/dev/null; then \
	  echo "v$$V is already tagged ($$(git rev-parse --short v$$V))"; \
	else \
	  git tag -a "v$$V" -m "steno $$V"; \
	  echo "tagged v$$V — push it with: git push origin v$$V"; \
	fi
	@echo "keep packaging/PKGBUILD pkgver in step with pyproject.toml"

status:          ## is it running, and what does the bar say
	@uv run steno status
	@printf 'bar:      '; uv run steno bar --once

watch:           ## print apps as they open the mic, to tune --allow/--deny
	uv run steno watch

sources:         ## what's available to capture
	@echo "== default mic =="; pactl get-default-source
	@echo "== default sink (system audio comes from <sink>.monitor) =="; pactl get-default-sink
	@echo "== all sources =="; pactl list short sources

clean:
	rm -rf build dist src/*.egg-info **/__pycache__ *.pkg.tar.zst packaging/{src,pkg}
