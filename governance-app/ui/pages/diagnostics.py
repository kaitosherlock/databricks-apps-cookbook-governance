"""What this deployment can do, and precisely why not when it cannot.

The capability matrix is generated from the backend registry rather than
maintained by hand, so it cannot drift from what the code actually supports.
A blocked feature always says which of the four reasons applies - missing
configuration, missing Unity Catalog privilege, unsupported by the workspace,
or not built - because those need four different responses from four different
people.
"""
from __future__ import annotations

import streamlit as st

from ucg.authz import Action
from ucg.capabilities import State
from ucg.config import Role
from ucg.identity import ExecutionIdentity
from ucg.services.base import Context
from ucg.services.sql import SqlRunner

from .. import chrome, widgets


def render(ctx: Context):
    st.markdown("### Khả năng và cấu hình")
    st.caption(
        "Trang này cho biết ứng dụng làm được gì trên workspace hiện tại, "
        "và nếu chưa làm được thì vướng ở đâu."
    )

    tabs = st.tabs(["Khả năng", "Danh tính và vai trò", "Cấu hình", "SQL Warehouse"])
    with tabs[0]:
        _capabilities(ctx)
    with tabs[1]:
        _identity(ctx)
    with tabs[2]:
        _config(ctx)
    with tabs[3]:
        _warehouse(ctx)


def _capabilities(ctx: Context):
    report = ctx.capabilities
    counts = report.summary()

    cols = st.columns(4)
    cols[0].metric("Sẵn sàng", counts.get(State.AVAILABLE, 0))
    cols[1].metric("Chỉ đọc", counts.get(State.READ_ONLY, 0))
    cols[2].metric("Chưa cấu hình", counts.get(State.NOT_CONFIGURED, 0))
    cols[3].metric("Chưa kiểm tra", counts.get(State.UNKNOWN, 0))
    st.caption(
        "Các số trên đếm chức năng đã khai báo trong ứng dụng — không phải số liệu "
        "về dữ liệu của bạn."
    )

    st.divider()
    rows = report.rows()
    groups = sorted({r["Nhóm"] for r in rows})
    chosen = st.multiselect("Lọc theo nhóm", groups, default=groups, key="cap_groups")
    filtered = [r for r in rows if r["Nhóm"] in chosen]
    widgets.data_table(filtered, key="capmatrix", download_name="capability-matrix")

    st.divider()
    with st.expander("Ý nghĩa từng trạng thái"):
        st.markdown(
            """
| Trạng thái | Nghĩa là | Ai xử lý |
|---|---|---|
| **Sẵn sàng** | Dùng được đầy đủ. | — |
| **Chỉ đọc** | Xem được, không thay đổi được. | Quản trị viên ứng dụng bật ghi và deploy lại. |
| **Chưa cấu hình** | Thiếu một thiết lập của ứng dụng. | Quản trị viên ứng dụng. |
| **Không đủ quyền** | Danh tính thực thi thiếu quyền Unity Catalog. | Người có thẩm quyền cấp quyền trong Catalog Explorer. |
| **Workspace chưa hỗ trợ** | API không có trên workspace hoặc phiên bản này. | Databricks / quản trị workspace. |
| **Chưa triển khai** | Ứng dụng chưa xây chức năng này. | Nhóm phát triển ứng dụng. |

Thiếu quyền **không** đồng nghĩa với tính năng không tồn tại, và tính năng không
tồn tại **không** được báo là lỗi quyền.
            """
        )


def _identity(ctx: Context):
    chrome.render_identity(ctx)
    st.divider()

    st.markdown("**Vai trò của bạn trong ứng dụng**")
    st.markdown(f"### {ctx.authz.role_label}")
    st.caption(Role.DESCRIPTIONS.get(ctx.authz.role, ""))

    st.markdown("**Những việc bạn được phép làm**")
    allowed = ctx.authz.allowed_actions()
    rows = [
        {
            "Hành động": info["label"],
            "Được phép": "Có" if info["allowed"] else "Không",
            "Lý do bị chặn": info["reason"] or "—",
        }
        for info in allowed.values()
    ]
    widgets.data_table(rows, key="myactions")
    st.caption(
        "Danh sách này chỉ để giải thích giao diện. Mọi thao tác thay đổi đều được "
        "kiểm tra lại ở phía máy chủ trước khi thực hiện — ẩn hoặc vô hiệu hoá nút "
        "không phải là cơ chế bảo mật."
    )

    st.divider()
    st.markdown("**Vì sao có hai danh tính?**")
    st.markdown(
        """
- **Bạn** là người đăng nhập và chịu trách nhiệm cho thay đổi. Ứng dụng ghi lại
  địa chỉ email của bạn trong nhật ký thao tác.
- **Tài khoản dịch vụ của ứng dụng** là danh tính thực sự gọi API Databricks.
  Mọi thứ bạn nhìn thấy là những gì tài khoản đó được phép nhìn thấy.

Hệ quả cần nhớ: danh sách tài sản và quyền trên màn hình phản ánh phạm vi của
tài khoản dịch vụ, **không phải** quyền Unity Catalog cá nhân của bạn.
        """
    )
    if ctx.actor.has_user_token:
        st.caption(
            "Ứng dụng có nhận token uỷ quyền người dùng, nên danh tính của bạn được "
            "xác minh trực tiếp với Databricks."
        )
    else:
        st.caption(
            "Ứng dụng chưa bật uỷ quyền người dùng, nên danh tính lấy từ header của "
            "proxy Databricks Apps. Ứng dụng không dùng header đó để quyết định quyền ghi; "
            "quyền ghi dựa trên cấu hình vai trò phía máy chủ."
        )


def _config(ctx: Context):
    st.markdown("**Cấu hình đang áp dụng**")
    st.caption("Chỉ hiển thị giá trị không nhạy cảm. Ứng dụng không hiển thị token hay secret.")
    widgets.data_table(
        [{"Biến": item["key"], "Giá trị": item["value"]} for item in ctx.settings.describe()],
        key="cfg",
    )
    st.info(
        "Thay đổi biến môi trường trong `app.yaml` chỉ có hiệu lực sau khi **deploy lại** ứng dụng.",
        icon=":material/deployed_code:",
    )

    st.divider()
    st.markdown("**Vai trò được cấu hình**")
    st.markdown(
        """
Gán vai trò bằng biến `GOVERNANCE_ROLES` theo dạng `email:vai_tro`, phân tách bằng dấu phẩy:

```
minh@congty.com:platform_admin,an@congty.com:access_admin,binh@congty.com:steward
```

Các vai trò hợp lệ: `viewer`, `auditor`, `steward`, `access_admin`, `platform_admin`.
Người không có trong danh sách nhận vai trò mặc định `GOVERNANCE_DEFAULT_ROLE` (mặc định là `viewer`).
        """
    )
    rows = [
        {"Vai trò": Role.LABELS[r], "Mã": r, "Phạm vi": Role.DESCRIPTIONS[r]}
        for r in Role.ALL
    ]
    widgets.data_table(rows, key="roles")


def _warehouse(ctx: Context):
    runner = SqlRunner(ctx)
    st.markdown("**SQL Warehouse**")
    st.caption(
        "Một số chức năng chỉ chạy được bằng SQL: lineage và nhật ký kiểm toán đọc từ "
        "system tables, và chuyển quyền sở hữu bảng/view. Các chức năng metadata và "
        "quyền truy cập **không** cần warehouse."
    )

    if runner.available():
        st.success(
            f"Đang dùng warehouse `{ctx.settings.warehouse_id}`.",
            icon=":material/check_circle:",
        )
    else:
        st.info(
            "Chưa cấu hình `GOVERNANCE_WAREHOUSE_ID`. "
            "Các chức năng cần SQL đang tắt, phần còn lại vẫn hoạt động bình thường.",
            icon=":material/settings:",
        )

    found = runner.warehouses()
    if found:
        st.markdown("**Warehouse mà danh tính thực thi nhìn thấy**")
        widgets.data_table(
            [{"Tên": w["name"], "ID": w["id"], "Trạng thái": w["state"]} for w in found],
            key="warehouses",
        )
        st.caption(
            "Đặt ID mong muốn vào `GOVERNANCE_WAREHOUSE_ID` trong `app.yaml` rồi deploy lại. "
            "Tài khoản dịch vụ của ứng dụng còn cần quyền CAN USE trên warehouse đó."
        )
    else:
        st.caption(
            "Không đọc được danh sách warehouse — có thể do danh tính thực thi "
            "chưa có quyền, hoặc workspace chưa có warehouse nào."
        )

    st.divider()
    st.caption(
        "Ứng dụng chỉ chạy các câu lệnh SQL đã định sẵn cho từng nghiệp vụ, có giới hạn "
        "số dòng và thời gian. Ứng dụng **không** cung cấp ô nhập SQL tự do."
    )
