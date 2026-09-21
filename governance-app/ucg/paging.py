"""Pagination helpers with the two Unity Catalog rules that bite.

1. A page may contain **zero results and still carry a next_page_token**.
   Stopping on an empty page silently truncates a grant inventory.
2. On the permissions endpoints ``max_results`` has a forbidden band: 0 means
   "server default", values of 150 or more are accepted, and anything from 1 to
   149 is *rejected*, not clamped.

Both rules are encoded here so no call site has to remember them.
"""
from __future__ import annotations

from typing import Callable, Iterator, TypeVar

from .errors import Code, GovernanceError

T = TypeVar("T")

#: Smallest legal non-zero page size on the permissions endpoints.
MIN_PERMISSIONS_PAGE = 150
#: ``max_results=0`` asks Databricks for its own default page size.
SERVER_DEFAULT = 0


class Completeness:
    """Whether a listing is the whole truth."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    PARTIAL_PERMISSION = "partial_permission"

    LABELS = {
        COMPLETE: "Đầy đủ trong phạm vi quyền của danh tính thực thi",
        TRUNCATED: "Đã cắt bớt vì vượt giới hạn số trang",
        PARTIAL_PERMISSION: "Thiếu dữ liệu do không đủ quyền",
    }


def permissions_page_size(requested: int | None) -> int:
    """Coerce a page size into something the permissions endpoints accept."""
    if not requested:
        return SERVER_DEFAULT
    if requested < MIN_PERMISSIONS_PAGE:
        return MIN_PERMISSIONS_PAGE
    return requested


def collect_pages(
    fetch: Callable[[str | None], tuple[list[T], str | None]],
    *,
    max_pages: int = 200,
) -> tuple[list[T], str]:
    """Drain a token-paginated endpoint.

    ``fetch`` takes the current page token and returns ``(items, next_token)``.
    Returns the accumulated items plus a :class:`Completeness` verdict, so a
    caller can tell the user "this list was cut short" instead of presenting a
    truncated list as the full picture.
    """
    items: list[T] = []
    token: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        page, token = fetch(token)
        # An empty page is legal and does NOT mean the listing is finished.
        items.extend(page or [])
        if not token:
            return items, Completeness.COMPLETE
        if token in seen:
            raise GovernanceError(
                Code.UPSTREAM_ERROR,
                "Databricks trả về page token lặp lại; kết quả có thể chưa đầy đủ.",
            )
        seen.add(token)
    return items, Completeness.TRUNCATED


def take(iterator: Iterator[T], limit: int) -> tuple[list[T], bool]:
    """Read at most ``limit`` items from an SDK iterator.

    Returns the items and whether more remained. SDK iterators page lazily, so
    this is how a screen stays responsive on a large metastore without
    pretending it showed everything.
    """
    out: list[T] = []
    for item in iterator:
        if len(out) >= limit:
            return out, True
        out.append(item)
    return out, False
