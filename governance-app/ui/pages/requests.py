"""Asking for access, and deciding where the ask lands.

Databricks' Request-for-Access surface has three operations: read destinations,
replace destinations, submit a request. There is no queue to list, nothing to
approve, and no grant performed on approval. So this screen stops at "đã gửi"
and says so before the user presses anything - an approval queue drawn on top of
an API that has none would be the most expensive kind of lie a governance tool
can tell.

The two tabs share one preview slot in session state, which is why the plan is
only discarded when it matches neither tab's current inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from ucg import privileges as priv
from ucg.audit import Status
from ucg.authz import Action
from ucg.errors import Code, GovernanceError
from ucg.naming import Target
from ucg.services.access_requests import (
    DESTINATION_LABELS,
    EMAIL,
    EXTERNAL_TYPES,
    MAX_COMMENT,
    MAX_EMAIL_DESTINATIONS,
    MAX_EXTERNAL_DESTINATIONS,
    URL,
    AccessRequestService,
)
from ucg.services.assets import AssetService
from ucg.services.base import Context

from .. import nav, picker, state, widgets

#: Codes that mean "không đủ quyền" rather than "không có dữ liệu".
_PERMISSION_CODES = {Code.PERMISSION_DENIED, Code.FORBIDDEN_ROLE, Code.FORBIDDEN_SCOPE}

COL_KIND = "Kiểu"
COL_VALUE = "Địa chỉ / mã"

#: The editor shows labels; the API takes the codes behind them.
_LABEL_TO_TYPE = {label: code for code, label in DESTINATION_LABELS.items()}
_TYPE_OPTIONS = [DESTINATION_LABELS[c] for c in (EMAIL, *sorted(EXTERNAL_TYPES), URL)]

#: The service prefixes "[UCG:xxxxxxxx] " and silently trims whatever no longer
#: fits, so the input is capped here instead - nothing typed is dropped unseen.
_COMMENT_ROOM = MAX_COMMENT - len("[UCG:") - 8 - len("] ")


def render(ctx: Context) -> None:
    assets = AssetService(ctx)
    service = AccessRequestService(ctx)

    widgets.page_title(
        "Yêu cầu truy cập",
        "Gửi yêu cầu xin quyền, và cấu hình nơi Databricks gửi yêu cầu đó tới.",
    )
    _show_result()

    target = state.current_target()
    with st.expander("Đối tượng đang thao tác", expanded=target is None):
        picked = picker.level_picker(ctx, assets)
        if picked is not None:
            target = picked
    if target is None:
        st.info("Chọn một đối tượng để bắt đầu.", icon=":material/search:")
        return

    picker.header(ctx, target, None)
    st.divider()

    tab_submit, tab_routing = st.tabs(["Gửi yêu cầu", "Nơi nhận yêu cầu"])

    # Inputs first, flows second: both tabs render on every rerun, so the shared
    # preview slot can only be judged once both signatures are known.
    with tab_submit:
        submit_draft = _submit_inputs(ctx, assets, target)
    with tab_routing:
        routing_draft = _routing_inputs(ctx, service, target)

    if routing_draft is not None and state.plan_matches(routing_draft.signature):
        state.invalidate_plan_if_changed(routing_draft.signature)
    elif submit_draft is not None:
        state.invalidate_plan_if_changed(submit_draft.signature)
    elif routing_draft is not None:
        state.invalidate_plan_if_changed(routing_draft.signature)
    else:
        state.clear_plan()

    with tab_submit:
        if submit_draft is not None:
            _submit_flow(service, target, submit_draft)
    with tab_routing:
        if routing_draft is not None:
            _routing_flow(service, target, routing_draft)

    st.divider()
    _known_limits()


# -- tab 1: submitting a request -----------------------------------------
@dataclass
class _SubmitDraft:
    principal: str
    privileges: list
    comment: str
    signature: str

    @property
    def ready(self) -> bool:
        return bool(self.principal.strip() and self.privileges and self.comment.strip())


def _submit_inputs(ctx: Context, assets: AssetService, target: Target):
    st.markdown("#### 1. Chọn người nhận quyền và quyền cần xin")

    decision = ctx.authz.check(Action.SUBMIT_REQUEST, target)
    if not decision.allowed:
        _denied(ctx, decision)
        return None

    available = priv.for_securable(
        target.securable_type, kind=target.kind, table_type=_table_type(assets, target),
    )
    if not available:
        st.warning(
            "Loại đối tượng này không nhận quyền qua API Unity Catalog, "
            "nên không có quyền nào để xin.",
            icon=":material/block:",
        )
        return None

    principal_key = f"rq_principal_{target.key}"
    if principal_key not in st.session_state:
        # Xin quyền cho chính mình là trường hợp phổ biến nhất.
        st.session_state[principal_key] = ctx.actor.email

    cols = st.columns([3, 2])
    with cols[0]:
        principal = st.text_input(
            "Principal sẽ nhận quyền", key=principal_key,
            placeholder="email@congty.com · tên account group · application ID",
            help="Unity Catalog nhận: email người dùng, tên account group, "
                 "hoặc application ID của service principal.",
        )
    with cols[1]:
        st.write("")
        nav.link_button(
            "Xem quyền hiện tại", "permissions", icon=":material/key:",
            width="stretch", widget_key=f"rq_seeperm_{target.key}",
            help="Kiểm tra principal đã có sẵn quyền gì trước khi xin thêm.",
        )

    chosen = st.multiselect(
        "Quyền cần xin", list(available), format_func=priv.display,
        key=f"rq_privs_{target.key}",
        help="Chỉ hiển thị những quyền hợp lệ với loại đối tượng này.",
    )
    if chosen:
        st.dataframe(priv.explain(chosen), hide_index=True, width="stretch")
        widgets.warn_block(priv.warnings_for(chosen, target.kind))
        if "SELECT" in chosen:
            st.info(AccessRequestService.PREREQUISITE_FANOUT, icon=":material/call_split:")

    note = priv.traversal_note(target.kind)
    if note:
        st.info(note, icon=":material/route:")

    comment = st.text_area(
        "Lý do cần quyền", max_chars=_COMMENT_ROOM, key=f"rq_comment_{target.key}",
        placeholder="Ví dụ: cần đọc bảng để dựng báo cáo doanh thu theo TICKET-123.",
        help=f"Databricks giới hạn ghi chú {MAX_COMMENT} ký tự; ứng dụng dành "
             f"{MAX_COMMENT - _COMMENT_ROOM} ký tự đầu cho mã đối chiếu của mình.",
    )

    signature = "|".join([
        "submit", target.key, principal.strip(), ",".join(sorted(chosen)),
        comment.strip(), ctx.actor.email, ctx.execution_identity,
    ])
    return _SubmitDraft(principal, chosen, comment, signature)


def _submit_flow(service: AccessRequestService, target: Target, draft: _SubmitDraft):
    st.divider()
    st.markdown("#### 2. Xem trước yêu cầu sẽ gửi")

    # Người dùng phải biết điều này TRƯỚC khi bấm gửi, không phải sau.
    st.warning(AccessRequestService.NO_APPROVAL_API, icon=":material/gavel:")

    plan = state.get_plan()
    if plan is None or not state.plan_matches(draft.signature):
        if st.button("Tạo bản xem trước", type="primary", disabled=not draft.ready,
                     icon=":material/preview:", key=f"rq_preview_{target.key}"):
            state.clear_plan()
            try:
                with st.spinner("Đang chuẩn bị yêu cầu…"):
                    built = service.plan_submit_request(
                        target, draft.principal, draft.privileges, draft.comment,
                    )
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, draft.signature)
            st.rerun()
        if not draft.ready:
            st.caption("Nhập đủ principal, quyền và lý do để tạo bản xem trước.")
        else:
            st.caption("Bản xem trước chỉ đọc dữ liệu, chưa gửi gì tới người duyệt. "
                       "Sửa bất kỳ trường nào ở trên sẽ huỷ bản xem trước cũ.")
        return

    _render_preview(plan)
    st.divider()
    _apply_step(
        service, target, plan,
        button_label="Gửi yêu cầu", icon=":material/send:",
        spinner="Đang gửi yêu cầu tới Databricks…",
        key_prefix="rq", build_notice=_submit_notice,
    )


def _submit_notice(outcome, plan) -> dict:
    return {
        "kind": "submit",
        "status": outcome.status,
        "event_id": outcome.event_id,
        "message": outcome.message,
        "principal": plan.payload.get("principal", ""),
        "privileges": plan.payload.get("privileges", []),
        "target": plan.target_name,
        "correlation_id": plan.payload.get("correlation_id", ""),
    }


# -- tab 2: where requests are routed ------------------------------------
@dataclass
class _RoutingDraft:
    entries: list
    reason: str
    signature: str
    blocked: bool

    @property
    def ready(self) -> bool:
        return bool(self.reason.strip()) and not self.blocked


def _routing_inputs(ctx: Context, service: AccessRequestService, target: Target):
    st.markdown("#### Nơi nhận đang được đặt trực tiếp trên đối tượng này")
    st.info(AccessRequestService.ROUTING_IS_SERVER_SIDE, icon=":material/alt_route:")

    read_decision = ctx.authz.check(Action.READ_REQUESTS, target)
    if not read_decision.allowed:
        widgets.empty_state("forbidden", what="cấu hình nơi nhận",
                            detail=read_decision.reason)
        return None

    view = _destinations(service, target)
    if view is None:
        return None
    if not view.ok:
        err = view.error
        if getattr(err, "code", "") in _PERMISSION_CODES:
            widgets.empty_state("forbidden", what="cấu hình nơi nhận", detail=err.message)
        else:
            widgets.show_error(err, context="Không đọc được nơi nhận yêu cầu.")
        return None

    if view.configured_here:
        widgets.data_table(
            view.rows(), key=f"rfa_dest_{target.key}",
            download_name=f"{target.full_name}-noi-nhan-yeu-cau",
        )
    else:
        widgets.empty_state("empty", what="nơi nhận đặt trực tiếp")

    if view.inherited_from:
        st.caption(f":material/account_tree: Databricks cho biết cấu hình đang áp dụng "
                   f"đến từ: **{view.inherited_from}**.")
    if view.any_hidden:
        st.warning(
            "Databricks báo rằng có nơi nhận bị ẩn với danh tính thực thi. "
            "Danh sách trên **chưa đầy đủ**, và vì cập nhật là thay thế toàn bộ, "
            "việc lưu danh sách mới có thể gỡ mất những nơi nhận bạn không nhìn thấy.",
            icon=":material/visibility_off:",
        )
    if view.observed_at:
        st.caption(f"Dữ liệu đọc lúc {view.observed_at}.")

    st.divider()
    st.markdown("#### 1. Sửa danh sách nơi nhận")

    decision = ctx.authz.check(Action.MANAGE_REQUEST_ROUTING, target)
    if not decision.allowed:
        _denied(ctx, decision)
        return None

    st.warning(AccessRequestService.REPLACE_SEMANTICS, icon=":material/swap_horiz:")

    editable, fixed = [], []
    for dest in view.destinations:
        if dest.special or not dest.destination_id or dest.destination_type not in DESTINATION_LABELS:
            fixed.append(dest)
        else:
            editable.append(dest)
    if fixed:
        st.warning(
            "Không sửa được qua API này: "
            + ", ".join(f"{d.type_label} {d.destination_id or ''}".strip() for d in fixed)
            + ". Nếu lưu danh sách mới, những mục đó sẽ bị gỡ khỏi cấu hình.",
            icon=":material/edit_off:",
        )

    rows = [{COL_KIND: DESTINATION_LABELS[d.destination_type], COL_VALUE: d.destination_id}
            for d in editable] or [{COL_KIND: None, COL_VALUE: ""}]
    edited = st.data_editor(
        rows, num_rows="dynamic", width="stretch", hide_index=True,
        key=f"rfa_editor_{target.key}",
        column_config={
            COL_KIND: st.column_config.SelectboxColumn(
                COL_KIND, options=_TYPE_OPTIONS, width="medium",
                help="Kiểu URL loại trừ mọi kiểu khác.",
            ),
            COL_VALUE: st.column_config.TextColumn(
                COL_VALUE, width="large",
                help="Email người nhận, hoặc mã nơi nhận trong Notification destinations, "
                     "hoặc địa chỉ https:// cho kiểu URL.",
            ),
        },
    )
    records = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)

    entries, problems = [], []
    for index, row in enumerate(records, start=1):
        label = str(row.get(COL_KIND) or "").strip()
        value = str(row.get(COL_VALUE) or "").strip()
        if not label and not value:
            continue
        entry = {"type": _LABEL_TO_TYPE.get(label, label), "id": value}
        try:
            service.validate_destination(entry)
        except GovernanceError as exc:
            problems.append(f"Dòng {index}: {exc.message}")
        entries.append(entry)

    for problem in problems:
        st.error(problem, icon=":material/error:")

    emails = sum(1 for e in entries if e["type"] == EMAIL)
    external = sum(1 for e in entries if e["type"] in EXTERNAL_TYPES)
    st.caption(
        f"Email: {emails}/{MAX_EMAIL_DESTINATIONS} · "
        f"Nơi nhận bên ngoài: {external}/{MAX_EXTERNAL_DESTINATIONS}. "
        "Danh sách rỗng là hợp lệ: Databricks sẽ định tuyến theo cấp cha hoặc metastore."
    )
    if any(e["type"] == URL for e in entries):
        st.warning(AccessRequestService.URL_EXCLUSIVE, icon=":material/link_off:")
    if external:
        st.info(AccessRequestService.WEBHOOK_ID_UNVERIFIED, icon=":material/help:")

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"rfa_reason_{target.key}",
        placeholder="Ví dụ: chuyển yêu cầu truy cập sang nhóm quản trị dữ liệu mới.",
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )

    signature = "|".join([
        "routing", target.key,
        ";".join(f"{e['type']}={e['id']}" for e in entries),
        reason.strip(), ctx.actor.email, ctx.execution_identity,
    ])
    return _RoutingDraft(entries, reason, signature, bool(problems))


def _routing_flow(service: AccessRequestService, target: Target, draft: _RoutingDraft):
    st.divider()
    st.markdown("#### 2. Xem trước thay đổi")

    plan = state.get_plan()
    if plan is None or not state.plan_matches(draft.signature):
        if st.button("Tạo bản xem trước", type="primary", disabled=not draft.ready,
                     icon=":material/preview:", key=f"rfa_preview_{target.key}"):
            state.clear_plan()
            try:
                with st.spinner("Đang đọc lại cấu hình hiện tại…"):
                    built = service.plan_set_destinations(target, draft.entries, draft.reason)
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, draft.signature)
            st.rerun()
        if draft.blocked:
            st.caption("Sửa các dòng còn lỗi ở trên để tạo được bản xem trước.")
        elif not draft.reason.strip():
            st.caption("Nhập lý do thay đổi để tạo bản xem trước.")
        else:
            st.caption("Bản xem trước chỉ đọc dữ liệu, chưa thay đổi gì. "
                       "Sửa danh sách hoặc lý do sẽ huỷ bản xem trước cũ.")
        return

    _render_preview(plan)
    st.divider()
    _apply_step(
        service, target, plan,
        button_label="Lưu danh sách nơi nhận", icon=":material/save:",
        spinner="Đang cập nhật nơi nhận tại Databricks…",
        key_prefix="rfa", build_notice=_routing_notice,
    )


def _routing_notice(outcome, plan) -> dict:
    return {
        "kind": "routing",
        "status": outcome.status,
        "event_id": outcome.event_id,
        "message": outcome.message,
        "target": plan.target_name,
        "verified": outcome.verified_state or {},
    }


def _destinations(service: AccessRequestService, target: Target):
    key = f"rfa-dest:{target.key}"
    cached = state.cache_get(key)
    if cached is not None:
        return cached
    try:
        with st.spinner("Đang đọc nơi nhận yêu cầu từ Databricks…"):
            view = service.destinations(target)
    except Exception as exc:
        # Phạm vi catalog bị chặn trước cả khi gọi API; đó vẫn là một lỗi đọc.
        widgets.show_error(exc, context="Không đọc được nơi nhận yêu cầu.")
        return None
    return state.cache_put(key, view)


# -- shared preview / apply ----------------------------------------------
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


def _apply_step(service: AccessRequestService, target: Target, plan, *,
                button_label: str, icon: str, spinner: str, key_prefix: str,
                build_notice):
    st.markdown("#### 3. Xác nhận và thực hiện")
    st.caption(f"Nhập lại tên đầy đủ của đối tượng để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đầy đủ", key=f"{key_prefix}_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            button_label, type="primary", width="stretch", disabled=not matches,
            icon=icon, key=f"{key_prefix}_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"{key_prefix}_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not apply_clicked:
        return

    # Một bản xem trước chỉ được gửi một lần, kể cả khi trang rerun.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner(spinner):
            outcome = service.apply(plan, target, confirmation)
    except Exception as exc:
        state.clear_plan()
        widgets.show_error(exc)
        return
    finally:
        state.finish()

    state.clear_plan()
    state.clear_caches()
    state.set_notice(build_notice(outcome, plan))
    st.rerun()


# -- results --------------------------------------------------------------
def _show_result():
    payload = state.pop_notice()
    if not payload:
        return
    if payload.get("kind") == "submit":
        _submit_result(payload)
    elif payload.get("kind") == "routing":
        _routing_result(payload)


def _submit_result(payload: dict):
    status = payload.get("status")
    principal = payload.get("principal", "")
    privileges = ", ".join(payload.get("privileges") or [])
    target = payload.get("target", "")

    if status in (Status.APPLIED, Status.VERIFIED):
        with st.container(border=True):
            st.success("**Đã gửi yêu cầu tới Databricks.**", icon=":material/send:")
            st.markdown(
                f"- Principal: **{principal}**\n"
                f"- Quyền đã xin: **{privileges}**\n"
                f"- Đối tượng: **{target}**"
            )
            st.caption(
                "Databricks đã nhận yêu cầu. Việc phê duyệt và cấp quyền do người duyệt "
                "thực hiện thủ công trong giao diện Databricks — ứng dụng không theo dõi được."
            )
            st.info(AccessRequestService.NO_REQUEST_ID, icon=":material/help:")
            st.info(AccessRequestService.APP_NOT_A_NOTIFIER, icon=":material/notifications_off:")
            st.caption(
                f"Mã đối chiếu của ứng dụng: `{payload.get('correlation_id', '')}` "
                f"· Event ID: `{payload.get('event_id', '')[:12]}`"
            )
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu có thể đã tới Databricks "
            "nhưng ứng dụng không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** hỏi người duyệt của **{target}** xem đã nhận được "
            "yêu cầu chưa TRƯỚC KHI gửi lại, để tránh gửi trùng."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Không gửi được yêu cầu.", icon=":material/error:")


def _routing_result(payload: dict):
    status = payload.get("status")
    target = payload.get("target", "")

    if status in (Status.APPLIED, Status.VERIFIED):
        with st.container(border=True):
            st.success(f"**Đã cập nhật nơi nhận yêu cầu cho {target}.**",
                       icon=":material/check_circle:")
            if status == Status.VERIFIED:
                current = (payload.get("verified") or {}).get("destinations") or []
                rows = [_verified_row(item) for item in current]
                if rows:
                    st.caption("Đọc lại từ Databricks — danh sách hiện tại:")
                    widgets.data_table(rows, key="rfa_result_table")
                else:
                    st.caption(
                        "Đọc lại từ Databricks: đối tượng này không còn nơi nhận đặt "
                        "trực tiếp. Yêu cầu vẫn được định tuyến theo cấp cha hoặc metastore."
                    )
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Thay đổi có thể đã được áp dụng "
            "nhưng ứng dụng chưa đọc lại được để xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** làm mới trang và đọc lại danh sách nơi nhận của "
            f"**{target}** TRƯỚC KHI lưu lại, vì mỗi lần lưu là thay thế toàn bộ."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Không cập nhật được nơi nhận.",
             icon=":material/error:")


def _verified_row(item: str) -> dict:
    kind, _, value = str(item).partition(":")
    return {COL_KIND: DESTINATION_LABELS.get(kind, kind), COL_VALUE: value or "—"}


# -- shared explanations ---------------------------------------------------
def _denied(ctx: Context, decision):
    st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")

    if decision.code == Code.WRITES_DISABLED:
        st.markdown(
            "Quản trị viên ứng dụng cần bật `GOVERNANCE_ENABLE_WRITES` trong cấu hình "
            "rồi **deploy lại** ứng dụng. Thay đổi biến môi trường chỉ có hiệu lực sau khi deploy."
        )
    elif decision.code == Code.FORBIDDEN_ROLE:
        st.markdown(
            f"Vai trò hiện tại của bạn là **{ctx.authz.role_label}**. "
            "Liên hệ quản trị viên ứng dụng nếu bạn cần thực hiện thao tác này."
        )
    elif decision.code == Code.FORBIDDEN_SCOPE:
        st.markdown(
            f"Ứng dụng chỉ được cấu hình quản lý: **{ctx.settings.scope_label}**."
        )
    elif decision.code == Code.NO_IDENTITY:
        st.markdown("Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn.")


def _table_type(assets: AssetService, target: Target) -> str:
    """Sub-type of a table, used only to narrow the privilege list."""
    if target.kind != "table":
        return ""
    key = f"subtype:{target.key}"
    cached = state.cache_get(key)
    if cached is not None:
        return cached
    try:
        with st.spinner("Đang đọc thông tin đối tượng…"):
            return state.cache_put(key, assets.detail(target).sub_type or "")
    except Exception:
        # Không đọc được sub-type chỉ làm danh sách quyền rộng hơn một chút;
        # Unity Catalog vẫn là bên quyết định cuối cùng.
        return state.cache_put(key, "")


def _known_limits():
    with st.expander("Giới hạn đã biết", expanded=False):
        for icon, text in (
            (":material/gavel:", AccessRequestService.NO_APPROVAL_API),
            (":material/schedule:", AccessRequestService.NO_TIME_BOUND_GRANTS),
            (":material/call_split:", AccessRequestService.PREREQUISITE_FANOUT),
            (":material/help:", AccessRequestService.WEBHOOK_ID_UNVERIFIED),
        ):
            st.markdown(f"{icon} {text}")
