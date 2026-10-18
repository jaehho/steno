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


def stylesheet(colors: dict[str, str], transcript_pt: int = TRANSCRIPT_PT_DEFAULT) -> str:
    """One stylesheet for the whole window.

    Speaker colour is deliberately never the only signal — every transcript line
    carries a text label too, so the split survives a colourblind reader and a
    grayscale screenshot alike.
    """
    return f"""
    /* ---- libadwaita's surfaces, in the desktop palette ---- */
    @define-color window_bg_color {colors['crust']};
    @define-color window_fg_color {colors['text']};
    @define-color view_bg_color {colors['base']};
    @define-color view_fg_color {colors['text']};
    @define-color headerbar_bg_color {colors['base']};
    @define-color headerbar_fg_color {colors['text']};
    @define-color headerbar_border_color {colors['surface0']};
    @define-color headerbar_shade_color {colors['surface0']};
    @define-color sidebar_bg_color {colors['mantle']};
    @define-color sidebar_fg_color {colors['text']};
    @define-color sidebar_border_color {colors['surface0']};
    @define-color popover_bg_color {colors['surface0']};
    @define-color popover_fg_color {colors['text']};
    @define-color dialog_bg_color {colors['surface0']};
    @define-color dialog_fg_color {colors['text']};
    @define-color card_bg_color {colors['base']};
    @define-color card_fg_color {colors['text']};
    @define-color accent_bg_color {colors['teal']};
    @define-color accent_fg_color {colors['crust']};
    @define-color accent_color {colors['teal']};
    @define-color destructive_bg_color {colors['red']};
    @define-color destructive_fg_color {colors['crust']};
    @define-color destructive_color {colors['red']};

    window.steno {{ background: {colors['crust']}; }}
    .page {{ background: {colors['base']}; }}

    /* ---- words around the content: small, plain, sentence case ---- */
    .sec {{ font-weight: 600; font-size: 10pt; color: {colors['overlay2']}; }}
    .hint {{ font-size: 9pt; color: {colors['overlay1']}; }}
    .owed-item {{ font-weight: normal; }}
    .mono {{ font-family: {DATA}; }}
    .count {{ font-size: 9pt; font-weight: 600; color: {colors['peach']}; }}
    .page-title {{ font-weight: 700; }}
    .page-subtitle {{ font-size: 9pt; color: {colors['overlay1']}; }}

    /* ---- recording state: colour plus a word, never colour alone ---- */
    .status-card {{
        background: {colors['base']};
        border-radius: 8px;
        padding: 10px;
    }}
    .status-card.state-recording {{ box-shadow: inset 0 0 0 1px alpha({colors['red']}, 0.4); }}
    .state-dot {{ font-size: 11pt; }}
    .state-recording .state-dot {{ color: {colors['red']}; }}
    .state-idle      .state-dot {{ color: {colors['overlay0']}; }}
    .state-paused    .state-dot {{ color: {colors['yellow']}; }}
    .state-finalizing .state-dot {{ color: {colors['blue']}; }}
    .status-title {{ font-weight: 600; }}

    /* ---- the meeting list ---- */
    .session-title {{ font-weight: 600; }}
    .session-untitled {{ color: {colors['overlay1']}; font-style: italic; }}
    .session-meta {{ font-size: 9pt; color: {colors['overlay1']}; }}
    .list-section {{
        font-weight: 600; font-size: 10pt; color: {colors['overlay2']};
        margin: 10px 10px 2px 10px;
    }}
    /* Put away, and still there: dimmer, never struck through. */
    .session-archived {{ opacity: 0.6; }}
    .archive-toggle {{ margin-top: 8px; }}

    /* ---- transcripts ---- */
    .transcript-live {{
        font-family: {READING};
        font-size: {transcript_pt}pt;
        line-height: 1.55;
        color: {colors['text']};
    }}
    .transcript-past {{ font-size: 12pt; line-height: 1.55; color: {colors['text']}; }}
    .transcript-live, .transcript-past, list.transcript-past {{ background: transparent; }}
    .transcript-line {{ padding: 3px 0; }}
    .speaker-tag {{
        font-size: 9pt;
        color: {colors['overlay1']};
        margin-right: 10px;
        margin-top: 4px;
    }}
    .from-them .speaker-tag {{ color: {colors['sapphire']}; }}
    .from-you  .speaker-tag {{ color: {colors['overlay1']}; }}
    .from-you  {{ color: {colors['subtext0']}; }}

    /* The recorded transcript doubles as a tape deck: every line is a seek
       point, so the line being played has to be findable at a glance. */
    .line-stamp {{
        font-family: {DATA};
        font-size: 9pt;
        color: {colors['overlay0']};
        margin-right: 12px;
        margin-top: 4px;
    }}
    list.transcript-past row {{ border-radius: 6px; padding: 2px 8px; }}
    list.transcript-past row:hover {{ background: {colors['surface0']}; }}
    list.transcript-past row.line-playing {{
        background: {colors['surface1']};
        box-shadow: inset 2px 0 0 {colors['teal']};
    }}

    /* Where a summary item was said. One hue (yellow, a highlighter) joins
       the item to the lines, and it is not the teal of the line being
       played, so the two read apart when they land on the same row. */
    list.transcript-past row.line-cited {{
        background: alpha({colors['yellow']}, 0.18);
        box-shadow: inset 2px 0 0 {colors['yellow']};
    }}
    list.transcript-past row.line-cited:hover {{
        background: alpha({colors['yellow']}, 0.26);
    }}
    list.transcript-past row.line-cited.line-playing {{
        background: {colors['surface1']};
        box-shadow: inset 2px 0 0 {colors['teal']};
    }}

    /* Interim text is provisional and says so by looking unfinished. */
    .interim {{ color: {colors['overlay1']}; font-style: italic; }}

    .empty-hint {{ font-size: 12pt; color: {colors['overlay1']}; }}

    /* ---- the side column beside a meeting, and the stacked page ---- */
    .side-column {{ background: {colors['crust']}; }}
    .side-column.below {{ border-top: 1px solid {colors['surface0']}; }}
    .side-column.beside {{ border-left: 1px solid {colors['surface0']}; }}

    /* ---- notes: a plain sheet of paper ---- */
    .notes-frame {{ background: {colors['base']}; border-radius: 6px; }}
    .stacked .notes-frame {{ background: {colors['crust']}; }}
    .notes, .notes text {{
        font-size: 12pt;
        line-height: 1.5;
        color: {colors['text']};
        background: transparent;
    }}
    .notes {{ padding: 8px 12px; }}

    /* ---- asking: present when asked for, never shouting ---- */
    .advice {{
        font-size: 11.5pt;
        line-height: 1.5;
        color: {colors['subtext1']};
        background: {colors['surface0']};
        border-radius: 6px;
        padding: 10px 12px;
    }}
    .advice-thinking {{ color: {colors['overlay1']}; font-style: italic; }}
    .stacked entry.ask {{ background: {colors['crust']}; }}

    /* ---- a past meeting ---- */
    .summary {{ font-size: 11.5pt; line-height: 1.6; color: {colors['text']}; }}
    .summary-item {{ padding: 2px 8px; border-radius: 6px; }}
    .cited-item:hover {{ background: alpha({colors['yellow']}, 0.10); }}
    .cited-item:focus-visible {{
        outline: 2px solid alpha({colors['yellow']}, 0.7);
        outline-offset: -2px;
    }}
    .cited-item.item-active, .item-active {{
        background: alpha({colors['yellow']}, 0.14);
        box-shadow: inset 2px 0 0 {colors['yellow']};
    }}
    /* The places a lit item points at, docked above the player. */
    .cite-bar {{
        background: {colors['mantle']};
        border-top: 1px solid {colors['surface0']};
        padding: 6px 12px 8px 12px;
    }}
    .cite-bar button.place {{ padding: 3px 8px; min-height: 0; border-radius: 8px; }}
    .cite-bar button.place.place-current {{
        background: alpha({colors['yellow']}, 0.16);
        box-shadow: inset 2px 0 0 {colors['yellow']};
    }}
    .place-stamp {{ font-family: {DATA}; font-size: 9pt; color: {colors['overlay2']}; }}
    .place-who {{ font-size: 9pt; color: {colors['overlay2']}; }}
    .todo-done {{ color: {colors['overlay0']}; text-decoration: line-through; }}
    .owner-you  {{ color: {colors['teal']}; }}
    .owner-them {{ color: {colors['overlay2']}; }}
    .todo-list {{ color: {colors['overlay0']}; }}

    .player-bar {{
        background: {colors['mantle']};
        border-top: 1px solid {colors['surface0']};
        padding: 8px 12px;
    }}
    .player-bar scale trough highlight {{ background: {colors['teal']}; }}

    .error {{ color: {colors['maroon']}; font-size: 10pt; }}
    """
