# Deploy lên Databricks Apps

Hướng dẫn đưa repo này từ GitHub lên Databricks Apps. Phần chi tiết về quyền Unity Catalog và cách vận hành app governance nằm ở [governance-app/README.md](governance-app/README.md).

## Chọn app để deploy

Repo có hai source root deploy được. Chọn một:

| Target bundle | Source root | Nội dung |
|---|---|---|
| `dev` (mặc định), `prod` | `streamlit/` | **App full tính năng**: toàn bộ demo Cookbook + menu `Governance → Unity Catalog Governance`. |
| `governance` | `governance-app/` | Chỉ phần governance. Ít dependencies, không có demo Cookbook. |

Bản full dùng `streamlit/requirements.txt` (databricks-connect, databricks-sql-connector, pandas, psycopg, streamlit-folium). Mỗi demo Cookbook có yêu cầu resource riêng (SQL Warehouse, Lakebase, model serving endpoint, secret scope…); demo nào chưa được cấp resource sẽ báo lỗi ngay trên trang của nó, các trang khác vẫn chạy.

> **Cảnh báo phạm vi quyền.** Các demo Cookbook gốc **không** áp dụng bộ giới hạn catalog/allowlist của trang Governance và có thể gọi API khác bằng app service principal. Nếu app này được cấp quyền UC rộng để làm governance, đừng mở `CAN USE` cho nhiều người khi deploy bản full. Muốn phát cho nhiều viewer, deploy target `governance` thay vì `dev`.

## Cách 1 — Databricks Asset Bundle (khuyến nghị)

Cần [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) v0.218 trở lên.

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com

git clone https://github.com/kaitosherlock/databricks-apps-cookbook-governance.git
cd databricks-apps-cookbook-governance

databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run cookbook_governance
```

Host workspace **không** được hard-code trong `databricks.yml` vì repo này public. Truyền host bằng một trong các cách:

```bash
databricks bundle deploy -t dev --var="workspace_host=https://<workspace>.cloud.databricks.com"
```

hoặc đặt `DATABRICKS_HOST`, hoặc dùng profile đã `auth login` (`--profile <tên>`).

Đổi tên app nếu cần:

```bash
databricks bundle deploy -t dev --var="cookbook_app_name=my-cookbook-app"
```

Deploy bản governance-only:

```bash
databricks bundle deploy -t governance
databricks bundle run uc_governance
```

## Cách 2 — Upload thủ công

```bash
databricks workspace import-dir ./streamlit /Workspace/Users/<email>/cookbook-governance --overwrite
databricks apps create cookbook-governance
databricks apps deploy cookbook-governance \
  --source-code-path /Workspace/Users/<email>/cookbook-governance
```

Thay `./streamlit` bằng `./governance-app` nếu deploy bản governance-only. Bỏ lệnh `apps create` nếu app đã tồn tại. `--overwrite` ghi đè đúng thư mục đích, nên dùng thư mục riêng cho mỗi app.

Hoặc upload thư mục source qua UI Workspace rồi Deploy app từ chính thư mục đó.

## Cấu hình sau khi deploy

`app.yaml` trong repo cố ý để **trống** các giá trị governance và tắt writes:

```yaml
- name: GOVERNANCE_CATALOGS
  value: ""
- name: GOVERNANCE_ADMIN_EMAILS
  value: ""
- name: GOVERNANCE_ENABLE_WRITES
  value: "false"
```

Đừng commit giá trị thật vào repo public. Điền chúng trong **App → Settings → Environment** trên Databricks Apps:

| Biến | Giá trị |
|---|---|
| `GOVERNANCE_CATALOGS` | Danh sách catalog, phân tách dấu phẩy, khớp chính xác. VD `governance_sandbox` |
| `GOVERNANCE_ADMIN_EMAILS` | Email được phép ghi, phân tách dấu phẩy, không phân biệt hoa/thường |
| `GOVERNANCE_ENABLE_WRITES` | `true` chỉ sau khi allowlist ở trên đã điền |
| `GOVERNANCE_LOCAL` | Phải là `false` trên Databricks Apps |
| `GOVERNANCE_DEMO` | Phải là `false` trên Databricks Apps |

Sau đó cấp quyền Unity Catalog cho **application/client ID** của app service principal (tab Authorization của app): `USE CATALOG` + `BROWSE` trên catalog, `USE SCHEMA` trên schema, và `MANAGE` đúng phạm vi cần quản trị. Chi tiết và các cảnh báo về phạm vi MANAGE: [governance-app/README.md](governance-app/README.md#3-cấp-quyền-unity-catalog).

Không cần PAT, client secret hay host trong source. Runtime cấp OAuth cho app.

## Kiểm thử trước khi deploy

```bash
python -m pytest tests -q
```

Chạy thử local (demo offline, không cần workspace):

```powershell
cd governance-app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GOVERNANCE_DEMO = "true"
streamlit run app.py
```

## Đồng bộ sau khi sửa code governance

Source chuẩn là `streamlit/governance/`. Sau khi sửa, export sang bản standalone rồi chạy test:

```bash
python scripts/export_governance.py
python -m pytest tests -q
```

## Lỗi thường gặp

### `No command to run and no Python file found`

Source code path đang trỏ vào gốc repo. Gốc repo không có `app.yaml` — đây là repo nhiều app. Điền `streamlit` hoặc `governance-app` vào ô **Source code path**.

### `No matching distribution found for databricks-connect~=18.1`

Wheel của `databricks-connect` từ bản 16.2 trở đi đánh dấu `Requires-Python ==3.12.*`. Build image của Databricks Apps không phải 3.12, nên pip không tìm được bản nào hợp lệ và fail toàn bộ bước cài package.

Đã xử lý trong `streamlit/requirements.txt` bằng environment marker:

```
databricks-connect~=18.1; python_version == "3.12"
```

Hệ quả: trên runtime không phải 3.12, hai trang `Connect to shared cluster` và `Connect to serverless cluster` hiển thị cảnh báo thay vì chạy được. Mọi trang khác, gồm Governance, không bị ảnh hưởng — `st.navigation` chỉ import module của một trang khi bạn bấm vào nó.

Muốn dùng hai trang đó thì cần runtime Python 3.12, hoặc hạ pin xuống bản chạy được với Python của image (`databricks-connect~=16.1`) — lưu ý bản 16.x có thể xung đột pin `pandas~=3.0`.

### Build fail ở package khác

`streamlit/requirements.txt` còn `databricks-sql-connector`, `psycopg[binary]`, `pandas~=3.0`, `streamlit-folium`. Nếu bản nào không resolve được trên image, deploy `governance-app` để xác nhận pipeline chạy thông trước — bản đó chỉ cần `databricks-sdk` + `streamlit` và có đủ 100% tính năng governance.

## Ghi chú về repo

- Workflow `.github/workflows/deploy.yml` của upstream (deploy Docusaurus lên Cloudflare Workers) đã được gỡ bỏ — nó cần secret `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` không có trong repo này và sẽ fail mỗi lần push.
- Repo chưa có workflow tự động deploy lên Databricks Apps. Muốn thêm thì cần secret `DATABRICKS_HOST` cùng OAuth service principal (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`) và một job chạy `databricks bundle deploy -t prod`.
