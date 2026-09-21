"""Chính sách bảo vệ dữ liệu: ABAC policies và ràng buộc kiểu cũ trên bảng.

Toàn bộ màn hình được dựng quanh một sự thật mà người dùng rất dễ hiểu sai:
Unity Catalog có hai cơ chế che dữ liệu độc lập nhau, và mỗi cơ chế chỉ hiện ở
đúng một chỗ. Vì vậy hai tab đầu tiên luôn tồn tại song song, kể cả khi một bên
rỗng - một tab rỗng vẫn là thông tin, còn giấu nó đi thì thành kết luận sai.

Mọi thay đổi đi theo ba bước bắt buộc: nhập nội dung -> xem trước -> gõ lại tên
đầy đủ của điểm gắn rồi áp dụng. Chữ ký của bản xem trước bao trùm mọi trường
nhập liệu, nên một lần rerun của Streamlit không thể áp dụng bản xem trước cũ.
"""
from __future__ import annotations

import json

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.capabilities import State
from ucg.errors import Code
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.policies import (
    COLUMN_MASK,
    CONFLICT_FAIL_CLOSED,
    DELETE_SIDE_EFFECTS,
    FUNCTION_ARGUMENT_RULES,
    GOVERNED_TAGS_REQUIRED,
    LEGACY_IS_SQL_ONLY,
    LEGACY_SQL_SHAPES,
    LISTING_CAVEAT,
    MATCH_COLUMNS_RULES,
    MAX_MATCH_COLUMNS,
    MAX_PRINCIPALS,
    NO_SIMULATION,
    PERMISSIONS_NOTE,
    PIPELINE_OWNER_RISK,
    POLICY_TYPE_LABELS,
    RENAME_NOT_OFFERED,
    ROW_FILTER,
    TIME_TRAVEL_BLOCKED,
    TWO_MECHANISMS,
    UPDATABLE_FIELDS,
    UPDATE_MASK_RULES,
    VIEW_NOT_ATTACHABLE,
    WHEN_CONDITION_DEFAULT_TRUE,
    PolicyDraft,
    PolicyService,
)

from .. import nav, picker, state, widgets

_SOURCE_ALL = "Tất cả"
_SOURCE_DIRECT = "Chỉ gắn trực tiếp tại đây"
_SOURCE_INHERITED = "Chỉ kế thừa từ cấp cha"
_SOURCES = [_SOURCE_ALL, _SOURCE_DIRECT, _SOURCE_INHERITED]

_OPERATIONS = [
    ("create", "Tạo chính sách mới"),
    ("update", "Cập nhật chính sách"),
    ("delete", "Xoá chính sách"),
]
_OPERATION_CODES = [c for c, _ in _OPERATIONS]
_OPERATION_LABELS = dict(_OPERATIONS)

#: Số ô nhập tham số bổ sung của UDF hiển thị sẵn trên form.
_ARGUMENT_SLOTS = 4
_ARG_NONE = "(không dùng)"
_ARG_ALIAS = "alias của cột đã khai báo"
_ARG_CONST = "hằng số"
_ARG_KINDS = [_ARG_NONE, _ARG_ALIAS, _ARG_CONST]


def render(ctx: Context) -> None:
    assets = AssetService(ctx)
    policies = PolicyService(ctx)

    widgets.page_title(
        "Chính sách bảo vệ dữ liệu",
        "Chính sách ABAC theo governed tag, và các ràng buộc row filter / column mask "
        "kiểu cũ gắn thẳng trên bảng.",
    )

    _show_result()

    target = state.current_target()
    with st.expander("Điểm gắn chính sách đang xem", expanded=target is None):
        picked = picker.level_picker(ctx, assets)
        if picked is not None:
            target = picked
    if target is None:
        st.info("Chọn một catalog, schema hoặc bảng để bắt đầu.", icon=":material/search:")
        return

    try:
        attach = policies.attach_point(target)
    except Exception as exc:
        picker.header(ctx, target, None)
        widgets.show_error(exc, context="Không gắn được chính sách ABAC vào đối tượng này.")
        st.caption(
            "Chọn lại cấp **Catalog**, **Schema** hoặc **Bảng / View** ở khung "
            "“Điểm gắn chính sách đang xem” bên trên."
        )
        return

    detail, detail_error = _table_detail(assets, target)
    table_type = detail.sub_type if detail is not None else ""

    picker.header(ctx, target, detail)
    if attach.cascades:
        st.warning(
            f"Điểm gắn `{attach.securable_type} {attach.full_name}` có tính LAN TOẢ: "
            "chính sách đặt ở đây áp xuống mọi bảng con trong phạm vi, kể cả bảng "
            "được tạo về sau.",
            icon=":material/account_tree:",
        )
    else:
        st.caption(
            f":material/place_item: Điểm gắn `{attach.securable_type} {attach.full_name}` "
            "— chính sách chỉ áp cho đúng bảng này."
        )

    st.divider()
    tab_abac, tab_legacy, tab_notes = st.tabs([
        "Chính sách ABAC",
        "Bảo vệ gắn trực tiếp trên bảng",
        "Lưu ý",
    ])

    with tab_abac:
        _abac_tab(ctx, policies, target, attach, table_type)
    with tab_legacy:
        _legacy_tab(ctx, policies, target, detail, detail_error)
    with tab_notes:
        _notes_tab(policies, attach)


# -- đọc dữ liệu ---------------------------------------------------------
def _table_detail(assets: AssetService, target: Target):
    """Metadata của bảng, chỉ đọc khi đang đứng ở cấp bảng.

    Catalog và schema không mang row filter / column mask kiểu cũ, nên gọi
    ``detail`` ở hai cấp đó chỉ tốn thêm một lượt gọi API mà không thêm thông tin.
    """
    if target.kind != "table":
        return None, None
    key = f"policy_detail:{target.key}"
    cached = state.cache_get(key)
    if cached is None:
        with st.spinner("Đang đọc metadata của bảng…"):
            try:
                cached = {"detail": assets.detail(target), "error": None}
            except Exception as exc:
                cached = {"detail": None, "error": exc}
        state.cache_put(key, cached)
    return cached["detail"], cached["error"]


def _listing_key(target: Target) -> str:
    return f"policies:{target.key}"


def _policy_listing(policies: PolicyService, target: Target):
    """Một lượt đọc duy nhất, gồm cả chính sách kế thừa.

    Bộ lọc trực tiếp / kế thừa chạy trên dữ liệu đã tải, nên đổi bộ lọc không
    gọi lại Databricks.
    """
    key = _listing_key(target)
    cached = state.cache_get(key)
    if cached is None:
        with st.spinner("Đang đọc chính sách ABAC từ Databricks…"):
            cached = policies.list_for(target, include_inherited=True)
        state.cache_put(key, cached)
    return cached


# -- tab 1: chính sách ABAC ----------------------------------------------
def _abac_tab(ctx: Context, policies: PolicyService, target: Target, attach,
              table_type: str):
    cap = ctx.capabilities.get("policies.read")
    # Một capability chưa được dò (UNKNOWN) không phải là bằng chứng chức năng
    # thiếu, nên chỉ cảnh báo khi đã có kết luận thật.
    if cap.state != State.UNKNOWN:
        widgets.capability_notice(cap)

    listing = _policy_listing(policies, target)
    rows = list(listing.items) if (listing is not None and listing.ok) else []
    direct = [r for r in rows if not r.inherited]

    if rows:
        _counts(listing, policies, attach, direct)

    cols = st.columns([3, 2, 1.2])
    with cols[0]:
        source = st.radio(
            "Nguồn gắn", _SOURCES, horizontal=True, key=f"pol_src_{target.key}",
            help="Chính sách kế thừa chỉ sửa được tại đúng nơi nó được gắn.",
        )
    with cols[1]:
        term = st.text_input(
            "Tìm theo tên chính sách", key=f"pol_q_{target.key}",
            placeholder="một phần của tên",
        )
    with cols[2]:
        st.write("")
        if st.button("Đọc lại", icon=":material/refresh:", width="stretch",
                     key=f"pol_reload_{target.key}",
                     help="Gọi lại Databricks thay vì dùng dữ liệu đã tải trong phiên."):
            state.cache_put(_listing_key(target), None)
            st.rerun()

    widgets.listing_or_empty(
        listing,
        what="chính sách ABAC",
        render=lambda result: _policy_list(result, source, term, target),
    )
    widgets.caveat(LISTING_CAVEAT)

    _mutations(ctx, policies, target, attach, direct, table_type)


def _counts(listing, policies: PolicyService, attach, direct: list):
    """Chỉ đếm khi danh sách đầy đủ; danh sách bị cắt thì không có con số thật."""
    truncated = getattr(listing, "completeness", "complete") == "truncated"
    total = len(listing.items)
    cols = st.columns(3)
    cols[0].metric("Gắn trực tiếp tại đây",
                   "Chưa xác định" if truncated else len(direct))
    cols[1].metric("Kế thừa từ cấp cha",
                   "Chưa xác định" if truncated else total - len(direct))
    with cols[2]:
        st.markdown("**Hạn mức tại điểm gắn**")
        note = policies.quota_note(attach, None if truncated else len(direct))
        st.caption(note or "Databricks không công bố hạn mức cho cấp này.")
    if truncated:
        st.caption(
            "Danh sách đã bị cắt bớt nên không đếm được chính xác — dùng ô tìm kiếm "
            "để thu hẹp."
        )


def _policy_list(listing, source: str, term: str, target: Target):
    rows = list(listing.items)
    if source == _SOURCE_DIRECT:
        rows = [r for r in rows if not r.inherited]
    elif source == _SOURCE_INHERITED:
        rows = [r for r in rows if r.inherited]
    needle = term.strip().lower()
    if needle:
        rows = [r for r in rows if needle in r.name.lower()]

    if not rows:
        st.info("Không có chính sách nào khớp bộ lọc.", icon=":material/filter_alt_off:")
        st.caption(
            "Đây là kết quả của bộ lọc trên màn hình, không phải câu trả lời của "
            "Databricks. Đặt lại “Nguồn gắn” về “Tất cả” để xem toàn bộ."
        )
        return

    broad = [r for r in rows if r.applies_to_every_table]
    if broad:
        st.warning(
            ":material/warning: **Phạm vi rộng nhất có thể** — "
            f"{len(broad)} chính sách không có `when_condition`: "
            + ", ".join(f"`{r.name}`" for r in broad[:8])
            + ("…" if len(broad) > 8 else "")
            + ". " + WHEN_CONDITION_DEFAULT_TRUE,
            icon=":material/warning:",
        )

    st.caption(f"{len(rows)}/{len(listing.items)} chính sách.")
    widgets.data_table(
        [r.row() for r in rows],
        key=f"pol_table_{target.key}",
        download_name=f"{target.full_name}-chinh-sach-abac",
    )

    st.markdown("**Chi tiết từng chính sách**")
    for row in rows:
        _policy_card(row, target)


def _policy_card(row, target: Target):
    """Một thẻ mở rộng cho mỗi chính sách.

    Bên trong không dùng expander lồng nhau, nên các quy tắc dài hiển thị bằng
    caption thay vì khối “giải thích” gập được.
    """
    icon = ":material/visibility_off:" if row.is_column_mask else ":material/filter_alt:"
    origin = "Kế thừa" if row.inherited else "Gắn trực tiếp"
    with st.expander(f"{row.name} — {row.type_label} · {origin}", icon=icon):
        st.caption(f":material/location_on: {row.scope_label}")

        cols = st.columns(2)
        with cols[0]:
            st.markdown("**Áp dụng cho (bị che dữ liệu)**")
            st.markdown("\n".join(f"- `{p}`" for p in row.to_principals)
                        or "_Không có principal nào._")
        with cols[1]:
            st.markdown("**Loại trừ (thấy dữ liệu nguyên bản)**")
            st.markdown("\n".join(f"- `{p}`" for p in row.except_principals)
                        or "_Không loại trừ ai._")

        st.markdown("**Điều kiện chọn bảng — when_condition**")
        if row.applies_to_every_table:
            st.warning(
                "Để trống — chính sách áp cho MỌI bảng trong phạm vi đã gắn.",
                icon=":material/warning:",
            )
            st.caption(WHEN_CONDITION_DEFAULT_TRUE)
        else:
            st.code(row.when_condition, language=None)

        st.markdown("**Bộ chọn cột — match_columns**")
        if row.match_columns:
            widgets.data_table(
                [{"Alias": m.get("alias") or "—", "Điều kiện thẻ": m.get("condition") or "—"}
                 for m in row.match_columns],
                key=f"pol_mc_{target.key}_{row.name}",
            )
            st.caption("Các alias đã khai báo: " + ", ".join(f"`{a}`" for a in row.aliases()))
            st.caption(MATCH_COLUMNS_RULES)
        else:
            st.caption("Không khai báo `match_columns`.")

        st.markdown("**Hàm và tham số**")
        st.markdown(f"- Hàm: `{row.function_name or '—'}`")
        if row.is_column_mask:
            st.markdown(f"- Áp lên alias: `{row.on_column or '—'}` (alias, không phải tên cột vật lý)")
        st.markdown("- Tham số bổ sung: " + (_arguments_text(row.using) or "không có"))
        st.caption(FUNCTION_ARGUMENT_RULES)

        if row.comment:
            st.markdown(f"**Mô tả**  \n{row.comment}")
        meta = " · ".join(x for x in [
            f"Tạo: {row.created_at}" if row.created_at else "",
            f"Bởi: {row.created_by}" if row.created_by else "",
            f"Cập nhật: {row.updated_at}" if row.updated_at else "",
        ] if x)
        if meta:
            st.caption(meta)

        if not row.editable_here:
            st.info(
                f"Chính sách này được gắn tại `{row.attach_fullname}`. Muốn sửa hoặc xoá "
                "thì phải mở đúng điểm gắn đó.",
                icon=":material/lock:",
            )


def _arguments_text(arguments) -> str:
    parts = []
    for arg in arguments or ():
        if arg.get("alias"):
            parts.append(f"alias `{arg['alias']}`")
        else:
            parts.append(f"hằng số `{arg.get('constant')}`")
    return ", ".join(parts)


# -- tab 1: phần thay đổi -------------------------------------------------
def _mutations(ctx: Context, policies: PolicyService, target: Target, attach,
               direct: list, table_type: str):
    st.divider()
    st.markdown("#### Thay đổi chính sách")

    decision = ctx.authz.check(Action.MANAGE_POLICY, target)
    if not decision.allowed:
        st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
        _denied_help(ctx, decision)
        return

    cap = ctx.capabilities.get("policies.write")
    if cap.state != State.UNKNOWN and not widgets.capability_notice(cap):
        return

    with st.container(border=True):
        operation = st.radio(
            "Thao tác", _OPERATION_CODES,
            format_func=lambda c: _OPERATION_LABELS[c],
            horizontal=True, key=f"pol_op_{target.key}",
        )

        st.markdown("##### 1. Nhập nội dung thay đổi")
        if operation == "create":
            prepared = _create_inputs(target)
        elif operation == "update":
            prepared = _update_inputs(target, direct)
        else:
            prepared = _delete_inputs(target, direct)
        if prepared is None:
            return
        draft, fields, delete_name = prepared

        reason = st.text_area(
            "Lý do thay đổi", max_chars=500, key=f"pol_reason_{target.key}_{operation}",
            placeholder="Ví dụ: che cột email cho nhóm phân tích theo yêu cầu TICKET-123.",
            help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
        )

        signature = _signature(ctx, target, attach, operation, draft, fields,
                               delete_name, reason, table_type)
        state.invalidate_plan_if_changed(signature)

        ready, missing = _readiness(operation, draft, fields, delete_name, reason)

        st.divider()
        st.markdown("##### 2. Xem trước phạm vi ảnh hưởng")
        st.info(NO_SIMULATION, icon=":material/help:")

        plan = state.get_plan()
        if plan is None or not state.plan_matches(signature):
            if not ready:
                st.caption("Còn thiếu: " + ", ".join(missing) + ".")
            if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                         icon=":material/preview:", key=f"pol_preview_{target.key}"):
                state.clear_plan()
                try:
                    with st.spinner("Đang kiểm tra trạng thái hiện tại tại Databricks…"):
                        built = _build_plan(policies, target, operation, draft, fields,
                                            delete_name, reason, table_type)
                except Exception as exc:
                    widgets.show_error(exc, context="Không dựng được bản xem trước.")
                    return
                state.set_plan(built, signature)
                st.rerun()
            st.caption(
                "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. Sửa bất kỳ trường "
                "nào ở trên sẽ huỷ bản xem trước cũ."
            )
            return

        _render_preview(plan)
        st.divider()
        _apply_step(ctx, policies, target, plan)


def _denied_help(ctx: Context, decision):
    if decision.code == Code.WRITES_DISABLED:
        st.markdown(
            "Quản trị viên ứng dụng cần bật `GOVERNANCE_ENABLE_WRITES` trong cấu hình "
            "rồi **deploy lại** ứng dụng."
        )
    elif decision.code == Code.FORBIDDEN_ROLE:
        st.markdown(
            f"Vai trò hiện tại của bạn là **{ctx.authz.role_label}**. Tạo và xoá chính "
            "sách ABAC thuộc vai trò quản trị nền tảng."
        )
    elif decision.code == Code.FORBIDDEN_SCOPE:
        st.markdown(f"Ứng dụng chỉ được cấu hình quản lý: **{ctx.settings.scope_label}**.")
    elif decision.code == Code.NO_IDENTITY:
        st.markdown(
            "Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn."
        )
    st.caption(PERMISSIONS_NOTE)
    nav.link_button("Xem quyền trên đối tượng này", "permissions",
                    icon=":material/key:", widget_key="pol_ro_perm")


def _create_inputs(target: Target):
    draft = _draft_inputs("new", target, existing=None)
    return draft, None, ""


def _update_inputs(target: Target, direct: list):
    if not direct:
        st.info("Chưa có chính sách nào gắn trực tiếp tại điểm gắn này để sửa.",
                icon=":material/inbox:")
        st.caption(
            "Chính sách kế thừa không sửa được ở đây; hãy mở đúng catalog hoặc schema "
            "mà nó được gắn."
        )
        return None

    names = [r.name for r in direct]
    chosen = st.selectbox("Chính sách cần cập nhật", names, key=f"pol_upd_pick_{target.key}")
    existing = next(r for r in direct if r.name == chosen)

    allowed = [
        f for f in UPDATABLE_FIELDS
        if f != ("row_filter" if existing.is_column_mask else "column_mask")
    ]
    fields = st.multiselect(
        "Trường sẽ được gửi đi (update_mask)", allowed,
        format_func=lambda f: UPDATABLE_FIELDS[f][1],
        key=f"pol_upd_fields_{target.key}_{chosen}",
        help="Chỉ những trường được chọn mới gửi lên; các trường khác giữ nguyên.",
    )
    widgets.explain("Vì sao phải chọn từng trường?", UPDATE_MASK_RULES)

    draft = _draft_inputs(f"upd_{chosen}", target, existing=existing)
    return draft, fields, ""


def _delete_inputs(target: Target, direct: list):
    if not direct:
        st.info("Chưa có chính sách nào gắn trực tiếp tại điểm gắn này để xoá.",
                icon=":material/inbox:")
        st.caption(
            "Chính sách kế thừa không xoá được ở đây; xoá tại đúng điểm gắn mới có "
            "tác dụng."
        )
        return None

    names = [r.name for r in direct]
    chosen = st.selectbox("Chính sách cần xoá", names, key=f"pol_del_pick_{target.key}")
    existing = next(r for r in direct if r.name == chosen)

    st.markdown(
        f"Sẽ xoá `{existing.name}` — {existing.type_label}, đang áp cho: "
        + (", ".join(f"`{p}`" for p in existing.to_principals) or "_không ai_")
    )
    if existing.applies_to_every_table:
        st.warning(
            "Chính sách này không có `when_condition` nên đang áp cho MỌI bảng trong "
            "phạm vi đã gắn; xoá nó sẽ gỡ bảo vệ trên toàn bộ phạm vi đó.",
            icon=":material/warning:",
        )
    st.warning(DELETE_SIDE_EFFECTS, icon=":material/warning:")

    draft = PolicyDraft(name=existing.name, policy_type=existing.policy_type)
    return draft, None, existing.name


def _draft_inputs(prefix: str, target: Target, *, existing=None) -> PolicyDraft:
    """Các ô nhập của một chính sách. Trả về bản nháp chưa qua kiểm tra.

    Việc kiểm tra thuộc về service: ở đây chỉ thu thập đúng những gì người dùng
    gõ, để thông báo lỗi luôn là thông báo của backend chứ không phải hai bộ quy
    tắc khác nhau.
    """
    ns = f"pol_{prefix}_{target.key}"

    if existing is None:
        cols = st.columns([2, 3])
        with cols[0]:
            policy_type = st.radio(
                "Loại chính sách", [ROW_FILTER, COLUMN_MASK],
                format_func=lambda t: POLICY_TYPE_LABELS[t],
                key=f"{ns}_type",
            )
        with cols[1]:
            name = st.text_input(
                "Tên chính sách", key=f"{ns}_name", max_chars=255,
                placeholder="che_email_khach_hang",
                help="Không chứa dấu chấm, dấu backtick hay ký tự điều khiển.",
            )
    else:
        policy_type = existing.policy_type
        name = existing.name
        st.markdown(f"**Đang sửa**  \n`{name}` — {existing.type_label}")
        st.caption(RENAME_NOT_OFFERED)

    is_mask = policy_type == COLUMN_MASK

    cols = st.columns(2)
    with cols[0]:
        to_text = st.text_area(
            "Áp dụng cho — mỗi dòng một principal", height=110, key=f"{ns}_to",
            value="\n".join(existing.to_principals) if existing else "",
            placeholder="nhom-phan-tich\nemail@congty.com",
            help="Những principal này sẽ bị che dữ liệu.",
        )
    with cols[1]:
        except_text = st.text_area(
            "Loại trừ — mỗi dòng một principal", height=110, key=f"{ns}_except",
            value="\n".join(existing.except_principals) if existing else "",
            placeholder="service-principal-chay-pipeline",
            help="Những principal này thấy dữ liệu nguyên bản.",
        )
    st.caption(
        f"Tổng số principal (áp dụng + loại trừ) tối đa {MAX_PRINCIPALS}. "
        "Dùng `account users` nếu thực sự muốn áp cho tất cả."
    )

    when_condition = st.text_input(
        "Điều kiện chọn bảng — when_condition", key=f"{ns}_when",
        value=existing.when_condition if existing else "",
        placeholder="has_tag_value('pii','email')   —   để trống = TRUE",
    )
    if not when_condition.strip():
        st.warning(
            "Đang để trống: chính sách sẽ áp cho MỌI bảng trong phạm vi đã gắn.",
            icon=":material/warning:",
        )
    widgets.explain("`when_condition` hoạt động thế nào?", WHEN_CONDITION_DEFAULT_TRUE)

    st.markdown("**Bộ chọn cột theo thẻ — match_columns**")
    widgets.explain("Quy tắc của `match_columns`", MATCH_COLUMNS_RULES)
    matchers: list[dict] = []
    for index in range(MAX_MATCH_COLUMNS):
        previous = (existing.match_columns[index]
                    if existing and index < len(existing.match_columns) else {})
        row = st.columns([2, 4])
        alias = row[0].text_input(
            f"Alias {index + 1}", key=f"{ns}_alias_{index}",
            value=str(previous.get("alias") or ""), placeholder="cot_email",
        )
        condition = row[1].text_input(
            f"Điều kiện thẻ {index + 1}", key=f"{ns}_cond_{index}",
            value=str(previous.get("condition") or ""),
            placeholder="hasTagValue('pii','email')",
        )
        if alias.strip() or condition.strip():
            matchers.append({"alias": alias.strip(), "condition": condition.strip()})

    aliases = [m["alias"] for m in matchers if m["alias"]]

    st.markdown("**Hàm UDF và tham số**")
    function_name = st.text_input(
        "Tên đầy đủ của UDF", key=f"{ns}_fn",
        value=existing.function_name if existing else "",
        placeholder="catalog.schema.ten_ham",
        help="Ba thành phần, ngăn cách bằng dấu chấm.",
    )

    on_column = ""
    if is_mask:
        if aliases:
            default = existing.on_column if existing and existing.on_column in aliases else aliases[0]
            on_column = st.selectbox(
                "Áp hàm che lên alias nào — on_column", aliases,
                index=aliases.index(default), key=f"{ns}_oncol",
                help="Đây là ALIAS trong match_columns, không phải tên cột vật lý.",
            )
        else:
            st.caption(
                "Khai báo ít nhất một alias ở `match_columns` thì mới chọn được "
                "`on_column`."
            )

    widgets.explain("Tham số của hàm được truyền thế nào?", FUNCTION_ARGUMENT_RULES)
    if aliases:
        st.caption("Alias đang khai báo: " + ", ".join(f"`{a}`" for a in aliases))
    using: list[dict] = []
    for index in range(_ARGUMENT_SLOTS):
        default_kind, default_value = _argument_default(existing, index)
        row = st.columns([2, 4])
        kind = row[0].selectbox(
            f"Tham số {index + 1}", _ARG_KINDS,
            index=_ARG_KINDS.index(default_kind), key=f"{ns}_argk_{index}",
        )
        value = row[1].text_input(
            f"Giá trị tham số {index + 1}", key=f"{ns}_argv_{index}",
            value=default_value, disabled=kind == _ARG_NONE,
            placeholder="tên alias" if kind == _ARG_ALIAS else "giá trị hằng",
            label_visibility="visible",
        )
        if kind == _ARG_ALIAS and value.strip():
            using.append({"alias": value.strip()})
        elif kind == _ARG_CONST and value != "":
            using.append({"constant": value})

    comment = st.text_area(
        "Mô tả chính sách", max_chars=1000, height=80, key=f"{ns}_comment",
        value=existing.comment if existing else "",
        placeholder="Chính sách này bảo vệ dữ liệu gì, theo yêu cầu nào.",
    )

    return PolicyDraft(
        name=name.strip(),
        policy_type=policy_type,
        to_principals=_lines(to_text),
        except_principals=_lines(except_text),
        when_condition=when_condition,
        match_columns=matchers,
        function_name=function_name,
        on_column=on_column,
        using=using,
        comment=comment,
    )


def _argument_default(existing, index: int) -> tuple[str, str]:
    if existing is None or index >= len(existing.using):
        return _ARG_NONE, ""
    argument = existing.using[index]
    if argument.get("alias"):
        return _ARG_ALIAS, str(argument["alias"])
    return _ARG_CONST, str(argument.get("constant") or "")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _readiness(operation: str, draft: PolicyDraft, fields, delete_name: str,
               reason: str) -> tuple[bool, list[str]]:
    missing: list[str] = []
    if operation == "create":
        if not draft.name:
            missing.append("tên chính sách")
        if not draft.to_principals:
            missing.append("danh sách áp dụng")
        if not draft.function_name.strip():
            missing.append("tên UDF")
    elif operation == "update":
        if not fields:
            missing.append("ít nhất một trường cần cập nhật")
    else:
        if not delete_name:
            missing.append("chính sách cần xoá")
    if not reason.strip():
        missing.append("lý do thay đổi")
    return not missing, missing


def _signature(ctx: Context, target: Target, attach, operation: str,
               draft: PolicyDraft, fields, delete_name: str, reason: str,
               table_type: str) -> str:
    """Chữ ký bao trùm mọi đầu vào vật chất của bản xem trước."""
    return json.dumps(
        {
            "actor": ctx.actor.email,
            "target": target.key,
            "attach": f"{attach.securable_type}:{attach.full_name}",
            "operation": operation,
            "table_type": table_type,
            "delete_name": delete_name,
            "fields": list(fields or []),
            "reason": reason.strip(),
            "draft": {
                "name": draft.name,
                "policy_type": draft.policy_type,
                "to_principals": draft.to_principals,
                "except_principals": draft.except_principals,
                "when_condition": draft.when_condition,
                "match_columns": draft.match_columns,
                "function_name": draft.function_name,
                "on_column": draft.on_column,
                "using": draft.using,
                "comment": draft.comment,
            },
        },
        sort_keys=True, ensure_ascii=False, default=str,
    )


def _build_plan(policies: PolicyService, target: Target, operation: str,
                draft: PolicyDraft, fields, delete_name: str, reason: str,
                table_type: str):
    if operation == "create":
        return policies.plan_create(target, draft, reason, table_type=table_type)
    if operation == "update":
        return policies.plan_update(target, draft.name, draft, fields or [], reason)
    return policies.plan_delete(target, delete_name, reason)


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


def _apply_step(ctx: Context, policies: PolicyService, target: Target, plan):
    st.markdown("##### 3. Xác nhận và áp dụng")
    st.warning(CONFLICT_FAIL_CLOSED, icon=":material/warning:")
    if plan.multi_object:
        st.warning(
            "Thao tác này không nguyên tử trên các bảng bị ảnh hưởng: chính sách lan "
            "xuống nhiều bảng và có hiệu lực ngay ở truy vấn kế tiếp.",
            icon=":material/account_tree:",
        )
    st.caption(f"Nhập lại tên đầy đủ của điểm gắn để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 2, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đầy đủ", key=f"pol_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            plan.summary[:44] or "Áp dụng", type="primary", width="stretch",
            disabled=not matches, icon=":material/check:",
            key=f"pol_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"pol_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not apply_clicked:
        return

    # Một bản xem trước chỉ được gửi đúng một lần, kể cả khi trang rerun.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner("Đang gửi thay đổi tới Databricks…"):
            outcome = policies.apply(plan, target, confirmation)
    except Exception as exc:
        state.finish()
        state.clear_plan()
        widgets.show_error(exc, context="Không thực hiện được thay đổi chính sách.")
        return
    finally:
        state.finish()

    state.clear_plan()
    state.clear_caches()
    state.set_notice({
        "status": outcome.status,
        "event_id": outcome.event_id,
        "summary": plan.summary,
        "operation": plan.payload.get("operation", ""),
        "policy": plan.payload.get("name", ""),
        "attach": plan.target_name,
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _show_result():
    payload = state.pop_notice()
    if not payload:
        return

    status = payload.get("status")
    policy = payload.get("policy", "")
    attach = payload.get("attach", "")
    operation = payload.get("operation", "")

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{payload.get('summary', 'Hoàn tất')}**",
                       icon=":material/check_circle:")
            st.markdown(
                f"- Chính sách: **{policy}**\n"
                f"- Điểm gắn: **{attach}**"
            )
            verified = payload.get("verified") or {}
            if status != Status.VERIFIED:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            elif operation == "delete":
                if verified.get("deleted"):
                    st.caption("Đã đọc lại: chính sách không còn tồn tại tại điểm gắn này.")
                else:
                    st.warning(
                        verified.get("note")
                        or "Databricks chấp nhận lệnh xoá nhưng chính sách vẫn đọc được.",
                        icon=":material/warning:",
                    )
            else:
                st.caption(
                    "Đã đọc lại từ Databricks. Áp dụng cho: "
                    + (", ".join(verified.get("to_principals") or []) or "—")
                    + " · điều kiện chọn bảng: "
                    + (verified.get("when_condition") or "(trống = TRUE)")
                    + " · hàm: " + (verified.get("function_name") or "—")
                )
            st.caption(
                "Số bảng và số cột thực sự bị ảnh hưởng: Chưa xác định. " + NO_SIMULATION
            )
            st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng không "
            "nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** Đọc lại chính sách **{policy}** tại **{attach}** "
            "TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
        )
        st.caption(f"Event ID: `{payload.get('event_id', '')[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi chính sách không thành công.",
             icon=":material/error:")


# -- tab 2: ràng buộc kiểu cũ --------------------------------------------
def _legacy_tab(ctx: Context, policies: PolicyService, target: Target, detail,
                detail_error):
    st.info(TWO_MECHANISMS, icon=":material/compare_arrows:")

    cap = ctx.capabilities.get("masks.legacy")
    if cap.state != State.UNKNOWN:
        widgets.capability_notice(cap)

    if target.kind != "table":
        st.caption(
            "Ràng buộc kiểu cũ chỉ gắn được trên một bảng cụ thể. Chọn cấp "
            "**Bảng / View** ở khung phía trên để xem bảng đó có gì."
        )
    elif detail is None:
        widgets.show_error(detail_error, context="Không đọc được metadata của bảng.")
        st.caption(
            "Không đọc được metadata thì KHÔNG kết luận được là bảng không có ràng "
            "buộc nào."
        )
    else:
        legacy = policies.legacy_protections(detail)
        rows = legacy.rows()
        if legacy.any_present:
            st.warning(
                f":material/lock: {len(rows)} ràng buộc kiểu cũ đang gắn trực tiếp "
                "trên bảng này.",
                icon=":material/lock:",
            )
            widgets.data_table(
                rows, key=f"pol_legacy_{target.key}",
                download_name=f"{target.full_name}-bao-ve-kieu-cu",
            )
        else:
            widgets.empty_state("empty", what="ràng buộc kiểu cũ")
        st.caption(legacy.note())
        if legacy.observed_at:
            st.caption(f"Dữ liệu đọc lúc {legacy.observed_at}.")

    st.divider()
    st.warning(LEGACY_IS_SQL_ONLY, icon=":material/terminal:")
    st.markdown("**Dạng câu lệnh để tham khảo**")
    st.code(LEGACY_SQL_SHAPES, language=None)
    st.caption(
        ":material/content_copy: Đây là văn bản tham khảo để sao chép sang Databricks "
        "SQL. Ứng dụng không có nút chạy và không gửi câu lệnh nào thay bạn."
    )


# -- tab 3: lưu ý ---------------------------------------------------------
def _notes_tab(policies: PolicyService, attach):
    st.markdown("**Những giới hạn cần biết trước khi đặt chính sách**")

    st.info(PERMISSIONS_NOTE, icon=":material/key:")
    st.info(VIEW_NOT_ATTACHABLE, icon=":material/block:")
    st.warning(PIPELINE_OWNER_RISK, icon=":material/conversion_path:")
    st.warning(TIME_TRAVEL_BLOCKED, icon=":material/history:")
    st.warning(GOVERNED_TAGS_REQUIRED, icon=":material/label:")

    st.divider()
    st.markdown("**Hạn mức tại điểm gắn đang xem**")
    note = policies.quota_note(attach)
    if note:
        st.info(note, icon=":material/inventory:")
    else:
        st.caption("Databricks không công bố hạn mức cho cấp gắn này.")
    st.caption(
        f"Điểm gắn: `{attach.securable_type} {attach.full_name}` · "
        + ("có lan toả xuống bảng con." if attach.cascades
           else "không lan toả — chỉ đúng đối tượng này.")
    )
