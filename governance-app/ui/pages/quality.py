"""Data quality: the monitor on one object, and that table's constraints.

Two honesty problems drive the layout.

The monitor API can only be *probed* per object, so a screen that simply showed
"no monitor" would read like an inventory of the workspace. It is not one, and
:data:`QualityService.NO_INVENTORY` sits next to the result saying so.

Constraints are shown split by whether Databricks enforces them, because a
declared PRIMARY KEY is not a uniqueness guarantee and a page that lists it
beside NOT NULL invites exactly that reading.

Creating a monitor is deliberately absent: the create payload needs schedule,
output schema and slicing configuration this app does not collect, and a
half-configured monitor costs money on serverless compute.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.errors import Code
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.quality import ENFORCED, QualityService

from .. import nav, picker, state, widgets

#: Only these two kinds can carry a monitor; everything else gets OBJECT_SCOPE.
LEVELS = [("table", "Bảng / View"), ("schema", "Schema")]
LEVEL_LABELS = dict(LEVELS)

OPERATIONS = {
    "refresh": "Chạy lại giám sát ngay",
    "delete": "Xoá cấu hình monitor",
}
CONFIRM_LABELS = {
    "refresh": "Chạy lại giám sát",
    "delete": "Xoá monitor",
}


def render(ctx: Context) -> None:
    assets = AssetService(ctx)
    quality = QualityService(ctx)

    widgets.page_title(
        "Chất lượng dữ liệu",
        "Giám sát chất lượng và ràng buộc của một đối tượng Unity Catalog.",
        icon=":material/rule:",
    )
    widgets.caveat(QualityService.PREVIEW_STATUS)

    _show_result()

    target = _pick_target(ctx, assets)
    if target is None:
        return

    picker.header(ctx, target, None)
    if st.button("Đọc lại từ Databricks", icon=":material/refresh:",
                 key=f"q_reload_{target.key}"):
        state.clear_caches()
        st.rerun()

    st.divider()
    tab_monitor, tab_constraints = st.tabs(["Giám sát", "Ràng buộc bảng"])
    with tab_monitor:
        _monitoring(ctx, quality, target)
    with tab_constraints:
        _constraints(ctx, quality, assets, target)


# -- object selection ----------------------------------------------------
def _pick_target(ctx: Context, assets: AssetService) -> Target | None:
    chosen = state.current_target()
    out_of_scope = chosen is not None and chosen.kind not in LEVEL_LABELS
    if out_of_scope:
        chosen = None

    with st.expander("Đối tượng đang xem", expanded=chosen is None):
        if out_of_scope:
            st.info(QualityService.OBJECT_SCOPE, icon=":material/info:")
        picked = _level_picker(ctx, assets)
        if picked is not None:
            chosen = picked

    if chosen is None:
        st.info("Chọn một bảng hoặc một schema để xem chất lượng dữ liệu.",
                icon=":material/search:")
    return chosen


def _level_picker(ctx: Context, assets: AssetService) -> Target | None:
    sel = state.selection()
    codes = [c for c, _ in LEVELS]
    level = st.radio(
        "Phạm vi giám sát", codes,
        index=codes.index(sel["kind"]) if sel.get("kind") in codes else 0,
        format_func=lambda c: LEVEL_LABELS[c], horizontal=True, key="q_level",
        help=QualityService.OBJECT_SCOPE,
    )
    if level != sel.get("kind"):
        state.set_selection(kind=level)

    cols = st.columns(3)
    with cols[0]:
        catalog = picker.catalog_selector(ctx, assets)
    if not catalog:
        return None
    with cols[1]:
        schema = picker.schema_selector(ctx, assets, catalog)
    if not schema:
        return None

    try:
        if level == "schema":
            return Target("schema", catalog, schema)
        with cols[2]:
            name = picker.object_selector(ctx, assets, catalog, schema, "table")
        if not name:
            return None
        return Target("table", catalog, schema, name)
    except Exception as exc:
        widgets.show_error(exc)
        return None


# -- cached reads --------------------------------------------------------
def _monitor_view(quality: QualityService, target: Target):
    key = f"quality:monitor:{target.key}"
    view = state.cache_get(key)
    if view is None:
        with st.spinner("Đang kiểm tra cấu hình giám sát…"):
            view = quality.monitor_for(target)
        state.cache_put(key, view)
    return view


def _refresh_listing(quality: QualityService, target: Target):
    key = f"quality:refresh:{target.key}"
    listing = state.cache_get(key)
    if listing is None:
        with st.spinner("Đang đọc lịch sử làm mới…"):
            listing = quality.refreshes(target)
        state.cache_put(key, listing)
    return listing


def _asset_detail(assets: AssetService, target: Target) -> dict:
    """Detail plus the failure, cached together so a failure is not re-read."""
    key = f"quality:detail:{target.key}"
    box = state.cache_get(key)
    if box is None:
        try:
            with st.spinner("Đang đọc metadata của bảng…"):
                box = {"detail": assets.detail(target), "error": None}
        except Exception as exc:
            box = {"detail": None, "error": exc}
        state.cache_put(key, box)
    return box


# -- monitoring tab ------------------------------------------------------
def _monitoring(ctx: Context, quality: QualityService, target: Target):
    st.info(QualityService.NO_INVENTORY, icon=":material/inventory:")

    view = _monitor_view(quality, target)
    if not view.ok:
        widgets.show_error(view.error, context="Không kiểm tra được cấu hình giám sát.")
        nav.link_button("Xem khả năng và cấu hình", "diagnostics",
                        icon=":material/settings:",
                        widget_key=f"q_diag_{target.key}")
        return

    with st.container(border=True):
        if view.exists:
            st.success(f"**Đã cấu hình** — {view.kind_label}", icon=":material/check_circle:")
        else:
            st.info("**Chưa có monitor** cho đối tượng này.", icon=":material/remove_circle:")
            st.caption(
                "Databricks trả về NOT_FOUND khi được hỏi về đối tượng này. "
                "Đây là câu trả lời cho một đối tượng, không phải cho workspace."
            )
        cols = st.columns(2)
        for index, (label, value) in enumerate(view.summary_fields()):
            cols[index % 2].markdown(f"**{label}**  \n{value or '—'}")

    if not view.exists:
        _no_create_here(target)
        return

    if view.raw:
        widgets.technical_details(
            view.raw, label="Cấu hình monitor (dữ liệu thô từ API)",
            download=f"monitor-{target.full_name}",
        )

    st.divider()
    _manage(ctx, quality, target, view)

    st.divider()
    _history(quality, target)


def _no_create_here(target: Target):
    with st.container(border=True):
        st.markdown("**Ứng dụng này không tạo monitor**")
        st.caption(
            "Tạo monitor cần khai báo lịch chạy, schema chứa bảng metric và cách chia lát "
            "dữ liệu. Ứng dụng không thu thập những thông tin đó, nên tạo ở đây sẽ sinh ra "
            "một monitor cấu hình dở dang."
        )
        st.markdown(
            "**Việc cần làm trong Databricks:** mở đối tượng trong Catalog Explorer và "
            "tạo monitor ở đó. Sau khi tạo xong, quay lại trang này để xem trạng thái, "
            "chạy lại và xoá."
        )
        st.warning(QualityService.COST_WARNING, icon=":material/payments:")


def _manage(ctx: Context, quality: QualityService, target: Target, view):
    st.markdown("#### Quản lý monitor")

    decision = ctx.authz.check(Action.MANAGE_QUALITY, target)
    if not decision.allowed:
        _denied(ctx, decision)
        return

    operations = []
    if target.kind == "table":
        operations.append("refresh")
    else:
        st.caption(f":material/info: {QualityService.REFRESH_TABLE_ONLY}")
    operations.append("delete")

    st.markdown("##### 1. Chọn thao tác và nhập lý do")
    operation = st.radio(
        "Thao tác", operations,
        format_func=lambda c: OPERATIONS[c], horizontal=True,
        key=f"q_op_{target.key}",
    )

    # The cost and the irreversibility belong here, where the choice is made.
    if operation == "refresh":
        st.warning(QualityService.COST_WARNING, icon=":material/payments:")
    else:
        st.warning(QualityService.DELETE_LEAVES_ASSETS, icon=":material/warning:")

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"q_reason_{target.key}",
        placeholder="Ví dụ: chạy lại sau khi nạp lại dữ liệu theo TICKET-123.",
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )

    # Everything that changes what would be sent invalidates an earlier preview.
    signature = "|".join([
        target.key, operation, reason.strip(), ctx.actor.email,
        view.object_id, "exists" if view.exists else "absent",
    ])
    state.invalidate_plan_if_changed(signature)

    st.markdown("##### 2. Xem trước")
    plan = state.get_plan()
    if plan is None or not state.plan_matches(signature):
        if st.button("Tạo bản xem trước", type="primary", disabled=not reason.strip(),
                     icon=":material/preview:", key=f"q_preview_{target.key}"):
            state.clear_plan()
            try:
                with st.spinner("Đang kiểm tra trạng thái hiện tại…"):
                    built = (quality.plan_refresh(target, reason)
                             if operation == "refresh"
                             else quality.plan_delete_monitor(target, reason))
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, signature)
            st.rerun()
        st.caption(
            "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. "
            "Sửa thao tác hoặc lý do sẽ huỷ bản xem trước cũ."
        )
        return

    _render_preview(plan)
    st.divider()
    _apply_step(quality, target, plan, operation)


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


def _apply_step(quality: QualityService, target: Target, plan, operation: str):
    st.markdown("##### 3. Xác nhận và thực hiện")
    st.caption(f"Nhập lại tên đầy đủ của đối tượng để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đầy đủ", key=f"q_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        clicked = st.button(
            CONFIRM_LABELS.get(operation, "Thực hiện"), type="primary", width="stretch",
            disabled=not matches,
            icon=":material/delete:" if operation == "delete" else ":material/play_arrow:",
            key=f"q_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"q_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not clicked:
        return

    # One plan, one submission: a rerun cannot resubmit it.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner("Đang gửi yêu cầu tới Databricks…"):
            outcome = quality.apply(plan, target, confirmation)
    except Exception as exc:
        state.finish()
        state.clear_plan()
        widgets.show_error(exc)
        return
    finally:
        state.finish()

    state.clear_plan()
    state.clear_caches()
    state.set_notice({
        "status": outcome.status,
        "event_id": outcome.event_id,
        "summary": plan.summary,
        "target": plan.target_name,
        "operation": operation,
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _history(quality: QualityService, target: Target):
    st.markdown("#### Lịch sử các lần làm mới")
    listing = _refresh_listing(quality, target)
    widgets.listing_or_empty(
        listing, what="lần làm mới",
        render=lambda items: widgets.data_table(
            [row.row() for row in items.items],
            key=f"q_refresh_{target.key}",
            download_name=f"{target.full_name}-lich-su-lam-moi",
        ),
    )


def _denied(ctx: Context, decision):
    st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
    if decision.code == Code.WRITES_DISABLED:
        st.markdown(
            "Quản trị viên ứng dụng cần bật `GOVERNANCE_ENABLE_WRITES` trong cấu hình "
            "rồi **deploy lại** ứng dụng."
        )
    elif decision.code == Code.FORBIDDEN_ROLE:
        st.markdown(
            f"Vai trò hiện tại của bạn là **{ctx.authz.role_label}**. "
            "Chạy lại hoặc xoá monitor cần vai trò “Quản trị nền tảng”."
        )
    elif decision.code == Code.FORBIDDEN_SCOPE:
        st.markdown(f"Ứng dụng chỉ được cấu hình quản lý: **{ctx.settings.scope_label}**.")
    elif decision.code == Code.NO_IDENTITY:
        st.markdown("Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn.")


# -- constraints tab -----------------------------------------------------
def _constraints(ctx: Context, quality: QualityService, assets: AssetService,
                 target: Target):
    if target.kind != "table":
        st.info(
            "Ràng buộc được khai báo trên từng bảng. Chọn một bảng ở phần “Đối tượng đang xem” "
            "để xem ràng buộc.",
            icon=":material/table_chart:",
        )
        return

    box = _asset_detail(assets, target)
    if box["error"] is not None:
        widgets.show_error(box["error"], context="Không đọc được metadata của bảng.")
        return

    rows = quality.constraints(box["detail"])
    st.warning(QualityService.CONSTRAINT_ENFORCEMENT, icon=":material/gavel:")

    if not rows:
        widgets.empty_state(
            "empty", what="ràng buộc",
            detail="Bảng này không khai báo ràng buộc nào trong metadata Unity Catalog.",
        )
        _constraint_help()
        return

    enforced = [r for r in rows if r.enforcement == ENFORCED]
    declarative = [r for r in rows if r.enforcement != ENFORCED]

    cols = st.columns(2)
    with cols[0]:
        st.markdown(":material/verified: **Được thực thi khi ghi**")
        if enforced:
            widgets.data_table([r.row() for r in enforced], key=f"q_enf_{target.key}")
        else:
            st.caption("Không có ràng buộc nào được thực thi trên bảng này.")
    with cols[1]:
        st.markdown(":material/info: **Chỉ mang tính khai báo**")
        if declarative:
            widgets.data_table([r.row() for r in declarative], key=f"q_dec_{target.key}")
            st.caption(
                "Những dòng này không chặn dữ liệu sai khi ghi. Đừng dùng chúng làm "
                "bằng chứng về tính duy nhất hay toàn vẹn tham chiếu."
            )
        else:
            st.caption("Không có ràng buộc khai báo nào trên bảng này.")

    with st.expander(f"Tất cả {len(rows)} ràng buộc trong một bảng (để tải CSV)"):
        widgets.data_table(
            [r.row() for r in rows], key=f"q_all_{target.key}",
            download_name=f"{target.full_name}-rang-buoc",
        )
    _constraint_help()


def _constraint_help():
    widgets.explain(
        "Nếu bạn định thêm hoặc gỡ ràng buộc bằng SQL",
        f"{QualityService.CONSTRAINT_PROTOCOL}\n\n"
        "Ứng dụng này không thay đổi ràng buộc: đó là thao tác `ALTER TABLE` trên dữ liệu, "
        "thuộc về quy trình kỹ thuật dữ liệu chứ không phải quản trị quyền.",
        icon=":material/warning:",
    )


# -- result of the last action -------------------------------------------
def _show_result():
    payload = state.pop_notice()
    if not payload:
        return

    status = payload.get("status")
    operation = payload.get("operation", "")
    verified = payload.get("verified") or {}

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{payload.get('summary', 'Hoàn tất')}**",
                       icon=":material/check_circle:")
            st.markdown(f"- Đối tượng: **{payload.get('target', '')}**")
            if status == Status.VERIFIED and operation == "refresh":
                st.caption(
                    "Đã đọc lại từ Databricks. Lần chạy mới nhất: "
                    f"`{verified.get('latest_refresh') or 'Chưa xác định'}` — "
                    f"trạng thái: **{verified.get('state', 'Chưa xác định')}**."
                )
                st.caption(
                    "Lần làm mới chạy bất đồng bộ; thời điểm hoàn thành: Chưa xác định. "
                    "Mở lại trang này để xem trạng thái mới."
                )
            elif status == Status.VERIFIED and operation == "delete":
                still_there = verified.get("monitor_exists")
                if still_there:
                    st.warning(
                        "Databricks nhận yêu cầu nhưng đọc lại vẫn thấy monitor. "
                        "Kiểm tra lại trong Catalog Explorer.",
                        icon=":material/warning:",
                    )
                else:
                    st.caption("Đã đọc lại từ Databricks: đối tượng không còn monitor.")
                st.caption(QualityService.DELETE_LEAVES_ASSETS)
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng "
            "không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            "**Việc cần làm:** Làm mới và kiểm tra trạng thái monitor của "
            f"**{payload.get('target', '')}** TRƯỚC KHI thử lại, để tránh thực hiện hai lần"
            + (" và bị tính phí hai lần." if operation == "refresh" else ".")
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Thao tác không thành công.",
             icon=":material/error:")
