"""Palette and stylesheet.

The window borrows the desktop's own Solarized-OKLCH palette rather than
shipping its own, so it sits at the same hue as waybar and the rest of the
session instead of announcing itself as a separate application.

Two faces, two jobs. The transcript is set large in a reading face because it is
the accessibility surface of this tool in the literal sense — it is captioning,
read at a glance by someone who is mid-sentence. Everything structural around it
is tracked mono, small and quiet, because it is chrome and must not compete.
"""
from __future__ import annotations

from pathlib import Path

PALETTE = Path.home() / ".config/theme/palette"

READING = '"Adwaita Sans", "Inter", sans-serif'
DATA = '"JetBrainsMono NF", "JetBrainsMono Nerd Font", monospace'

# The live transcript's type scale, in points. Generous by default and moved by
# ctrl +/- ; a glance has to land without leaning in.
TRANSCRIPT_PT_DEFAULT = 15
TRANSCRIPT_PT_MIN = 11
TRANSCRIPT_PT_MAX = 28

FALLBACK = {
    "base": "#141c1e", "mantle": "#101719", "crust": "#0c1315",
    "text": "#cfdadc", "subtext1": "#b6c0c2", "subtext0": "#9da7a9",
    "surface0": "#20282a", "surface1": "#2f3739", "surface2": "#41494c",
    "overlay0": "#565f62", "overlay1": "#6d7679", "overlay2": "#848e91",
    "blue": "#3f7dae", "teal": "#118a7a", "green": "#4f8751",
    "yellow": "#8e7426", "peach": "#a6653e", "maroon": "#aa5d64",
    "red": "#ab5e59", "mauve": "#7c6bac", "sapphire": "#00898f",
}


def palette() -> dict[str, str]:
    """The desktop's palette, falling back to a copy of it when absent."""
    out = dict(FALLBACK)
    try:
        for line in PALETTE.read_text().splitlines():
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key and not key.startswith("#"):
                out[key] = "#" + value.strip()
    except OSError:
        pass
    return out


def stylesheet(C: dict[str, str], transcript_pt: int = TRANSCRIPT_PT_DEFAULT) -> str:
    """One stylesheet for the whole window.

    Speaker colour is deliberately never the only signal — every transcript line
    carries a text label too, so the split survives a colourblind reader and a
    grayscale screenshot alike.
    """
    return f"""
    window.steno {{ background: {C['crust']}; }}

    /* ---- chrome: quiet, tracked, out of the way ---- */
    .chrome {{
        font-family: {DATA};
        font-size: 10pt;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: {C['overlay2']};
    }}
    .hairline {{ background: {C['surface0']}; min-height: 1px; }}

    /* ---- recording state: colour plus a word, never colour alone ---- */
    .state-dot {{ font-size: 13pt; }}
    .state-recording .state-dot {{ color: {C['red']}; }}
    .state-idle      .state-dot {{ color: {C['overlay0']}; }}
    .state-paused    .state-dot {{ color: {C['yellow']}; }}
    .state-finalizing .state-dot {{ color: {C['blue']}; }}

    /* ---- the live transcript: the one thing sized to be read ---- */
    .transcript {{
        font-family: {READING};
        font-size: {transcript_pt}pt;
        line-height: 1.55;
        color: {C['text']};
        background: {C['base']};
    }}
    .transcript-line {{ padding: 3px 0; }}
    .speaker-tag {{
        font-family: {DATA};
        font-size: 9pt;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: {C['overlay1']};
        margin-right: 10px;
    }}
    .from-them .speaker-tag {{ color: {C['sapphire']}; }}
    .from-you  .speaker-tag {{ color: {C['overlay1']}; }}
    .from-you  {{ color: {C['subtext0']}; }}

    /* The archive's transcript doubles as a tape deck: every line is a seek
       point, so the line being played has to be findable at a glance. */
    .line-stamp {{
        font-family: {DATA};
        font-size: 9pt;
        color: {C['overlay0']};
        margin-right: 10px;
    }}
    .transcript row {{ border-radius: 4px; }}
    .transcript row:hover {{ background: {C['surface0']}; }}
    row.line-playing {{
        background: {C['surface1']};
        box-shadow: inset 2px 0 0 {C['teal']};
    }}

    /* Interim text is provisional and says so by looking unfinished. */
    .interim {{ color: {C['overlay1']}; font-style: italic; }}

    .empty-hint {{
        font-family: {READING};
        font-size: 12pt;
        color: {C['overlay0']};
    }}

    /* ---- notes: a plain sheet of paper, no chrome ---- */
    .notes, .notes text {{
        font-family: {READING};
        font-size: 12pt;
        line-height: 1.5;
        color: {C['text']};
        background: {C['base']};
    }}
    .notes {{ padding: 10px 12px; }}

    /* ---- advice: present when asked for, never shouting ---- */
    .advice {{
        font-family: {READING};
        font-size: 11.5pt;
        line-height: 1.5;
        color: {C['subtext1']};
        background: {C['surface0']};
        border-left: 2px solid {C['teal']};
        padding: 10px 12px;
    }}
    .advice-thinking {{ color: {C['overlay1']}; font-style: italic; }}

    /* ---- archive ---- */
    .session-title {{
        font-family: {READING};
        font-size: 12pt;
        font-weight: 600;
        color: {C['text']};
    }}
    .session-untitled {{ color: {C['overlay1']}; font-style: italic; }}
    .session-meta {{
        font-family: {DATA};
        font-size: 9pt;
        color: {C['overlay1']};
    }}
    .todo-badge {{
        font-family: {DATA};
        font-size: 9pt;
        color: {C['peach']};
    }}
    .summary {{
        font-family: {READING};
        font-size: 11.5pt;
        line-height: 1.6;
        color: {C['text']};
    }}
    .todo-done {{ color: {C['overlay0']}; text-decoration: line-through; }}
    .owner-you  {{ color: {C['teal']}; }}
    .owner-them {{ color: {C['overlay2']}; }}

    .error {{ color: {C['maroon']}; font-family: {DATA}; font-size: 10pt; }}
    """
