# steno - listens to your meetings, hands you notes and todos.

.PHONY: help run listen test lint sources watch link unlink desktop undesktop \
	hypr unhypr autostart unautostart waybar unwaybar echo-cancel unecho-cancel \
	install uninstall package status clean

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
	@update-desktop-database $(HOME)/.local/share/applications 2>/dev/null || true
	@echo "installed; rofi should list Steno"

undesktop:       ## remove the launcher entry
	rm -f $(HOME)/.local/share/applications/dev.jaeho.Steno.desktop

hypr:            ## start listening with the Hyprland session (this machine)
	uv run python packaging/install-hypr.py

unhypr:          ## stop starting with the session
	uv run python packaging/install-hypr.py --remove

autostart:       ## XDG autostart entry, for sessions that read ~/.config/autostart
	install -Dm644 packaging/steno-autostart.desktop \
		$(HOME)/.config/autostart/steno.desktop
	@echo "linked; note Hyprland does not read this - use `make hypr` there"

unautostart:     ## stop starting at login
	rm -f $(HOME)/.config/autostart/steno.desktop \
		$(HOME)/.config/autostart/dev.jaeho.MeetingCopilot.desktop

echo-cancel:     ## ask pipewire for an echo-cancelled mic (for meetings on speakers)
	uv run python packaging/install-echo-cancel.py

unecho-cancel:   ## remove it; the microphone goes back to unfiltered
	uv run python packaging/install-echo-cancel.py --remove

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

status:          ## is it running, and what does the bar say
	@uv run steno status
	@printf 'bar:      '; uv run steno bar --once

watch:           ## print apps as they open the mic, to tune --allow/--deny
	uv run steno watch

sources:         ## what's available to capture
	@echo "== default mic =="; pactl get-default-source
	@echo "== default sink (system audio comes from <sink>.monitor) =="; pactl get-default-sink
	@echo "== all sources =="; pactl list short sources
	@echo "== echo-cancelled mic (make echo-cancel) =="; \
	  pactl list short sources | grep steno_echo_cancel || echo "not installed"

clean:
	rm -rf build dist src/*.egg-info **/__pycache__ *.pkg.tar.zst packaging/{src,pkg}
