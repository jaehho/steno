.PHONY: install run sources clean

install:
	uv sync

run:
	uv run python copilot.py

# Inspect available audio sources so you know what's being captured.
sources:
	@echo "== default mic =="; pactl get-default-source
	@echo "== default sink (system audio comes from <sink>.monitor) =="; pactl get-default-sink
	@echo "== all sources =="; pactl list short sources

clean:
	rm -rf .venv __pycache__ *.egg-info
