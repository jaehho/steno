.PHONY: install run sources install-tool reinstall-tool package install-pkg clean

install:
	uv sync

run:
	uv run copilot

# Install (or reinstall) `copilot` globally via uv tool, so you can run it from any cwd.
install-tool:
	uv tool install .

reinstall-tool:
	uv tool install --reinstall .

# Build the Arch package (.pkg.tar.zst) without installing.
package:
	makepkg -f

# Build and install via pacman. Removable with `sudo pacman -R meeting-copilot`.
install-pkg:
	makepkg -si

# Inspect available audio sources so you know what's being captured.
sources:
	@echo "== default mic =="; pactl get-default-source
	@echo "== default sink (system audio comes from <sink>.monitor) =="; pactl get-default-sink
	@echo "== all sources =="; pactl list short sources

clean:
	rm -rf .venv __pycache__ *.egg-info src/*.egg-info src/**/__pycache__
