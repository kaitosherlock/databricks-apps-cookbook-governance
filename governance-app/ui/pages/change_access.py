"""Grant and revoke, in three steps: choose, preview, apply.

The whole screen is built around one rule: a preview may only be applied if it
still describes what is on screen and what is true at Databricks. Every input
feeds a signature; if the signature changes, the preview is discarded before it
can be confirmed. That is what stops a Streamlit rerun from applying a stale
plan.
"""
from __future__ import annotations

import streamlit as st

from ucg import privileges as priv
from ucg.audit import Status
from ucg.authz import Action
from ucg.errors import Code, GovernanceError
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.grants import GrantService
from ucg.services.principals import PrincipalService

from .. import nav, picker, state, widgets

ACTIONS = [("grant", "Cấp quyền"), ("revoke", "Thu hồi quyền")]
ACTION_LABELS = dict(ACTIONS)


def render(ctx: Context):
    assets = AssetService(ctx)
    grants = GrantService(ctx)

    st.markdown("### Thay đổi quyền truy cập")

    _show_result()

    target = state.current_target()
    with st.expander("Đối tượng đang thao tác", expanded=target is None):
        picked = picker.level_picker(ctx, assets)
        if picked is not None:
            target = picked
    if target is None:
        st.info("Chọn một đối tượng để bắt đầu.", icon=":material/search:")
        return

    decision = ctx.authz.check(Action.GRANT, target)
    if not decision.allowed:
        _read_only(ctx, target, decision)
        return

    detail = None
    try:
        detail = assets.detail(target)
    except Exception:
        detail = None
    table_type = (detail.sub_type if detail is not None and target.kind == "table" else "")

    picker.header(ctx, target, detail)
    st.divider()

    try:
        available = grants.available_privileges(target, table_type=table_type)
    except GovernanceError as exc:
        widgets.show_error(exc)
        return
    if not available:
        st.warning(
            "Loại đối tượng này không nhận quyền qua API Unity Catalog.",
            icon=":material/block:",
        )
        return

    step1 = _step_choose(ctx, target, available, table_type)
    if step1 is None:
        return
    action, principal, chosen, reason, signature = step1

    st.divider()
    _step_preview(ctx, grants, target, action, principal, chosen, reason,
                  signature, table_type)


# -- step 1 --------------------------------------------------------------
def _step_choose(ctx: Context, target: Target, available, table_type: str):
    prefill = st.session_state.pop("ucg_prefill", None) or {}

    st.markdown("#### 1. Chọn người nhận và quyền")

    cols = st.columns([2, 4])
    with cols[0]:
        codes = [c for c, _ in ACTIONS]
        default_action = prefill.get("action", "grant")
        action = st.radio(
            "Thao tác", codes,
            index=codes.index(default_action) if default_action in codes else 0,
            format_func=lambda c: ACTION_LABELS[c],
            key=f"ca_action_{target.key}", horizontal=True,
        )
    with cols[1]:
        principal = _principal_field(ctx, target, prefill.get("principal", ""))

    chosen = st.multiselect(
        "Quyền", list(available),
        default=[p for p in prefill.get("privileges", []) if p in available],
        format_func=lambda c: priv.info(c).display,
        key=f"ca_privs_{target.key}",
        help="Chỉ hiển thị những quyền hợp lệ với loại đối tượng này.",
    )
    if chosen:
        st.dataframe(priv.explain(chosen), hide_index=True, width="stretch")

    note = priv.traversal_note(target.kind)
    if note:
        st.info(note, icon=":material/route:")

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"ca_reason_{target.key}",
        placeholder="Ví dụ: cấp quyền đọc cho nhóm phân tích theo yêu cầu TICKET-123.",
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )

    # Any change to a material field must invalidate a preview built earlier.
    signature = "|".join([
        target.key, action, principal.strip(),
        ",".join(sorted(chosen)), reason.strip(), ctx.actor.email,
    ])
    state.invalidate_plan_if_changed(signature)

    if not principal.strip() or not chosen or not reason.strip():
        st.caption("Nhập đủ principal, quyền và lý do để tạo bản xem trước.")
        return action, principal, chosen, reason, signature
    return action, principal, chosen, reason, signature


def _principal_field(ctx: Context, target: Target, default: str) -> str:
    """Principal entry with lookup when the API allows it."""
    principals = PrincipalService(ctx)
    key = f"ca_principal_{target.key}"

    mode = st.radio(
        "Chọn người nhận quyền", ["Tìm trong workspace", "Nhập trực tiếp"],
        horizontal=True, key=f"ca_pmode_{target.key}", label_visibility="collapsed",
    )

    if mode == "Nhập trực tiếp":
        value = st.text_input(
            "Principal", value=default, key=key,
            placeholder="email@congty.com · tên account group · application ID",
            help="Unity Catalog nhận: email người dùng, tên account group, "
                 "hoặc application ID của service principal.",
        )
        if value.strip():
            described = principals.describe(value)
            if not described.valid_for_uc:
                st.error(described.note, icon=":material/block:")
            else:
                st.caption(f":material/info: {described.type_label} — {described.note}")
        return value

    term = st.text_input("Tìm theo tên hoặc email", key=f"ca_psearch_{target.key}",
                         placeholder="Nhập ít nhất 2 ký tự")
    if len(term.strip()) < 2:
        st.caption(PrincipalService.SCOPE_NOTE)
        return st.session_state.get(key, default) or default

    with st.spinner("Đang tìm principal…"):
        found = principals.search(term.strip())
    if not found.ok:
        widgets.show_error(found.error, context="Không tra cứu được principal.")
        st.caption("Chuyển sang “Nhập trực tiếp” để nhập định danh thủ công.")
        return default

    usable = [p for p in found.items if p.valid_for_uc]
    if not usable:
        # "Nothing matched" and "we were not allowed to look" must not render
        # the same way: only one of them means the principal does not exist.
        if found.completeness == "partial_permission":
            st.warning(
                "Không đọc được danh bạ người dùng/nhóm của workspace, nên chưa thể "
                "khẳng định có hay không principal khớp.",
                icon=":material/lock:",
            )
            st.caption(
                "Tài khoản dịch vụ của ứng dụng thường chỉ nhìn thấy chính nó trong SCIM "
                "cấp workspace, và SCIM cấp workspace vốn không thấy account group. "
                "Hãy dùng “Nhập trực tiếp” — Unity Catalog vẫn nhận đúng định danh bạn nhập."
            )
        else:
            st.info(
                f"Không có principal nào khớp “{term.strip()}” trong danh bạ đọc được.",
                icon=":material/person_search:",
            )
            st.caption(PrincipalService.SCOPE_NOTE)
        return default

    options = [p.identifier for p in usable]
    labels = {p.identifier: f"{p.label} · {p.type_label}" for p in usable}
    value = st.selectbox(
        "Kết quả", options, key=key,
        index=options.index(default) if default in options else 0,
        format_func=lambda i: labels.get(i, i),
    )
    widgets.completeness_note(found)
    return value


# -- step 2 / 3 ----------------------------------------------------------
def _step_preview(ctx: Context, grants: GrantService, target: Target, action: str,
                  principal: str, chosen: list, reason: str, signature: str,
                  table_type: str):
    st.markdown("#### 2. Xem trước phạm vi ảnh hưởng")

    ready = bool(principal.strip() and chosen and reason.strip())
    plan = state.get_plan()
    valid_plan = plan is not None and state.plan_matches(signature)

    if not valid_plan:
        if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                     icon=":material/preview:", key=f"ca_preview_{target.key}"):
            state.clear_plan()
            try:
                with st.spinner("Đang kiểm tra quyền hiện tại…"):
                    built = grants.plan_change(
                        target, principal, action, chosen, reason,
                        table_type=table_type,
                    )
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, signature)
            st.rerun()
        st.caption(
            "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. "
            "Sửa bất kỳ trường nào ở trên sẽ huỷ bản xem trước cũ."
        )
        return

    _render_preview(plan)
    st.divider()
    _step_apply(ctx, grants, target, plan)


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


def _step_apply(ctx: Context, grants: GrantService, target: Target, plan):
    st.markdown("#### 3. Xác nhận và áp dụng")
    st.caption(
        f"Nhập lại tên đầy đủ của đối tượng để xác nhận: `{plan.target_name}`"
    )

    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đầy đủ", key=f"ca_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            plan.summary[:40] or "Áp dụng", type="primary", width="stretch",
            disabled=not matches, icon=":material/check:",
            key=f"ca_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch",
                     icon=":material/close:", key=f"ca_cancel_{plan.plan_id}"):
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
            outcome = grants.apply(plan, target, confirmation)
    except Exception as exc:
        state.finish()
        state.clear_plan()
        widgets.show_error(exc)
        return
    finally:
        state.finish()

    state.clear_plan()
    state.clear_caches()
    state.set_notice(_outcome_notice(outcome, plan))
    st.rerun()


def _outcome_notice(outcome, plan) -> dict:
    payload = {
        "status": outcome.status,
        "event_id": outcome.event_id,
        "summary": plan.summary,
        "principal": plan.payload.get("principal", ""),
        "target": plan.target_name,
        "privileges": plan.payload.get("privileges", []),
        "verified": outcome.verified_state,
        "message": outcome.message,
    }
    return payload


def _show_result():
    payload = state.pop_notice()
    if not payload:
        return

    status = payload.get("status")
    principal = payload.get("principal", "")
    target = payload.get("target", "")
    privileges = ", ".join(payload.get("privileges") or [])

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(
                f"**{payload.get('summary', 'Hoàn tất')}**", icon=":material/check_circle:"
            )
            st.markdown(
                f"- Principal: **{principal}**\n"
                f"- Quyền: **{privileges}**\n"
                f"- Đối tượng: **{target}**"
            )
            verified = payload.get("verified") or {}
            if status == Status.VERIFIED:
                now = ", ".join(verified.get("privileges_now") or []) or "(không còn quyền trực tiếp nào)"
                st.caption(f"Đã đọc lại từ Databricks. Quyền trực tiếp hiện tại của principal: {now}")
                if not verified.get("applied", True):
                    st.warning(
                        "Databricks nhận yêu cầu nhưng trạng thái đọc lại chưa khớp mong đợi. "
                        "Hãy kiểm tra lại trong Catalog Explorer.",
                        icon=":material/warning:",
                    )
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(
                "Thu hồi một quyền trực tiếp không đảm bảo principal mất toàn bộ "
                "quyền truy cập — họ vẫn có thể còn quyền qua nhóm hoặc cấp cha."
                if "Thu hồi" in payload.get("summary", "") else
                "Quyền có hiệu lực theo mô hình Unity Catalog; principal còn cần "
                "USE_CATALOG / USE_SCHEMA để thực sự dùng được."
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
            "**Việc cần làm:** Làm mới và kiểm tra quyền hiện tại của "
            f"**{principal}** trên **{target}** TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi không thành công.",
             icon=":material/error:")


def _read_only(ctx: Context, target: Target, decision):
    picker.header(ctx, target, None)
    st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")

    if decision.code == Code.WRITES_DISABLED:
        st.markdown(
            "Quản trị viên ứng dụng cần bật `GOVERNANCE_ENABLE_WRITES` trong "
            "cấu hình rồi **deploy lại** ứng dụng. Thay đổi biến môi trường chỉ "
            "có hiệu lực sau khi deploy."
        )
    elif decision.code == Code.FORBIDDEN_ROLE:
        st.markdown(
            f"Vai trò hiện tại của bạn là **{ctx.authz.role_label}**. "
            "Liên hệ quản trị viên ứng dụng để được gán vai trò "
            "“Quản trị quyền truy cập” nếu bạn cần cấp/thu hồi quyền."
        )
    elif decision.code == Code.FORBIDDEN_SCOPE:
        st.markdown(
            f"Ứng dụng chỉ được cấu hình quản lý: **{ctx.settings.scope_label}**."
        )
    elif decision.code == Code.NO_IDENTITY:
        st.markdown("Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn.")

    st.divider()
    nav.link_button("Xem quyền hiện tại", "permissions", icon=":material/key:",
                    widget_key=f"ro_perm_{target.key}")
