"""What you can do to one meeting, as a menu.

The header's ⋯ button and a right-click on a row in the sidebar open the
same menu, so it is defined here, once. Every item is an action in the
`meeting.` group (`MeetingPage.actions`), and those act on the meeting
that is open: a row has to be opened before its menu is shown.
"""
from __future__ import annotations

from gi.repository import Gio


def meeting_menu_model(
    archived: bool = False,
    has_notes: bool = False,
    has_previous: bool = False,
) -> Gio.Menu:
    """The menu of a meeting. It offers to put the meeting away or to
    bring it back, by whether it is put away: one item, never both. It
    offers its pages in the project's notes only when it has some, and
    joining with the meeting before only when there is one."""
    menu = Gio.Menu()
    menu.append("Copy this meeting", "meeting.copy")
    menu.append("Save this meeting...", "meeting.save")
    menu.append("Set project...", "meeting.project")
    if has_notes:
        menu.append("Show in project notes", "meeting.show-notes")
    menu.append("Summarize again...", "meeting.summarize")
    menu.append("Re-time against the audio...", "meeting.realign")
    if has_previous:
        menu.append("Join with the meeting before this…", "meeting.merge")
    if archived:
        menu.append("Unarchive", "meeting.unarchive")
    else:
        menu.append("Archive", "meeting.archive")
    danger = Gio.Menu()
    danger.append("Delete the audio, keep the text...", "meeting.delete-audio")
    danger.append("Delete this meeting...", "meeting.delete")
    menu.append_section(None, danger)
    return menu


def menu_actions(menu: Gio.MenuModel) -> list[str]:
    """Every action name in a menu, sections included, in order."""
    names = []
    for i in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(i, "action", None)
        if action is not None:
            names.append(action.get_string())
        section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
        if section is not None:
            names.extend(menu_actions(section))
    return names
