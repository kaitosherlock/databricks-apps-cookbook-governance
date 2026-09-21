# governance-app

Đây là **source code path** của Databricks App: toàn bộ ứng dụng
Quản trị Unity Catalog nằm trong thư mục này, và `app.py` là entry point duy nhất.

```
app.py          entry point Streamlit
app.yaml        cấu hình Databricks Apps (chỉ có `command` và `env`)
requirements.txt
ucg/            backend — không import Streamlit
ui/             giao diện Streamlit
```

Tài liệu đầy đủ (kiến trúc, vai trò, ma trận khả năng, giới hạn đã biết) nằm ở
[`../README.md`](../README.md). Quy trình deploy và xử lý sự cố nằm ở
[`../DEPLOY.md`](../DEPLOY.md).

Kiểm thử chạy từ thư mục gốc của repository, không chạy ở đây:

```bash
python -m pytest
```

Ứng dụng không có chế độ demo và không dùng dữ liệu giả. Khi chưa kết nối được
Databricks hoặc thiếu cấu hình, ứng dụng hiển thị hướng dẫn thiết lập.
