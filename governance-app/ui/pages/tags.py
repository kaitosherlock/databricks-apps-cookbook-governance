"""Thẻ, governed tag và Data Classification của một đối tượng.

Ba thứ trông giống một tính năng nhưng hành xử khác hẳn nhau: một lần gán thẻ
nằm trên đối tượng, một tag policy nằm ở cấp account, còn Data Classification là
cấu hình mà ứng dụng chỉ được đọc. Trang này tách chúng thành ba tab để không ai
sửa nhầm cái này khi tưởng đang sửa cái kia.

Mọi thao tác thay đổi đều đi qua ba bước: dựng bản xem trước, đọc bản xem trước,
gõ lại tên đầy đủ rồi mới gửi. Streamlit vẽ lại toàn bộ trang sau mỗi lần bấm,
nên bản xem trước bị huỷ ngay khi bất kỳ ô nhập nào thay đổi.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import Status
from ucg.authz import Action
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.tags import TagService

from .. import nav, picker, state, widgets

#: plan.payload["operation"] mà mỗi biểu mẫu sở hữu. st.tabs vẽ nội dung của MỌI
#: tab trong cùng một lần chạy, nên nếu một biểu mẫu huỷ bản xem trước vô điều
#: kiện thì nó sẽ xoá mất bản xem trước mà biểu mẫu kia vừa dựng.
_ASSIGN_OPS = ("create", "update", "delete")
_POLICY_OPS = ("policy_create", "policy_update", "policy_delete")

_ASSIGN_MODES = ["Gán hoặc sửa giá trị thẻ", "Gỡ thẻ khỏi đối tượng"]
_POLICY_MODES = ["Tạo governed tag", "Cập nhật governed tag", "Xoá governed tag"]

_SCOPE_OBJECT = "Thẻ của bảng"
_SCOPE_COLUMN = "Thẻ của một cột"


def render(ctx: Context) -> None:
    assets = AssetService(ctx)
    tags = TagService(ctx)

    st.markdown("### Thẻ và phân loại")
    st.caption(
        "Xem và thay đổi thẻ trên một đối tượng, quản lý định nghĩa governed tag, "
        "và xem cấu hình Data Classification của catalog."
    )

    _show_result()

    target = state.current_target()
    with st.expander("Đối tượng đang thao tác", expanded=target is None):
        picked = picker.compact_picker(ctx, assets)
        if picked is not None:
            target = picked
    if target is None:
        st.info("Chọn một đối tượng để bắt đầu.", icon=":material/search:")
        return

    picker.header(ctx, target, None)
    st.divider()

    tab_object, tab_policy, tab_class = st.tabs(
        ["Thẻ trên tài sản", "Governed tag", "Data Classification"]
    )
    with tab_object:
        _object_tags(ctx, tags, assets, target)
    with tab_policy:
        _policies(ctx, tags)
    with tab_class:
        _classification(tags, target.catalog)


# =========================================================================
# Tab 1 - thẻ trên tài sản
# =========================================================================
def _object_tags(ctx: Context, tags: TagService, assets: AssetService, target):
    taggable, why = tags.can_tag(target)
    if not taggable:
        st.warning(why, icon=":material/block:")
        widgets.caveat(TagService.UNSUPPORTED_ENTITY_NOTE)
        return

    column = _column_scope(ctx, assets, target)
    if column is None:
        return

    try:
        entity_type, entity_name = tags.entity_ref(target, column=column)
    except Exception as exc:
        widgets.show_error(exc, context="Không xác định được cách địa chỉ hoá đối tượng này.")
        return
    st.caption(f"API địa chỉ hoá đối tượng này là: `{entity_type}` · `{entity_name}`")

    listing = _tag_listing(tags, target, column)
    rows = list(listing.items) if listing.ok else []

    head = st.columns([1.2, 2, 2])
    with head[0]:
        if listing.ok:
            st.metric("Số thẻ đang gắn", len(rows))
        else:
            st.markdown("**Số thẻ đang gắn**  \nChưa xác định")
    with head[1]:
        st.write("")
        if st.button("Đọc lại từ Databricks", icon=":material/refresh:",
                     width="stretch", key=f"tg_reload_{_sfx(target, column)}"):
            state.clear_caches()
            st.rerun()
    with head[2]:
        st.write("")
        nav.link_button(
            "Xem quyền truy cập", "permissions", icon=":material/key:",
            width="stretch", widget_key=f"tg_perm_{target.key}",
        )

    widgets.listing_or_empty(
        listing,
        what="thẻ",
        render=lambda lst: widgets.data_table(
            [r.row() for r in lst.items],
            key=f"tags_{_sfx(target, column)}",
            download_name=f"{entity_name}-the",
        ),
    )

    system_rows = [r for r in rows if r.system]
    if system_rows:
        st.info(
            "Chỉ đọc — do Databricks quản lý: " + ", ".join(r.key for r in system_rows),
            icon=":material/lock:",
        )
        widgets.caveat(TagService.SYSTEM_TAG_NOTE)

    widgets.caveat(TagService.NO_INHERITANCE_NOTE)
    widgets.explain(
        "Làm sao biết “những đối tượng nào đang mang thẻ này”?",
        TagService.REVERSE_LOOKUP_NOTE,
    )
    widgets.explain("Giới hạn của Databricks khi gắn thẻ", TagService.LIMITS_NOTE)
    _probe_key(tags, target, column)

    st.divider()
    _assign_section(ctx, tags, target, column, rows)


def _column_scope(ctx: Context, assets: AssetService, target) -> str | None:
    """Chọn gắn thẻ ở mức bảng hay ở mức một cột. '' nghĩa là mức bảng."""
    if target.kind != "table":
        return ""

    scope = st.radio(
        "Phạm vi thẻ", [_SCOPE_OBJECT, _SCOPE_COLUMN], horizontal=True,
        key=f"tg_scope_{target.key}",
        help="Thẻ cột là một đối tượng riêng trong API, không liên quan tới thẻ của bảng.",
    )
    if scope == _SCOPE_OBJECT:
        return ""

    columns = _table_columns(assets, target)
    if not columns:
        widgets.empty_state(
            "empty", what="cột",
            detail="Không đọc được danh sách cột của bảng này, nên không chọn được cột để gắn thẻ.",
        )
        return None
    column = st.selectbox("Cột", columns, key=f"tg_col_{target.key}")
    st.caption(
        "Thẻ cột được địa chỉ hoá bằng tên bốn thành phần "
        "(catalog.schema.bảng.cột) với entity_type `columns`."
    )
    return column


def _table_columns(assets: AssetService, target) -> list[str]:
    key = f"tagcols:{target.key}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    try:
        with st.spinner("Đang đọc danh sách cột…"):
            detail = assets.detail(target)
    except Exception as exc:
        widgets.show_error(exc, context="Không đọc được danh sách cột của bảng này.")
        return state.cache_put(key, [])
    return state.cache_put(
        key, [str(c.get("Cột")) for c in detail.columns if c.get("Cột")]
    )


def _tag_listing(tags: TagService, target, column: str):
    key = f"tags:{target.key}:{column or '-'}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    with st.spinner("Đang đọc thẻ từ Databricks…"):
        listing = (
            tags.list_column_tags(target, column) if column else tags.list_tags(target)
        )
    return state.cache_put(key, listing)


def _probe_key(tags: TagService, target, column: str):
    """“Chưa gán” và “đã gán nhưng không có giá trị” là hai trạng thái khác nhau."""
    sfx = _sfx(target, column)
    with st.expander("Kiểm tra một khoá thẻ cụ thể", expanded=False):
        st.caption(
            "Dùng khi cần phân biệt “chưa gán thẻ” với “đã gán thẻ nhưng không có giá trị”. "
            "Đây vẫn là tra cứu theo đối tượng đang chọn, không phải tra ngược theo thẻ."
        )
        probe = st.text_input("Khoá thẻ", key=f"tg_probe_{sfx}", placeholder="ví dụ: pii")
        if st.button("Kiểm tra khoá này", icon=":material/search:",
                     disabled=not probe.strip(), key=f"tg_probe_go_{sfx}"):
            try:
                with st.spinner("Đang kiểm tra…"):
                    value = tags.tag_value(target, probe, column=column)
            except Exception as exc:
                widgets.show_error(exc)
            else:
                if value is None:
                    st.info(f"Chưa gán thẻ “{probe}” trên đối tượng này.",
                            icon=":material/inbox:")
                elif value == "":
                    st.success(f"Đã gán thẻ “{probe}”, không có giá trị.",
                               icon=":material/check_circle:")
                else:
                    st.success(f"Đã gán thẻ “{probe}” = {value}",
                               icon=":material/check_circle:")


# -- thay đổi thẻ ---------------------------------------------------------
def _assign_section(ctx: Context, tags: TagService, target, column: str, rows: list):
    st.markdown("#### Thay đổi thẻ")

    decision = ctx.authz.check(Action.ASSIGN_TAG, target)
    if not decision.allowed:
        st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
        widgets.caveat(TagService.ASSIGN_PERMISSION_NOTE)
        return

    widgets.caveat(TagService.ASSIGN_PERMISSION_NOTE)
    if target.kind == "table" and not column:
        widgets.caveat(TagService.NO_INHERITANCE_NOTE)

    sfx = _sfx(target, column)
    mode_key = f"tg_mode_{sfx}"
    mode = st.radio("Thao tác", _ASSIGN_MODES, horizontal=True, key=mode_key)

    if mode == _ASSIGN_MODES[1]:
        _remove_form(ctx, tags, target, column, rows, sfx)
    else:
        _assign_form(ctx, tags, target, column, rows, sfx, mode_key)


def _assign_form(ctx: Context, tags: TagService, target, column: str, rows: list,
                 sfx: str, mode_key: str):
    current_values = {r.key: r.value for r in rows}
    own_keys = [r.key for r in rows if not r.system]
    policies = _policy_listing(tags)
    policy_keys = [p.key for p in policies.items if not p.system] if policies.ok else []

    sources: list[str] = []
    if own_keys:
        sources.append("Khoá đã có trên đối tượng")
    if policy_keys:
        sources.append("Governed tag đã định nghĩa")
    sources.append("Nhập khoá mới")

    cols = st.columns([2.2, 3])
    with cols[0]:
        source = st.radio("Nguồn khoá thẻ", sources, key=f"tg_ksrc_{sfx}")
    with cols[1]:
        if source == "Khoá đã có trên đối tượng":
            key = st.selectbox("Khoá thẻ", own_keys, key=f"tg_kexist_{sfx}")
        elif source == "Governed tag đã định nghĩa":
            key = st.selectbox("Khoá thẻ", policy_keys, key=f"tg_kgov_{sfx}")
        else:
            # Không cắt khoảng trắng: khoá thẻ phân biệt chữ hoa chữ thường và
            # dịch vụ có thông báo riêng cho khoá có khoảng trắng thừa.
            key = st.text_input("Khoá thẻ mới", key=f"tg_knew_{sfx}",
                                placeholder="ví dụ: pii")

    if not policies.ok and source == "Nhập khoá mới":
        st.caption(
            "Không đọc được danh sách tag policy, nên ứng dụng không gợi ý được "
            "governed tag. Databricks vẫn kiểm tra ràng buộc khi gửi."
        )

    existing = current_values.get(key) if key.strip() else None
    if existing is not None:
        st.caption(f"Giá trị hiện tại: **{existing or '(không có giá trị)'}**")
        widgets.caveat(TagService.IMMUTABLE_KEY_NOTE)
        if st.button(f"Gỡ thẻ {key} (để đổi sang khoá khác)",
                     icon=":material/label_off:", key=f"tg_switch_{sfx}"):
            st.session_state[mode_key] = _ASSIGN_MODES[1]
            state.clear_plan()
            st.rerun()

    governed, allowed, policy_note = _policy_for_key(tags, key)
    value = _value_field(sfx, key, existing, governed, allowed, policy_note)

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"tg_reason_{sfx}",
        placeholder="Ví dụ: đánh dấu cột chứa dữ liệu cá nhân theo yêu cầu TICKET-123.",
        help="Lý do được ghi vào nhật ký thao tác của ứng dụng.",
    )

    widgets.warn_block([TagService.ABAC_IMPACT_NOTE])

    signature = "|".join([
        "tag-assign", target.key, column, key, value, reason.strip(), ctx.actor.email,
    ])
    ready = bool(key.strip() and reason.strip())
    if not ready:
        st.caption("Nhập khoá thẻ và lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, tags, signature,
        build=lambda: tags.plan_assign(target, key, value, reason, column=column),
        ops=_ASSIGN_OPS, ready=ready, prefix=f"tg_asg_{sfx}", apply_target=target,
    )


def _value_field(sfx: str, key: str, existing: str | None, governed: bool | None,
                 allowed: list[str], policy_note: str) -> str:
    """Governed tag có danh sách giá trị thì chỉ cho chọn trong danh sách đó."""
    if governed and allowed:
        index = allowed.index(existing) if existing in allowed else 0
        value = st.selectbox("Giá trị", allowed, index=index,
                             key=f"tg_valsel_{sfx}_{key}")
        st.caption(policy_note)
        widgets.caveat(TagService.GOVERNED_TAG_PERMISSION_NOTE)
        return value

    value = st.text_input(
        "Giá trị", key=f"tg_val_{sfx}",
        placeholder="Để trống nếu thẻ không cần giá trị",
        help="Thẻ có thể tồn tại mà không có giá trị; đó là trạng thái hợp lệ.",
    )
    if policy_note:
        st.caption(policy_note)
    if governed:
        widgets.caveat(TagService.GOVERNED_TAG_PERMISSION_NOTE)
    elif governed is None and key.strip():
        st.caption(
            "Chưa xác định được khoá này có phải governed tag hay không, nên ứng dụng "
            "không giới hạn giá trị. Databricks vẫn có thể từ chối khi gửi."
        )
    return value


def _remove_form(ctx: Context, tags: TagService, target, column: str, rows: list,
                 sfx: str):
    removable = [r for r in rows if not r.system]
    if not removable:
        st.info("Không có thẻ nào có thể gỡ trên đối tượng này.", icon=":material/inbox:")
        if any(r.system for r in rows):
            widgets.caveat(TagService.SYSTEM_TAG_NOTE)
        return

    values = {r.key: r.value for r in removable}
    key = st.selectbox(
        "Thẻ cần gỡ", [r.key for r in removable],
        format_func=lambda k: f"{k} = {values.get(k) or '(không có giá trị)'}",
        key=f"tg_rmkey_{sfx}",
    )
    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"tg_rmreason_{sfx}",
        placeholder="Ví dụ: thẻ gắn nhầm đối tượng, gỡ theo yêu cầu TICKET-123.",
    )

    widgets.warn_block([TagService.ABAC_IMPACT_NOTE])
    widgets.caveat(TagService.IMMUTABLE_KEY_NOTE)

    signature = "|".join([
        "tag-remove", target.key, column, key, reason.strip(), ctx.actor.email,
    ])
    ready = bool(key and reason.strip())
    if not ready:
        st.caption("Nhập lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, tags, signature,
        build=lambda: tags.plan_remove(target, key, reason, column=column),
        ops=_ASSIGN_OPS, ready=ready, prefix=f"tg_rm_{sfx}", apply_target=target,
    )


# =========================================================================
# Tab 2 - governed tag
# =========================================================================
def _policy_listing(tags: TagService):
    hit = state.cache_get("tagpolicies")
    if hit is not None:
        return hit
    with st.spinner("Đang đọc danh sách tag policy…"):
        return state.cache_put("tagpolicies", tags.governed_tags())


def _policy_for_key(tags: TagService, key: str) -> tuple[bool | None, list[str], str]:
    """(governed, giá trị cho phép, ghi chú). ``None`` = không đọc được policy."""
    if not key.strip():
        return None, [], ""
    cache_key = f"tagpolicy:{key}"
    hit = state.cache_get(cache_key)
    if hit is not None:
        return hit
    try:
        with st.spinner("Đang đọc tag policy của khoá này…"):
            governed, allowed, note = tags.allowed_values(key)
    except Exception as exc:
        widgets.show_error(exc, context="Không kiểm tra được tag policy của khoá này.")
        return None, [], ""
    return state.cache_put(cache_key, (governed, allowed, note))


def _policies(ctx: Context, tags: TagService):
    listing = _policy_listing(tags)
    items = list(listing.items) if listing.ok else []

    head = st.columns([1.2, 2, 2])
    with head[0]:
        if listing.ok:
            st.metric("Số governed tag", len(items))
        else:
            st.markdown("**Số governed tag**  \nChưa xác định")
    with head[1]:
        st.write("")
        if st.button("Đọc lại danh sách tag policy", icon=":material/refresh:",
                     width="stretch", key="tg_preload"):
            state.clear_caches()
            st.rerun()

    widgets.listing_or_empty(
        listing,
        what="governed tag",
        render=lambda lst: widgets.data_table(
            [p.row() for p in lst.items],
            key="tag_policies",
            download_name="governed-tag",
        ),
    )

    widgets.caveat(TagService.GOVERNED_TAG_PERMISSION_NOTE)
    widgets.explain("Thẻ hệ thống “class.*”", TagService.SYSTEM_TAG_NOTE)
    widgets.explain(
        "Đối tượng nào đang mang governed tag này?",
        TagService.REVERSE_LOOKUP_NOTE,
    )
    widgets.caveat(TagService.LIMITS_NOTE)

    st.divider()
    _policy_section(ctx, tags, items)


def _policy_section(ctx: Context, tags: TagService, items: list):
    st.markdown("#### Quản lý định nghĩa governed tag")

    decision = ctx.authz.check(Action.MANAGE_TAG_POLICY, None)
    if not decision.allowed:
        st.info(f"**Chỉ đọc** — {decision.reason}", icon=":material/lock:")
        widgets.caveat(TagService.GOVERNED_TAG_PERMISSION_NOTE)
        return

    editable = [p for p in items if not p.system]
    system = [p for p in items if p.system]
    if system:
        st.info(
            "Chỉ đọc — do Databricks định nghĩa: " + ", ".join(p.key for p in system),
            icon=":material/lock:",
        )
        widgets.caveat(TagService.SYSTEM_TAG_NOTE)

    mode = st.radio("Thao tác", _POLICY_MODES, horizontal=True, key="tg_pmode")

    if mode == _POLICY_MODES[0]:
        _policy_upsert_form(ctx, tags, existing=None)
    elif mode == _POLICY_MODES[1]:
        if not editable:
            st.info(
                "Không có governed tag nào do tổ chức tự định nghĩa để cập nhật.",
                icon=":material/inbox:",
            )
            return
        chosen = st.selectbox(
            "Governed tag cần cập nhật", [p.key for p in editable], key="tg_pupd",
        )
        existing = next((p for p in editable if p.key == chosen), None)
        _policy_upsert_form(ctx, tags, existing=existing)
    else:
        if not editable:
            st.info(
                "Không có governed tag nào do tổ chức tự định nghĩa để xoá.",
                icon=":material/inbox:",
            )
            return
        _policy_delete_form(ctx, tags, editable)


def _policy_upsert_form(ctx: Context, tags: TagService, existing):
    creating = existing is None
    slot = "new" if creating else existing.key

    if creating:
        key = st.text_input("Khoá governed tag", key="tg_pkey_new",
                            placeholder="ví dụ: data_sensitivity")
    else:
        key = existing.key
        st.caption(f"Khoá: **{key}**")
        widgets.caveat(TagService.IMMUTABLE_KEY_NOTE)

    description = st.text_input(
        "Mô tả", value="" if creating else existing.description,
        key=f"tg_pdesc_{slot}",
        help="Mô tả hiển thị cho người gán thẻ trong Databricks.",
    )
    values_text = st.text_area(
        "Giá trị cho phép — mỗi dòng một giá trị",
        value="" if creating else "\n".join(existing.values),
        key=f"tg_pvals_{slot}",
        placeholder="public\ninternal\nconfidential",
        help="Để trống nghĩa là governed tag không giới hạn danh sách giá trị.",
    )
    values = [v.strip() for v in values_text.splitlines() if v.strip()]
    st.caption(
        f"Đang khai báo {len(values)} giá trị."
        if values else "Không giới hạn giá trị."
    )
    if not creating:
        removed = [v for v in existing.values if v not in values]
        if removed:
            st.warning(
                "Sẽ bỏ khỏi danh sách cho phép: " + ", ".join(removed)
                + ". Các đối tượng đang mang giá trị này không được ứng dụng sửa giúp.",
                icon=":material/warning:",
            )

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"tg_preason_{slot}",
        placeholder="Ví dụ: chuẩn hoá bộ nhãn mức nhạy cảm theo chính sách nội bộ.",
    )

    widgets.caveat(TagService.GOVERNED_TAG_PERMISSION_NOTE)
    widgets.warn_block([TagService.ABAC_IMPACT_NOTE])
    widgets.caveat(TagService.LIMITS_NOTE)

    signature = "|".join([
        "tag-policy-upsert", key, description.strip(), "\n".join(values),
        reason.strip(), ctx.actor.email,
    ])
    ready = bool(key.strip() and reason.strip())
    if not ready:
        st.caption("Nhập khoá governed tag và lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, tags, signature,
        build=lambda: tags.plan_policy_upsert(key, description, values, reason),
        ops=_POLICY_OPS, ready=ready, prefix=f"tg_pol_{slot}", apply_target=None,
    )


def _policy_delete_form(ctx: Context, tags: TagService, editable: list):
    chosen = st.selectbox(
        "Governed tag cần xoá", [p.key for p in editable], key="tg_pdel",
    )
    policy = next((p for p in editable if p.key == chosen), None)
    if policy is not None:
        st.caption(
            "Giá trị cho phép hiện tại: "
            + (", ".join(policy.values) if policy.values else "Không giới hạn")
        )

    reason = st.text_area(
        "Lý do thay đổi", max_chars=500, key=f"tg_pdelreason_{chosen}",
        placeholder="Ví dụ: nhãn này đã ngừng dùng theo quyết định của hội đồng dữ liệu.",
    )

    st.warning(
        "Xoá tag policy sẽ bỏ ràng buộc giá trị và bỏ lớp quyền ASSIGN/MANAGE gắn với thẻ. "
        "Các lần gán thẻ đã có trên đối tượng: Chưa xác định — ứng dụng không liệt kê ngược được.",
        icon=":material/warning:",
    )
    widgets.warn_block([TagService.ABAC_IMPACT_NOTE])

    signature = "|".join([
        "tag-policy-delete", chosen, reason.strip(), ctx.actor.email,
    ])
    ready = bool(chosen and reason.strip())
    if not ready:
        st.caption("Nhập lý do để tạo bản xem trước.")

    _preview_and_apply(
        ctx, tags, signature,
        build=lambda: tags.plan_policy_delete(chosen, reason),
        ops=_POLICY_OPS, ready=ready, prefix=f"tg_poldel_{chosen}", apply_target=None,
    )


# =========================================================================
# Tab 3 - Data Classification (chỉ đọc)
# =========================================================================
def _classification(tags: TagService, catalog: str):
    st.caption(f"Cấu hình Data Classification của catalog **{catalog}**.")

    view = _classification_view(tags, catalog)
    if not view.ok:
        widgets.show_error(view.error, context="Không đọc được cấu hình Data Classification.")
    else:
        fields = view.summary_fields()
        cols = st.columns(2)
        for index, (label, value) in enumerate(fields):
            cols[index % 2].markdown(f"**{label}**  \n{value}")

        st.divider()
        if not view.configured:
            widgets.empty_state(
                "unconfigured",
                detail="Databricks chưa có cấu hình Data Classification cho catalog này. "
                       "Đây là kết quả đọc được, không phải lỗi quyền.",
            )
        else:
            rows = view.rows()
            if rows:
                metric_cols = st.columns([1.2, 3])
                with metric_cols[0]:
                    st.metric("Thẻ đang bật tự động gắn", len(view.enabled_tags))
                widgets.data_table(
                    rows, key=f"classification_{catalog}",
                    download_name=f"{catalog}-data-classification",
                )
            else:
                widgets.empty_state(
                    "empty", what="thẻ phân loại",
                    detail="Catalog đã có cấu hình nhưng không khai báo thẻ phân loại nào.",
                )
            st.caption(f"Phạm vi quét: {view.scope_label}")

        if view.raw:
            widgets.technical_details(
                view.raw, label="Cấu hình thô (JSON)",
                download=f"{catalog}-data-classification",
            )
        if view.observed_at:
            st.caption(f"Dữ liệu đọc lúc {view.observed_at}.")

    st.divider()
    st.info(TagService.CLASSIFICATION_READ_ONLY, icon=":material/lock:")
    st.caption(tags.classification_results_note())
    widgets.explain("Data Classification hoạt động thế nào",
                    TagService.CLASSIFICATION_NOTE)
    widgets.explain("Cách khai báo phạm vi quét",
                    TagService.CLASSIFICATION_SCOPE_NOTE)


def _classification_view(tags: TagService, catalog: str):
    key = f"classification:{catalog}"
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    with st.spinner("Đang đọc cấu hình Data Classification…"):
        return state.cache_put(key, tags.classification_config(catalog))


# =========================================================================
# Xem trước -> xác nhận -> áp dụng
# =========================================================================
def _preview_and_apply(ctx: Context, tags: TagService, signature: str, *, build,
                       ops: tuple[str, ...], ready: bool, prefix: str, apply_target):
    plan = state.get_plan()
    if plan is not None and plan.payload.get("operation") in ops:
        state.invalidate_plan_if_changed(signature)
        plan = state.get_plan()
    else:
        plan = None

    st.markdown("**Xem trước rồi mới gửi**")
    if plan is None or not state.plan_matches(signature):
        if st.button("Tạo bản xem trước", type="primary", disabled=not ready,
                     icon=":material/preview:", key=f"{prefix}_preview"):
            state.clear_plan()
            try:
                with st.spinner("Đang kiểm tra trạng thái hiện tại tại Databricks…"):
                    built = build()
            except Exception as exc:
                widgets.show_error(exc)
                return
            state.set_plan(built, signature)
            st.rerun()
        st.caption(
            "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. "
            "Sửa bất kỳ ô nào ở trên sẽ huỷ bản xem trước cũ."
        )
        return

    _render_preview(plan)
    _apply_step(ctx, tags, plan, apply_target, prefix)


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


def _apply_step(ctx: Context, tags: TagService, plan, apply_target, prefix: str):
    st.caption(f"Nhập lại tên đầy đủ để xác nhận: `{plan.target_name}`")

    cols = st.columns([4, 2.4, 2])
    with cols[0]:
        confirmation = st.text_input(
            "Tên đầy đủ", key=f"{prefix}_confirm_{plan.plan_id}",
            placeholder=plan.target_name, label_visibility="collapsed",
        )
    matches = confirmation.strip() == plan.target_name
    with cols[1]:
        apply_clicked = st.button(
            plan.summary[:40] or "Áp dụng", type="primary", width="stretch",
            disabled=not matches, icon=":material/check:",
            key=f"{prefix}_apply_{plan.plan_id}",
        )
    with cols[2]:
        if st.button("Huỷ bản xem trước", width="stretch", icon=":material/close:",
                     key=f"{prefix}_cancel_{plan.plan_id}"):
            state.clear_plan()
            st.rerun()

    if not apply_clicked:
        return

    # Một bản xem trước chỉ được gửi một lần: rerun không gửi lại được.
    if not state.begin(plan.operation_id):
        st.warning("Yêu cầu đang được xử lý, vui lòng đợi.", icon=":material/hourglass:")
        return

    try:
        with st.spinner("Đang gửi thay đổi tới Databricks…"):
            outcome = tags.apply(plan, apply_target, confirmation)
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
        "target_type": plan.target_type,
        "tag_key": plan.payload.get("tag_key", ""),
        "verified": outcome.verified_state,
        "message": outcome.message,
    })
    st.rerun()


def _show_result():
    payload = state.pop_notice()
    if not payload:
        return

    status = payload.get("status")
    summary = payload.get("summary") or "Hoàn tất"
    target = payload.get("target", "")
    tag_key = payload.get("tag_key", "")

    if status in (Status.VERIFIED, Status.APPLIED):
        with st.container(border=True):
            st.success(f"**{summary}**", icon=":material/check_circle:")
            st.markdown(
                f"- Khoá thẻ: **{tag_key or '—'}**\n"
                f"- Đối tượng: **{target}** ({payload.get('target_type', '')})"
            )
            verified = payload.get("verified") or {}
            if status == Status.VERIFIED:
                st.caption(_verified_text(verified))
                if not verified.get("applied", True):
                    st.warning(
                        "Databricks đã nhận yêu cầu nhưng trạng thái đọc lại chưa khớp "
                        "mong đợi. Hãy kiểm tra lại trong Catalog Explorer.",
                        icon=":material/warning:",
                    )
            else:
                st.caption("Đã gửi thành công nhưng chưa đọc lại được để xác minh.")
            st.caption(TagService.ABAC_IMPACT_NOTE)
            st.caption(f"Event ID: `{str(payload.get('event_id', ''))[:12]}`")
        return

    if status in Status.NEEDS_RECONCILE:
        st.warning(
            "**Chưa xác định được kết quả.** Yêu cầu đã được gửi nhưng ứng dụng "
            "không nhận được xác nhận.",
            icon=":material/help:",
        )
        st.markdown(
            f"**Việc cần làm:** Đọc lại trạng thái thẻ **{tag_key or '—'}** trên "
            f"**{target}** TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
        )
        st.caption(f"Event ID: `{str(payload.get('event_id', ''))[:12]}`")
        return

    st.error(payload.get("message") or "Thay đổi không thành công.",
             icon=":material/error:")


def _verified_text(verified: dict) -> str:
    if "present" in verified:
        return (
            "Đã đọc lại từ Databricks: mục cần gỡ VẪN CÒN."
            if verified.get("present")
            else "Đã đọc lại từ Databricks: mục cần gỡ đã không còn."
        )
    if "values_now" in verified:
        values = ", ".join(verified.get("values_now") or [])
        return "Đã đọc lại từ Databricks. Giá trị cho phép hiện tại: " + (
            values or "Không giới hạn"
        )
    now = verified.get("value_now")
    if now is None:
        return "Đã đọc lại từ Databricks: thẻ hiện không có trên đối tượng."
    return "Đã đọc lại từ Databricks. Giá trị hiện tại: " + (
        now if now else "(không có giá trị)"
    )


def _sfx(target, column: str) -> str:
    """Khoá widget phải gắn với cả đối tượng lẫn cột, nếu không trạng thái lẫn nhau."""
    return f"{target.key}_{column or 'obj'}"
