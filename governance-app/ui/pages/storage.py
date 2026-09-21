"""Storage credentials, service credentials, external locations and isolation.

The screen is built around the one thing this area gets wrong most often:
isolation is two settings. The isolation mode and the workspace list are always
rendered together, in the same block, so "bound to 2 workspaces" can never be
read as protection while the mode is still open to every workspace.

Only one object at a time may have its binding form open. Streamlit renders
every tab body on every rerun, so several open forms would each call
``state.invalidate_plan_if_changed`` and destroy one another's preview - and a
preview that disappears on an unrelated keystroke is how people end up applying
the wrong one.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.services.base import Context
from ucg.services.storage import (
    KIND_LABELS,
    READ_ONLY,
    READ_WRITE,
    StorageService,
)

from .. import nav, state, widgets

#: Which object currently owns the binding form. One token, page-wide.
ACTIVE_FORM = "ucg_storage_form"

_OPERATIONS = [
    ("isolation", "Đổi chế độ cách ly"),
    ("bind", "Gán workspace"),
    ("unbind", "Gỡ ràng buộc workspace"),
]
_OPERATION_LABELS = dict(_OPERATIONS)

_BINDING_MODES = [
    (READ_WRITE, "Đọc và ghi"),
    (READ_ONLY, "Chỉ đọc"),
]
_BINDING_MODE_LABELS = dict(_BINDING_MODES)


def render(ctx: Context) -> None:
    storage = StorageService(ctx)

    widgets.page_title(
        "Lưu trữ và cách ly",
        "External location, credential và phạm vi workspace được phép dùng chúng.",
    )

    _show_result(ctx)

    decision = ctx.authz.check(Action.READ_STORAGE)
    if not decision.allowed:
        st.warning(f"**Không xem được** — {decision.reason}", icon=":material/lock:")
        st.caption(
            "Đây là giới hạn vai trò trong ứng dụng, không phải kết luận rằng "
            "workspace không có đối tượng lưu trữ nào."
        )
        nav.link_button("Xem vai trò và khả năng", "diagnostics",
                        icon=":material/help:", widget_key="st_ro_diag")
        return

    cols = st.columns([6, 2])
    with cols[0]:
        st.caption(
            "Danh sách lấy cả đối tượng chưa gán workspace nào (`include_unbound`), "
            "nên bản kiểm kê không bỏ sót đối tượng đang cách ly ở nơi khác."
        )
    with cols[1]:
        if st.button("Đọc lại từ Databricks", icon=":material/refresh:",
                     width="stretch", key="st_refresh"):
            state.clear_caches()
            st.rerun()

    tabs = st.tabs([
        "External locations", "Storage credentials", "Service credentials",
        "Cần xem xét",
    ])
    with tabs[0]:
        _inventory_tab(ctx, storage, "external_location")
    with tabs[1]:
        _inventory_tab(ctx, storage, "storage_credential")
    with tabs[2]:
        _inventory_tab(ctx, storage, "service_credential")
    with tabs[3]:
        _findings_tab(storage)


# -- inventory -----------------------------------------------------------
def _inventory_tab(ctx: Context, storage: StorageService, kind: str):
    _tab_intro(kind)

    listing = _load_listing(storage, kind)
    what = KIND_LABELS.get(kind, kind).lower()

    shown = widgets.listing_or_empty(
        listing,
        what=what,
        render=lambda l: _inventory_body(l, kind),
    )
    if not shown:
        return

    st.divider()
    _detail_panel(ctx, storage, kind, listing)


def _tab_intro(kind: str):
    if kind == "external_location":
        st.caption(
            "External location là đường dẫn lưu trữ đám mây mà Unity Catalog quản lý "
            "truy cập, dựa trên một storage credential."
        )
        st.info(
            "Databricks không hỗ trợ ràng buộc **chỉ đọc** cho external location. "
            "Muốn chặn ghi, dùng thuộc tính `read_only` của chính external location.",
            icon=":material/edit_off:",
        )
        return

    if kind == "storage_credential":
        st.info(StorageService.CREDENTIAL_BINDING_TIMING, icon=":material/schedule:")
    st.info(StorageService.NO_CREATE_CREDENTIAL, icon=":material/block:")
    st.caption(
        "Ứng dụng không hiển thị giá trị bí mật của credential — chỉ mô tả cách "
        "credential xác thực (`auth_summary`)."
    )


def _load_listing(storage: StorageService, kind: str):
    key = f"storage:list:{kind}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    readers = {
        "external_location": storage.external_locations,
        "storage_credential": storage.storage_credentials,
        "service_credential": storage.service_credentials,
    }
    with st.spinner(f"Đang đọc {KIND_LABELS.get(kind, kind)} từ Databricks…"):
        return state.cache_put(key, readers[kind]())


def _inventory_body(listing, kind: str):
    items = list(listing.items)
    open_count = sum(1 for r in items if not r.isolated)
    unknown_mode = sum(1 for r in items if not r.isolation_mode)

    cols = st.columns(3)
    cols[0].metric("Đọc được", len(items))
    cols[1].metric("Mở cho mọi workspace", open_count)
    cols[2].metric("Chưa đọc được chế độ", unknown_mode)
    st.caption(
        "Các số trên đếm đúng những gì danh tính thực thi đọc được lúc này. "
        + ("Danh sách đã bị cắt bớt, nên đây là cận dưới."
           if getattr(listing, "completeness", "") == "truncated" else "")
    )

    widgets.data_table(
        [r.row() for r in items],
        key=f"storage_table_{kind}",
        download_name=f"kiem-ke-{kind.replace('_', '-')}",
    )


# -- one object ----------------------------------------------------------
def _detail_panel(ctx: Context, storage: StorageService, kind: str, listing):
    names = [r.name for r in listing.items]
    chosen = st.selectbox(
        "Chọn đối tượng để xem chi tiết", names, key=f"storage_pick_{kind}",
        help="Chi tiết và ràng buộc workspace được đọc riêng cho đối tượng đang chọn.",
    )
    ref = next((r for r in listing.items if r.name == chosen), None)
    if ref is None:
        return

    with st.expander(f"Chi tiết: {ref.name}", expanded=True):
        view = _load_bindings(storage, kind, ref.name)

        left, right = st.columns([3, 4])
        with left:
            _facts(kind, ref)
        with right:
            _isolation_and_bindings(kind, ref, view)

        st.divider()
        _validate_block(storage, kind, ref)

        st.divider()
        _binding_section(ctx, storage, kind, ref, view)

        widgets.technical_details(_technical_payload(kind, ref, view),
                                  download=f"{kind}-{ref.name}")


def _facts(kind: str, ref):
    st.markdown("**Thông tin đối tượng**")
    rows = [
        ("Tên", ref.name),
        ("Loại", ref.kind_label),
        ("Chủ sở hữu", ref.owner),
    ]
    if kind == "external_location":
        rows += [
            ("Đường dẫn", ref.url),
            ("Storage credential dùng", ref.credential_name),
            ("Chỉ đọc", "Có" if ref.read_only else "Không"),
            ("Cho phép fallback credential", "Có" if ref.fallback else "Không"),
        ]
    else:
        rows += [
            ("Xác thực", ref.auth_summary),
            ("Chỉ đọc", "Có" if ref.read_only else "Không"),
        ]
        if kind == "storage_credential":
            rows.append(("Dùng cho managed storage",
                         "Có" if ref.used_for_managed_storage else "Không"))
        if kind == "service_credential":
            rows.append(("Mục đích (purpose)", ref.purpose))
    rows += [("Tạo lúc", ref.created), ("Cập nhật lúc", ref.updated)]

    for label, value in rows:
        st.markdown(f"**{label}**  \n{value or 'Chưa xác định'}")
    st.markdown(f"**Mô tả**  \n{ref.comment or '_Chưa có mô tả._'}")

    if ref.fallback:
        st.warning(
            "fallback=true: truy cập có thể rơi về credential của cluster, "
            "tức là đi vòng qua kiểm soát của Unity Catalog.",
            icon=":material/warning:",
        )


def _load_bindings(storage: StorageService, kind: str, name: str):
    key = f"storage:bindings:{kind}:{name}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    with st.spinner("Đang đọc chế độ cách ly và danh sách workspace…"):
        try:
            view = storage.bindings(kind, name)
        except Exception as exc:  # the service raises only for unsupported kinds
            return state.cache_put(key, exc)
    return state.cache_put(key, view)


def _isolation_and_bindings(kind: str, ref, view):
    """Mode and workspace list, never one without the other."""
    st.markdown("**Cách ly workspace**")

    if isinstance(view, BaseException):
        widgets.show_error(view, context="Không đọc được ràng buộc workspace.")
        st.markdown(_isolation_line(ref.isolation_mode))
        st.info(StorageService.TWO_STEP_NOTE, icon=":material/shield:")
        return

    if not view.ok:
        widgets.show_error(view.error, context="Không đọc được ràng buộc workspace.")
        st.markdown(_isolation_line(ref.isolation_mode))
        st.info(StorageService.TWO_STEP_NOTE, icon=":material/shield:")
        return

    st.markdown(_isolation_line(view.isolation_mode))
    st.markdown(view.effective_note)

    rows = view.rows()
    if rows:
        widgets.data_table(rows, key=f"bind_rows_{kind}_{ref.name}")
    else:
        st.info("Chưa gán workspace nào.", icon=":material/inbox:")

    st.info(StorageService.TWO_STEP_NOTE, icon=":material/shield:")
    if view.observed_at:
        st.caption(f"Đọc lúc {view.observed_at}.")


def _isolation_line(mode: str) -> str:
    value = (mode or "").upper()
    if not value:
        return ":material/help: **Chế độ cách ly:** Chưa xác định"
    if value in ("ISOLATED", "ISOLATION_MODE_ISOLATED"):
        return ":material/lock: **Chế độ cách ly:** Chỉ workspace được gán"
    return ":material/public: **Chế độ cách ly:** Mở cho mọi workspace"


# -- connectivity check --------------------------------------------------
def _validate_block(storage: StorageService, kind: str, ref):
    st.markdown("**Kiểm tra kết nối tới hệ thống lưu trữ**")
    cache_key = f"storage:validate:{kind}:{ref.name}"

    cols = st.columns([2, 5])
    with cols[0]:
        if st.button("Kiểm tra kết nối", icon=":material/network_check:",
                     width="stretch", key=f"st_val_{kind}_{ref.name}"):
            try:
                with st.spinner("Đang thử đường truy cập…"):
                    state.cache_put(cache_key, {"data": storage.validate(kind, ref.name)})
            except Exception as exc:
                state.cache_put(cache_key, {"error": exc})
            st.rerun()
    with cols[1]:
        st.caption(
            "Chỉ đọc: Databricks thử đọc/ghi thử bằng credential và trả về kết quả "
            "từng thao tác. Ứng dụng không gọi API sinh credential tạm thời."
        )

    result = state.cache_get(cache_key)
    if result is None:
        return
    if "error" in result:
        widgets.show_error(result["error"], context="Không chạy được kiểm tra kết nối.")
        return

    data = result["data"]
    rows = [
        {
            "Thao tác": _plain(item.get("operation")),
            "Kết quả": _plain(item.get("result")),
            "Thông báo": _plain(item.get("message")),
        }
        for item in data.get("results") or []
    ]
    if rows:
        widgets.data_table(rows, key=f"val_rows_{kind}_{ref.name}")
    else:
        st.info("Databricks không trả về mục kiểm tra nào.", icon=":material/inbox:")

    if kind == "external_location":
        is_dir = data.get("is_dir")
        st.caption("Đường dẫn là thư mục: "
                   + ("Có" if is_dir is True else "Không" if is_dir is False else "Chưa xác định"))

    st.warning(data.get("note") or StorageService.NOT_IAM, icon=":material/info:")
    st.caption(f"Kiểm tra lúc {data.get('observed_at', 'Chưa xác định')}.")


def _plain(value) -> str:
    """Unwrap an SDK enum without importing the SDK into the UI layer."""
    if value is None:
        return "—"
    return str(getattr(value, "value", value))


# -- changing isolation and bindings -------------------------------------
def _binding_section(ctx: Context, storage: StorageService, kind: str, ref, view):
    st.markdown("**Thay đổi cách ly và ràng buộc workspace**")

    # Storage credentials, service credentials and external locations are
    # metastore-level securables: there is no catalog to scope-check against.
    decision = ctx.authz.check(Action.MANAGE_BINDINGS, None)
    if not decision.allowed:
        st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
        st.caption(
            "Ứng dụng vẫn kiểm tra lại quyền ở phía máy chủ trước khi gửi bất kỳ "
            "thay đổi nào; việc ẩn nút chỉ là giao diện."
        )
        return

    if isinstance(view, BaseException) or not view.ok:
        st.caption(
            "Chưa đọc được trạng thái ràng buộc hiện tại, nên chưa mở được thao tác "
            "thay đổi. Đọc lại rồi thử tiếp."
        )
        return

    token = f"{kind}:{ref.name}"
    if st.session_state.get(ACTIVE_FORM) != token:
        if st.button(f"Mở bảng thay đổi cho {ref.name}", icon=":material/tune:",
                     key=f"st_open_{token}"):
            st.session_state[ACTIVE_FORM] = token
            state.clear_plan()
            st.rerun()
        st.caption(
            "Mỗi lần chỉ mở bảng thay đổi cho một đối tượng, để bản xem trước "
            "không thể bị nhầm sang đối tượng khác."
        )
        return

    if st.button("Đóng bảng thay đổi", icon=":material/close:", key=f"st_close_{token}"):
        st.session_state.pop(ACTIVE_FORM, None)
        state.clear_plan()
        st.rerun()

    codes = [c for c, _ in _OPERATIONS]
    operation = st.radio(
        "Thao tác", codes, format_func=lambda c: _OPERATION_LABELS[c],
        horizontal=True, key=f"st_op_{token}",
    )

    if operation == "isolation":
        _isolation_form(ctx, storage, kind, ref, view, token)
    elif operation == "bind":
        _bind_form(ctx, storage, kind, ref, view, token)
    else:
        _unbind_form(ctx, storage, kind, ref, view, token)


def _isolation_form(ctx: Context, storage: StorageService, kind: str, ref, view, token: str):
    options = [True, False]
    labels = {True: "Chỉ workspace được gán", False: "Mở cho mọi workspace"}
    isolated = st.radio(
        "Chế độ cách ly mong muốn", options,
        index=0 if view.isolated else 1,
        format_func=lambda v: labels[v], key=f"st_iso_{token}",
    )
    st.info(StorageService.TWO_STEP_NOTE, icon=":material/shield:")
    if not isolated:
        st.warning(
            "Mở cho mọi workspace là thao tác MỞ RỘNG phạm vi truy cập, không phải thu hẹp.",
            icon=":material/warning:",
        )
    elif not view.bindings:
        st.warning(
            "Chưa gán workspace nào. Bật cách ly bây giờ sẽ khiến đối tượng không "
            "dùng được ở bất kỳ workspace nào cho tới khi gán.",
            icon=":material/warning:",
        )

    reason = _reason_field(token, "isolation")
    signature = _signature(ctx, token, "isolation", view,
                           extra=str(isolated), reason=reason)
    state.invalidate_plan_if_changed(signature)

    unchanged = isolated == view.isolated
    if unchanged:
        st.caption("Chế độ cách ly hiện tại đã đúng như vậy — không có gì để thay đổi.")

    _preview_and_apply(
        ctx, storage, ref, signature,
        ready=bool(reason.strip()) and not unchanged,
        build=lambda: storage.plan_set_isolation(kind, ref.name, isolated, reason),
        hint="Nhập lý do và chọn chế độ khác chế độ hiện tại để tạo bản xem trước.",
        widget_prefix=f"{token}_iso",
    )


def _bind_form(ctx: Context, storage: StorageService, kind: str, ref, view, token: str):
    raw = st.text_input(
        "Workspace ID cần gán", key=f"st_bindids_{token}",
        placeholder="1234567890123456, 2345678901234567",
        help="Nhập một hoặc nhiều workspace ID, phân tách bằng dấu phẩy hoặc khoảng trắng.",
    )
    ids = _parse_ids(raw)

    if storage.read_only_binding_supported(kind):
        modes = [c for c, _ in _BINDING_MODES]
        binding_type = st.radio(
            "Chế độ ràng buộc", modes,
            format_func=lambda c: _BINDING_MODE_LABELS[c],
            horizontal=True, key=f"st_bindmode_{token}",
        )
    else:
        binding_type = READ_WRITE
        st.caption(
            "Databricks không hỗ trợ ràng buộc chỉ đọc cho external location, nên "
            "chỉ có chế độ đọc và ghi. Muốn chặn ghi, dùng thuộc tính `read_only` "
            "của chính external location."
        )

    if not view.isolated:
        st.warning(
            "Đối tượng đang mở cho mọi workspace: gán thêm workspace CHƯA hạn chế "
            "được gì. Bật chế độ cách ly trước thì danh sách này mới có tác dụng.",
            icon=":material/warning:",
        )
    if kind == "storage_credential":
        st.info(StorageService.CREDENTIAL_BINDING_TIMING, icon=":material/schedule:")

    reason = _reason_field(token, "bind")
    signature = _signature(ctx, token, "bind", view,
                           extra=",".join(ids) + "|" + binding_type, reason=reason)
    state.invalidate_plan_if_changed(signature)

    if raw.strip() and not ids:
        st.caption("Chưa nhận ra workspace ID nào trong ô trên.")

    _preview_and_apply(
        ctx, storage, ref, signature,
        ready=bool(ids and reason.strip()),
        build=lambda: storage.plan_bind(kind, ref.name, ids, binding_type, reason),
        hint="Nhập workspace ID và lý do để tạo bản xem trước.",
        widget_prefix=f"{token}_bind",
    )


def _unbind_form(ctx: Context, storage: StorageService, kind: str, ref, view, token: str):
    current = [str(b["workspace_id"]) for b in view.bindings]
    if not current:
        st.info("Đối tượng chưa gán workspace nào nên không có gì để gỡ.",
                icon=":material/inbox:")
        return

    ids = st.multiselect(
        "Workspace cần gỡ ràng buộc", current, key=f"st_unbindids_{token}",
        help="Chỉ liệt kê những workspace đang được gán theo dữ liệu vừa đọc.",
    )

    st.warning(StorageService.UNBIND_EFFECT_UNKNOWN, icon=":material/help:")
    if kind == "storage_credential":
        st.info(StorageService.CREDENTIAL_BINDING_TIMING, icon=":material/schedule:")

    reason = _reason_field(token, "unbind")
    signature = _signature(ctx, token, "unbind", view,
                           extra=",".join(sorted(ids)), reason=reason)
    state.invalidate_plan_if_changed(signature)

    _preview_and_apply(
        ctx, storage, ref, signature,
        ready=bool(ids and reason.strip()),
        build=lambda: storage.plan_unbind(kind, ref.name, ids, reason),
        hint="Chọn ít nhất một workspace và nhập lý do để tạo bản xem trước.",
        widget_prefix=f"{token}_unbind",
    )


def _reason_field(token: str, operation: str) -> str:
    return st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"st_reason_{token}_{operation}",
        placeholder="Ví dụ: thu hẹp phạm vi theo yêu cầu kiểm soát TICKET-123.",
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )


def _signature(ctx: Context, token: str, operation: str, view, *, extra: str,
               reason: str) -> str:
    """Every material input, plus the state the preview will be built on."""
    observed = "|".join(
        f"{b['workspace_id']}:{b['binding_type']}" for b in sorted(
            view.bindings, key=lambda b: str(b["workspace_id"])
        )
    )
    return "|".join([
        token, operation, extra, reason.strip(), ctx.actor.email,
        view.isolation_mode, observed,
    ])


def _parse_ids(raw: str) -> list[str]:
    cleaned = (raw or "").replace("\n", ",").replace(";", ",").replace(" ", ",")
    out: list[str] = []
    for part in cleaned.split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


# -- preview / confirm / apply -------------------------------------------
def _preview_and_apply(ctx: Context, storage: StorageService, ref, signature: str,
                       *, ready: bool, build, hint: str, widget_prefix: str):
    plan = state.get_plan()
    valid = plan is not None and state.plan_matches(signature)

    if not valid:
        if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                     icon=":material/preview:", key=f"st_prev_{widget_prefix}"):
            state.clear_plan()
            try:
                with st.spinner("Đang đọc trạng thái hiện tại để dựng bản xem trước…"):
                    built = build()
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, signature)
            st.rerun()
        st.caption(hint + " Bản xem trước chỉ đọc dữ liệu; sửa bất kỳ trường nào "
                          "ở trên sẽ huỷ bản xem trước cũ.")
        return

    _render_preview(plan)
    _apply_step(ctx, storage, ref, plan)


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


def _apply_step(ctx: Context, storage: StorageService, ref, plan):
    st.markdown("**Xác nhận**")
    st.caption(f"Nhập lại tên đối tượng để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đối tượng", key=f"st_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            plan.summary[:40] or "Áp dụng", type="primary", width="stretch",
            disabled=not matches, icon=":material/check:",
            key=f"st_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"st_cancel_{plan.plan_id}"):
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
            outcome = storage.apply(plan, confirmation)
    except Exception as exc:
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
        "target_type": plan.target_type,
        "operation": plan.payload.get("operation", ""),
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _show_result(ctx: Context):
    payload = state.pop_notice()
    if not payload:
        return

    status = payload.get("status")
    target = payload.get("target", "")
    target_type = payload.get("target_type", "")

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{payload.get('summary', 'Hoàn tất')}**",
                       icon=":material/check_circle:")
            st.markdown(f"- Đối tượng: **{target}** ({target_type})")
            verified = payload.get("verified") or {}
            if status == Status.VERIFIED and verified:
                st.markdown(_isolation_line(verified.get("isolation_mode", "")))
                workspaces = verified.get("workspaces") or []
                st.markdown(
                    "- Workspace đang được gán: **"
                    + (", ".join(str(w) for w in workspaces) if workspaces
                       else "chưa có workspace nào")
                    + "**"
                )
                st.caption("Đây là trạng thái đọc lại từ Databricks sau khi thay đổi.")
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            if payload.get("operation") == "unbind":
                st.warning(StorageService.UNBIND_EFFECT_UNKNOWN, icon=":material/help:")
            st.caption(f"Event ID: `{str(payload.get('event_id', ''))[:12]}`")
            nav.link_button("Xem nhật ký thao tác", "activity",
                            icon=":material/history:", widget_key="st_notice_activity")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng "
            "không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** Đọc lại chế độ cách ly và danh sách workspace của "
            f"**{target}** TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
        )
        st.caption(f"Event ID: `{str(payload.get('event_id', ''))[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi không thành công.",
             icon=":material/error:")


# -- findings ------------------------------------------------------------
def _findings_tab(storage: StorageService):
    st.caption(
        "Những cấu hình nên được người có thẩm quyền nhìn lại. Đây là tín hiệu "
        "để xem xét, KHÔNG phải kết luận vi phạm."
    )

    key = "storage:findings"
    listing = state.cache_get(key)
    if listing is None:
        with st.spinner("Đang rà soát cấu hình lưu trữ…"):
            listing = state.cache_put(key, storage.findings())

    if not listing.ok:
        widgets.show_error(listing.error, context="Không rà soát được cấu hình lưu trữ.")
        st.caption("Số điểm cần xem xét: Chưa xác định.")
        return

    st.metric("Điểm cần xem xét", len(listing.items))

    if listing.empty:
        st.info(
            "Không có điểm nào được gắn cờ trong phần đọc được.",
            icon=":material/inbox:",
        )
        st.caption(
            "Đây là kết quả trên dữ liệu danh tính thực thi đọc được — không phải "
            "chứng nhận rằng toàn bộ metastore đã cấu hình đúng."
        )
        widgets.completeness_note(listing)
        return

    widgets.data_table(
        [f.row() for f in listing.items],
        key="storage_findings",
        download_name="luu-tru-can-xem-xet",
    )
    widgets.completeness_note(listing)
    st.divider()
    widgets.explain(
        "Vì sao không có nút tạo hay xoá ở đây?",
        f"{StorageService.NO_CREATE_CREDENTIAL}\n\n{StorageService.FORCE_NOTE}",
    )
    widgets.caveat(StorageService.DEFAULT_WORKSPACE_CATALOG)


def _technical_payload(kind: str, ref, view) -> dict:
    """Non-secret fields only: no credential material ever leaves the service."""
    payload = {
        "kind": kind,
        "name": ref.name,
        "owner": ref.owner,
        "isolation_mode": ref.isolation_mode,
        "read_only": ref.read_only,
        "created": ref.created,
        "updated": ref.updated,
    }
    if kind == "external_location":
        payload.update({
            "url": ref.url,
            "credential_name": ref.credential_name,
            "fallback": ref.fallback,
        })
    else:
        payload.update({
            "auth_summary": ref.auth_summary,
            "used_for_managed_storage": ref.used_for_managed_storage,
            "purpose": ref.purpose,
        })
    if not isinstance(view, BaseException) and view.ok:
        payload["bindings"] = view.bindings
        payload["observed_at"] = view.observed_at
    return payload
