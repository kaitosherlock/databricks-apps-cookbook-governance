# Quản trị Unity Catalog (Unity Catalog Governance)

Ứng dụng Databricks App độc lập dành cho Data Steward và người quản trị dữ liệu:
tìm tài sản dữ liệu, xem ai có quyền gì và quyền đến từ đâu, rồi thay đổi quyền
một cách có kiểm soát.

- **Một tiến trình duy nhất khi chạy**: `governance-app/server.py`
- **Backend Python + `databricks-sdk`**; **giao diện React / TypeScript / Next.js**
- Không có Spark, không cần database riêng.
- Không còn chế độ demo, không còn dữ liệu giả trong luồng người dùng.

> **Giao diện Streamlit cũ vẫn còn trong cây mã** (`governance-app/app.py` và
> `governance-app/ui/`) như một đường lui. Đổi `command` trong `app.yaml` rồi
> deploy lại là quay về được. Xem [`DEPLOY.md`](DEPLOY.md) mục 2.

> Bản này bắt nguồn từ [databricks-apps-cookbook](https://github.com/databricks-solutions/databricks-apps-cookbook)
> (upstream commit `88a0387`). Toàn bộ trang demo, menu demo, ví dụ và tài liệu của
> Cookbook đã được gỡ bỏ. Giấy phép và ghi nhận bắt buộc được giữ nguyên trong
> [`LICENSE.md`](LICENSE.md) và [`NOTICE.md`](NOTICE.md).

---

## 1. Kiến trúc

```
governance-app/
├── server.py               entry point: FastAPI phục vụ /api và giao diện tĩnh
├── app.yaml                cấu hình Databricks Apps (chỉ có command + env)
├── requirements.txt        databricks-sdk==0.105.0, fastapi, uvicorn
├── static/                 GIAO DIỆN ĐÃ BUILD — commit vào git, xem mục 1.1
├── api/                    TẦNG HTTP — chỉ chuyển vận, không có logic quản trị
│   ├── context.py          dựng Context từ header proxy, mỗi request một lần
│   ├── runtime.py          kho tiến trình: client, capability, log, plan
│   ├── http.py             ánh xạ mã lỗi sang HTTP
│   └── routers/            mỗi nhóm màn hình một module
├── app.py                  entry point Streamlit cũ (đường lui)
├── ucg/                    BACKEND — không import Streamlit
│   ├── config.py           cấu hình và ánh xạ vai trò (phía máy chủ)
│   ├── errors.py           mã lỗi ổn định + thông báo tiếng Việt
│   ├── identity.py         người dùng vs danh tính thực thi
│   ├── clients.py          tạo client, không fallback ngầm
│   ├── authz.py            phân quyền theo vai trò và phạm vi
│   ├── capabilities.py     đăng ký và dò khả năng của workspace
│   ├── naming.py           Target, kiểm tra tên, quote SQL identifier
│   ├── privileges.py       danh mục privilege theo loại securable
│   ├── paging.py           phân trang đúng quy tắc của Unity Catalog
│   ├── plans.py            vòng đời Plan → Preview → Outcome
│   ├── audit.py            nhật ký thao tác của ứng dụng
│   └── services/           mỗi nhóm chức năng một module
└── ui/                     giao diện Streamlit cũ (đường lui)
    ├── session.py  chrome.py  widgets.py  state.py  picker.py  nav.py
    └── pages/              mỗi màn hình một module

web/                        MÃ NGUỒN GIAO DIỆN — không được deploy
├── src/app/                một thư mục một route
├── src/components/         ui/ · shell/ · governance/
└── src/lib/                hợp đồng API, React Query, trạng thái chọn đối tượng
```

Backend hoàn toàn tách khỏi giao diện: `ucg/` không import Streamlit ở bất kỳ đâu
(có test kiểm tra điều này), nên có thể kiểm thử và tái sử dụng độc lập. Tầng
`api/` cũng không thêm logic quản trị nào: mọi quyết định phân quyền, mọi kiểm
tra đầu vào và toàn bộ vòng đời thay đổi vẫn nằm trong `ucg/`.

### 1.1. Vì sao `static/` được commit

Databricks Apps chạy **đúng một lệnh** và chỉ cài gói Python — không có Node lúc
deploy. Giao diện vì thế được build sẵn (`cd web && npm run build`) và kết quả
commit vào `governance-app/static/`, để `server.py` phục vụ. Chi tiết trong
[`web/README.md`](web/README.md).

### 1.2. Bản xem trước nằm ở máy chủ

Khi chuyển sang mô hình trình duyệt + API, điều dễ mất nhất là tính an toàn của
bước xem trước. Ở đây `Plan` **không bao giờ đi qua dây**: nó nằm trong kho phía
máy chủ, trình duyệt chỉ giữ một `plan_id` mờ, và `/api/grants/apply` chỉ nhận
`plan_id` cùng chuỗi xác nhận người dùng gõ. Nội dung thay đổi được đọc lại từ
bản lưu của máy chủ, nên một client bị sửa đổi không thể áp dụng thứ khác với
thứ đã được xem trước.

### Quy trình thay đổi quyền

Mọi thay đổi đi qua đúng một đường:

```
Validate → Authorize → Build plan → Preview → Confirm
         → Revalidate → Execute → Verify → Audit
```

- Bản xem trước (`Plan`) gắn với **người thao tác, danh tính thực thi, đối tượng**
  và một fingerprint của trạng thái lúc xem trước.
- Sửa bất kỳ trường quan trọng nào sẽ **huỷ** bản xem trước cũ.
- Trước khi ghi, ứng dụng **đọc lại** và từ chối nếu trạng thái đã đổi.
- Sau khi ghi, ứng dụng **đọc lại lần nữa** để báo cáo trạng thái thật, không
  lặp lại nội dung đã gửi.
- Một bản xem trước chỉ gửi được **một lần**.
- Timeout cho ra trạng thái **“Chưa xác định kết quả”**, không phải “thất bại”, và
  ứng dụng **không tự thử lại**.

---

## 2. Vai trò và phân quyền

Ánh xạ vai trò nằm ở phía máy chủ, trong biến môi trường `GOVERNANCE_ROLES`.
Giao diện chỉ dùng nó để làm mờ nút; **backend luôn kiểm tra lại** trước khi thực thi.

| Vai trò | Mã | Được phép |
|---|---|---|
| Người xem | `viewer` | Xem metadata, quyền, thẻ, chính sách, chia sẻ, lineage; gửi yêu cầu truy cập |
| Kiểm toán | `auditor` | Như trên, thêm nhật ký kiểm toán và báo cáo rà soát |
| Data Steward | `steward` | Thêm: sửa mô tả, gán/gỡ thẻ |
| Quản trị quyền truy cập | `access_admin` | Thêm: cấp/thu hồi quyền, chuyển quyền sở hữu, cấu hình nơi nhận yêu cầu |
| Quản trị nền tảng | `platform_admin` | Thêm: governed tag, chính sách ABAC, lưu trữ, ràng buộc workspace, chia sẻ dữ liệu, giám sát chất lượng |

Ba cổng chặn ghi độc lập nhau, và màn hình luôn nói rõ cái nào đang chặn:

1. `GOVERNANCE_ENABLE_WRITES` (hoặc `GOVERNANCE_LOCAL=true`) — công tắc tổng.
2. Vai trò của người dùng.
3. `GOVERNANCE_CATALOGS` — phạm vi catalog.

Trên hết, Unity Catalog vẫn là bên quyết định cuối cùng: ứng dụng không thể làm
gì vượt quá quyền của danh tính thực thi.

### Hai danh tính, không được nhầm

| | Là ai | Dùng để |
|---|---|---|
| **Người dùng** | Người đăng nhập, lấy từ proxy Databricks Apps | Ghi nhận trách nhiệm, phân vai trò trong ứng dụng |
| **Danh tính thực thi** | Service principal của app | Thực sự gọi API Databricks |

Mọi thứ hiển thị trên màn hình là những gì **service principal** được phép thấy,
không phải quyền Unity Catalog cá nhân của người đang xem. Banner và thanh bên
nhắc lại điều này trên mọi trang.

Databricks không cam kết header `X-Forwarded-*` chống giả mạo, nên ứng dụng chỉ
dùng chúng để hiển thị và ghi nhật ký. Khi app được bật *user authorization*,
ứng dụng xác minh danh tính bằng token của chính người dùng và nói rõ là đã xác minh.

---

## 3. Phạm vi chức năng

Màn hình **Khả năng và cấu hình** trong app sinh ma trận khả năng trực tiếp từ
đăng ký ở `ucg/capabilities.py`, nên nó không thể lệch khỏi code. Tóm tắt:

| Nhóm | Chức năng | Trạng thái |
|---|---|---|
| Tài sản dữ liệu | Duyệt catalog/schema/table/view/volume/function/model, xem metadata và cột | Đầy đủ |
| | Sửa mô tả (catalog, schema, volume, model) | Đầy đủ |
| | Chuyển quyền sở hữu (catalog, schema, volume, function, model) | Đầy đủ |
| | Chuyển quyền sở hữu **bảng/view** | Cần SQL Warehouse — API Tables không có endpoint update |
| Quyền truy cập | Xem quyền trực tiếp và quyền hiệu lực (kèm nguồn kế thừa) | Đầy đủ |
| | Cấp / thu hồi quyền (gửi delta) | Đầy đủ |
| | Tra cứu principal | Giới hạn: SCIM workspace không thấy account group |
| Thẻ | Xem/gán/sửa/gỡ thẻ trên catalog, schema, table, column, volume | Đầy đủ |
| | Governed tag (tag policy) | Đầy đủ |
| | Data Classification | **Chỉ đọc** — xem mục 6 |
| Chính sách | Xem/tạo/sửa/xoá chính sách ABAC (row filter, column mask) | Đầy đủ |
| | Row filter / column mask gắn trực tiếp trên bảng | **Chỉ đọc** — Databricks chỉ hỗ trợ qua SQL |
| Lưu trữ | Storage credential, service credential, external location | Đọc, cập nhật, ràng buộc |
| | Tạo credential | **Không** — Databricks không cho service principal tạo |
| | Ràng buộc workspace (2 bước: chế độ cách ly + danh sách) | Đầy đủ |
| Federation | Kết nối, foreign catalog, kiểm tra phụ thuộc | Đọc + chuyển chủ sở hữu |
| Chia sẻ dữ liệu | Share, recipient, provider, quyền trên share | Đầy đủ (không bao giờ hiện credential) |
| Lineage | Lineage bảng/cột | Cần SQL Warehouse (system tables) |
| | Lineage tài sản ngoài Databricks | Đầy đủ (chỉ quan hệ khai báo thủ công) |
| Kiểm toán | `system.access.audit` | Cần SQL Warehouse |
| Chất lượng | Giám sát chất lượng (xem, chạy lại, xoá) | Đầy đủ; **không** kiểm kê được toàn workspace |
| | Ràng buộc bảng, phân loại có/không hiệu lực | Đầy đủ |
| Yêu cầu truy cập | Cấu hình nơi nhận, gửi yêu cầu | Đầy đủ |
| | **Phê duyệt yêu cầu** | **Không tồn tại** — xem mục 6 |

---

## 4. Cấu hình

Tất cả nằm trong `governance-app/app.yaml`. Đổi giá trị chỉ có hiệu lực sau khi
**deploy lại**.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `GOVERNANCE_CATALOGS` | `""` | Danh sách catalog được phép quản lý. Rỗng = không giới hạn. |
| `GOVERNANCE_ROLES` | — | `email:role`, phân tách bằng dấu phẩy. |
| `GOVERNANCE_DEFAULT_ROLE` | `viewer` | Vai trò cho người không có trong danh sách. |
| `GOVERNANCE_ENABLE_WRITES` | `false` | Công tắc ghi tổng. |
| `GOVERNANCE_LOCAL` | `false` | Chạy bằng hồ sơ CLI cá nhân; luôn chỉ đọc. |
| `GOVERNANCE_ENVIRONMENT` | `""` | `DEV`/`UAT`/`STAGING`/`PROD`. Để trống thì app **không đoán**. |
| `GOVERNANCE_WAREHOUSE_ID` | `""` | SQL Warehouse cho lineage, kiểm toán, chuyển chủ sở hữu bảng. |
| `GOVERNANCE_SQL_ROW_LIMIT` | `1000` | Trần số dòng cho truy vấn SQL. |
| `GOVERNANCE_SQL_TIMEOUT_SECONDS` | `50` | Trần thời gian chờ truy vấn. |

Tương thích ngược: `GOVERNANCE_ADMIN_EMAILS` vẫn được đọc và ánh xạ thành
`access_admin`.

### Quyền cần cấp cho service principal của app

Trong Catalog Explorer, dùng danh tính có thẩm quyền, cấp cho **application ID**
của service principal (xem tab Authorization của app):

| Mục tiêu | Quyền tối thiểu |
|---|---|
| Nhìn thấy catalog | `USE CATALOG`, và `BROWSE` nếu muốn thấy đối tượng không có quyền đọc |
| Đi vào schema | `USE SCHEMA` |
| Đọc và quản lý grants | `MANAGE` trên đúng phạm vi cần quản trị, hoặc quyền sở hữu |
| Gán thẻ | `APPLY TAG` (+ `ASSIGN` trên governed tag, cấp qua Account Access Control) |
| Lineage / kiểm toán | `CAN USE` trên SQL Warehouse, `USE CATALOG` trên `system`, `USE SCHEMA` + `SELECT` trên `system.access` |

`MANAGE` **không** đồng nghĩa với `SELECT` dữ liệu. Ứng dụng chỉ gọi API metadata
và grants; nó không đọc nội dung bảng hay tệp.

Cấp `MANAGE` ở cấp catalog có phạm vi rất rộng xuống mọi đối tượng con — chỉ làm
khi đó đúng là chủ đích.

---

## 5. Triển khai

App hiện tại deploy từ **Git source**, nên quy trình là: đẩy code lên nhánh, rồi
bấm **Deploy** trong giao diện Databricks Apps.

```
Repository:       kaitosherlock/databricks-apps-cookbook-governance
Branch:           main
Source code path: governance-app
```

Hoặc bằng Databricks CLI:

```bash
databricks auth login --host https://<workspace-host>
databricks bundle deploy -t dev --var="workspace_host=https://<workspace-host>"
```

Hoặc thủ công:

```bash
databricks workspace import-dir ./governance-app /Workspace/Users/<email>/uc-governance --overwrite
databricks apps deploy uc-governance --source-code-path /Workspace/Users/<email>/uc-governance
```

### Sau khi deploy

1. Mở URL của app, kiểm tra thanh bên: workspace, môi trường, chế độ, hai danh tính.
2. Mở **Khả năng và cấu hình** để xem chức năng nào sẵn sàng và chức năng nào
   đang thiếu quyền hoặc thiếu cấu hình.
3. Chỉ cấp `CAN USE` app cho nhóm phù hợp. Mọi người vào được app đều thấy metadata
   và quyền **theo tầm nhìn của service principal**.

---

## 6. Giới hạn đã biết — và vì sao

Những mục dưới đây **không phải là việc chưa làm xong**; chúng là giới hạn của
nền tảng, và ứng dụng cố ý không dựng nút giả để che đi.

| Chức năng | Vì sao không có |
|---|---|
| **Phê duyệt yêu cầu truy cập** | Databricks không có API liệt kê, phê duyệt hay từ chối yêu cầu, và không tự cấp quyền khi được duyệt. Người duyệt thao tác trong giao diện Databricks. Ứng dụng chỉ cấu hình nơi nhận và gửi yêu cầu. |
| **Quyền có thời hạn** | `GRANT` của Unity Catalog không có hạn dùng, và Databricks Apps không giữ được trạng thái hay tiến trình nền qua các lần khởi động lại. Muốn có thì phải xây scheduler + lưu trữ bền vững (Lakeflow Job + bảng UC), không làm trong tiến trình app. |
| **Tạo / gỡ row filter, column mask gắn trực tiếp** | Databricks chỉ hỗ trợ qua `ALTER TABLE`; không có REST/SDK. |
| **Bật/tắt Data Classification** | `CatalogConfig` không có công tắc bật/tắt ở cấp catalog: tự động gắn thẻ cấu hình theo **từng thẻ**, và trạng thái hiệu lực còn phụ thuộc một thiết lập cấp metastore mà Databricks chưa công bố API. Ứng dụng hiển thị cấu hình, không sửa. |
| **Kiểm kê toàn bộ monitor chất lượng** | `list_monitor` được Databricks đánh dấu *Unimplemented*. Ứng dụng chỉ dò được từng đối tượng. |
| **Tra ngược “tài sản nào mang thẻ X”** | API thẻ chỉ tra theo từng đối tượng. Tra ngược cần SQL trên `INFORMATION_SCHEMA`. |
| **Tạo storage/service credential** | Quyền `CREATE SERVICE CREDENTIAL` không uỷ quyền được cho service principal. |
| **Chuyển quyền sở hữu bảng/view qua REST** | API Tables không có thao tác update. |
| **Ràng buộc workspace cho connection** | Connection không nằm trong danh sách securable hỗ trợ binding. |
| **Hiển thị link kích hoạt recipient** | Đó là credential, tải một lần. Ứng dụng không hiển thị, không ghi log, không xuất. |
| **Đọc giá trị credential trong connection** | Databricks không tài liệu hoá việc có che hay không, nên ứng dụng coi mọi giá trị là nhạy cảm và không trả về. |

Hai điều ứng dụng **luôn nói rõ** thay vì im lặng:

- Kết quả rỗng do **thiếu quyền** khác với **không có dữ liệu**.
- “Không ghi nhận được lineage” khác với “không có phụ thuộc”. Lineage của
  Databricks chỉ là **một phần** các sự kiện đọc/ghi; không được dùng nó để kết
  luận an toàn khi xoá.

---

## 7. Phát triển và kiểm thử

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows
pip install -r governance-app/requirements.txt pytest
python -m pytest
```

Kiểm thử dùng SDK giả lập tái tạo đúng **hình dạng phản hồi** của Databricks —
nơi bug thực sự nằm: `grants.get` trả privilege là chuỗi còn `grants.get_effective`
trả object kèm nguồn kế thừa; một trang có thể **rỗng mà vẫn có next token**;
`update` là delta chứ không phải replace.

Chạy local với workspace thật (chỉ đọc):

```bash
databricks auth login --host https://<workspace-host>
$env:GOVERNANCE_LOCAL = "true"
cd governance-app; streamlit run app.py
```

---

## 8. Bảo mật

- Không bao giờ hiển thị hay ghi log token, secret, credential, link kích hoạt
  recipient, giá trị option của connection, hay nội dung phản hồi thô từ API.
- Không có ô nhập SQL tự do. Chỉ chạy các template SQL đã kiểm soát, tham số
  được bind, identifier được quote qua `naming.sql_identifier` (từ chối backtick
  và dấu chấm), có trần số dòng và thời gian.
- Không chấp nhận host/URL do client truyền vào.
- Không tự fallback từ uỷ quyền người dùng sang service principal có quyền cao hơn.
- Cache dữ liệu Unity Catalog nằm trong session state của từng phiên trình duyệt,
  không chia sẻ giữa các danh tính.
- Ẩn hoặc vô hiệu hoá nút **không** phải là cơ chế bảo mật; backend kiểm tra lại
  mọi thao tác.

Nhật ký thao tác của ứng dụng là bản ghi vận hành trong phiên, **không phải**
nhật ký kiểm toán đầy đủ. Databricks Apps không giữ log sau khi compute dừng.
Bản ghi đầy đủ nằm ở `system.access.audit`.

---

## Giấy phép

Xem [`LICENSE.md`](LICENSE.md) và [`NOTICE.md`](NOTICE.md).
