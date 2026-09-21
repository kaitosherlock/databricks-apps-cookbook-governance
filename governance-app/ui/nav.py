"""Page registry.

``st.switch_page`` needs the page object, not a path, so the objects are
registered at startup and looked up lazily by key. That keeps pages free to
navigate to each other without importing one another.
"""
from __future__ import annotations

import streamlit as st

_PAGES: dict[str, object] = {}


def register(key: str, page):
    _PAGES[key] = page
    return page


def page(key: str):
    return _PAGES.get(key)


def goto(key: str):
    """Navigate, or do nothing if the page is not part of this build."""
    target = _PAGES.get(key)
    if target is not None:
        st.switch_page(target)


def link_button(label: str, key: str, *, icon: str = "", width: str = "content",
                widget_key: str = "", disabled: bool = False, help: str = ""):
    if st.button(label, key=widget_key or f"nav_{key}_{label}", icon=icon or None,
                 width=width, disabled=disabled, help=help or None):
        goto(key)
