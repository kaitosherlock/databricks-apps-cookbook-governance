"""Shared presentation pieces.

Two rules run through everything here:

* nothing communicates by colour alone - every state carries an icon *and* a
  word, so the app works for a colour-blind reader and in a screenshot;
* an empty result is never rendered the same way as a permission problem or a
  failed load. :func:`empty_state` forces the caller to say which one it is.
"""
from __future__ import annotations

import csv
import io
import json

import streamlit as st

from ucg.errors import Code, GovernanceError

#: Codes that mean "you are not allowed", as opposed to "there is nothing here".
_PERMISSION_CODES = {Code.PERMISSION_DENIED, Code.FORBIDDEN_ROLE, Code.FORBIDDEN_SCOPE}


def page_title(title: str, subtitle: str = "", icon: str = ""):
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


def breadcrumb(parts: list[tuple[str, str]]):
    """Catalog / Schema / Object trail. ``parts`` is a list of (label, value)."""
    if not parts:
        return
    trail = "  ›  ".join(
        f"{label}: **{value}**" if value else f"{label}: —" for label, value in parts
    )
    st.caption(trail)


def copyable(label: str, value: str, key: str):
    """Show a full name with an easy way to copy it."""
    st.text_input(label, value=value, key=key, disabled=False,
                  help="Chọn ô và nhấn Ctrl+C để sao chép tên đầy đủ.")


def badge(text: str, icon: str = "", tone: str = "neutral"):
    """A label that reads correctly without colour."""
    prefix = f"{icon} " if icon else ""
    return f"{prefix}{text}"


def show_error(err: BaseException, *, context: str = ""):
    """Render a GovernanceError with a cause and a next step.

    Never shows a stack trace, a token, or an upstream response body.
    """
    if not isinstance(err, GovernanceError):
        from ucg.errors import translate
        err = translate(err)

    icon = ":material/lock:" if err.code in _PERMISSION_CODES else ":material/error:"
    if err.outcome_unknown:
        icon = ":material/help:"
        st.warning(f"**Chưa xác định được kết quả.** {err.message}", icon=icon)
    else:
        st.error(f"{context + ' ' if context else ''}{err.message}", icon=icon)

    if err.next_step:
        st.caption(err.next_step)
    st.caption(f"Mã tra cứu: `{err.correlation_id}`"
               + (f" · Loại: `{err.detail}`" if err.detail else ""))


def empty_state(kind: str, *, what: str = "mục", detail: str = ""):
    """Distinguish the three reasons a list can be blank.

    ``kind`` is one of ``empty``, ``forbidden``, ``failed`` or ``unconfigured``.
    """
    if kind == "empty":
        st.info(f"Không có {what} nào trong phạm vi này.", icon=":material/inbox:")
        st.caption(
            "Đây là kết quả rỗng do Databricks trả về cho danh tính thực thi — "
            "không phải bằng chứng rằng không tồn tại đối tượng nào."
        )
    elif kind == "forbidden":
        st.warning(
            f"Không đủ quyền để xem {what} ở đây.", icon=":material/lock:"
        )
        st.caption(
            "Danh tính thực thi cần thêm quyền Unity Catalog. "
            "Đây KHÔNG có nghĩa là danh sách trống."
        )
    elif kind == "unconfigured":
        st.info("Chức năng này chưa được cấu hình.", icon=":material/settings:")
    else:
        st.error(f"Không tải được {what}.", icon=":material/sync_problem:")
    if detail:
        st.caption(detail)


def listing_or_empty(listing, *, what: str, render):
    """Render a :class:`ucg.services.base.Listing`, or the right empty state."""
    if listing is None:
        empty_state("failed", what=what)
        return False
    if not listing.ok:
        err = listing.error
        if isinstance(err, GovernanceError) and err.code in _PERMISSION_CODES:
            empty_state("forbidden", what=what, detail=err.message)
        else:
            show_error(err)
        return False
    if listing.empty:
        empty_state("empty", what=what, detail=listing.note)
        return False
    render(listing)
    completeness_note(listing)
    return True


def completeness_note(listing):
    notes = []
    if getattr(listing, "completeness", "complete") == "truncated":
        notes.append("Danh sách đã bị cắt bớt vì quá dài — hãy thu hẹp bằng ô tìm kiếm.")
    elif getattr(listing, "completeness", "complete") == "partial_permission":
        notes.append("Một phần dữ liệu không đọc được do thiếu quyền.")
    if getattr(listing, "note", ""):
        notes.append(listing.note)
    if getattr(listing, "observed_at", ""):
        notes.append(f"Dữ liệu đọc lúc {listing.observed_at}.")
    if notes:
        st.caption(" ".join(notes))


def data_table(rows: list[dict], *, key: str, download_name: str = "", height: int | None = None):
    """A readable table with an honest CSV export."""
    if not rows:
        return
    # Streamlit rejects height=None outright, so the kwarg is omitted entirely
    # rather than passed as a null.
    extra = {"height": height} if height else {}
    st.dataframe(rows, hide_index=True, width="stretch", **extra)
    if download_name:
        st.download_button(
            "Tải CSV",
            _csv_bytes(rows),
            f"{download_name}.csv",
            "text/csv",
            key=f"dl_{key}",
            icon=":material/download:",
        )


def _csv_bytes(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow({
            # Keep exported object names inert when the file is opened in Excel.
            k: ("'" + str(v)) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v
            for k, v in row.items()
        })
    # BOM so Vietnamese renders correctly in Excel.
    return buffer.getvalue().encode("utf-8-sig")


def technical_details(payload: dict, *, label: str = "Chi tiết kỹ thuật", download: str = ""):
    """Raw metadata, tucked away from the working surface."""
    with st.expander(label, expanded=False):
        st.caption(
            "Phần này dành cho người cần đối chiếu với API Databricks. "
            "Thao tác hằng ngày không cần đọc ở đây."
        )
        st.json(payload, expanded=False)
        if download:
            st.download_button(
                "Tải JSON",
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                f"{download}.json",
                "application/json",
                key=f"json_{download}",
            )


def explain(title: str, body: str, *, icon: str = ":material/info:"):
    """In-place explanation, where the decision is made."""
    with st.expander(title, expanded=False):
        st.markdown(body)


def caveat(text: str):
    st.caption(f":material/info: {text}")


def warn_block(lines: list[str]):
    if not lines:
        return
    for line in lines:
        st.warning(line, icon=":material/warning:")


def capability_notice(cap) -> bool:
    """Explain a disabled feature precisely. Returns True when usable."""
    from ucg.capabilities import State

    if cap.usable:
        return True
    messages = {
        State.NOT_CONFIGURED: (":material/settings:", "Chưa cấu hình"),
        State.NO_PERMISSION: (":material/lock:", "Không đủ quyền"),
        State.UNSUPPORTED: (":material/block:", "Workspace chưa hỗ trợ"),
        State.NOT_IMPLEMENTED: (":material/construction:", "Chưa triển khai"),
        State.UNKNOWN: (":material/help:", "Chưa kiểm tra được"),
    }
    icon, label = messages.get(cap.state, (":material/help:", cap.state_label))
    st.info(f"**{label}** — {cap.name}", icon=icon)
    if cap.detail:
        st.caption(cap.detail)
    return False
