"""Databricks' own audit record, read from ``system.access.audit``.

This screen only ever reads. It is deliberately not the same thing as
"Thao tác gần đây", which shows what *this app* did in the current session, and
the two are kept visibly apart everywhere they could be confused.

Three answers are never blurred together: an empty window, a missing privilege
and a failed read each render differently, because on an audit screen "we saw
nothing" and "we were not allowed to look" lead to opposite conclusions.
"""
from __future__ import annotations

import streamlit as st

from ucg.authz import Action
from ucg.capabilities import State
from ucg.naming import Target
from ucg.paging import Completeness
from ucg.services.assets import AssetService
from ucg.services.audit_query import (
    ACTOR_FIELD_LABELS,
    AUDIT_TABLE,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_DAYS,
    FINDING_DISCLAIMER,
    IDENTITY_NOTE,
    MAX_WINDOW_DAYS,
    OBJECT_MATCH_NOTE,
    PREVIEW_NOTE,
    REDACTION_NOTE,
    RETENTION_NOTE,
    SCHEMA_DRIFT_NOTE,
    TIMEZONE_NOTE,
    UC_SERVICE_NAME,
    WORKSPACE_NOTE,
    AuditService,
    ReviewInput,
    Severity,
    describe_requirements,
    distinction_note,
)
from ucg.services.base import Context
from ucg.services.grants import GrantService
from ucg.services.sharing import SharingService

from .. import nav, picker, state, widgets

_ROW_LIMITS = [100, DEFAULT_ROW_LIMIT, 500, 1000]

#: Worst-case wins when several reads feed one review.
_COMPLETENESS_RANK = {
    Completeness.COMPLETE: 0,
    Completeness.TRUNCATED: 1,
    Completeness.PARTIAL_PERMISSION: 2,
}

_SEVERITY_ICONS = {
    Severity.HIGH: ":material/priority_high:",
    Severity.MEDIUM: ":material/flag:",
    Severity.LOW: ":material/info:",
}

_REVIEW_KINDS = [
    ("table", "Bảng / View"),
    ("volume", "Volume"),
    ("function", "Hàm"),
    ("model", "Mô hình"),
]

#: Reading grants and share contents costs one API call per object, so the
#: review reads a bounded slice and says how much it left out.
_GRANT_CAP_CHOICES = [10, 25, 50, 100]
_SHARE_CAP = 20


def render(ctx: Context) -> None:
    widgets.page_title(
        "Nhật ký kiểm toán",
        f"Bản ghi kiểm toán do Databricks ghi, đọc trực tiếp từ `{AUDIT_TABLE}`.",
    )

    decision = ctx.authz.check(Action.READ_AUDIT, None)
    if not decision.allowed:
        st.warning(f"**Không được phép xem** — {decision.reason}", icon=":material/lock:")
        st.caption(
            "Nhật ký kiểm toán chứa danh tính và hành vi của người dùng, nên được giới hạn "
            "theo vai trò trong ứng dụng. Liên hệ quản trị viên ứng dụng nếu bạn cần quyền này."
        )
        return

    service = AuditService(ctx)
    availability = _availability(service)
    if availability is None:
        return
    if not _availability_panel(availability):
        return

    st.info(distinction_note(), icon=":material/fact_check:")
    _standing_notes()

    assets = AssetService(ctx)
    target = state.current_target()
    with st.expander("Đối tượng đang chọn", expanded=target is None):
        st.caption(
            "Lựa chọn này dùng chung cho tab “Theo đối tượng” và phạm vi của tab “Rà soát quản trị”."
        )
        picked = picker.level_picker(ctx, assets)
        if picked is not None:
            target = picked

    st.divider()
    tab_recent, tab_object, tab_review = st.tabs(
        ["Sự kiện gần đây", "Theo đối tượng", "Rà soát quản trị"]
    )
    with tab_recent:
        _tab_recent(service)
    with tab_object:
        _tab_object(ctx, service, target)
    with tab_review:
        _tab_review(ctx, service, target)


def _worst(values) -> str:
    """The least complete answer wins: a partial read never reports as whole."""
    known = [v for v in values if v]
    if not known:
        return Completeness.COMPLETE
    return max(known, key=lambda v: _COMPLETENESS_RANK.get(v, 2))


def _slug(text: str) -> str:
    """A stable widget-key fragment; Streamlit keys must not collide."""
    return "".join(c if c.isalnum() else "_" for c in text)[:80]


# -- availability ---------------------------------------------------------
def _availability(service: AuditService):
    cached = state.cache_get("audit:availability")
    if cached is not None:
        return cached
    try:
        with st.spinner("Đang kiểm tra khả năng đọc nhật ký kiểm toán…"):
            result = service.available()
    except Exception as exc:
        widgets.show_error(exc, context="Không kiểm tra được nhật ký kiểm toán.")
        return None
    return state.cache_put("audit:availability", result)


def _availability_panel(availability) -> bool:
    """Show the state, and when it is unusable say exactly what is missing."""
    icons = {
        State.READ_ONLY: ":material/visibility:",
        State.AVAILABLE: ":material/check_circle:",
        State.NOT_CONFIGURED: ":material/settings:",
        State.NO_PERMISSION: ":material/lock:",
        State.UNSUPPORTED: ":material/block:",
        State.UNKNOWN: ":material/help:",
    }
    icon = icons.get(availability.state, ":material/help:")
    line = f"**{availability.state_label}** — {availability.message}"

    if availability.usable:
        st.success(line, icon=icon)
        if availability.checked_at:
            st.caption(f"Kiểm tra lúc {availability.checked_at}.")
        return True

    st.warning(line, icon=icon)
    st.markdown("**Điều kiện cần có để đọc được nhật ký kiểm toán**")
    widgets.data_table(
        describe_requirements(), key="audit_requirements",
    )
    if availability.state == State.NOT_CONFIGURED:
        st.caption(
            "Biến môi trường chỉ có hiệu lực sau khi deploy lại ứng dụng."
        )
    elif availability.state == State.UNSUPPORTED:
        st.caption(
            "Việc bật schema hệ thống là thao tác của quản trị viên account/metastore. "
            "Ứng dụng này không tự bật và cố ý không yêu cầu quyền đó."
        )
    if availability.detail:
        st.caption(f"Loại: `{availability.detail}`")
    if availability.checked_at:
        st.caption(f"Kiểm tra lúc {availability.checked_at}.")
    st.caption(PREVIEW_NOTE)

    st.divider()
    nav.link_button(
        "Xem khả năng và cấu hình", "diagnostics",
        icon=":material/tune:", widget_key="audit_to_diag",
    )
    return False


def _standing_notes():
    widgets.explain(
        "Cần biết trước khi đọc bảng kiểm toán",
        "\n\n".join(
            f"- {text}" for text in (
                WORKSPACE_NOTE, IDENTITY_NOTE, RETENTION_NOTE,
                TIMEZONE_NOTE, REDACTION_NOTE, SCHEMA_DRIFT_NOTE, PREVIEW_NOTE,
            )
        ),
        icon=":material/menu_book:",
    )


# -- tab 1: recent events -------------------------------------------------
def _tab_recent(service: AuditService):
    st.markdown("#### Bộ lọc")

    row1 = st.columns([1.3, 1.7, 1.7])
    with row1[0]:
        days = st.slider(
            "Số ngày nhìn lại", 1, MAX_WINDOW_DAYS, DEFAULT_WINDOW_DAYS,
            key="au_rc_days",
            help="Cửa sổ tính theo `event_date` (ngày UTC) — cột phân vùng của bảng.",
        )
    with row1[1]:
        actor = st.text_input(
            "Danh tính", key="au_rc_actor",
            placeholder="email@congty.com hoặc application ID",
        )
    with row1[2]:
        fields = list(ACTOR_FIELD_LABELS)
        actor_field = st.selectbox(
            "Lọc danh tính theo cột", fields,
            index=fields.index("both"),
            format_func=lambda f: ACTOR_FIELD_LABELS[f],
            key="au_rc_actor_field",
            help="Một người có thể xuất hiện ở cột này mà không ở cột kia.",
        )

    row2 = st.columns([1.5, 2, 1.5, 1])
    with row2[0]:
        action = st.text_input(
            "Hành động (`action_name`)", key="au_rc_action",
            placeholder="ví dụ: getTable",
            help="Khớp chính xác, phân biệt đúng tên hành động Databricks ghi nhận.",
        )
    with row2[1]:
        object_name = st.text_input(
            "Tên đối tượng đầy đủ", key="au_rc_object",
            placeholder="catalog.schema.bang",
        )
    with row2[2]:
        scopes = ["Tất cả dịch vụ", f"Chỉ {UC_SERVICE_NAME}"]
        scope = st.selectbox("Dịch vụ", scopes, key="au_rc_scope")
    with row2[3]:
        limit = st.selectbox(
            "Số dòng tối đa", _ROW_LIMITS,
            index=_ROW_LIMITS.index(DEFAULT_ROW_LIMIT), key="au_rc_limit",
        )

    uc_only = scope != scopes[0]
    if uc_only:
        st.info(WORKSPACE_NOTE, icon=":material/account_tree:")
    if object_name.strip():
        st.info(OBJECT_MATCH_NOTE, icon=":material/rule:")

    signature = "|".join([
        str(days), actor.strip().lower(), actor_field, action.strip(),
        object_name.strip().lower(), str(uc_only), str(limit),
    ])
    request_key = "au_rc_request"

    if st.button("Tải sự kiện", type="primary", icon=":material/download:",
                 key="au_rc_load"):
        st.session_state[request_key] = signature

    if st.session_state.get(request_key) != signature:
        st.caption(
            "Bộ lọc đã thay đổi. Nhấn **Tải sự kiện** để chạy truy vấn — "
            "màn hình này không tự truy vấn lại sau mỗi lần chỉnh bộ lọc."
        )
        return

    cache_key = f"audit:recent:{signature}"
    page = state.cache_get(cache_key)
    if page is None:
        try:
            with st.spinner("Đang đọc nhật ký kiểm toán từ Databricks…"):
                if uc_only and not (actor.strip() or action.strip() or object_name.strip()):
                    # The dedicated call refuses to filter by workspace_id and
                    # attaches the reason itself.
                    page = service.unity_catalog_actions(days, limit=limit)
                else:
                    page = service.recent(
                        days,
                        actor=actor,
                        action=action,
                        object_name=object_name,
                        actor_field=actor_field,
                        service=UC_SERVICE_NAME if uc_only else None,
                        limit=limit,
                    )
        except Exception as exc:
            widgets.show_error(exc, context="Không chạy được truy vấn kiểm toán.")
            return
        state.cache_put(cache_key, page)

    _render_page(page, key=f"rc_{abs(hash(signature))}", download_name="kiem-toan-gan-day")


# -- tab 2: one object ----------------------------------------------------
def _tab_object(ctx: Context, service: AuditService, target: Target | None):
    if target is None:
        st.info("Chưa chọn đối tượng nào.", icon=":material/search:")
        st.caption(
            "Mở “Đối tượng đang chọn” ở trên để chọn catalog, schema hoặc một đối tượng cụ thể."
        )
        nav.link_button("Tìm tài sản dữ liệu", "home", icon=":material/search:",
                        widget_key="au_ob_home")
        return

    decision = ctx.authz.check(Action.READ_AUDIT, target)
    if not decision.allowed:
        st.warning(f"**Không được phép xem** — {decision.reason}", icon=":material/lock:")
        return

    widgets.breadcrumb([
        ("Catalog", target.catalog),
        ("Schema", target.schema),
        (target.label, target.name),
    ])
    st.markdown(f"**Đối tượng**  \n`{target.full_name}`")
    st.info(OBJECT_MATCH_NOTE, icon=":material/rule:")

    cols = st.columns([1.5, 1, 1.5])
    with cols[0]:
        days = st.slider("Số ngày nhìn lại", 1, MAX_WINDOW_DAYS, DEFAULT_WINDOW_DAYS,
                         key=f"au_ob_days_{target.key}")
    with cols[1]:
        limit = st.selectbox("Số dòng tối đa", _ROW_LIMITS,
                             index=_ROW_LIMITS.index(DEFAULT_ROW_LIMIT),
                             key=f"au_ob_limit_{target.key}")
    with cols[2]:
        st.write("")
        load = st.button("Tải sự kiện của đối tượng", type="primary",
                         icon=":material/download:", width="stretch",
                         key=f"au_ob_load_{target.key}")

    signature = f"{target.key}|{days}|{limit}"
    request_key = "au_ob_request"
    if load:
        st.session_state[request_key] = signature
    if st.session_state.get(request_key) != signature:
        st.caption("Nhấn **Tải sự kiện của đối tượng** để chạy truy vấn.")
        return

    cache_key = f"audit:object:{signature}"
    page = state.cache_get(cache_key)
    if page is None:
        try:
            with st.spinner(f"Đang đọc sự kiện của `{target.full_name}`…"):
                page = service.for_object(target, days, limit=limit)
        except Exception as exc:
            widgets.show_error(exc, context="Không đọc được sự kiện của đối tượng này.")
            return
        state.cache_put(cache_key, page)

    _render_page(page, key=f"ob_{abs(hash(signature))}",
                 download_name=f"kiem-toan-{target.full_name}")

    if page.ok and not page.empty:
        st.divider()
        _correlation(ctx, service, page, target)


def _correlation(ctx: Context, service: AuditService, page, target: Target):
    with st.expander("Đối chiếu với nhật ký thao tác của ứng dụng", expanded=False):
        st.warning(service.correlation_note(), icon=":material/link_off:")

        events = [e for e in ctx.log.events if (e.target or "") == target.full_name]
        if not events:
            st.info("Phiên làm việc này chưa có thao tác nào trên đối tượng đang chọn.",
                    icon=":material/history:")
            return

        ids = [e.event_id for e in events]
        labels = {
            e.event_id: f"{(e.time_utc or '')[:19].replace('T', ' ')} · {e.summary or e.action}"
            for e in events
        }
        chosen = st.selectbox(
            "Thao tác của ứng dụng", ids,
            format_func=lambda i: labels.get(i, i),
            key=f"au_corr_{target.key}",
        )
        event = next((e for e in events if e.event_id == chosen), None)
        if event is None:
            return

        candidates = service.correlate(event, page)
        if not candidates:
            st.info(
                "Không có dòng kiểm toán nào khớp gần đúng trong cửa sổ đang xem.",
                icon=":material/inbox:",
            )
            st.caption(
                "Không khớp KHÔNG có nghĩa là thao tác không xảy ra: cửa sổ thời gian, "
                "độ trễ ghi nhận hoặc danh tính khác nhau đều có thể làm mất dấu."
            )
            return

        st.caption(f"{len(candidates)} dòng ứng viên, sắp theo dữ liệu kiểm toán đang xem.")
        widgets.data_table(candidates, key=f"corr_{target.key}",
                           download_name=f"doi-chieu-{target.full_name}")


# -- tab 3: governance review ---------------------------------------------
def _tab_review(ctx: Context, service: AuditService, target: Target | None):
    st.warning(FINDING_DISCLAIMER, icon=":material/gavel:")

    if target is None:
        st.info("Chưa chọn phạm vi rà soát.", icon=":material/search:")
        st.caption(
            "Chọn một catalog, schema hoặc đối tượng ở “Đối tượng đang chọn” phía trên. "
            "Rà soát chỉ chạy trên dữ liệu ứng dụng thật sự đọc được trong phạm vi đó."
        )
        return

    st.caption(f"Phạm vi rà soát: **{target.label} `{target.full_name}`**")
    st.caption(
        "Các kiểm tra dưới đây KHÔNG truy vấn bảng kiểm toán. Chúng chạy trên dữ liệu "
        "mà các màn hình khác đọc được qua API Unity Catalog và Delta Sharing."
    )

    kinds = ["table"]
    if target.kind == "schema":
        codes = [c for c, _ in _REVIEW_KINDS]
        labels = dict(_REVIEW_KINDS)
        kinds = st.multiselect(
            "Loại đối tượng đưa vào rà soát", codes, default=["table"],
            format_func=lambda c: labels[c], key="au_rv_kinds",
        )

    cols = st.columns([1.6, 1.4, 1.4])
    with cols[0]:
        grant_mode = st.radio(
            "Dữ liệu quyền", ["Quyền hiệu lực (gồm kế thừa)", "Chỉ quyền cấp trực tiếp"],
            key="au_rv_grantmode",
            help="Quyền kế thừa từ catalog/schema chỉ được rà soát nếu chọn quyền hiệu lực.",
        )
    with cols[1]:
        grant_cap = st.selectbox(
            "Số đối tượng đọc quyền", _GRANT_CAP_CHOICES, index=1, key="au_rv_cap",
            help="Mỗi đối tượng là một lệnh gọi API riêng, nên số lượng được giới hạn.",
        )
    with cols[2]:
        include_sharing = st.checkbox(
            "Đọc dữ liệu chia sẻ ra ngoài", value=True, key="au_rv_sharing",
            help="Đọc danh sách share và tài sản trong từng share.",
        )

    effective = grant_mode.startswith("Quyền hiệu lực")
    signature = "|".join([
        target.key, ",".join(sorted(kinds)), str(effective), str(grant_cap),
        str(include_sharing),
    ])
    request_key = "au_rv_request"

    if st.button("Chạy rà soát", type="primary", icon=":material/fact_check:",
                 key="au_rv_run"):
        st.session_state[request_key] = signature
    if st.session_state.get(request_key) != signature:
        st.caption("Nhấn **Chạy rà soát** để đọc dữ liệu và chạy các quy tắc.")
        return

    cache_key = f"audit:review:{signature}"
    bundle = state.cache_get(cache_key)
    if bundle is None:
        bundle = _run_review(ctx, service, target, kinds, effective, grant_cap,
                             include_sharing)
        if bundle is None:
            return
        state.cache_put(cache_key, bundle)

    _render_review(bundle)


def _run_review(ctx: Context, service: AuditService, target: Target, kinds,
                effective: bool, grant_cap: int, include_sharing: bool):
    """Collect what the app can genuinely read, then run the rules over it."""
    assets_svc = AssetService(ctx)
    grants_svc = GrantService(ctx)

    try:
        with st.spinner("Đang đọc tài sản trong phạm vi…"):
            asset_items, asset_targets, asset_source, asset_completeness, asset_notes = (
                _collect_assets(ctx, assets_svc, target, kinds)
            )
        with st.spinner("Đang đọc quyền của từng đối tượng…"):
            grant_rows, grant_source, grant_completeness, grant_notes = _collect_grants(
                ctx, grants_svc, asset_targets, effective, grant_cap
            )
        share_rows: list[dict] = []
        share_notes: list[str] = []
        if include_sharing:
            with st.spinner("Đang đọc dữ liệu chia sẻ…"):
                share_rows, share_source, share_completeness, share_notes = _collect_shares(
                    ctx, SharingService(ctx), target
                )
        else:
            share_source = (
                "Không thu thập — người dùng không chọn đọc dữ liệu chia sẻ trong lần rà soát này."
            )
            share_completeness = Completeness.PARTIAL_PERMISSION
            share_notes = [
                "Kiểm tra chia sẻ ra ngoài chưa chạy trên bản ghi nào vì dữ liệu chia sẻ "
                "không được đọc."
            ]
    except Exception as exc:
        widgets.show_error(exc, context="Không thu thập được dữ liệu để rà soát.")
        return None

    completeness = _worst([asset_completeness, grant_completeness, share_completeness])
    scope_note = f"{target.label} {target.full_name}"

    review_input = ReviewInput(
        assets=asset_items,
        grants=grant_rows,
        external_shares=share_rows,
        asset_source=asset_source,
        grant_source=grant_source,
        sharing_source=share_source,
        completeness=completeness,
        scope_note=scope_note,
    )
    try:
        report = service.review(review_input)
    except Exception as exc:
        widgets.show_error(exc, context="Không chạy được các quy tắc rà soát.")
        return None

    return {
        "report": report,
        "notes": [*asset_notes, *grant_notes, *share_notes],
        "sources": {
            "Tài sản": asset_source,
            "Quyền": grant_source,
            "Chia sẻ": share_source,
        },
        "counts": {
            "Tài sản đã đọc": len(asset_items),
            "Dòng quyền đã đọc": len(grant_rows),
            "Tài sản trong share": len(share_rows),
        },
    }


def _collect_assets(ctx: Context, assets_svc: AssetService, target: Target, kinds):
    """Assets in scope, plus an honest account of what could not be read."""
    notes: list[str] = []
    items: list = []
    targets: list[Target] = []
    states: list[str] = []

    if not ctx.authz.allows(Action.READ_METADATA, target):
        reason = ctx.authz.check(Action.READ_METADATA, target).reason
        return [], [], f"Không đọc được metadata: {reason}", Completeness.PARTIAL_PERMISSION, [reason]

    if target.kind == "catalog":
        listing = assets_svc.schemas(target.catalog)
        source = "Unity Catalog API (schemas.list) — các schema trong catalog"
        if not listing.ok:
            notes.append(
                f"Không đọc được danh sách schema của `{target.catalog}`: {listing.error.message}"
            )
            states.append(Completeness.PARTIAL_PERMISSION)
        else:
            items.extend(listing.items)
            targets.extend(ref.target for ref in listing.items)
            states.append(listing.completeness)
            notes.append(
                "Chỉ rà soát các schema trong catalog này. Bảng, volume, hàm và mô hình "
                "bên trong từng schema KHÔNG nằm trong lần rà soát này — chọn từng schema "
                "để rà soát sâu hơn."
            )
    elif target.kind == "schema":
        source = "Unity Catalog API (tables / volumes / functions / registered_models .list)"
        if not kinds:
            notes.append("Chưa chọn loại đối tượng nào, nên không có tài sản nào được rà soát.")
            states.append(Completeness.PARTIAL_PERMISSION)
        for kind in kinds:
            listing = assets_svc.objects(target.catalog, target.schema, kind)
            if not listing.ok:
                notes.append(
                    f"Không đọc được danh sách {picker.KIND_LABELS.get(kind, kind)}: "
                    f"{listing.error.message}"
                )
                states.append(Completeness.PARTIAL_PERMISSION)
                continue
            items.extend(listing.items)
            targets.extend(ref.target for ref in listing.items)
            states.append(listing.completeness)
    else:
        source = "Unity Catalog API (đọc metadata của đúng một đối tượng)"
        detail = assets_svc.detail(target)
        items.append({
            "full_name": target.full_name,
            "owner": detail.owner,
            "comment": detail.comment,
            "type_label": detail.type_label,
        })
        targets.append(target)
        states.append(Completeness.COMPLETE)
        notes.append("Phạm vi rà soát chỉ gồm đúng đối tượng đang chọn.")

    return items, targets, source, _worst(states), notes


def _collect_grants(ctx: Context, grants_svc: GrantService, targets, effective: bool,
                    cap: int):
    """Grant rows for a bounded slice of the assets in scope."""
    label = "grants.get_effective" if effective else "grants.get"
    source = f"Unity Catalog API ({label})"
    notes: list[str] = []

    if not targets:
        return [], source + " — không có đối tượng nào để đọc quyền", \
            Completeness.PARTIAL_PERMISSION, \
            ["Không đọc quyền của đối tượng nào vì danh sách tài sản trống."]

    if not ctx.authz.allows(Action.READ_GRANTS, targets[0]):
        reason = ctx.authz.check(Action.READ_GRANTS, targets[0]).reason
        return [], f"Không đọc được quyền: {reason}", Completeness.PARTIAL_PERMISSION, [reason]

    selected = targets[:cap]
    rows: list[dict] = []
    failed: list[str] = []
    for item in selected:
        view = grants_svc.effective(item) if effective else grants_svc.direct(item)
        if not view.ok:
            failed.append(item.full_name)
            continue
        for grant in view.rows:
            rows.append({
                "object": item.full_name,
                "principal": grant.principal,
                "privilege": grant.privilege,
                "source": grant.source_label,
            })

    states = [Completeness.COMPLETE]
    if len(targets) > len(selected):
        states.append(Completeness.TRUNCATED)
        notes.append(
            f"Chỉ đọc quyền của {len(selected)}/{len(targets)} đối tượng trong phạm vi. "
            "Phần còn lại chưa được rà soát — kết quả trống ở đó không có nghĩa là không có vấn đề."
        )
    if failed:
        states.append(Completeness.PARTIAL_PERMISSION)
        shown = ", ".join(f"`{name}`" for name in failed[:5])
        notes.append(
            f"Không đọc được quyền của {len(failed)} đối tượng ({shown}"
            + (", …" if len(failed) > 5 else "")
            + "). Danh tính thực thi cần là chủ sở hữu, có MANAGE, hoặc là metastore admin "
            "để đọc đầy đủ ACL."
        )
    if not effective:
        notes.append(
            "Đang rà soát trên quyền cấp trực tiếp, nên quyền kế thừa từ catalog/schema "
            "không nằm trong kết quả."
        )
    return rows, source, _worst(states), notes


def _collect_shares(ctx: Context, sharing_svc: SharingService, target: Target):
    """Objects in scope that appear inside a Delta Sharing share."""
    source = "Delta Sharing API (shares.list + shares.get(include_shared_data=True))"
    notes: list[str] = []

    if not ctx.authz.allows(Action.READ_SHARING, None):
        reason = ctx.authz.check(Action.READ_SHARING, None).reason
        return [], f"Không đọc được dữ liệu chia sẻ: {reason}", \
            Completeness.PARTIAL_PERMISSION, [reason]

    listing = sharing_svc.shares()
    if not listing.ok:
        message = listing.error.message
        return [], f"Không đọc được danh sách share: {message}", \
            Completeness.PARTIAL_PERMISSION, \
            [f"Không đọc được danh sách share, nên kiểm tra chia sẻ ra ngoài chưa chạy: {message}"]

    states = [listing.completeness]
    shares = listing.items[:_SHARE_CAP]
    if len(listing.items) > len(shares):
        states.append(Completeness.TRUNCATED)
        notes.append(
            f"Chỉ mở {len(shares)}/{len(listing.items)} share để xem danh sách tài sản bên trong."
        )

    prefix = f"{target.full_name}." if target.kind in ("catalog", "schema") else ""
    rows: list[dict] = []
    unreadable = 0
    for ref in shares:
        detail = sharing_svc.share_detail(ref.name)
        if not detail.ok:
            unreadable += 1
            continue
        recipients = ", ".join(
            r.get("Recipient", "") for r in detail.recipients if r.get("Recipient")
        )
        for obj in detail.objects:
            name = obj.name or ""
            if not (name == target.full_name or (prefix and name.startswith(prefix))):
                continue
            rows.append({
                "object": name,
                "share": ref.name,
                "recipient": recipients or "Chưa xác định",
                "detail": (
                    f"{obj.object_type or 'Không rõ loại'} · "
                    + ("Đang hoạt động" if obj.active
                       else "Chủ share không còn quyền trên tài sản này")
                ),
            })

    if unreadable:
        states.append(Completeness.PARTIAL_PERMISSION)
        notes.append(
            f"Không đọc được nội dung của {unreadable} share, nên tài sản bên trong "
            "không được rà soát."
        )
    if not rows:
        notes.append(
            "Không có tài sản nào trong phạm vi xuất hiện ở các share đã đọc được."
        )
    return rows, source, _worst(states), notes


def _render_review(bundle: dict):
    report = bundle["report"]

    cols = st.columns([1, 1, 1, 2])
    with cols[0]:
        st.metric("Tổng điểm cần xem xét", report.total)
    counts = bundle["counts"]
    with cols[1]:
        st.metric("Tài sản đã đọc", counts["Tài sản đã đọc"])
    with cols[2]:
        st.metric("Dòng quyền đã đọc", counts["Dòng quyền đã đọc"])
    with cols[3]:
        st.markdown(
            f"**Mức đầy đủ của dữ liệu nguồn**  \n"
            f"{Completeness.LABELS.get(report.completeness, report.completeness)}"
        )
        st.caption(f"Quan sát lúc {report.observed_at} · Phạm vi: {report.scope_note}")

    st.info(report.disclaimer, icon=":material/gavel:")

    notes = [n for n in bundle["notes"] if n]
    if notes:
        with st.container(border=True):
            st.markdown("**Những gì lần rà soát này KHÔNG nhìn thấy**")
            for note in notes:
                st.markdown(f":material/visibility_off: {note}")

    st.markdown("**Tổng hợp theo quy tắc**")
    widgets.data_table(report.summary_rows(), key="audit_review_summary",
                       download_name="ra-soat-quan-tri")

    for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        group = report.by_severity(severity)
        if not group:
            continue
        icon = _SEVERITY_ICONS.get(severity, ":material/info:")
        st.markdown(f"**{icon} {Severity.LABELS.get(severity, severity)}**")
        for finding in group:
            header = (
                f"{icon} {finding.rule_id} · {finding.title} — "
                f"{finding.count} điểm cần xem xét"
            )
            with st.expander(header, expanded=bool(finding.rows and severity == Severity.HIGH)):
                st.caption(finding.conclusion)
                if finding.rows:
                    widgets.data_table(
                        finding.rows, key=f"finding_{finding.rule_id}",
                        download_name=f"can-xem-xet-{finding.rule_id}",
                    )
                st.caption(f"Nguồn dữ liệu: {finding.source}")
                st.caption(finding.caveat)

    widgets.technical_details(
        {"Nguồn dữ liệu đã dùng": bundle["sources"], "Số bản ghi đã đọc": counts},
        label="Nguồn dữ liệu của lần rà soát này",
        download="nguon-du-lieu-ra-soat",
    )


# -- shared rendering -----------------------------------------------------
def _render_page(page, *, key: str, download_name: str,
                 what: str = "sự kiện kiểm toán"):
    """One audit window, with empty / forbidden / failed kept apart."""
    if not page.ok:
        widgets.show_error(page.error, context="Không đọc được nhật ký kiểm toán.")
        st.caption(
            "Thu hẹp cửa sổ thời gian hoặc thêm bộ lọc rồi thử lại. "
            "Bộ lọc trên màn hình vẫn được giữ nguyên."
        )
        return

    window = f"{page.window_start} → {page.window_end}"
    if page.empty:
        kind = "forbidden" if page.completeness == Completeness.PARTIAL_PERMISSION else "empty"
        widgets.empty_state(kind, what=what, detail=page.no_data_reason)
        st.caption(f"Cửa sổ đã truy vấn theo `event_date` (UTC): {window}.")
        return

    cols = st.columns([1, 3])
    with cols[0]:
        st.metric("Số dòng đọc được", len(page.rows))
    with cols[1]:
        st.markdown(f"**Cửa sổ `event_date` (UTC)**  \n{window}")
        if page.filters:
            st.caption(" · ".join(f"{k}: {v}" for k, v in page.filters.items()))

    if page.truncated:
        st.warning(
            "Kết quả chưa đầy đủ: đã chạm giới hạn số dòng của truy vấn này.",
            icon=":material/content_cut:",
        )

    widgets.data_table(page.table(), key=f"audit_{key}", download_name=download_name)
    st.info(IDENTITY_NOTE, icon=":material/badge:")
    widgets.completeness_note(page)

    widgets.explain(
        "Vì sao không thấy định nghĩa SQL của view hay hàm?",
        REDACTION_NOTE,
        icon=":material/visibility_off:",
    )
    if page.caveats:
        with st.expander("Giới hạn của dữ liệu trong bảng này", expanded=False):
            for text in page.caveats:
                st.markdown(f":material/info: {text}")

    widgets.technical_details(
        {
            "Bảng nguồn": AUDIT_TABLE,
            "Cửa sổ event_date": {"từ": page.window_start, "đến": page.window_end},
            "Bộ lọc đã áp dụng": page.filters or "Không có",
            "Đã cắt bớt": bool(page.truncated),
            "request_params bị lược bỏ": AuditService.omitted_request_params(),
        },
        label="Chi tiết kỹ thuật của truy vấn",
        download=f"kiem-toan-ky-thuat-{key}",
    )
