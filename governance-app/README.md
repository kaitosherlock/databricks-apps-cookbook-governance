# Databricks Apps Cookbook + Unity Catalog Governance

Bản mở rộng từ https://github.com/databricks-solutions/databricks-apps-cookbook

- Upstream commit: `88a0387f09eb3dc2bb3130eeabb188d3fa3dd26c`
- Ngày bổ sung: 2026-09-20.
- Giữ source Cookbook và license/notice gốc. File navigation đã đánh dấu chỉnh sửa.
- Bản thêm dùng Python + Streamlit + Databricks SDK. Không cần Spark cluster, SQL Warehouse hoặc database riêng cho các chức năng governance dưới đây.

## Chạy bản nào?

| Thư mục | Mục đích |
|---|---|
| `governance-app/` | App governance riêng, ít dependencies. **Khuyến nghị triển khai bản này cho người quản trị.** |
| `streamlit/` | Cookbook gốc có thêm menu Governance. Các demo gốc vẫn giữ nguyên và có dependencies/resources riêng. |
| `streamlit/governance/` | Source chính: `service.py` gọi SDK, `ui.py` giao diện, `demo.py` dữ liệu minh hoạ. |
| `tests/` | Kiểm thử phần governance và giao diện Streamlit bằng SDK giả lập. |

Các demo gốc của Cookbook không áp dụng bộ giới hạn của trang Governance. Vì chúng có thể gọi những API khác bằng app service principal, không dùng chung identity có quyền quản trị với app demo mở rộng cho nhiều người. Bản `governance-app/` chỉ chứa các chức năng governance được mô tả dưới đây.

## Chức năng đã có

- Duyệt Catalog → Schema → Table/View hoặc Function theo catalog được cấu hình.
- Xem owner, comment, cấu trúc cột và metadata JSON; tải metadata JSON.
- Xem quyền trực tiếp và quyền hiệu lực, có nguồn kế thừa; xuất CSV.
- Grant/Revoke quyền trực tiếp với principal nhập vào (account group, user email, service principal application ID).
- Xem trước thao tác, nhập lý do, xác nhận tên đầy đủ trước khi ghi.
- Kiểm tra lại quyền và điều kiện ghi trước khi gọi SDK; chỉ gửi delta của principal/quyền đã chọn.
- Phân trang đến hết kết quả, kể cả trang trống có next-page token.
- Ghi JSON event vào stdout/app logs với người thao tác, identity thực thi, đối tượng, lý do, trạng thái và event ID.
- Demo offline chỉ đọc; local bằng Databricks CLI OAuth profile chỉ đọc.

| Đối tượng | Các quyền giao diện hỗ trợ |
|---|---|
| Catalog | USE_CATALOG, USE_SCHEMA, BROWSE, SELECT, MODIFY, EXECUTE |
| Schema | USE_SCHEMA, CREATE_TABLE, CREATE_FUNCTION, SELECT, MODIFY, EXECUTE |
| Table | SELECT, MODIFY |
| View/Materialized View | SELECT |
| Function | EXECUTE |

Quyền dữ liệu được cấp ở Catalog/Schema có thể kế thừa xuống đối tượng con hiện tại và tương lai. Thu hồi trực tiếp không đảm bảo một người mất mọi đường truy cập: quyền có thể còn qua nhóm, parent, ownership hoặc chính sách khác. USE_CATALOG/USE_SCHEMA cần được quản lý riêng; app không tự cấp thêm chúng khi cấp SELECT/EXECUTE.

Chưa có: sửa comment/tag, ABAC, row filter/column mask, quản lý user/group, ownership transfer, access-request approval workflow, dashboard lineage/audit, thực thi UDF. Mục Function hiện quản lý metadata và EXECUTE privilege, không gọi UDF. Các ví dụ gọi workflows/model vẫn ở Cookbook gốc.

## Triển khai lên Databricks Apps

### 1. Chuẩn bị

- Workspace có Databricks Apps và Unity Catalog.
- Giải nén source. Chọn `governance-app/` làm source root, nơi có `app.py`, `app.yaml`, `requirements.txt`.
- Với thử nghiệm đầu tiên, dùng một catalog sandbox riêng.

### 2. Tạo app và lấy service principal

Trong Databricks Apps, tạo Custom app, ví dụ `uc-governance`.
Trong tab Authorization của app, lấy **application/client ID** của service principal để cấp quyền Unity Catalog.

Không cần nhập PAT, client secret hoặc host vào source. Runtime cung cấp OAuth cho app; code cố định dùng `WorkspaceClient(auth_type="oauth-m2m")` khi deploy. Bản này không dùng on-behalf-of-user.

### 3. Cấp quyền Unity Catalog

Dùng Catalog Explorer, bằng identity có thẩm quyền:

- Cấp `USE CATALOG` và `BROWSE` trên catalog được quản lý.
- Cấp `USE SCHEMA` trên schema cần dùng (hoặc cấp tại catalog để kế thừa theo thiết kế).
- Để đọc đầy đủ và quản lý grants, cấp `MANAGE` đúng các đối tượng trong phạm vi cần quản trị hoặc sử dụng quyền ownership phù hợp. Cấp MANAGE tại catalog có phạm vi rộng xuống đối tượng con; chỉ áp dụng khi đó là chủ đích của bạn.
- Tên principal cho service principal là application/client ID.

MANAGE không đồng nghĩa với SELECT trên dữ liệu. App sử dụng API metadata và grants, không truy vấn các dòng dữ liệu. Kết quả API vẫn phụ thuộc privilege model, loại securable và quyền identity của workspace. Nếu thiếu quyền xem grants, tab metadata vẫn hoạt động khi metadata được phép đọc.

### 4. Cấu hình `governance-app/app.yaml`

Thay các giá trị:

```yaml
command: ["streamlit", "run", "app.py"]
env:
  - name: GOVERNANCE_CATALOGS
    value: "governance_sandbox"
  - name: GOVERNANCE_ADMIN_EMAILS
    value: "your-admin@company.com"
  - name: GOVERNANCE_ENABLE_WRITES
    value: "true"
  - name: GOVERNANCE_LOCAL
    value: "false"
  - name: GOVERNANCE_DEMO
    value: "false"
```

- Danh sách catalog/email phân tách bằng dấu phẩy. Catalog khớp chính xác; email không phân biệt hoa/thường.
- **`GOVERNANCE_CATALOGS` để trống = đọc mọi catalog** mà service principal của app nhìn thấy. Tiện để khảo sát, nhưng phạm vi đọc bằng đúng quyền UC đã cấp cho app — muốn giới hạn thì khai báo danh sách catalog.
- **Ghi luôn yêu cầu `GOVERNANCE_CATALOGS` có giá trị.** Phạm vi đọc mở không kéo theo phạm vi ghi mở: app từ chối mọi Grant/Revoke khi allowlist trống, kể cả `writes=true` và email đúng allowlist.
- Gói giao mặc định để catalog/email trống và writes=false, tức là chỉ đọc. Điền cấu hình thật trước khi cần quản trị.
- Chỉ email trong allowlist mới ghi được. Thiếu email proxy, cấu hình ghi, hoặc catalog scope thì bị chặn.
- Chỉ cấp `CAN USE` app cho nhóm quản trị/đọc governance thích hợp. Mọi viewer được phép vào app sẽ thấy metadata và grants bằng quyền **service principal**, không theo quyền UC cá nhân.
- Email người thao tác lấy từ `X-Forwarded-Email` của Databricks Apps proxy. Chỉ dùng mô hình này sau proxy Databricks Apps; không host công khai app này trên server khác rồi tin header do client gửi.
- Cấu hình và source phải chỉ cho maintainer tin cậy sửa. Không cấp rộng `CAN MANAGE` app.

### 5. Upload và Deploy

Có thể upload thư mục source vào Workspace rồi Deploy app từ chính thư mục đó.

Hoặc dùng Databricks CLI đã được cài đặt, chạy từ thư mục gốc gói source:

```bash
databricks auth login --host https://<your-workspace-host>
databricks workspace import-dir ./governance-app /Workspace/Users/<your-email>/uc-governance --overwrite
databricks apps create uc-governance
databricks apps deploy uc-governance --source-code-path /Workspace/Users/<your-email>/uc-governance
```

Nếu đã tạo app ở bước 2, bỏ lệnh `apps create`. `--overwrite` thay source ở đúng thư mục đích: sử dụng thư mục dành riêng cho app này. Nếu profile không phải DEFAULT, thêm `--profile <profile-name>` vào các lệnh phù hợp.

### 6. Kiểm tra trên workspace

1. Vào URL của app, kiểm tra email và dòng identity thực thi.
2. Chọn catalog sandbox và một table thử nghiệm; so sánh metadata/grants với Catalog Explorer.
3. Grant SELECT cho account group thử nghiệm; kiểm tra bản xem trước và nhập tên đầy đủ.
4. Đối chiếu kết quả trong Catalog Explorer, sau đó revoke quyền vừa cấp.
5. Kiểm tra tài khoản viewer ngoài allowlist không có nút ghi.
6. Kiểm tra quyền kế thừa hiển thị đúng parent; muốn thay đổi phải chọn parent tương ứng.

## Chạy demo local (không cần workspace)

Python 3.12 được dùng để kiểm thử gói này.

```bash
cd governance-app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
GOVERNANCE_DEMO=true streamlit run app.py
```

Windows PowerShell:

```powershell
cd governance-app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GOVERNANCE_DEMO = "true"
streamlit run app.py
```

Demo luôn chỉ đọc, có nhãn rõ ràng và không kết nối Databricks. Nó không chứng minh quyền hoặc dữ liệu của workspace thật.

## Local với workspace thật (chỉ đọc)

Đăng nhập bằng Databricks CLI trước, đặt `DATABRICKS_CONFIG_PROFILE` nếu dùng profile riêng, sau đó:

```bash
GOVERNANCE_DEMO=false GOVERNANCE_LOCAL=true GOVERNANCE_CATALOGS=governance_sandbox streamlit run app.py
```

Local dùng `WorkspaceClient()` với unified authentication. Code chặn mọi thao tác ghi trong local mode kể cả nếu enable_writes=true. Khi deploy về Databricks Apps phải để LOCAL=false và DEMO=false.

## Dùng trong Cookbook đầy đủ

Menu `Governance → Unity Catalog Governance` đã được đăng ký trong `streamlit/view_groups.py`. Dùng `streamlit/` làm source root và thêm các biến GOVERNANCE_* vào `streamlit/app.yaml` theo cấu hình ở trên. Dependencies gốc vẫn ở `streamlit/requirements.txt`; mỗi demo gốc có yêu cầu resources riêng. Chỉ phần governance đã được kiểm thử trong lần chỉnh sửa này.

## Phát triển và kiểm thử

Source chuẩn nằm ở `streamlit/governance/`. Sau khi sửa, đồng bộ bản standalone:

```bash
python scripts/export_governance.py
pip install -r governance-app/requirements.txt pytest
python -m pytest tests -q
```

19 tests đã pass: logic kiểm soát ghi, catalog scope, phân trang, direct/inherited grants, thay đổi đồng thời trước apply, delta update, lỗi API, render demo và luồng giao diện review/apply với SDK giả lập.

Chưa chạy integration test trên workspace thật. Không có deployment hoặc thay đổi quyền thật nào được thực hiện khi tạo gói này.

Kiểm tra lại snapshot trước apply giảm rủi ro ghi từ màn hình cũ nhưng không phải giao dịch compare-and-swap: UC grants API không cung cấp transaction bao trùm read/update. Nếu timeout, trạng thái mutation có thể chưa rõ; làm mới grants trước khi thử lại. API update chỉ thay đổi danh sách add/remove được gửi, không replace toàn bộ ACL.

Audit JSON của app là log vận hành, không phải kho audit bền vững. Databricks ghi thao tác API dưới identity service principal; email viewer được app ghi thêm ở log riêng. Muốn báo cáo audit lâu dài phải cấu hình lưu log/đối chiếu system audit theo chính sách của workspace. App không lưu token hoặc raw exception message vào log.

## Nguồn tham khảo

- Cookbook: https://github.com/databricks-solutions/databricks-apps-cookbook
- SDK grants: https://databricks-sdk-py.readthedocs.io/en/latest/workspace/catalog/grants.html
- SDK tables: https://databricks-sdk-py.readthedocs.io/en/latest/workspace/catalog/tables.html
- App authorization: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth
- Proxy identity headers: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/http-headers
- App deployment: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/deploy
- UC privileges: https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/privileges-reference
