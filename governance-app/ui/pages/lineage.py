"""Lineage and impact: two sources, kept apart on screen.

This is the screen people use to decide whether changing or dropping a table is
safe, so the layout is built around three refusals:

* it refuses to draw an empty graph when the source is simply unreadable - the
  availability probe says which of "no warehouse", "no privilege" and "system
  schema not enabled" is the actual cause, and the panels repeat it;
* it refuses to merge native lineage with hand-declared external relationships
  into one number, because Databricks does not record the latter anywhere near
  the former;
* it refuses to turn a row count into a verdict. Every count comes from rows
  that were actually returned, and the disclaimer sits next to the counts rather
  than at the bottom of the page.

The service is read-only by design (Unity Catalog exposes no API that writes
native lineage), so there is no plan/preview/apply path on this page.
"""
from __future__ import annotations

import streamlit as st

from ucg.authz import Action
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.lineage import (
    DEFAULT_WINDOW_DAYS,
    DIRECT_ACCESS_NOTE,
    EARLIEST_EVENT_DATE,
    EMPTY_IS_NOT_PROOF,
    EXTERNAL_LINEAGE_NOTE,
    FRESHNESS_NOTE,
    IDENTITY_WARNING,
    IMPACT_DISCLAIMER,
    MAX_WINDOW_DAYS,
    MISSING_LINEAGE_CAUSES,
    ONE_HOP_NOTE,
    PATH_LIMITATION_NOTE,
    RECORD_ID_NOTE,
    RETENTION_NOTE,
    STATEMENT_ID_NOTE,
    SYSTEM_TABLE_REQUIREMENTS,
    LineageService,
    LineageState,
)

from .. import nav, picker, state, widgets

#: How an empty-but-successful panel announces itself: never colour alone, and
#: never the same block for "nothing recorded" as for "could not read".
_STATE_BLOCK = {
    LineageState.NOT_RECORDED: (st.info, ":material/inbox:"),
    LineageState.NO_PERMISSION: (st.warning, ":material/lock:"),
    LineageState.NOT_ENABLED: (st.info, ":material/settings:"),
    LineageState.NOT_CONFIGURED: (st.info, ":material/settings:"),
    LineageState.UNKNOWN: (st.warning, ":material/help:"),
}


def render(ctx: Context) -> None:
    assets = AssetService(ctx)
    lineage = LineageService(ctx)

    widgets.page_title(
        "Lineage và ảnh hưởng",
        "Dữ liệu này đến từ đâu, ai đang dùng nó, và điều gì có thể hỏng nếu nó thay đổi.",
    )

    gate = ctx.authz.check(Action.READ_LINEAGE, None)
    if not gate.allowed:
        st.warning(f"**Không xem được lineage** — {gate.reason}", icon=":material/lock:")
        st.caption(f"Vai trò hiện tại: **{ctx.authz.role_label}**.")
        return

    st.warning(IDENTITY_WARNING, icon=":material/person_search:")
    st.caption(
        f"Danh tính thực thi của ứng dụng: **{ctx.connection.identity_label}**. "
        "Mọi truy vấn lineage bên dưới chạy bằng danh tính này."
    )

    availability = _availability(lineage)
    _availability_panel(ctx, availability)
    if not availability.anything_usable:
        return

    st.divider()
    target, days = _scope(ctx, assets)
    if target is None:
        return

    scope_check = ctx.authz.check(Action.READ_LINEAGE, target)
    if not scope_check.allowed:
        st.warning(f"**Không xem được lineage** — {scope_check.reason}", icon=":material/lock:")
        return

    if target.kind != "table":
        st.info(
            f"Lineage chỉ tra cứu được cho bảng hoặc view. Đối tượng đang chọn là "
            f"**{target.label}**.",
            icon=":material/block:",
        )
        st.caption(
            "Databricks chỉ ghi lineage theo tên ba phần của bảng/view trong "
            "`system.access.table_lineage`."
        )
        return

    detail = _detail(assets, target)
    picker.header(ctx, target, detail)
    st.divider()

    impact = _impact(lineage, target, days)
    _impact_panel(impact)

    st.divider()
    tab_up, tab_down, tab_col, tab_ext = st.tabs([
        "Nguồn dữ liệu (upstream)",
        "Ảnh hưởng xuôi dòng (downstream)",
        "Theo cột",
        "Tài sản ngoài",
    ])

    with tab_up:
        _upstream_tab(lineage, target, days, availability)
    with tab_down:
        _downstream_tab(impact, availability, target, days)
    with tab_col:
        _column_tab(lineage, target, detail, days, availability)
    with tab_ext:
        _external_tab(lineage, impact, target, availability)


# -- availability ---------------------------------------------------------
def _availability(lineage: LineageService):
    """Probe once per session; the probe itself costs a SQL round-trip."""
    cached = state.cache_get("lineage:availability")
    if cached is not None:
        return cached
    with st.spinner("Đang kiểm tra nguồn dữ liệu lineage…"):
        return state.cache_put("lineage:availability", lineage.available())


def _availability_panel(ctx: Context, availability) -> None:
    cols = st.columns([5, 1.6])
    with cols[0]:
        st.markdown("**Nguồn dữ liệu lineage của môi trường này**")
    with cols[1]:
        if st.button("Kiểm tra lại", icon=":material/refresh:", width="stretch",
                     key="lin_recheck"):
            state.clear_caches()
            st.rerun()

    st.dataframe(availability.rows(), hide_index=True, width="stretch")
    st.caption(
        "Hai nguồn này tách biệt và không được cộng gộp: system tables chứa lineage "
        "native của Databricks, External Lineage chỉ chứa quan hệ do con người khai báo."
    )

    _system_diagnosis(ctx, availability)

    if not availability.external_usable:
        st.info(
            f"**Lineage tài sản ngoài chưa dùng được** — {availability.external_detail}",
            icon=":material/link_off:",
        )

    if not availability.anything_usable:
        st.error(
            "Không nguồn lineage nào đọc được, nên màn hình này không hiển thị đồ thị "
            "rỗng để tránh bị hiểu nhầm là “không có phụ thuộc”.",
            icon=":material/error:",
        )
        st.caption(EMPTY_IS_NOT_PROOF)
        nav.link_button("Xem ma trận chức năng", "diagnostics",
                        icon=":material/checklist:", widget_key="lin_to_diag")


def _system_diagnosis(ctx: Context, availability) -> None:
    """Name the cause, instead of one vague “không đọc được”."""
    if availability.system_usable:
        st.success(
            "**Đọc được** — lineage bảng/cột từ `system.access`.",
            icon=":material/check_circle:",
        )
        st.caption(availability.detail or FRESHNESS_NOTE)
        return

    if not availability.warehouse_configured:
        st.error(
            "**Thiếu cấu hình SQL Warehouse** — lineage bảng/cột chỉ đọc được bằng SQL "
            "trên system tables.",
            icon=":material/settings:",
        )
        st.markdown(
            "Quản trị viên ứng dụng cần đặt `GOVERNANCE_WAREHOUSE_ID` trong cấu hình "
            "rồi **deploy lại** ứng dụng."
        )
        st.caption(availability.detail)
        return

    state_code = availability.system_tables
    if state_code == LineageState.NO_PERMISSION:
        st.warning(
            "**Không đủ quyền** — danh tính thực thi chưa đọc được "
            "`system.access.table_lineage`.",
            icon=":material/lock:",
        )
    elif state_code == LineageState.NOT_ENABLED:
        st.info(
            "**Schema `system.access` chưa được bật** trên metastore này.",
            icon=":material/settings:",
        )
        st.caption(
            "Việc bật system schema là thao tác cấp account/metastore admin và có hiệu "
            "lực toàn metastore; ứng dụng chỉ phát hiện, không tự bật."
        )
    else:
        st.warning(
            "**Chưa phân biệt được nguyên nhân** — có thể schema `access` chưa bật, "
            "hoặc danh tính thực thi thiếu quyền. Ứng dụng không đoán.",
            icon=":material/help:",
        )
    st.caption(availability.detail or SYSTEM_TABLE_REQUIREMENTS)
    with st.expander("Điều kiện để đọc được lineage", expanded=False):
        st.markdown(SYSTEM_TABLE_REQUIREMENTS)


# -- scope: object + time window -----------------------------------------
def _scope(ctx: Context, assets: AssetService) -> tuple[Target | None, int]:
    target = state.current_target()
    with st.expander("Đối tượng đang xem", expanded=target is None):
        picked = picker.compact_picker(ctx, assets)
        if picked is not None:
            target = picked

    filters = state.filters()
    saved = filters.get("lineage_days", DEFAULT_WINDOW_DAYS)
    try:
        saved = min(max(int(saved), 1), MAX_WINDOW_DAYS)
    except (TypeError, ValueError):
        saved = DEFAULT_WINDOW_DAYS

    cols = st.columns([4, 3])
    with cols[0]:
        days = st.slider(
            "Cửa sổ thời gian (ngày)", min_value=1, max_value=MAX_WINDOW_DAYS,
            value=saved, step=1, key="lin_days",
            help="Mọi truy vấn đều chặn dưới theo event_date; không có truy vấn không giới hạn.",
        )
        state.set_filter("lineage_days", days)
    with cols[1]:
        st.caption(RETENTION_NOTE)
        st.caption(
            f"Không có sự kiện nào trước {EARLIEST_EVENT_DATE.isoformat()}, nên nới rộng "
            "cửa sổ quá mốc này không làm kết quả đầy đủ hơn."
        )

    if target is None:
        st.info("Chọn một bảng hoặc view để xem lineage.", icon=":material/search:")
    return target, days


def _detail(assets: AssetService, target: Target):
    """Object metadata, cached in a wrapper so a failure is not retried every rerun."""
    key = f"lineage:detail:{target.key}"
    box = state.cache_get(key)
    if box is None:
        try:
            with st.spinner("Đang đọc metadata đối tượng…"):
                box = state.cache_put(key, {"detail": assets.detail(target)})
        except Exception:
            # The page works without it; the column tab falls back to free text.
            box = state.cache_put(key, {"detail": None})
    return box.get("detail")


# -- impact ---------------------------------------------------------------
def _impact(lineage: LineageService, target: Target, days: int):
    key = f"lineage:impact:{target.key}:{days}"
    cached = state.cache_get(key)
    if cached is not None:
        return cached
    with st.spinner("Đang đọc phụ thuộc xuôi dòng và quan hệ ngoài Databricks…"):
        return state.cache_put(key, lineage.impact_summary(target, days))


def _impact_panel(impact) -> None:
    st.markdown("#### Tóm tắt ảnh hưởng")

    down_ok = impact.downstream.ok
    ext_ok = impact.external.ok

    cols = st.columns(3)
    with cols[0]:
        _count("Phụ thuộc trực tiếp", impact.direct_count if down_ok else None,
               impact.downstream)
    with cols[1]:
        _count("Phụ thuộc gián tiếp qua view", impact.indirect_count if down_ok else None,
               impact.downstream)
    with cols[2]:
        _count("Quan hệ tới tài sản ngoài", impact.external_count if ext_ok else None,
               impact.external)

    st.caption(
        f"Đếm từ các dòng thực sự đọc được trong {impact.window_days} ngày gần nhất, "
        f"lúc {impact.observed_at}. Dòng không xác định được cờ direct_access được tính "
        "vào nhóm trực tiếp."
    )

    # The service headline mixes both sources into one sentence, so it is only
    # honest when both sources actually answered.
    if down_ok and ext_ok:
        st.markdown(f":material/summarize: {impact.headline}")
    else:
        if not down_ok:
            st.caption(
                ":material/error: Phụ thuộc xuôi dòng: "
                f"{impact.downstream.state_label} — xem tab “Ảnh hưởng xuôi dòng”."
            )
        if not ext_ok:
            st.caption(
                ":material/error: Tài sản ngoài Databricks: "
                f"{impact.external.state_label} — xem tab “Tài sản ngoài”."
            )

    st.warning(IMPACT_DISCLAIMER, icon=":material/gavel:")
    with st.expander("Vì sao con số này không thể coi là đầy đủ", expanded=False):
        for line in impact.warnings():
            st.markdown(f"- {line}")
    st.caption(
        "Hai nguồn được tải xuống riêng ở từng tab: gộp chúng vào một tệp sẽ tạo ra "
        "một “đồ thị lineage” không tồn tại."
    )


def _count(label: str, value: int | None, result) -> None:
    """A metric only when the number came from rows Databricks returned."""
    if value is None:
        st.markdown(f"**{label}**  \n:material/help: Chưa xác định")
        st.caption(result.state_label)
        return
    st.metric(label, value)


# -- tabs -----------------------------------------------------------------
def _upstream_tab(lineage: LineageService, target: Target, days: int, availability) -> None:
    st.caption(
        "Những đối tượng mà bảng này đã đọc dữ liệu từ đó, trong cửa sổ thời gian đang chọn."
    )
    if not availability.system_usable:
        _system_unavailable(availability)
        return

    key = f"lineage:up:{target.key}:{days}"
    result = state.cache_get(key)
    # Streamlit runs every tab body on every rerun, so a heavy read behind a tab
    # has to be asked for explicitly instead of firing when the page opens.
    if result is None:
        if not st.button("Đọc nguồn dữ liệu (upstream)", icon=":material/upload:",
                         type="primary", key=f"lin_up_go_{target.key}_{days}"):
            st.caption("Truy vấn chỉ chạy khi bạn bấm nút, và kết quả được giữ lại trong phiên.")
            return
        with st.spinner("Đang truy vấn system.access.table_lineage…"):
            result = state.cache_put(key, lineage.upstream(target, days))

    _result_panel(result, key=f"up_{target.key}_{days}",
                  download=f"{target.full_name}-upstream")


def _downstream_tab(impact, availability, target: Target, days: int) -> None:
    st.caption(
        "Những đối tượng đã đọc dữ liệu từ bảng này — nơi thay đổi của bạn sẽ lan tới trước tiên."
    )
    if not availability.system_usable:
        _system_unavailable(availability)
        return
    _result_panel(impact.downstream, key=f"down_{target.key}_{days}",
                  download=f"{target.full_name}-downstream")


def _column_tab(lineage: LineageService, target: Target, detail, days: int,
                availability) -> None:
    st.caption(
        "Lineage cấp cột cho một cột cụ thể, cả hai chiều, trong cửa sổ thời gian đang chọn."
    )
    if not availability.system_usable:
        _system_unavailable(availability)
        return

    names = [c.get("Cột") for c in getattr(detail, "columns", []) or [] if c.get("Cột")]
    cols = st.columns([3, 2])
    with cols[0]:
        if names:
            column = st.selectbox("Cột", names, key=f"lin_col_{target.key}")
        else:
            column = st.text_input(
                "Cột", key=f"lin_coltext_{target.key}",
                placeholder="Nhập đúng tên cột",
                help="Không đọc được danh sách cột của bảng này nên phải nhập tay.",
            )
            st.caption("Không đọc được danh sách cột từ metadata của bảng.")
    column = (column or "").strip()
    with cols[1]:
        st.write("")
        run = st.button(
            f"Tra lineage cột {column}"[:60] if column else "Chọn cột để tra lineage",
            disabled=not column, icon=":material/view_column:",
            key=f"lin_col_go_{target.key}", width="stretch",
        )

    if not column:
        return

    key = f"lineage:col:{target.key}:{column.lower()}:{days}"
    result = state.cache_get(key)
    if result is None:
        if not run:
            st.caption("Truy vấn cấp cột chỉ chạy khi bạn bấm nút.")
            return
        with st.spinner("Đang truy vấn system.access.column_lineage…"):
            result = state.cache_put(key, lineage.column_lineage(target, column, days))

    _result_panel(result, key=f"col_{target.key}_{column}_{days}",
                  download=f"{target.full_name}-{column}-cot", column_view=True)
    st.caption(RECORD_ID_NOTE)


def _external_tab(lineage: LineageService, impact, target: Target, availability) -> None:
    st.info(EXTERNAL_LINEAGE_NOTE, icon=":material/hub:")

    if not availability.external_usable:
        st.warning(
            f"**Không đọc được External Lineage** — {availability.external_detail}",
            icon=":material/link_off:",
        )
        st.caption(
            "Đây là nguồn riêng biệt: không đọc được nguồn này KHÔNG ảnh hưởng tới "
            "lineage native ở các tab còn lại."
        )
        return

    st.markdown("**Quan hệ đã khai báo cho đối tượng này**")
    _result_panel(impact.external, key=f"ext_{target.key}",
                  download=f"{target.full_name}-tai-san-ngoai", native=False)

    st.divider()
    st.markdown("**Hệ thống ngoài đã được đăng ký trên metastore**")
    st.caption(
        "Danh sách này cho biết có hệ thống nào từng được khai báo hay chưa — để đọc "
        "“không có quan hệ” cho đúng ngữ cảnh."
    )
    systems = _external_systems(lineage)
    widgets.listing_or_empty(
        systems, what="hệ thống ngoài Databricks",
        render=lambda listing: widgets.data_table(
            list(listing.items), key=f"extsys_{target.key}",
            download_name="he-thong-ngoai-databricks",
        ),
    )


def _external_systems(lineage: LineageService):
    cached = state.cache_get("lineage:external_systems")
    if cached is not None:
        return cached
    with st.spinner("Đang đọc danh sách hệ thống ngoài…"):
        return state.cache_put("lineage:external_systems", lineage.external_systems())


# -- shared panel rendering ----------------------------------------------
def _result_panel(result, *, key: str, download: str, column_view: bool = False,
                  native: bool = True) -> None:
    """One lineage panel. ``native`` is False for the external-lineage source,
    whose caveats and causes of absence are a different set entirely."""
    if result is None:
        widgets.empty_state("failed", what="dữ liệu lineage")
        return

    if not result.ok:
        widgets.show_error(result.error, context="Không đọc được lineage.")
        st.caption(result.explains_emptiness)
        return

    if not result.has_rows:
        block, icon = _STATE_BLOCK.get(result.state, (st.warning, ":material/help:"))
        tail = ("chưa có dòng nào trong cửa sổ thời gian này." if native
                else "chưa có quan hệ nào được khai báo cho đối tượng này.")
        block(f"**{result.state_label}** — {tail}", icon=icon)
        st.caption(result.explains_emptiness)
        if native:
            _why_missing()
        else:
            st.caption(
                "Quan hệ tới tài sản ngoài Databricks phải do con người khai báo qua API "
                "External Lineage; không có dòng nào chỉ nghĩa là chưa ai khai báo."
            )
        _limits(result, {result.note, EMPTY_IS_NOT_PROOF}, key)
        return

    rows = result.column_table() if column_view else result.table()
    st.caption(f"{len(rows)} dòng đọc được.")
    widgets.data_table(rows, key=key, download_name=download)
    widgets.completeness_note(result)

    shown = {result.note}
    if show_direct_note:
        st.caption(DIRECT_ACCESS_NOTE)
        st.caption(PATH_LIMITATION_NOTE)
        shown |= {DIRECT_ACCESS_NOTE, PATH_LIMITATION_NOTE}
    st.caption(EMPTY_IS_NOT_PROOF)
    shown.add(EMPTY_IS_NOT_PROOF)

    _limits(result, shown, key)


def _limits(result, shown: set, key: str) -> None:
    lines = [
        line for line in list(result.caveats) + list(result.notes)
        if line and line not in shown
    ]
    # IDENTITY_WARNING is already at the top of the page; repeating it inside a
    # collapsed expander would only dilute it.
    lines = [line for line in lines if line != IDENTITY_WARNING]
    if not lines:
        return
    seen: list[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)
    with st.expander("Giới hạn của kết quả này", expanded=False):
        for line in seen:
            st.markdown(f"- {line}")
        # The external panel has no time window; saying "30 ngày" there would be wrong.
        if getattr(result, "source", "") != "external_lineage":
            st.caption(f"Cửa sổ: {result.window_days} ngày. {FRESHNESS_NOTE}")


def _why_missing() -> None:
    with st.expander("Vì sao một phụ thuộc có thật vẫn không xuất hiện", expanded=False):
        for cause in MISSING_LINEAGE_CAUSES:
            st.markdown(f"- {cause}")
        st.caption(ONE_HOP_NOTE)
        st.caption(STATEMENT_ID_NOTE)


def _system_unavailable(availability) -> None:
    st.warning(
        "**Không đọc được lineage bảng/cột** — ứng dụng không hiển thị đồ thị rỗng "
        "thay cho một nguồn dữ liệu chưa dùng được.",
        icon=":material/query_stats:",
    )
    st.caption(availability.detail or SYSTEM_TABLE_REQUIREMENTS)
    st.caption(EMPTY_IS_NOT_PROOF)
