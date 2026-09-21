"""Lakehouse Federation connections, reviewed without touching their secrets.

The service hands this screen an already-redacted view: a value survives only
for keys it considers safe, and a ``secret('<scope>','<key>')`` reference is
shown on purpose - the reference is not the credential, and it is the only
thing that makes the credential auditable. This module never asks for the raw
options map and never exports a connection's configuration.

The only write offered here is owner transfer, because it is the only
connection change the service will build a plan for. Everything the app
*cannot* do is printed where a user would go looking for it, using the
service's own wording rather than a claim invented here.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.errors import translate
from ucg.naming import Target
from ucg.services.base import Context, Listing
from ucg.services.federation import FederationService

from .. import nav, state, widgets

#: Session key for the connection under review. A connection is a
#: metastore-level securable, so it does not belong in the catalog/schema/object
#: selection that :mod:`ui.state` keeps for the rest of the app.
_SELECTED = "ucg_federation_connection"

_LIST_KEY = "federation:connections"
_CATALOGS_KEY = "federation:foreign_catalogs"
_FINDINGS_KEY = "federation:findings"

#: Finding.KIND_LABELS values, in the order a reviewer wants to scan them.
_FINDING_LEVELS = ["Cần soát xét", "Chưa xác định", "Thông tin", "Đạt"]
_FINDING_ICONS = {
    "Cần soát xét": ":material/warning:",
    "Chưa xác định": ":material/help:",
    "Thông tin": ":material/info:",
    "Đạt": ":material/check_circle:",
}


def render(ctx: Context) -> None:
    svc = FederationService(ctx)

    widgets.page_title(
        "Kết nối federation",
        "Soát xét kết nối Lakehouse Federation: cấu hình không nhạy cảm, "
        "catalog phụ thuộc và chủ sở hữu.",
    )

    _show_result()

    # Said once for the whole screen: everything below is Unity Catalog
    # metadata, not the access control of the system behind the connection.
    st.info(FederationService.METADATA_IS_NOT_SOURCE_POLICY, icon=":material/policy:")

    top = st.columns([5, 2])
    with top[0]:
        st.caption(
            "Dữ liệu được đọc một lần cho mỗi phiên làm việc và giữ lại để quay lại nhanh."
        )
    with top[1]:
        if st.button("Đọc lại từ Databricks", icon=":material/refresh:",
                     width="stretch", key="fed_refresh"):
            state.clear_caches()
            st.rerun()

    chosen = _connection_list(svc)
    if not chosen:
        return

    st.divider()
    _connection_detail(ctx, svc, chosen)
    st.divider()
    _all_foreign_catalogs(svc)
    st.divider()
    _review_section(svc)


# -- reading helpers -----------------------------------------------------
def _read(call) -> Listing:
    """Run a listing call and keep a refusal inside the Listing.

    The service authorises before it wraps the call, so a denial arrives as an
    exception; without this it would render as "tải thất bại" instead of
    "không đủ quyền".
    """
    try:
        return call()
    except Exception as exc:
        return Listing(items=[], completeness="partial_permission",
                       observed_at="", error=translate(exc))


def _dependencies(svc: FederationService, name: str):
    """(report, error) for one connection - exactly one of the two is set."""
    key = f"federation:deps:{name}"
    cached = state.cache_get(key)
    if cached is None:
        try:
            with st.spinner("Đang tìm foreign catalog phụ thuộc…"):
                cached = (svc.dependency_check(name), None)
        except Exception as exc:
            cached = (None, translate(exc))
        state.cache_put(key, cached)
    return cached


# -- connection list -----------------------------------------------------
def _connection_list(svc: FederationService) -> str:
    st.markdown("#### Danh sách kết nối")

    listing = state.cache_get(_LIST_KEY)
    if listing is None:
        with st.spinner("Đang đọc danh sách kết nối…"):
            listing = state.cache_put(_LIST_KEY, _read(svc.connections))

    chosen = ""

    def _render(result: Listing):
        nonlocal chosen
        refs = list(result.items)

        term = st.text_input(
            "Lọc theo tên kết nối", value=state.filters().get("search", ""),
            key="fed_search", placeholder="Nhập một phần tên kết nối",
        )
        state.set_filter("search", term)
        needle = term.strip().lower()
        shown = [r for r in refs if needle in r.name.lower()] if needle else refs

        st.caption(f"{len(shown)}/{len(refs)} kết nối.")
        if not shown:
            st.info("Không có kết nối nào khớp ô lọc.", icon=":material/filter_alt_off:")
            return

        widgets.data_table([r.row() for r in shown], key="fed_connections",
                           download_name="ket-noi-federation")
        chosen = _select(shown)

    widgets.listing_or_empty(listing, what="kết nối federation", render=_render)

    # Where someone would look for a "Tạo kết nối" button.
    widgets.explain("Tạo kết nối federation mới?",
                    FederationService.can_create_connection()[1])
    return chosen


def _select(refs) -> str:
    names = [r.name for r in refs]
    stored = st.session_state.get(_SELECTED, "")
    index = names.index(stored) if stored in names else 0
    chosen = st.selectbox("Kết nối đang xem", names, index=index, key="fed_pick")
    if chosen != stored:
        st.session_state[_SELECTED] = chosen
        # A pending preview belongs to the connection it was built for.
        state.clear_plan()
    return chosen


# -- one connection ------------------------------------------------------
def _connection_detail(ctx: Context, svc: FederationService, name: str):
    key = f"federation:detail:{name}"
    detail = state.cache_get(key)
    if detail is None:
        try:
            with st.spinner(f"Đang đọc cấu hình kết nối {name}…"):
                detail = state.cache_put(key, svc.connection_detail(name))
        except Exception as exc:
            widgets.show_error(exc, context="Không đọc được chi tiết kết nối.")
            return

    ref = detail.ref
    widgets.breadcrumb([("Loại securable", "CONNECTION"), ("Tên đầy đủ", ref.name)])
    st.markdown(f"## {ref.name}")

    cols = st.columns([2, 2, 2])
    cols[0].markdown(f"**Loại kết nối**  \n{ref.connection_type or 'Chưa xác định'}")
    cols[1].markdown(f"**Chủ sở hữu**  \n{ref.owner or 'Chưa xác định'}")
    cols[2].markdown(f"**Trạng thái cấp phát**  \n{_state_badge(ref.provisioning_state)}")

    if ref.is_beta:
        st.warning(
            f"Loại kết nối {ref.connection_type} đang ở mức Beta theo tài liệu Databricks.",
            icon=":material/science:",
        )

    report, deps_error = _dependencies(svc, ref.name)
    counters = st.columns(3)
    counters[0].metric("Tham số cấu hình", detail.option_count)
    counters[1].metric("Tham chiếu secret scope", len(detail.secret_reference_keys))
    counters[2].metric(
        "Foreign catalog phụ thuộc",
        report.count_text if report is not None else "Chưa xác định",
    )

    tab_overview, tab_options, tab_deps, tab_owner = st.tabs([
        "Tổng quan", "Tham số & credential", "Foreign catalog phụ thuộc",
        "Chuyển quyền sở hữu",
    ])

    with tab_overview:
        _overview(svc, detail)
    with tab_options:
        _options(svc, detail)
    with tab_deps:
        _dependency_panel(ref.name, report, deps_error)
    with tab_owner:
        _owner_transfer(ctx, svc, detail)


def _state_badge(provisioning_state: str) -> str:
    if not provisioning_state:
        return ":material/help: Chưa xác định"
    if provisioning_state in ("ACTIVE", "STATE_UNSPECIFIED"):
        return f":material/check_circle: {provisioning_state}"
    return f":material/warning: {provisioning_state}"


def _overview(svc: FederationService, detail):
    ref = detail.ref

    st.markdown("**Mô tả**")
    st.markdown(ref.comment or "_Chưa có mô tả._")
    widgets.caveat(FederationService.can_update_comment()[1])

    st.info(svc.connector_caveat(ref.connection_type),
            icon=":material/integration_instructions:")

    facts = st.columns(2)
    facts[0].markdown(f"**Kiểu credential**  \n{ref.credential_type or 'Chưa xác định'}")
    facts[1].markdown(
        "**Cờ read_only của kết nối**  \n"
        + (":material/lock: Có" if ref.read_only else ":material/lock_open: Không")
    )

    st.markdown(
        f"- Tạo lúc: **{ref.created or 'Chưa xác định'}** "
        f"bởi **{ref.created_by or 'Chưa xác định'}**\n"
        f"- Cập nhật lần cuối: **{ref.updated or 'Chưa xác định'}** "
        f"bởi **{ref.updated_by or 'Chưa xác định'}**"
    )
    st.caption(f"Đọc lúc {detail.observed_at}.")

    # Where someone would look for workspace binding.
    widgets.explain("Giới hạn kết nối này theo workspace?",
                    FederationService.can_bind_to_workspaces()[1])

    with st.expander(f"Phát hiện cho kết nối này ({len(detail.findings)})",
                     expanded=False):
        if not detail.findings:
            widgets.empty_state("empty", what="phát hiện")
        else:
            widgets.data_table([f.row() for f in detail.findings],
                               key=f"fed_detail_findings_{ref.name}")

    widgets.technical_details(
        {
            "name": ref.name,
            "connection_type": ref.connection_type,
            "credential_type": ref.credential_type,
            "owner": ref.owner,
            "read_only": ref.read_only,
            "provisioning_state": ref.provisioning_state,
            "created_at": ref.created,
            "created_by": ref.created_by,
            "updated_at": ref.updated,
            "updated_by": ref.updated_by,
            "option_count": detail.option_count,
            # Key names and verdicts only - no option value reaches this block.
            "option_status": {o.key: o.status_label for o in detail.options},
            "observed_at": detail.observed_at,
        },
        label="Chi tiết kỹ thuật (không gồm giá trị tham số)",
    )


def _options(svc: FederationService, detail):
    st.caption(
        "Ứng dụng chỉ hiển thị giá trị của những tham số nằm trong danh sách cho phép "
        "và không mang tên gợi ý bí mật. Mọi tham số khác chỉ hiện tên và trạng thái. "
        "Bảng này không có bản xuất CSV."
    )

    rows = detail.option_rows()
    if not rows:
        widgets.empty_state("empty", what="tham số cấu hình")
    else:
        widgets.data_table(rows, key=f"fed_options_{detail.name}")

    st.markdown("**Credential của kết nối**")

    if detail.secret_reference_keys:
        st.success(
            "**Dùng tham chiếu secret scope** — "
            + ", ".join(detail.secret_reference_keys),
            icon=":material/verified_user:",
        )
        st.caption(
            "Tham chiếu secret('<scope>','<key>') cho biết credential nằm ở đâu "
            "mà không để lộ giá trị."
        )

    if detail.literal_secret_keys:
        widgets.warn_block([
            "**Tham số nhạy cảm có vẻ ghi giá trị trực tiếp**: "
            + ", ".join(detail.literal_secret_keys)
            + ". Ứng dụng không đọc và không hiển thị giá trị của chúng. "
            "Xem chi tiết ở mục “Cần xem xét”."
        ])

    if detail.suspect_redacted_keys:
        st.warning(
            "**Giá trị trả về trông như đã bị che**: "
            + ", ".join(detail.suspect_redacted_keys)
            + ". Vì vậy ứng dụng không thực hiện thay đổi nào phải gửi lại options, "
            "kể cả chuyển quyền sở hữu.",
            icon=":material/help:",
        )

    # The credential limit belongs exactly here, next to the credential keys.
    usable, reason = svc.can_verify_credential_rotation()
    if not usable:
        st.info(f"**Không kiểm tra được vòng đời credential** — {reason}",
                icon=":material/help:")


def _dependency_panel(name: str, report, error):
    st.caption(
        "Kiểm tra phụ thuộc phải chạy TRƯỚC mọi thay đổi có tính phá vỡ trên kết nối."
    )

    if error is not None:
        widgets.show_error(error, context="Không kiểm tra được phụ thuộc.")
        return
    if report is None:
        widgets.empty_state("failed", what="kết quả kiểm tra phụ thuộc")
        return

    if report.error is not None:
        widgets.show_error(report.error, context="Không đọc được danh sách catalog.")
        st.warning(report.verdict(), icon=":material/help:")
    elif not report.exhaustive:
        st.warning(report.verdict(), icon=":material/help:")
    elif report.catalogs:
        st.warning(report.verdict(), icon=":material/warning:")
    else:
        st.info(report.verdict(), icon=":material/inbox:")

    if report.catalogs:
        widgets.data_table(report.rows(), key=f"fed_deps_{name}",
                           download_name=f"{name}-foreign-catalog")

        out_of_scope = [c.name for c in report.catalogs if not c.in_app_scope]
        if out_of_scope:
            st.caption(
                "Ngoài phạm vi quản lý của ứng dụng nhưng vẫn được liệt kê vì chúng "
                "vẫn hỏng nếu kết nối thay đổi: " + ", ".join(out_of_scope)
            )

        in_scope = [c.name for c in report.catalogs if c.in_app_scope]
        if in_scope:
            cols = st.columns([3, 2])
            with cols[0]:
                catalog = st.selectbox("Foreign catalog", in_scope,
                                       key=f"fed_cat_pick_{name}")
            with cols[1]:
                st.write("")
                if st.button("Xem quyền của catalog", icon=":material/key:",
                             width="stretch", key=f"fed_cat_go_{name}"):
                    state.select_target(Target("catalog", catalog))
                    nav.goto("permissions")
            st.caption(FederationService.FOREIGN_TABLE_READ_ONLY)

    if report.observed_at:
        st.caption(f"Đọc lúc {report.observed_at}.")

    widgets.explain("Cần quyền gì để tạo foreign catalog?",
                    FederationService.CREATE_FOREIGN_CATALOG_REQUIREMENT)
    widgets.explain("Cần compute nào để truy vấn bảng foreign?",
                    FederationService.COMPUTE_REQUIREMENT)


# -- owner transfer: choose, preview, apply ------------------------------
def _owner_transfer(ctx: Context, svc: FederationService, detail):
    ref = detail.ref
    try:
        target = Target("connection", ref.name)
    except Exception as exc:
        widgets.show_error(exc)
        return

    decision = ctx.authz.check(Action.MANAGE_STORAGE, target)
    if not decision.allowed:
        st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
        st.caption(
            "Chuyển quyền sở hữu kết nối trong Databricks bằng "
            "ALTER CONNECTION … OWNER TO."
        )
        return

    st.markdown("#### 1. Chọn chủ sở hữu mới")
    st.caption(f"Chủ sở hữu hiện tại: **{ref.owner or 'Chưa xác định'}**")

    cols = st.columns([3, 3])
    with cols[0]:
        new_owner = st.text_input(
            "Chủ sở hữu mới", key=f"fed_owner_{ref.name}",
            placeholder="email@congty.com · tên account group · application ID",
            help="Unity Catalog nhận: email người dùng, tên account group, "
                 "hoặc application ID của service principal.",
        )
    with cols[1]:
        reason = st.text_area(
            "Lý do thay đổi", max_chars=500, key=f"fed_reason_{ref.name}",
            placeholder="Ví dụ: bàn giao kết nối cho nhóm nền tảng dữ liệu theo TICKET-123.",
            help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
        )

    # Every material input feeds the signature, including the owner we read:
    # if the connection was re-owned elsewhere, the preview must not survive.
    signature = "|".join([
        "federation_owner", target.key, new_owner.strip(), reason.strip(),
        ref.owner or "", ctx.actor.email, ctx.execution_identity,
    ])
    state.invalidate_plan_if_changed(signature)

    blocked = bool(detail.suspect_redacted_keys)
    if blocked:
        st.warning(
            "Không tạo được bản xem trước: đổi chủ sở hữu bắt buộc gửi lại toàn bộ "
            "options, mà một số giá trị đọc về trông như đã bị che ("
            + ", ".join(detail.suspect_redacted_keys)
            + "). Hãy dùng ALTER CONNECTION … OWNER TO trong Databricks.",
            icon=":material/block:",
        )

    st.divider()
    st.markdown("#### 2. Xem trước phạm vi ảnh hưởng")

    ready = bool(new_owner.strip() and reason.strip()) and not blocked
    plan = state.get_plan()
    valid_plan = (
        plan is not None
        and state.plan_matches(signature)
        and plan.target_key == target.key
        and plan.payload.get("operation") == "update_owner"
    )

    if not valid_plan:
        if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                     icon=":material/preview:", key=f"fed_preview_{ref.name}"):
            state.clear_plan()
            try:
                with st.spinner("Đang kiểm tra trạng thái kết nối…"):
                    built = svc.plan_update_owner(ref.name, new_owner, reason)
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, signature)
            st.rerun()
        if not blocked and (not new_owner.strip() or not reason.strip()):
            st.caption("Nhập chủ sở hữu mới và lý do để tạo bản xem trước.")
        st.caption(
            "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. "
            "Sửa bất kỳ trường nào ở trên sẽ huỷ bản xem trước cũ."
        )
        return

    _render_preview(plan)
    st.divider()
    _apply(svc, plan)


def _render_preview(plan):
    with st.container(border=True):
        st.markdown("**Những gì sẽ được gửi tới Databricks**")
        for line in plan.preview:
            if line.kind == "change":
                st.markdown(f":material/arrow_right: {line.text}")
            elif line.kind == "warning":
                st.warning(line.text, icon=":material/warning:")
            elif line.kind == "unknown":
                st.info(line.text, icon=":material/help:")
            else:
                st.caption(line.text)
        st.caption(f"Lý do: {plan.reason}")
        st.caption(
            f"Bản xem trước tạo lúc {plan.created_at.strftime('%H:%M:%S')} UTC "
            f"· mã thao tác `{plan.operation_id[:12]}`"
        )


def _apply(svc: FederationService, plan):
    st.markdown("#### 3. Xác nhận và áp dụng")
    st.caption(f"Nhập lại tên đầy đủ của kết nối để xác nhận: `{plan.target_name}`")

    principal = str(plan.payload.get("principal", ""))
    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên kết nối", key=f"fed_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            f"Chuyển sở hữu cho {principal}"[:40], type="primary", width="stretch",
            disabled=not matches, icon=":material/manage_accounts:",
            key=f"fed_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"fed_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not apply_clicked:
        return

    # One plan, one submission: a rerun cannot resubmit it.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner("Đang gửi thay đổi tới Databricks…"):
            outcome = svc.apply(plan, confirmation)
    except Exception as exc:
        state.clear_plan()
        widgets.show_error(exc)
        return
    finally:
        state.finish()

    state.clear_plan()
    state.clear_caches()
    state.set_notice({
        "kind": "federation_owner",
        "status": outcome.status,
        "event_id": outcome.event_id,
        "summary": plan.summary,
        "connection": plan.target_name,
        "principal": principal,
        "previous_owner": plan.payload.get("previous_owner", ""),
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _show_result():
    payload = state.pop_notice()
    if not payload:
        return
    if payload.get("kind") != "federation_owner":
        # A notice belongs to the flow that created it; hand it back untouched.
        state.set_notice(payload)
        return

    status = payload.get("status")
    connection = payload.get("connection", "")
    principal = payload.get("principal", "")

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{payload.get('summary', 'Hoàn tất')}**",
                       icon=":material/check_circle:")
            st.markdown(
                f"- Kết nối: **{connection}**\n"
                f"- Chủ sở hữu mới: **{principal}**\n"
                f"- Chủ sở hữu trước đó: **{payload.get('previous_owner') or 'Chưa xác định'}**"
            )
            verified = payload.get("verified") or {}
            if status == Status.VERIFIED:
                st.caption(
                    "Đã đọc lại từ Databricks. Chủ sở hữu hiện tại: "
                    f"**{verified.get('owner_now') or 'Chưa xác định'}** · "
                    f"số tham số cấu hình: {verified.get('option_count', 'Chưa xác định')}."
                )
                if not verified.get("applied", True):
                    st.warning(
                        "Databricks đã nhận yêu cầu nhưng chủ sở hữu đọc lại chưa khớp "
                        "mong đợi. Hãy kiểm tra lại trong Catalog Explorer.",
                        icon=":material/warning:",
                    )
                if verified.get("note"):
                    st.caption(verified["note"])
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(
                "Đổi chủ sở hữu KHÔNG đổi credential của kết nối. "
                + FederationService.SECRET_ROTATION_LIMIT
            )
            st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng "
            "không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            "**Việc cần làm:** Đọc lại kết nối **" + connection + "** và kiểm tra chủ "
            f"sở hữu hiện tại TRƯỚC KHI thử lại, để tránh thực hiện hai lần. "
            "Hãy chạy thử một truy vấn qua kết nối này để xác nhận nó vẫn hoạt động."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi không thành công.",
             icon=":material/error:")


# -- metastore-wide panels -----------------------------------------------
def _all_foreign_catalogs(svc: FederationService):
    with st.expander("Tất cả foreign catalog trong metastore", expanded=False):
        listing = state.cache_get(_CATALOGS_KEY)
        if listing is None:
            with st.spinner("Đang đọc danh sách catalog…"):
                listing = state.cache_put(_CATALOGS_KEY, _read(svc.foreign_catalogs))

        widgets.listing_or_empty(
            listing, what="foreign catalog",
            render=lambda result: widgets.data_table(
                [c.row() for c in result.items], key="fed_all_catalogs",
                download_name="foreign-catalog",
            ),
        )


def _review_section(svc: FederationService):
    st.markdown("#### Cần xem xét")
    st.caption(
        "Soát xét đọc cấu hình của từng kết nối một, nên được chạy theo yêu cầu "
        "thay vì mỗi lần mở trang."
    )

    listing = state.cache_get(_FINDINGS_KEY)
    if listing is None:
        if st.button("Chạy soát xét toàn bộ kết nối", icon=":material/fact_check:",
                     key="fed_run_review"):
            with st.spinner("Đang đọc cấu hình từng kết nối…"):
                state.cache_put(_FINDINGS_KEY, _read(svc.findings))
            st.rerun()
        return

    widgets.listing_or_empty(listing, what="phát hiện cần soát xét",
                             render=_render_findings)


def _render_findings(listing: Listing):
    rows = [f.row() for f in listing.items]

    counts: dict[str, int] = {}
    for row in rows:
        level = row["Mức"]
        counts[level] = counts.get(level, 0) + 1
    summary = " · ".join(
        f"{_FINDING_ICONS.get(level, ':material/info:')} {level}: **{counts[level]}**"
        for level in _FINDING_LEVELS if counts.get(level)
    )
    if summary:
        st.markdown(summary)

    chosen = st.multiselect("Lọc theo mức", _FINDING_LEVELS, default=[],
                            key="fed_finding_levels")
    shown = [r for r in rows if r["Mức"] in chosen] if chosen else rows
    if not shown:
        st.info("Không có phát hiện nào khớp bộ lọc.", icon=":material/filter_alt_off:")
        return

    st.caption(f"{len(shown)}/{len(rows)} phát hiện.")
    widgets.data_table(shown, key="fed_findings",
                       download_name="soat-xet-ket-noi-federation", height=420)
