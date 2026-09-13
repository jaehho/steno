# steno - listens to your meetings, hands you notes and todos.

.PHONY: help run listen test lint sources watch venv link unlink desktop undesktop \
	hypr unhypr autostart unautostart waybar unwaybar \
	install uninstall package aur status clean

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
	@mkdir -p $(HOME)/.local/bin
	@printf '#!/bin/sh\n# steno, run from the working checkout.\n# Not `uv tool install`: that venv is isolated and can never see the system\n# python-gobject the GTK window needs. This one is --system-site-packages.\nexec %s/.venv/bin/steno "$$@"\n' "$(CURDIR)" > $(HOME)/.local/bin/steno
	@chmod +x $(HOME)/.local/bin/steno
	@echo "linked $(HOME)/.local/bin/steno -> $(CURDIR)"

unlink:          ## remove that wrapper
	rm -f $(HOME)/.local/bin/steno

desktop:         ## launcher entry, so rofi and friends can find it
	install -Dm644 packaging/dev.jaeho.Steno.desktop \
		$(HOME)/.local/share/applications/dev.jaeho.Steno.desktop
	install -Dm644 src/steno/data/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg \
		$(HOME)/.local/share/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg
	@update-desktop-database $(HOME)/.local/share/applications 2>/dev/null || true
	@echo "installed; rofi should list Steno"

undesktop:       ## remove the launcher entry
	rm -f $(HOME)/.local/share/applications/dev.jaeho.Steno.desktop \
		$(HOME)/.local/share/icons/hicolor/scalable/apps/dev.jaeho.Steno.svg

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

install:         ## build and install the Arch package (asks for sudo)
	cd packaging && paru -Bi .

uninstall:       ## remove the Arch package
	sudo pacman -Rns steno

package:         ## build the Arch package without installing
	cd packaging && makepkg -f

aur:             ## regenerate the AUR package's .SRCINFO
	cd packaging/aur/steno-git && makepkg --printsrcinfo > .SRCINFO

status:          ## is it running, and what does the bar say
	@uv run steno status
	@printf 'bar:      '; uv run steno bar --once

watch:           ## print apps as they open the mic, to tune --allow/--deny
	uv run steno watch

sources:         ## what's available to capture
	@echo "== default mic =="; pactl get-default-source
	@echo "== default sink (system audio comes from <sink>.monitor) =="; pactl get-default-sink
	@echo "== all sources =="; pactl list short sources
	@echo "== echo canceller in front of the mic (dotfiles: audio package) =="; \
	  pw-dump | grep -q '"node.name": "echo_cancel"' && echo installed || echo "not installed"

clean:
	rm -rf build dist src/*.egg-info **/__pycache__ *.pkg.tar.zst packaging/{src,pkg}
