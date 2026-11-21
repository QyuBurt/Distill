"""The Convert hub: which converter pages there are, how they are grouped, and what each one turns into what.

The pages themselves are unchanged and keep their keys (chd, cso, ...); the hub is only a way in. No Qt.
"""
from __future__ import annotations

from .orgpages import ORG_PAGES

# (group title, ((page key, the tool that page needs), ...)): by the job, not the console, because CHD and ZAR serve several
GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Compress disc images", (("chd", "chdman"), ("cso", "maxcso"), ("rvz", "dolphin"), ("xbox", "xdvdfs"))),
    ("Unpack and decrypt", (("ps3", "ps3qdd"), ("pkg", "shadplus"), ("pkg5", "anyps5"))),
    ("Pack and archive", (("switch", "nscb"), ("zar", "zarchive"))),
)

# one line each: what goes in, what comes out
IN_OUT = {
    "chd": "CD and DVD images \u2192 .chd",
    "cso": "PSP ISOs \u2192 .cso",
    "rvz": "GameCube and Wii discs \u2192 .rvz",
    "xbox": "Xbox and Xbox 360 ISOs \u2192 .xiso",
    "ps3": "Encrypted PS3 ISOs \u2192 ISO or game folder",
    "pkg": "PS4 .pkg files \u2192 game folders",
    "pkg5": "PS5 .pkg files \u2192 AnyPS5 or KytyPS5",
    "switch": "NSP / XCI \u2194 NSZ / XCZ, or Switch 2 \u2192 .nup",
    "zar": "PS4, PS5 and Xbox 360 games \u2192 .zar",
}

TOOL_OF = {key: tool for _title, items in GROUPS for key, tool in items}


def page_keys() -> list[str]:
    """Every converter page the hub opens, in the order it shows them."""
    return [key for _title, items in GROUPS for key, _tool in items]


def organise_keys() -> list[str]:
    """The library organiser pages, in the order the sidebar lists them."""
    return [page.key for page in ORG_PAGES]


def all_tool_keys() -> list[str]:
    """Every page the hub opens: the converters, then the organisers."""
    return page_keys() + organise_keys()


# On a fresh install only the hub is pinned among these: the person pins the converters and organisers their collection needs (from
# the hub, or Settings > Sidebar). A settings file that already has pins keeps them.
DEFAULT_UNPINNED = frozenset(all_tool_keys())
