"""Who has access to this object, and where that access comes from.

The distinction the brief cares most about is direct vs inherited, so it is a
column, a filter, and the thing that decides whether a revoke button appears at
all. A grant inherited from a catalog cannot be revoked here, and the page says
where to go instead rather than offering a button that would fail.
"""
from __future__ import annotations

import streamlit as st

from ucg import privileges as priv
from ucg.authz import Action
from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.grants import DIRECT, INHERITED, OWNERSHIP, GrantService

from .. import nav, picker, state, widgets

_SOURCE_FILTERS = ["Tất cả", "Cấp trực tiếp", "Kế thừa"]


def render(ctx: Context):
    assets = AssetService(ctx)
    grants = GrantService(ctx)

    st.markdown("### Quyền truy cập")
    st.caption(
        "Xem ai có quyền gì trên một đối tượng, và quyền đó đến từ đâu."
    )

    target = state.current_target()
    with st.expander("Đối tượng đang xem", expanded=target is None):
        picked = picker.level_picker(ctx, assets)
        if picked is not None:
            target = picked
    if target is None:
        return

    st.divider()

    owner = _owner_of(assets, target)
    picker.header(ctx, target, None)
    if owner:
        st.caption(f"Chủ sở hữu hiện tại: **{owner}**")

    tab_effective, tab_direct = st.tabs(
        ["Quyền hiệu lực (gồm kế thừa)", "Chỉ quyền cấp trực tiếp"]
    )

    with tab_effective:
        _grant_table(ctx, grants, target, owner, effective=True)
    with tab_direct:
        _grant_table(ctx, grants, target, owner, effective=False)

    st.divider()
    _privilege_glossary(ctx, target, assets)


def _owner_of(assets: AssetService, target: Target) -> str:
    key = f"owner:{target.key}"
    cached = state.cache_get(key)
    if cached is not None:
        return cached
    try:
        return state.cache_put(key, assets.detail(target).owner or "")
    except Exception:
        return state.cache_put(key, "")


def _grant_table(ctx: Context, grants: GrantService, target: Target, owner: str,
                 *, effective: bool):
    cache_key = f"grants:{'eff' if effective else 'dir'}:{target.key}"
    view = state.cache_get(cache_key)
    if view is None:
        with st.spinner("Đang đọc quyền từ Databricks…"):
            view = grants.effective(target) if effective else grants.direct(target)
            if view.ok:
                grants.with_owner(view, owner)
            state.cache_put(cache_key, view)

    if not view.ok:
        widgets.show_error(view.error, context="Không đọc được danh sách quyền.")
        st.caption(
            "Để đọc được toàn bộ quyền trên một đối tượng, danh tính thực thi cần "
            "là chủ sở hữu, hoặc có MANAGE trên đối tượng, hoặc là metastore admin. "
            "Nếu không, Databricks chỉ trả về quyền của chính danh tính đó."
        )
        return

    rows = view.rows
    if not rows:
        st.info(
            "Databricks không trả về quyền nào trên đối tượng này cho danh tính thực thi.",
            icon=":material/inbox:",
        )
        st.caption(
            "Đây **không** phải kết luận “không ai có quyền”: nếu danh tính thực thi "
            "không đủ quyền đọc ACL, API chỉ trả về phần nó được phép thấy."
        )
        return

    filtered = _filters(rows, target, effective)
    if not filtered:
        st.info("Không có dòng nào khớp bộ lọc.", icon=":material/filter_alt_off:")
        return

    st.caption(f"{len(filtered)}/{len(rows)} dòng.")
    widgets.data_table(
        [r.row() for r in filtered],
        key=f"{cache_key}_table",
        download_name=f"{target.full_name}-{'quyen-hieu-luc' if effective else 'quyen-truc-tiep'}",
    )
    st.caption(view.CAVEAT)

    if effective:
        _inheritance_help(ctx, target, filtered)
    else:
        _direct_actions(ctx, target, filtered)


def _filters(rows, target: Target, effective: bool):
    filters = state.filters()
    cols = st.columns([2.5, 2.5, 2])
    with cols[0]:
        term = st.text_input(
            "Tìm principal", value=filters.get("principal", ""),
            key=f"pf_principal_{target.key}_{effective}",
            placeholder="email, tên nhóm hoặc application ID",
        )
        state.set_filter("principal", term)
    with cols[1]:
        codes = sorted({r.privilege for r in rows})
        chosen = st.multiselect(
            "Quyền", codes, default=[], key=f"pf_priv_{target.key}_{effective}",
            format_func=lambda c: priv.info(c).display if c != "(chủ sở hữu)" else "Chủ sở hữu",
        )
    with cols[2]:
        source = st.selectbox(
            "Nguồn quyền", _SOURCE_FILTERS,
            index=_SOURCE_FILTERS.index(filters.get("source", "Tất cả"))
            if filters.get("source") in _SOURCE_FILTERS else 0,
            key=f"pf_src_{target.key}_{effective}",
        )
        state.set_filter("source", source)

    out = rows
    if term:
        needle = term.strip().lower()
        out = [r for r in out if needle in r.principal.lower()]
    if chosen:
        out = [r for r in out if r.privilege in chosen]
    if source == "Cấp trực tiếp":
        out = [r for r in out if r.source == DIRECT]
    elif source == "Kế thừa":
        out = [r for r in out if r.source == INHERITED]
    return out


def _inheritance_help(ctx: Context, target: Target, rows):
    inherited = [r for r in rows if r.source == INHERITED]
    if not inherited:
        return
    parents = sorted({(r.inherited_from, r.inherited_from_type)
                      for r in inherited if r.inherited_from})
    if not parents:
        return

    st.markdown("**Quyền kế thừa — sửa ở cấp cha**")
    st.caption(
        "Quyền kế thừa không thu hồi được tại đối tượng này. "
        "Hãy mở đúng đối tượng cha đã cấp quyền đó."
    )
    for name, kind in parents:
        cols = st.columns([4, 2])
        cols[0].markdown(f":material/account_tree: `{name}` ({kind.lower() or 'cấp cha'})")
        parent_target = _parent_target(name, kind)
        with cols[1]:
            if parent_target is None:
                st.caption("Ngoài phạm vi được phép mở.")
            elif not ctx.settings.in_scope(parent_target.catalog):
                st.caption("Ngoài phạm vi cấu hình.")
            elif st.button("Xem quyền tại cấp cha", key=f"goparent_{name}",
                           width="stretch", icon=":material/north_east:"):
                state.select_target(parent_target)
                state.clear_caches()
                st.rerun()


def _parent_target(full_name: str, kind: str) -> Target | None:
    parts = [p for p in (full_name or "").split(".") if p]
    try:
        if len(parts) == 1:
            return Target("catalog", parts[0])
        if len(parts) == 2:
            return Target("schema", parts[0], parts[1])
    except Exception:
        return None
    return None


def _direct_actions(ctx: Context, target: Target, rows):
    revocable = [r for r in rows if r.source == DIRECT]
    owners = [r for r in rows if r.source == OWNERSHIP]
    if owners:
        st.caption(
            "Dòng “Từ quyền sở hữu” không phải một grant: chủ sở hữu có toàn quyền "
            "một cách ngầm định và không xuất hiện trong API quyền. "
            "Muốn thay đổi, phải chuyển quyền sở hữu."
        )
    if not revocable:
        return

    can_revoke = ctx.authz.allows(Action.REVOKE, target)
    st.markdown("**Thu hồi quyền cấp trực tiếp**")
    if not can_revoke:
        st.caption(ctx.authz.check(Action.REVOKE, target).reason)
        return

    cols = st.columns([3, 3, 2])
    principals = sorted({r.principal for r in revocable})
    with cols[0]:
        principal = st.selectbox("Principal", principals, key=f"rv_p_{target.key}")
    held = sorted({r.privilege for r in revocable if r.principal == principal})
    with cols[1]:
        chosen = st.multiselect(
            "Quyền cần thu hồi", held, key=f"rv_v_{target.key}",
            format_func=lambda c: priv.info(c).display,
        )
    with cols[2]:
        st.write("")
        label = ("Thu hồi " + ", ".join(chosen)) if chosen else "Chọn quyền để thu hồi"
        if st.button(label[:60], disabled=not chosen, width="stretch",
                     icon=":material/remove_moderator:", key=f"rv_go_{target.key}"):
            state.set_selection(catalog=target.catalog, schema=target.schema,
                                kind=target.kind, name=target.name)
            st.session_state["ucg_prefill"] = {
                "principal": principal, "privileges": chosen, "action": "revoke",
            }
            nav.goto("change_access")


def _privilege_glossary(ctx: Context, target: Target, assets: AssetService):
    with st.expander("Các quyền này nghĩa là gì?", expanded=False):
        try:
            table_type = assets.detail(target).sub_type if target.kind == "table" else ""
            codes = priv.for_securable(target.securable_type, kind=target.kind,
                                       table_type=table_type)
        except Exception:
            codes = priv.for_securable(target.securable_type, kind=target.kind)
        if not codes:
            st.caption("Loại đối tượng này không nhận quyền trực tiếp.")
            return
        st.dataframe(priv.explain(codes), hide_index=True, width="stretch")
        note = priv.traversal_note(target.kind)
        if note:
            st.info(note, icon=":material/route:")
        st.caption(priv.inheritance_note(target.kind))
