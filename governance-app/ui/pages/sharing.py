"""Delta Sharing: shares, recipients và providers.

Đây là màn hình có hệ quả lớn nhất: mỗi thay đổi ở đây đều đẩy dữ liệu ra
ngoài phạm vi kiểm soát của workspace. Ba quy tắc chi phối toàn bộ layout:

* không có credential nào xuất hiện trên màn hình - chỗ người dùng đi tìm link
  kích hoạt là chỗ đặt lời giải thích vì sao không có nó;
* phần giải thích của service (bảo vệ không đi theo dữ liệu, share phụ thuộc
  quyền của chủ share) nằm ngay cạnh nút bấm, không nằm trong tài liệu;
* mọi thay đổi đi qua đúng ba bước: dựng plan, xem trước, gõ lại tên share.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.capabilities import State as CapState
from ucg.errors import Code
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.sharing import SharingService

from .. import nav, picker, state, widgets

#: Kind trong ứng dụng -> data_object_type mà API shares nhận.
_KIND_TO_SHARE_TYPE = {
    "table": "TABLE",
    "volume": "VOLUME",
    "function": "FUNCTION",
    "model": "MODEL",
}

#: TableInfo.table_type -> data_object_type, khi hai bên không trùng tên.
_TABLE_TYPE_TO_SHARE_TYPE = {
    "VIEW": "VIEW",
    "MATERIALIZED_VIEW": "MATERIALIZED_VIEW",
    "STREAMING_TABLE": "STREAMING_TABLE",
    "FOREIGN": "FOREIGN_TABLE",
}

_OPERATIONS = [
    ("add", "Thêm tài sản vào share"),
    ("remove", "Gỡ tài sản khỏi share"),
    ("permission", "Cấp / thu hồi quyền recipient"),
]
_OPERATION_LABELS = dict(_OPERATIONS)

#: Trạng thái capability đã đủ dứt khoát để chặn cả tab. UNKNOWN thì không:
#: capability chỉ được probe khi có người thực sự gọi API.
_BLOCKING_CAPABILITY_STATES = (
    CapState.NOT_CONFIGURED, CapState.UNSUPPORTED, CapState.NOT_IMPLEMENTED,
)


def render(ctx: Context) -> None:
    service = SharingService(ctx)

    widgets.page_title(
        "Chia sẻ dữ liệu",
        "Share, recipient và provider của Delta Sharing. "
        "Mọi thao tác ở đây đều ảnh hưởng tới dữ liệu đi ra ngoài workspace.",
    )

    _show_result()

    read = ctx.authz.check(Action.READ_SHARING)
    if not read.allowed:
        st.warning(f"**Không đủ quyền** — {read.reason}", icon=":material/lock:")
        st.caption("Liên hệ quản trị viên ứng dụng nếu bạn cần xem phần chia sẻ dữ liệu.")
        nav.link_button("Xem khả năng và cấu hình", "diagnostics",
                        icon=":material/settings:", widget_key="sh_ro_diag")
        return

    cols = st.columns([6, 2])
    with cols[1]:
        if st.button("Đọc lại từ Databricks", width="stretch",
                     icon=":material/refresh:", key="sh_refresh"):
            state.clear_caches()
            st.rerun()

    tab_shares, tab_recipients, tab_providers, tab_findings = st.tabs(
        ["Shares", "Recipients", "Providers", "Cần xem xét"]
    )

    with tab_shares:
        _shares_tab(ctx, service)
    with tab_recipients:
        _recipients_tab(ctx, service)
    with tab_providers:
        _providers_tab(ctx, service)
    with tab_findings:
        _findings_tab(ctx, service)


# -- đọc có cache --------------------------------------------------------
def _cached(key: str, produce, message: str):
    """Đọc một lần cho mỗi phiên, để quay lại tab không phải gọi API lại."""
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    with st.spinner(message):
        return state.cache_put(key, produce())


def _capability_blocked(ctx: Context, key: str) -> bool:
    cap = ctx.capabilities.get(key)
    if cap.state in _BLOCKING_CAPABILITY_STATES:
        widgets.capability_notice(cap)
        return True
    return False


# -- tab Shares ----------------------------------------------------------
def _shares_tab(ctx: Context, service: SharingService):
    if _capability_blocked(ctx, "sharing.shares"):
        return

    listing = _cached("sharing:shares", service.shares, "Đang đọc danh sách share…")
    rendered = widgets.listing_or_empty(
        listing, what="share",
        render=lambda l: widgets.data_table(
            [r.row() for r in l.items], key="sharing_shares",
            download_name="danh-sach-share",
        ),
    )
    if not rendered:
        # Ở nhánh lỗi, listing.note không được hiển thị, nên nói rõ giới hạn:
        # một danh sách ngắn hoặc rỗng ở đây không chứng minh điều gì.
        widgets.caveat(SharingService.LIST_MAY_BE_PARTIAL)
        return

    st.divider()
    names = [r.name for r in listing.items]
    chosen = st.selectbox("Chọn share để xem chi tiết", names, key="sh_pick_share")
    if not chosen:
        return
    _share_panel(ctx, service, chosen)


def _share_panel(ctx: Context, service: SharingService, share: str):
    detail = _cached(f"sharing:share:{share}",
                     lambda: service.share_detail(share),
                     f"Đang đọc chi tiết share {share}…")
    if not detail.ok:
        widgets.show_error(detail.error, context="Không đọc được chi tiết share.")
        return

    widgets.breadcrumb([("Share", detail.ref.name)])
    st.markdown(f"#### {detail.ref.name}")
    if detail.ref.comment:
        st.markdown(detail.ref.comment)

    cols = st.columns([2, 2, 3])
    cols[0].metric("Số tài sản trong share", detail.ref.object_count)
    cols[1].metric("Tài sản chủ share mất quyền", detail.ref.denied_count)
    cols[2].markdown(
        f"**Chủ sở hữu**  \n{detail.ref.owner or 'Chưa xác định'}"
    )
    st.caption(
        f"Đọc lúc {detail.observed_at}."
        + (f" Tạo lúc {detail.ref.created}." if detail.ref.created else "")
    )

    _share_objects(detail)
    st.divider()
    _share_recipients(detail)

    st.divider()
    decision = ctx.authz.check(Action.MANAGE_SHARING)
    if not decision.allowed:
        _read_only(ctx, decision)
        return
    _mutations(ctx, service, detail)


def _share_objects(detail):
    st.markdown("**Tài sản đang được chia sẻ**")
    if not detail.objects:
        st.info("Share này chưa chứa tài sản nào.", icon=":material/inbox:")
        st.caption(
            "Đây là nội dung Databricks trả về cho danh tính thực thi tại thời điểm đọc."
        )
        return

    widgets.data_table(
        [o.row() for o in detail.objects],
        key=f"sharing_objects_{detail.ref.name}",
        download_name=f"share-{detail.ref.name}-tai-san",
    )

    inactive = [o for o in detail.objects if not o.active]
    if not inactive:
        st.markdown(
            ":material/check_circle: **Đang hoạt động** — mọi tài sản trong share "
            "đều ở trạng thái ACTIVE."
        )
        return

    st.warning(
        f"**Mất truy cập** — {len(inactive)} tài sản không còn đến được recipient.",
        icon=":material/block:",
    )
    for obj in inactive:
        st.markdown(
            f":material/block: `{obj.name}` — trạng thái `{obj.status}` "
            f"({obj.object_type or 'không rõ loại'})"
        )
    st.warning(SharingService.OWNER_PRIVILEGE_DEPENDENCY, icon=":material/warning:")


def _share_recipients(detail):
    st.markdown("**Recipient đang có quyền trên share này**")
    if detail.recipients:
        widgets.data_table(
            detail.recipients,
            key=f"sharing_share_recipients_{detail.ref.name}",
            download_name=f"share-{detail.ref.name}-recipient",
        )
        return

    st.info("Không thấy recipient nào trên share này.", icon=":material/inbox:")
    st.caption(
        "Ứng dụng **không phân biệt được** giữa “share chưa cấp cho recipient nào” và "
        "“danh tính thực thi không đọc được quyền của share”. Nếu cần chắc chắn, "
        "đối chiếu trong Catalog Explorer."
    )


def _read_only(ctx: Context, decision):
    st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
    if decision.code == Code.WRITES_DISABLED:
        st.markdown(
            "Quản trị viên ứng dụng cần bật `GOVERNANCE_ENABLE_WRITES` trong cấu hình "
            "rồi **deploy lại**. Đổi biến môi trường chỉ có hiệu lực sau khi deploy."
        )
    elif decision.code == Code.FORBIDDEN_ROLE:
        st.markdown(
            f"Vai trò hiện tại của bạn là **{ctx.authz.role_label}**. "
            "Chỉ vai trò quản trị nền tảng mới được thay đổi share."
        )
    elif decision.code == Code.NO_IDENTITY:
        st.markdown(
            "Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn."
        )
    nav.link_button("Xem khả năng và cấu hình", "diagnostics",
                    icon=":material/settings:", widget_key="sh_ro_caps")


# -- ba bước thay đổi ----------------------------------------------------
def _mutations(ctx: Context, service: SharingService, detail):
    share = detail.ref.name
    st.markdown("#### Thay đổi share")
    codes = [c for c, _ in _OPERATIONS]
    operation = st.radio(
        "Thao tác", codes, format_func=lambda c: _OPERATION_LABELS[c],
        horizontal=True, key=f"sh_op_{share}",
    )

    if operation == "add":
        _flow_add(ctx, service, detail)
    elif operation == "remove":
        _flow_remove(ctx, service, detail)
    else:
        _flow_permission(ctx, service, detail)


def _reason_field(share: str, operation: str, placeholder: str) -> str:
    return st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"sh_reason_{operation}_{share}",
        placeholder=placeholder,
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )


def _flow_add(ctx: Context, service: SharingService, detail):
    share = detail.ref.name
    st.warning(SharingService.PROTECTION_DOES_NOT_TRAVEL, icon=":material/warning:")
    widgets.caveat(SharingService.OPEN_SHARING_LIMIT)

    st.markdown("##### 1. Chọn tài sản cần đưa vào share")
    assets = AssetService(ctx)
    target = picker.compact_picker(ctx, assets)

    object_type = ""
    protections: dict | None = None
    blocked = ""
    if target is not None:
        object_type, protections, blocked = _asset_facts(assets, target)
        cols = st.columns([4, 2])
        cols[0].markdown(f"**Tên đầy đủ gửi tới API**  \n`{target.full_name}`")
        cols[1].markdown(f"**data_object_type**  \n`{object_type}`")
        nav.link_button("Mở thông tin tài sản", "asset", icon=":material/north_east:",
                        widget_key=f"sh_go_asset_{share}")

    reason = _reason_field(
        share, "add",
        "Ví dụ: chia sẻ bảng doanh thu tháng cho đối tác theo hợp đồng HD-2026-11.",
    )

    signature = "|".join([
        "add", share,
        target.full_name if target is not None else "",
        object_type,
        "chan" if blocked else ("da-doc" if protections is not None else "chua-doc"),
        reason.strip(), ctx.actor.email,
    ])
    state.invalidate_plan_if_changed(signature)

    if blocked:
        st.error(blocked, icon=":material/block:")

    ready = target is not None and bool(reason.strip()) and not blocked
    if target is not None and not reason.strip():
        st.caption("Nhập lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, service, detail, signature, operation="add", ready=ready,
        build=lambda: service.plan_add_object(
            share, target.full_name, object_type, reason, protections=protections,
        ),
        apply_label=f"Thêm {target.full_name} vào {share}" if target is not None else "Thêm tài sản",
    )


def _asset_facts(assets: AssetService, target: Target):
    """data_object_type, protections và lý do chặn (nếu có) của một tài sản.

    Bảng có row filter hoặc column mask gắn trực tiếp thì Databricks từ chối
    chia sẻ, nên đọc trước để nói rõ lý do thay vì để plan ném lỗi.
    """
    object_type = _KIND_TO_SHARE_TYPE.get(target.kind, "TABLE")
    if target.kind != "table":
        return object_type, None, ""

    key = f"sharing:asset:{target.key}"
    detail = state.cache_get(key)
    if detail is None:
        try:
            with st.spinner("Đang đọc cấu hình bảo vệ của bảng…"):
                detail = state.cache_put(key, assets.detail(target))
        except Exception as exc:
            widgets.show_error(exc, context="Không đọc được chi tiết bảng.")
            st.caption(
                "Chưa xác định được bảng này có row filter hay column mask hay không. "
                "Nếu có, Databricks sẽ từ chối khi áp dụng."
            )
            return object_type, None, ""

    object_type = _TABLE_TYPE_TO_SHARE_TYPE.get(
        (detail.sub_type or "").upper(), "TABLE"
    )
    masks = sorted(detail.column_masks or {})
    if detail.row_filter or masks:
        parts = []
        if detail.row_filter:
            parts.append("row filter gắn trực tiếp trên bảng")
        if masks:
            parts.append("column mask trên cột: " + ", ".join(f"`{c}`" for c in masks))
        return object_type, {"row_filter": detail.row_filter,
                             "column_masks": detail.column_masks}, (
            "**Không chia sẻ được** — bảng này có " + " và ".join(parts) + ". "
            "Databricks không cho chia sẻ bảng như vậy; đây là giới hạn của sản phẩm, "
            "không phải vấn đề quyền."
        )

    st.markdown(
        ":material/check_circle: **Không có** row filter hay column mask gắn trực tiếp "
        "trên bảng này."
    )
    return object_type, {"row_filter": None, "column_masks": {}}, ""


def _flow_remove(ctx: Context, service: SharingService, detail):
    share = detail.ref.name
    st.markdown("##### 1. Chọn tài sản cần gỡ")
    names = [o.name for o in detail.objects]
    if not names:
        st.info("Share này chưa có tài sản nào để gỡ.", icon=":material/inbox:")
        return

    chosen = st.selectbox("Tài sản trong share", names, key=f"sh_rm_obj_{share}")
    reason = _reason_field(
        share, "remove",
        "Ví dụ: kết thúc hợp đồng chia sẻ, gỡ bảng theo yêu cầu TICKET-456.",
    )

    signature = "|".join(["remove", share, chosen, reason.strip(), ctx.actor.email])
    state.invalidate_plan_if_changed(signature)

    if not reason.strip():
        st.caption("Nhập lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, service, detail, signature, operation="remove",
        ready=bool(chosen and reason.strip()),
        build=lambda: service.plan_remove_object(share, chosen, reason),
        apply_label=f"Gỡ {chosen} khỏi {share}",
    )


def _flow_permission(ctx: Context, service: SharingService, detail):
    share = detail.ref.name
    st.markdown("##### 1. Chọn recipient và chiều thay đổi")

    grant = st.radio(
        "Chiều thay đổi", [True, False],
        format_func=lambda g: "Cấp SELECT trên share" if g else "Thu hồi SELECT trên share",
        horizontal=True, key=f"sh_perm_dir_{share}",
    )

    current = sorted({row.get("Recipient", "") for row in detail.recipients if row.get("Recipient")})
    recipient = _recipient_choice(ctx, service, share, grant, current)

    info = None
    if grant and recipient:
        info = _recipient_from_cache(service, recipient)
        if info is not None and info.external:
            st.warning(
                f":material/public: **Ngoài tổ chức** — recipient `{recipient}` dùng "
                f"{info.kind_label}. Dữ liệu trong share sẽ ra ngoài phạm vi kiểm soát "
                "của workspace.",
                icon=":material/public:",
            )
            widgets.caveat(SharingService.OPEN_SHARING_LIMIT)
        st.warning(SharingService.PROTECTION_DOES_NOT_TRAVEL, icon=":material/warning:")

    reason = _reason_field(
        share, "permission",
        "Ví dụ: cấp quyền cho đối tác theo phụ lục hợp đồng PL-07.",
    )

    signature = "|".join([
        "permission", share, recipient.strip(), "grant" if grant else "revoke",
        reason.strip(), ctx.actor.email,
    ])
    state.invalidate_plan_if_changed(signature)

    if recipient and not reason.strip():
        st.caption("Nhập lý do để tạo bản xem trước.")

    verb = "Cấp SELECT cho" if grant else "Thu hồi SELECT của"
    _preview_and_apply(
        ctx, service, detail, signature, operation="permission",
        ready=bool(recipient.strip() and reason.strip()),
        build=lambda: service.plan_share_permission(share, recipient, bool(grant), reason),
        apply_label=f"{verb} {recipient}" if recipient else "Thay đổi quyền recipient",
    )


def _recipient_choice(ctx: Context, service: SharingService, share: str,
                      grant: bool, current: list[str]) -> str:
    """Danh sách recipient để chọn, hoặc ô nhập tay khi không đọc được."""
    if not grant:
        if not current:
            st.info("Share này chưa cấp quyền cho recipient nào.", icon=":material/inbox:")
            return ""
        return st.selectbox("Recipient đang có quyền", current,
                            key=f"sh_perm_rv_{share}") or ""

    listing = _cached("sharing:recipients", service.recipients,
                      "Đang đọc danh sách recipient…")
    if not listing.ok:
        widgets.show_error(listing.error, context="Không đọc được danh sách recipient.")
        st.caption("Nhập tên recipient thủ công nếu bạn biết chính xác tên.")
        return st.text_input("Tên recipient", key=f"sh_perm_manual_{share}",
                             placeholder="ten-recipient").strip()

    options = [r.name for r in listing.items if r.name not in set(current)]
    if not options:
        if listing.empty:
            widgets.empty_state("empty", what="recipient")
        else:
            st.info("Mọi recipient đọc được đều đã có quyền trên share này.",
                    icon=":material/check_circle:")
        return ""

    labels = {
        r.name: f"{r.name} · {r.kind_label}" + (" · ngoài tổ chức" if r.external else "")
        for r in listing.items
    }
    return st.selectbox("Recipient nhận quyền", options,
                        format_func=lambda n: labels.get(n, n),
                        key=f"sh_perm_gr_{share}") or ""


def _recipient_from_cache(service: SharingService, name: str):
    listing = state.cache_get("sharing:recipients")
    if listing is not None and listing.ok:
        for ref in listing.items:
            if ref.name == name:
                return ref
    key = f"sharing:recipient:{name}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    try:
        with st.spinner(f"Đang đọc recipient {name}…"):
            return state.cache_put(key, service.recipient_detail(name))
    except Exception:
        # Không đọc được thì phần cảnh báo "ngoài tổ chức" đơn giản là không
        # hiện; bản xem trước của service vẫn tự đánh giá lại.
        return None


def _preview_and_apply(ctx: Context, service: SharingService, detail, signature: str,
                       *, operation: str, ready: bool, build, apply_label: str):
    share = detail.ref.name
    st.markdown("##### 2. Xem trước phạm vi ảnh hưởng")

    plan = state.get_plan()
    valid = plan is not None and state.plan_matches(signature)

    if not valid:
        if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                     icon=":material/preview:", key=f"sh_preview_{operation}_{share}"):
            state.clear_plan()
            try:
                with st.spinner("Đang đọc lại trạng thái share…"):
                    built = build()
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
    _apply_step(ctx, service, plan, apply_label)


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


def _apply_step(ctx: Context, service: SharingService, plan, apply_label: str):
    st.markdown("##### 3. Xác nhận và áp dụng")
    st.caption(f"Nhập lại tên đầy đủ của share để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 3, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên share", key=f"sh_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            apply_label[:60], type="primary", width="stretch", disabled=not matches,
            icon=":material/check:", key=f"sh_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"sh_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not apply_clicked:
        return

    # Một plan, một lần gửi: rerun của Streamlit không gửi lại được.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner("Đang gửi thay đổi tới Databricks…"):
            outcome = service.apply(plan, confirmation)
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
        "page": "sharing",
        "status": outcome.status,
        "event_id": outcome.event_id,
        "summary": plan.summary,
        "share": plan.target_name,
        "operation": plan.payload.get("operation", ""),
        "subject": plan.payload.get("object") or plan.payload.get("recipient", ""),
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _show_result():
    payload = state.pop_notice()
    if not payload:
        return
    if payload.get("page") != "sharing":
        # Thông báo của màn hình khác: trả lại để đúng màn hình đó hiển thị.
        state.set_notice(payload)
        return

    status = payload.get("status")
    share = payload.get("share", "")
    subject = payload.get("subject", "")

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{payload.get('summary', 'Hoàn tất')}**",
                       icon=":material/check_circle:")
            st.markdown(f"- Share: **{share}**\n- Đối tượng thay đổi: **{subject or '—'}**")
            verified = payload.get("verified") or {}
            if status == Status.VERIFIED and verified:
                objects = verified.get("objects") or []
                recipients = verified.get("recipients") or []
                st.caption(
                    f"Đã đọc lại từ Databricks: share đang có {len(objects)} tài sản "
                    f"và {len(recipients)} recipient được cấp quyền."
                )
                inactive = verified.get("inactive_objects") or []
                if inactive:
                    st.warning(
                        "**Mất truy cập** — sau thay đổi vẫn còn tài sản không hoạt động: "
                        + ", ".join(f"`{n}`" for n in inactive),
                        icon=":material/block:",
                    )
                    st.caption(SharingService.OWNER_PRIVILEGE_DEPENDENCY)
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(SharingService.PROTECTION_DOES_NOT_TRAVEL)
            st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng "
            "không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** Đọc lại share **{share}** và kiểm tra trạng thái thực tế "
            "TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi không thành công.",
             icon=":material/error:")


# -- tab Recipients ------------------------------------------------------
def _recipients_tab(ctx: Context, service: SharingService):
    if _capability_blocked(ctx, "sharing.recipients"):
        return

    listing = _cached("sharing:recipients", service.recipients,
                      "Đang đọc danh sách recipient…")
    rendered = widgets.listing_or_empty(
        listing, what="recipient",
        render=lambda l: widgets.data_table(
            [r.row() for r in l.items], key="sharing_recipients",
            download_name="danh-sach-recipient",
        ),
    )
    if not rendered:
        return

    external = [r for r in listing.items if r.external]
    if external:
        st.warning(
            f"**Ngoài tổ chức** — {len(external)} recipient nhận dữ liệu bằng hình thức "
            "chia sẻ mở.",
            icon=":material/public:",
        )
        for ref in external:
            st.markdown(f":material/public: `{ref.name}` — **ngoài tổ chức** · {ref.kind_label}")
        widgets.caveat(SharingService.OPEN_SHARING_LIMIT)
    else:
        st.markdown(
            ":material/business: **Trong Databricks** — mọi recipient đọc được đều dùng "
            "chia sẻ Databricks-to-Databricks."
        )

    st.divider()
    names = [r.name for r in listing.items]
    chosen = st.selectbox("Chọn recipient để xem chi tiết", names, key="sh_pick_recipient")
    if chosen:
        _recipient_panel(service, chosen)


def _recipient_panel(service: SharingService, name: str):
    key = f"sharing:recipient:{name}"
    ref = state.cache_get(key)
    if ref is None:
        try:
            with st.spinner(f"Đang đọc recipient {name}…"):
                ref = state.cache_put(key, service.recipient_detail(name))
        except Exception as exc:
            widgets.show_error(exc, context="Không đọc được chi tiết recipient.")
            return

    st.markdown(f"#### {ref.name}")
    if ref.comment:
        st.markdown(ref.comment)

    cols = st.columns(3)
    if ref.external:
        cols[0].markdown(
            f":material/public: **Ngoài tổ chức**  \n{ref.kind_label}"
        )
    else:
        cols[0].markdown(
            f":material/business: **Trong Databricks**  \n{ref.kind_label}"
        )
    cols[1].markdown(
        ":material/check_circle: **Đã kích hoạt**" if ref.activated
        else ":material/pending: **Chưa kích hoạt**"
    )
    cols[2].markdown(
        ":material/shield: **Có** chặn theo IP" if ref.has_ip_allowlist
        else ":material/shield_moon: **Không** chặn theo IP"
    )

    cols = st.columns(3)
    cols[0].markdown(f"**Số token đang có**  \n{ref.token_count}")
    cols[1].markdown(f"**Token hết hạn sớm nhất**  \n{ref.token_expires or 'Chưa xác định'}")
    cols[2].markdown(f"**Recipient hết hạn**  \n{ref.expiration_time or 'Chưa xác định'}")

    cols = st.columns(2)
    cols[0].markdown(f"**Chủ sở hữu**  \n{ref.owner or 'Chưa xác định'}")
    cols[1].markdown(f"**Tạo lúc**  \n{ref.created or 'Chưa xác định'}")

    if ref.token_expiring_soon:
        st.warning(
            f"**Sắp hết hạn** — token hết hạn vào {ref.token_expires}. "
            "Lên kế hoạch xoay vòng trước thời điểm đó để tránh gián đoạn.",
            icon=":material/schedule:",
        )

    st.markdown("**Link kích hoạt và token**")
    st.info(SharingService.CREDENTIALS_NEVER_SHOWN, icon=":material/key_off:")
    st.caption(
        "Việc tạo lại hoặc xoay vòng token thực hiện trong giao diện Databricks, "
        "không thực hiện trong ứng dụng này."
    )

    widgets.explain(
        "Thuộc tính recipient và dynamic view",
        SharingService.RECIPIENT_PROPERTIES_OVERRIDE,
    )


# -- tab Providers -------------------------------------------------------
def _providers_tab(ctx: Context, service: SharingService):
    if _capability_blocked(ctx, "sharing.providers"):
        return

    st.caption(
        "Provider là phía chia sẻ dữ liệu **đến** workspace này."
    )
    listing = _cached("sharing:providers", service.providers,
                      "Đang đọc danh sách provider…")
    rendered = widgets.listing_or_empty(
        listing, what="provider",
        render=lambda l: widgets.data_table(
            [r.row() for r in l.items], key="sharing_providers",
            download_name="danh-sach-provider",
        ),
    )
    if not rendered:
        return

    st.caption(
        "Hồ sơ recipient (`recipient_profile`) của provider không được hiển thị: "
        "nó chứa thông tin xác thực."
    )

    st.divider()
    names = [p.name for p in listing.items]
    chosen = st.selectbox("Chọn provider để xem share nhận được", names,
                          key="sh_pick_provider")
    if not chosen:
        return

    shares = _cached(f"sharing:provider_shares:{chosen}",
                     lambda: service.provider_shares(chosen),
                     f"Đang đọc share của provider {chosen}…")
    st.markdown(f"**Share do `{chosen}` chia sẻ tới workspace này**")
    widgets.listing_or_empty(
        shares, what="share từ provider",
        render=lambda l: widgets.data_table(
            list(l.items), key=f"sharing_provider_shares_{chosen}",
            download_name=f"provider-{chosen}-share",
        ),
    )


# -- tab Cần xem xét -----------------------------------------------------
def _findings_tab(ctx: Context, service: SharingService):
    st.caption(
        "Các điểm đáng chú ý được suy ra từ dữ liệu đọc được. "
        "Ứng dụng không tự sửa bất cứ điểm nào ở đây."
    )
    listing = _cached("sharing:findings", service.findings,
                      "Đang rà soát share và recipient…")
    rendered = widgets.listing_or_empty(
        listing, what="điểm cần xem xét",
        render=lambda l: widgets.data_table(
            [f.row() for f in l.items], key="sharing_findings",
            download_name="chia-se-can-xem-xet",
        ),
    )
    if not rendered:
        return

    widgets.warn_block([
        SharingService.OWNER_PRIVILEGE_DEPENDENCY,
        SharingService.PROTECTION_DOES_NOT_TRAVEL,
    ])
