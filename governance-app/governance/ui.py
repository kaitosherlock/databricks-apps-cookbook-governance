"""Streamlit UI shared by the Cookbook page and standalone deployment."""
import csv
import io
import json
import logging
import sys
from uuid import uuid4

import streamlit as st
from databricks.sdk.errors import DatabricksError
from governance.service import (
    GovernanceError, GovernanceService, Settings, Target, PRIVILEGES,
    actor_from_headers, grant_rows, make_client,
)

logger = logging.getLogger("governance.audit")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def show_error(exc):
    if isinstance(exc, GovernanceError):
        st.error(str(exc))
    elif isinstance(exc, DatabricksError):
        code = getattr(exc, "error_code", None) or type(exc).__name__
        st.error(f"Databricks từ chối hoặc không hoàn tất yêu cầu ({code}). Kiểm tra quyền của app và kết nối workspace.")
    else:
        ref = str(uuid4())
        logger.error('Unexpected error ref=%s type=%s', ref, type(exc).__name__)
        st.error(f"Không hoàn tất yêu cầu. Mã tra cứu app logs: {ref}")


def table_export(rows, key):
    if not rows:
        st.info("API không trả về quyền nào trong phạm vi identity hiện tại; đây không phải bằng chứng đối tượng không có quyền khác.")
        return
    st.dataframe(rows, hide_index=True, width="stretch")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    # Keep exported principal/object strings inert when opened in Excel.
    writer.writerows({k: "'" + str(v) if str(v).startswith(('=', '+', '-', '@', '\t', '\r')) else v
                     for k, v in row.items()} for row in rows)
    st.download_button("Tải CSV", output.getvalue().encode("utf-8-sig"), f"{key}.csv", "text/csv", key=key)


def render():
    settings = Settings.from_env()
    actor = actor_from_headers(st.context.headers)
    st.title("Unity Catalog Governance")
    st.caption("Metadata · Quyền truy cập · Catalog / Schema / Table / Function")
    if settings.demo:
        from governance.demo import demo_client
        settings = Settings(frozenset({"demo_governance"}), frozenset(), demo=True)
        service = GovernanceService(demo_client(), settings, "demo@example.com")
        actor = "demo@example.com"
        st.warning("DEMO — dữ liệu minh hoạ, không kết nối Databricks và không thay đổi quyền.")
    else:
        if not settings.local and not actor:
            st.error("Không có identity từ Databricks Apps proxy. Mở ứng dụng bằng URL Databricks Apps; local chỉ hỗ trợ chế độ đọc.")
            return
        try:
            service = GovernanceService(make_client(settings), settings, actor)
        except Exception as exc:
            show_error(exc)
            return
    st.caption(f"Người dùng: {actor or 'local profile'} · API chạy bằng: {'demo' if settings.demo else 'local profile' if settings.local else 'app service principal'}")
    with st.sidebar:
        st.markdown("**Governance scope**")
        # Modified 2026-09-21: empty allowlist = read every visible catalog.
        if settings.catalogs:
            st.write(", ".join(sorted(settings.catalogs)))
        else:
            st.write("Tất cả catalog nhìn thấy được")
            st.caption("GOVERNANCE_CATALOGS trống → chỉ đọc. Khai báo catalog để bật Grant/Revoke.")
        st.caption("Chỉ hiển thị những đối tượng identity thực thi có thể truy cập.")
        if st.button("Làm mới", key="gov_refresh"):
            st.session_state.pop("gov_pending", None)
            st.rerun()
    try:
        catalogs = service.catalogs()
        if not catalogs:
            if settings.catalogs:
                st.info("Không tìm thấy catalog trong phạm vi cấu hình. Kiểm tra tên và quyền của identity thực thi.")
            else:
                st.info(
                    "Service principal của app chưa nhìn thấy catalog nào. "
                    "Cấp USE CATALOG và BROWSE cho application ID của app (tab Authorization) trong Catalog Explorer."
                )
            return
        catalog = st.selectbox("Catalog", [c["name"] for c in catalogs], key="gov_catalog")
        kind_label = st.radio("Loại đối tượng", ["Catalog", "Schema", "Table", "Function"], horizontal=True, key="gov_kind")
        kind, schema, name = kind_label.lower(), "", ""
        if kind != "catalog":
            schemas = service.schemas(catalog)
            if not schemas:
                st.info("Không có schema khả dụng.")
                return
            schema = st.selectbox("Schema", [s["name"] for s in schemas], key=f"gov_schema_{catalog}")
        if kind in ("table", "function"):
            objects = service.objects(catalog, schema, kind)
            if not objects:
                st.info("Không có đối tượng khả dụng.")
                return
            name = st.selectbox(kind_label, [o["name"] for o in objects], key=f"gov_object_{catalog}_{schema}_{kind}")
        target = Target(kind, catalog, schema, name)
        metadata = service.metadata(target)
    except Exception as exc:
        show_error(exc)
        return
    # Pending reviews are tied to both target and actor, not a global cache.
    context = (target, actor)
    if st.session_state.get("gov_context") != context:
        st.session_state.pop("gov_pending", None)
        st.session_state["gov_context"] = context
    if notice := st.session_state.pop("gov_notice", None):
        st.success(notice)
    st.subheader(target.full_name)
    cols = st.columns(3)
    cols[0].metric("Loại", kind_label)
    cols[1].metric("Owner", metadata.get("owner") or "—")
    try:
        service.check_write(target)
        can_write = True
    except GovernanceError:
        can_write = False
    cols[2].metric("Chế độ", "Đọc / Ghi" if can_write else "Chỉ đọc")
    info, direct_tab, effective_tab, changes_tab = st.tabs(["Metadata", "Quyền trực tiếp", "Quyền hiệu lực", "Thay đổi quyền"])
    with info:
        st.write(metadata.get("comment") or "Chưa có mô tả.")
        if columns := metadata.get("columns"):
            st.dataframe([{k: c.get(k) for k in ("name", "type_text", "nullable", "comment")} for c in columns], hide_index=True, width="stretch")
        with st.expander("Metadata JSON"):
            st.json(metadata)
        st.download_button("Tải metadata JSON", json.dumps(metadata, ensure_ascii=False, indent=2),
                           f"{target.full_name}.json", "application/json")
    # Metadata remains useful even when the app is not allowed to inspect grants.
    with direct_tab:
        try:
            table_export(grant_rows(service.grants(target)), "direct-grants")
        except Exception as exc:
            show_error(exc)
    with effective_tab:
        st.caption("Gồm quyền trực tiếp và quyền kế thừa. API grants không thay thế việc kiểm tra quyền truy cập toàn diện (ownership, group membership, ABAC, row filters…).")
        try:
            table_export(grant_rows(service.grants(target, effective=True), True), "effective-grants")
        except Exception as exc:
            show_error(exc)
    with changes_tab:
        if not can_write:
            if not settings.catalogs:
                st.info(
                    "Chỉ đọc vì GOVERNANCE_CATALOGS đang trống. Phạm vi đọc mở cho mọi catalog, "
                    "nhưng ghi thì phải khai báo rõ catalog được phép sửa. Khai báo GOVERNANCE_CATALOGS, "
                    "bật GOVERNANCE_ENABLE_WRITES, cấu hình GOVERNANCE_ADMIN_EMAILS và cấp quyền UC "
                    "tương ứng cho app service principal."
                )
            else:
                st.info("Chỉ đọc. Để quản trị: bật GOVERNANCE_ENABLE_WRITES, cấu hình GOVERNANCE_ADMIN_EMAILS và cấp quyền UC tương ứng cho app service principal. Local/demo luôn chỉ đọc.")
            return
        st.caption("Grant ở Catalog/Schema có thể ảnh hưởng cả đối tượng con hiện tại và tương lai. Grant/Revoke chỉ sửa quyền trực tiếp. Quyền kế thừa phải sửa ở cấp cha; người dùng vẫn có thể có quyền qua nhóm khác. USE_CATALOG và USE_SCHEMA được quản lý riêng.")
        with st.form(f"gov_change_{target.kind}_{target.full_name}"):
            principal = st.text_input("Principal", placeholder="Account group, user email hoặc service principal application ID")
            action = st.selectbox("Thao tác", ["grant", "revoke"])
            privileges = st.multiselect("Quyền", service.available_privileges(target))
            reason = st.text_area("Lý do", max_chars=500)
            prepare = st.form_submit_button("Xem trước thay đổi")
        if prepare:
            st.session_state.pop("gov_pending", None)
            try:
                st.session_state["gov_pending"] = service.prepare(target, principal, action, privileges, reason)
            except Exception as exc:
                show_error(exc)
        pending = st.session_state.get("gov_pending")
        review = st.empty()
        if pending:
            with review.container():
                st.markdown("**Thay đổi đang chờ áp dụng**")
                st.json(pending.preview())
                with st.form("gov_confirm"):
                    confirmation = st.text_input("Nhập lại tên đầy đủ của đối tượng", placeholder=target.full_name, key="gov_confirmation")
                    apply = st.form_submit_button("Áp dụng thay đổi", type="primary")
                if st.button("Huỷ bản xem trước", key="gov_cancel"):
                    st.session_state.pop("gov_pending", None)
                    review.empty()
                    st.rerun()
                if apply:
                    # Consume pending action before network call to prevent accidental re-submit.
                    st.session_state.pop("gov_pending", None)
                    try:
                        event = service.apply(pending, confirmation)
                        st.session_state["gov_notice"] = f"Databricks đã xác nhận thay đổi. Event ID: {event['event_id']}"
                    except Exception as exc:
                        show_error(exc)
                        st.info("Nếu có lỗi kết nối/timeout, làm mới quyền để kiểm tra kết quả trước khi thử lại.")
                    else:
                        review.empty()
                        st.rerun()
