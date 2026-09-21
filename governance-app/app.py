"""Unity Catalog Governance - Databricks App entry point.

One entry point, one source tree. The UI lives in ``ui/`` and the
Databricks-facing logic in ``ucg/``; nothing in ``ucg`` imports Streamlit, so
the backend is testable and reusable on its own.
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Quản trị Unity Catalog",
    page_icon=":material/admin_panel_settings:",
    layout="wide",
    initial_sidebar_state="expanded",
)

from ui import chrome, nav  # noqa: E402
from ui.pages import (  # noqa: E402
    activity, asset, audit, change_access, diagnostics, federation, home,
    lineage, permissions, policies, quality, requests, sharing, storage, tags,
)
from ui.session import build_context  # noqa: E402


def main():
    ctx = build_context()
    if ctx is None:
        return

    pages = {
        "Bắt đầu": [
            nav.register("home", st.Page(
                lambda: home.render(ctx), title="Tìm tài sản dữ liệu",
                icon=":material/search:", default=True, url_path="tim-tai-san",
            )),
        ],
        "Tài sản dữ liệu": [
            nav.register("asset", st.Page(
                lambda: asset.render(ctx), title="Thông tin tài sản",
                icon=":material/table_chart:", url_path="tai-san",
            )),
            nav.register("tags", st.Page(
                lambda: tags.render(ctx), title="Thẻ và phân loại",
                icon=":material/label:", url_path="the",
            )),
            nav.register("quality", st.Page(
                lambda: quality.render(ctx), title="Chất lượng dữ liệu",
                icon=":material/rule:", url_path="chat-luong",
            )),
        ],
        "Quyền truy cập": [
            nav.register("permissions", st.Page(
                lambda: permissions.render(ctx), title="Xem quyền",
                icon=":material/key:", url_path="quyen",
            )),
            nav.register("change_access", st.Page(
                lambda: change_access.render(ctx), title="Cấp / thu hồi quyền",
                icon=":material/edit:", url_path="thay-doi-quyen",
            )),
            nav.register("policies", st.Page(
                lambda: policies.render(ctx), title="Chính sách bảo vệ dữ liệu",
                icon=":material/shield:", url_path="chinh-sach",
            )),
            nav.register("requests", st.Page(
                lambda: requests.render(ctx), title="Yêu cầu truy cập",
                icon=":material/how_to_reg:", url_path="yeu-cau",
            )),
        ],
        "Nền tảng": [
            nav.register("storage", st.Page(
                lambda: storage.render(ctx), title="Lưu trữ và cách ly",
                icon=":material/cloud:", url_path="luu-tru",
            )),
            nav.register("federation", st.Page(
                lambda: federation.render(ctx), title="Kết nối federation",
                icon=":material/link:", url_path="ket-noi",
            )),
            nav.register("sharing", st.Page(
                lambda: sharing.render(ctx), title="Chia sẻ dữ liệu",
                icon=":material/share:", url_path="chia-se",
            )),
        ],
        "Theo dõi": [
            nav.register("lineage", st.Page(
                lambda: lineage.render(ctx), title="Lineage và ảnh hưởng",
                icon=":material/account_tree:", url_path="lineage",
            )),
            nav.register("audit", st.Page(
                lambda: audit.render(ctx), title="Nhật ký kiểm toán",
                icon=":material/policy:", url_path="kiem-toan",
            )),
            nav.register("activity", st.Page(
                lambda: activity.render(ctx), title="Thao tác gần đây",
                icon=":material/history:", url_path="thao-tac",
            )),
        ],
        "Hệ thống": [
            nav.register("diagnostics", st.Page(
                lambda: diagnostics.render(ctx), title="Khả năng và cấu hình",
                icon=":material/settings:", url_path="he-thong",
            )),
        ],
    }

    chrome.render_sidebar(ctx)
    st.navigation(pages).run()


main()
