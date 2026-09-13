"""Palette and stylesheet.

The window borrows the desktop's own Solarized-OKLCH palette rather than
shipping its own, so it sits at the same hue as waybar and the rest of the
session instead of announcing itself as a separate application. That includes
libadwaita's own surfaces: the header bars, sidebar, popovers and accent are
recoloured through its named colours, so no stock grey is left clashing with
the palette.

Two faces, two jobs. The transcript is set large in a reading face because it is
the accessibility surface of this tool in the literal sense — it is captioning,
read at a glance by someone who is mid-sentence. Times and offsets are set in
mono so they line up; every label is a plain sentence-case word.
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
    /* ---- libadwaita's surfaces, in the desktop palette ---- */
    @define-color window_bg_color {C['crust']};
    @define-color window_fg_color {C['text']};
    @define-color view_bg_color {C['base']};
    @define-color view_fg_color {C['text']};
    @define-color headerbar_bg_color {C['base']};
    @define-color headerbar_fg_color {C['text']};
    @define-color headerbar_border_color {C['surface0']};
    @define-color headerbar_shade_color {C['surface0']};
    @define-color sidebar_bg_color {C['mantle']};
    @define-color sidebar_fg_color {C['text']};
    @define-color sidebar_border_color {C['surface0']};
    @define-color popover_bg_color {C['surface0']};
    @define-color popover_fg_color {C['text']};
    @define-color dialog_bg_color {C['surface0']};
    @define-color dialog_fg_color {C['text']};
    @define-color card_bg_color {C['base']};
    @define-color card_fg_color {C['text']};
    @define-color accent_bg_color {C['teal']};
    @define-color accent_fg_color {C['crust']};
    @define-color accent_color {C['teal']};
    @define-color destructive_bg_color {C['red']};
    @define-color destructive_fg_color {C['crust']};
    @define-color destructive_color {C['red']};

    window.steno {{ background: {C['crust']}; }}
    .page {{ background: {C['base']}; }}

    /* ---- words around the content: small, plain, sentence case ---- */
    .sec {{ font-weight: 600; font-size: 10pt; color: {C['overlay2']}; }}
    .hint {{ font-size: 9pt; color: {C['overlay1']}; }}
    .mono {{ font-family: {DATA}; }}
    .count {{ font-size: 9pt; font-weight: 600; color: {C['peach']}; }}
    .page-title {{ font-weight: 700; }}
    .page-subtitle {{ font-size: 9pt; color: {C['overlay1']}; }}

    /* ---- recording state: colour plus a word, never colour alone ---- */
    .status-card {{
        background: {C['base']};
        border-radius: 8px;
        padding: 10px;
    }}
    .status-card.state-recording {{ box-shadow: inset 0 0 0 1px alpha({C['red']}, 0.4); }}
    .state-dot {{ font-size: 11pt; }}
    .state-recording .state-dot {{ color: {C['red']}; }}
    .state-idle      .state-dot {{ color: {C['overlay0']}; }}
    .state-paused    .state-dot {{ color: {C['yellow']}; }}
    .state-finalizing .state-dot {{ color: {C['blue']}; }}
    .status-title {{ font-weight: 600; }}

    /* ---- the meeting list ---- */
    .session-title {{ font-weight: 600; }}
    .session-untitled {{ color: {C['overlay1']}; font-style: italic; }}
    .session-meta {{ font-size: 9pt; color: {C['overlay1']}; }}
    .list-section {{
        font-weight: 600; font-size: 10pt; color: {C['overlay2']};
        margin: 10px 10px 2px 10px;
    }}

    /* ---- transcripts ---- */
    .transcript-live {{
        font-family: {READING};
        font-size: {transcript_pt}pt;
        line-height: 1.55;
        color: {C['text']};
    }}
    .transcript-past {{ font-size: 12pt; line-height: 1.55; color: {C['text']}; }}
    .transcript-live, .transcript-past, list.transcript-past {{ background: transparent; }}
    .transcript-line {{ padding: 3px 0; }}
    .speaker-tag {{
        font-size: 9pt;
        color: {C['overlay1']};
        margin-right: 10px;
        margin-top: 4px;
    }}
    .from-them .speaker-tag {{ color: {C['sapphire']}; }}
    .from-you  .speaker-tag {{ color: {C['overlay1']}; }}
    .from-you  {{ color: {C['subtext0']}; }}

    /* The recorded transcript doubles as a tape deck: every line is a seek
       point, so the line being played has to be findable at a glance. */
    .line-stamp {{
        font-family: {DATA};
        font-size: 9pt;
        color: {C['overlay0']};
        margin-right: 12px;
        margin-top: 4px;
    }}
    list.transcript-past row {{ border-radius: 6px; padding: 2px 8px; }}
    list.transcript-past row:hover {{ background: {C['surface0']}; }}
    list.transcript-past row.line-playing {{
        background: {C['surface1']};
        box-shadow: inset 2px 0 0 {C['teal']};
    }}

    /* Interim text is provisional and says so by looking unfinished. */
    .interim {{ color: {C['overlay1']}; font-style: italic; }}

    .empty-hint {{ font-size: 12pt; color: {C['overlay1']}; }}

    /* ---- the side column beside a meeting, and the stacked page ---- */
    .side-column {{ background: {C['crust']}; }}
    .side-column.below {{ border-top: 1px solid {C['surface0']}; }}
    .side-column.beside {{ border-left: 1px solid {C['surface0']}; }}

    /* ---- notes: a plain sheet of paper ---- */
    .notes-frame {{ background: {C['base']}; border-radius: 6px; }}
    .stacked .notes-frame {{ background: {C['crust']}; }}
    .notes, .notes text {{
        font-size: 12pt;
        line-height: 1.5;
        color: {C['text']};
        background: transparent;
    }}
    .notes {{ padding: 8px 12px; }}

    /* ---- asking: present when asked for, never shouting ---- */
    .advice {{
        font-size: 11.5pt;
        line-height: 1.5;
        color: {C['subtext1']};
        background: {C['surface0']};
        border-radius: 6px;
        padding: 10px 12px;
    }}
    .advice-thinking {{ color: {C['overlay1']}; font-style: italic; }}
    .stacked entry.ask {{ background: {C['crust']}; }}

    /* ---- a past meeting ---- */
    .summary {{ font-size: 11.5pt; line-height: 1.6; color: {C['text']}; }}
    .todo-done {{ color: {C['overlay0']}; text-decoration: line-through; }}
    .owner-you  {{ color: {C['teal']}; }}
    .owner-them {{ color: {C['overlay2']}; }}

    .player-bar {{
        background: {C['mantle']};
        border-top: 1px solid {C['surface0']};
        padding: 8px 12px;
    }}
    .player-bar scale trough highlight {{ background: {C['teal']}; }}

    .error {{ color: {C['maroon']}; font-size: 10pt; }}
    """
