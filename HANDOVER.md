# Bàn giao — Ứng dụng Quản trị Unity Catalog

Tài liệu bàn giao. Kiến trúc và hướng dẫn sử dụng ở [`README.md`](README.md);
quy trình vận hành ở [`DEPLOY.md`](DEPLOY.md).

---

## 1. Đã bàn giao những gì

| Hạng mục | Vị trí |
|---|---|
| Source đã triển khai | `governance-app/` — một source tree, một entry point |
| Cấu hình deploy | `governance-app/app.yaml`, `databricks.yml` |
| Phiên bản dependency | `governance-app/requirements.txt` (đã ghim) |
| Ma trận quyền (vai trò) | Mục 3 dưới đây + `ucg/authz.py` |
| Ma trận khả năng | Sinh tự động trong app: **Khả năng và cấu hình** → xuất CSV |
| Hợp đồng service | Mục 5 dưới đây |
| Runbook | [`DEPLOY.md`](DEPLOY.md) |
| Kết quả kiểm thử | Mục 6 dưới đây |

Không có bước migration nào: ứng dụng **không** có kho dữ liệu riêng. Mọi trạng
thái nằm ở Databricks; ứng dụng chỉ giữ trạng thái trong phiên làm việc.

**Quy mô**: backend 11.686 dòng, giao diện 9.002 dòng, kiểm thử 1.664 dòng.

---

## 2. Đã gỡ bỏ những gì

| Đã xoá | Lý do |
|---|---|
| `dash/`, `fastapi/`, `reflex/` | Ba framework demo của Cookbook |
| `docs/` | Trang tài liệu Docusaurus của Cookbook |
| `streamlit/` | App Cookbook đầy đủ với khoảng 24 trang demo |
| `scripts/export_governance.py` | Không còn hai bản source để đồng bộ |
| `governance-app/governance/` | Module governance cũ, đã được `ucg/` thay thế |
| `governance-app/governance/demo.py` | Dữ liệu giả — không còn chế độ demo |
| `GOVERNANCE_DEMO` | Biến môi trường của chế độ demo |

Giữ nguyên: `LICENSE.md`, `NOTICE.md` (ghi nhận bắt buộc từ upstream) và
`SECURITY.md`.

Fixture/mock chỉ còn trong `tests/conftest.py`, không nằm trên đường đi của
người dùng. Có test kiểm tra `ucg/` không import Streamlit.

---

## 3. Ma trận quyền

### Vai trò ứng dụng → hành động

| Hành động | viewer | auditor | steward | access_admin | platform_admin |
|---|:--:|:--:|:--:|:--:|:--:|
| Xem metadata, quyền, thẻ, chính sách, chia sẻ, lineage | ✔ | ✔ | ✔ | ✔ | ✔ |
| Gửi yêu cầu truy cập | ✔ | ✔ | ✔ | ✔ | ✔ |
| Xem nhật ký kiểm toán | | ✔ | ✔ | ✔ | ✔ |
| Sửa mô tả, gán/gỡ thẻ | | | ✔ | ✔ | ✔ |
| Cấp / thu hồi quyền, chuyển quyền sở hữu | | | | ✔ | ✔ |
| Cấu hình nơi nhận yêu cầu truy cập | | | | ✔ | ✔ |
| Governed tag, chính sách ABAC, lưu trữ, ràng buộc workspace, chia sẻ, giám sát chất lượng | | | | | ✔ |

Kiểm tra ở backend (`Authorizer.require`), không phải ở giao diện. Có test xác
nhận viewer bị từ chối kể cả khi gọi thẳng service.

### Quyền Unity Catalog cần cấp cho service principal

| Muốn dùng | Cần cấp cho application ID của app |
|---|---|
| Thấy catalog | `USE CATALOG` (+ `BROWSE` để thấy đối tượng không có quyền đọc) |
| Đi vào schema | `USE SCHEMA` |
| Đọc và sửa quyền | `MANAGE` đúng phạm vi, hoặc quyền sở hữu |
| Gán thẻ | `APPLY TAG`, và `ASSIGN` trên governed tag (qua Account Access Control) |
| Chính sách ABAC | `MANAGE` trên điểm gắn, cộng `EXECUTE` trên UDF |
| Lineage và kiểm toán | `CAN USE` trên SQL Warehouse; `USE CATALOG` trên `system`; `USE SCHEMA` + `SELECT` trên `system.access` |

---

## 4. Trạng thái trên workspace đích

Đã deploy và kiểm tra trực tiếp trên
`https://adser-7474654536971820.aws.databricksapps.com`.

**Hoạt động đúng, đã xác minh trên workspace thật:**

- Đăng nhập và xác minh danh tính (`minh18052003@gmail.com`, vai trò Quản trị nền tảng).
- Phân biệt người dùng và danh tính thực thi trên mọi màn hình.
- Duyệt `catalog1` → `schema1` → `routes`; metadata, cột, thuộc tính.
- Xem quyền trực tiếp và quyền hiệu lực, phân biệt đúng nguồn kế thừa
  (“Kế thừa từ catalog”, cấp tại `catalog1`), có nút “Xem quyền tại cấp cha”.
- Ma trận khả năng: **23 chức năng ở trạng thái Sẵn sàng** sau khi dò.
- Giữ ngữ cảnh đối tượng khi chuyển trang bằng thanh điều hướng.
- Luồng cấp quyền hiển thị đúng ba bước, có giải thích tại chỗ.

**Chưa dùng được trên workspace này, và app báo đúng lý do:**

| Chức năng | Trạng thái | Cần làm gì |
|---|---|---|
| Lineage bảng và cột | Không đọc được `system.access.table_lineage` | Bật system schema `access` (account/metastore admin), rồi cấp `USE CATALOG` trên `system` và `USE SCHEMA`/`SELECT` trên `system.access` cho service principal, cùng `CAN USE` trên SQL Warehouse `8d61be86fba6dcbd` |
| Nhật ký kiểm toán | Như trên | Như trên |
| Tra cứu principal trong workspace | Không đọc được danh bạ | Service principal của app chỉ thấy chính nó trong SCIM cấp workspace. Dùng “Nhập trực tiếp” — Unity Catalog vẫn nhận đúng định danh. Muốn có danh bạ thì cần quyền SCIM rộng hơn; đây là quyết định bảo mật của bạn |

Ứng dụng **không** tự cấp các quyền trên. Mở quyền đọc `system.access` cho
service principal đồng nghĩa mọi người mở được app đều đọc được nhật ký kiểm
toán của workspace — đó là quyết định cần bạn cân nhắc.

### Ba lỗi phát hiện khi test thật và đã sửa

1. **Dòng quyền trống trong bảng quyền hiệu lực.** Databricks trả về quyền mà
   enum của SDK đang ghim chưa có (`READ METADATA`), SDK parse thành `None`.
   Nay giữ dòng lại và ghi “Không đọc được mã quyền” kèm giải thích — bỏ dòng đi
   sẽ báo thiếu quyền, sai lệch nguy hiểm hơn. Dòng này không bao giờ vào lệnh
   thu hồi.
2. **Cột “Loại principal” luôn là “Chưa xác định”.** Nay suy ra theo đúng cách
   Unity Catalog đọc chuỗi principal: có `@` là người dùng, UUID là service
   principal, còn lại là account group.
3. **Ma trận khả năng không kiểm tra gì.** Cơ chế dò đã có nhưng không ai gọi
   nên mọi dòng đều “Chưa kiểm tra”. Nay chạy tự động khi mở trang, và tách
   riêng phần cần SQL Warehouse vào một nút để không tự phát sinh chi phí.

---

## 5. Hợp đồng service

Mọi service kế thừa `ucg.services.base.Service`, nhận `Context` và không import
Streamlit.

| Module | Lớp | Đọc | Ghi |
|---|---|---|---|
| `assets.py` | `AssetService` | `catalogs`, `schemas`, `objects`, `search`, `detail` | `update_comment`, `update_owner` |
| `grants.py` | `GrantService` | `direct`, `effective`, `available_privileges` | `plan_change` → `apply` |
| `principals.py` | `PrincipalService` | `search`, `membership`, `describe` | — |
| `tags.py` | `TagService` | `list_tags`, `governed_tags`, `classification_config` | `plan_assign` / `plan_remove` / `plan_policy_*` → `apply` |
| `policies.py` | `PolicyService` | `list_for`, `get`, `legacy_protections` | `plan_create` / `plan_update` / `plan_delete` → `apply` |
| `storage.py` | `StorageService` | `storage_credentials`, `service_credentials`, `external_locations`, `bindings`, `validate`, `findings` | `plan_set_isolation` / `plan_bind` / `plan_unbind` → `apply` |
| `federation.py` | `FederationService` | `connections`, `connection_detail`, `dependency_check`, `findings` | `plan_update_owner` → `apply` |
| `sharing.py` | `SharingService` | `shares`, `share_detail`, `recipients`, `providers`, `findings` | `plan_add_object` / `plan_remove_object` / `plan_share_permission` → `apply` |
| `lineage.py` | `LineageService` | `available`, `upstream`, `downstream`, `column_lineage`, `external_relationships`, `impact_summary` | — |
| `audit_query.py` | `AuditService` | `available`, `recent`, `for_object`, `review` | — |
| `quality.py` | `QualityService` | `available`, `monitor_for`, `refreshes`, `constraints` | `plan_refresh` / `plan_delete_monitor` → `apply` |
| `access_requests.py` | `AccessRequestService` | `destinations` | `plan_set_destinations` / `plan_submit_request` → `apply` |
| `sql.py` | `SqlRunner` | `run` (chỉ template đã kiểm soát) | — |

Quy ước chung:

- Đọc trả về `Listing` (`items`, `completeness`, `observed_at`, `note`, `error`)
  hoặc một dataclass có `.ok` / `.error`.
- Ghi luôn đi hai bước: `plan_*(...) -> Plan`, rồi `apply(plan, …) -> Outcome`.
- `Plan` mang `preview` (danh sách `PreviewLine` loại
  `change` / `warning` / `unknown` / `info`), `before` (fingerprint) và `operation_id`.
- `Outcome.status` thuộc `applied` / `verified` / `failed` / `unknown` / `verify_failed`.
- Lỗi luôn là `GovernanceError(code, message, next_step, correlation_id)`.

---

## 6. Kết quả kiểm thử

**209 test, tất cả pass.**

| Tệp | Nội dung |
|---|---|
| `test_core.py` | Tên và chống SQL injection, danh mục privilege, phân trang, ánh xạ lỗi, vòng đời Plan |
| `test_authz.py` | Ma trận vai trò, viewer không ghi được khi gọi thẳng backend, phạm vi catalog, separation of duties |
| `test_grants.py` | Direct vs effective, hai hình dạng phản hồi khác nhau, trang rỗng có token, dải `max_results` bị cấm, gửi delta, thay đổi đồng thời, timeout, chống gửi lặp, quyền SDK không đọc được |
| `test_probes.py` | Dò khả năng phân biệt thiếu quyền với thiếu API, không tự chạy phần tốn tiền |
| `test_services_smoke.py` | Mọi service khởi tạo được, suy giảm có giải thích, không rò secret, không có API phê duyệt |
| `test_ui.py` | 15 trang render được, render được cả khi Databricks từ chối, viewer không thấy nút ghi, bản xem trước cũ bị huỷ |

### Phần đã kiểm thử giả lập (SDK giả)

Toàn bộ 209 test chạy với SDK giả tái tạo đúng **hình dạng phản hồi** thật.
Đây **không** phải bằng chứng chức năng chạy đúng trên Databricks.

### Phần đã kiểm thử trên workspace thật

Xem mục 4. Thực hiện bằng trình duyệt trên app đã deploy: đăng nhập, duyệt tài
sản, xem quyền trực tiếp và hiệu lực, kế thừa, dò khả năng, giữ ngữ cảnh, ba
bước của luồng cấp quyền.

### Phần CHƯA kiểm thử trên workspace thật

- **Chưa thực thi một lệnh cấp hoặc thu hồi quyền thật nào.** Luồng đã chạy tới
  bước xem trước; không bấm áp dụng để không thay đổi quyền thật khi chưa được
  yêu cầu.
- Thẻ, chính sách ABAC, chia sẻ dữ liệu, lưu trữ và ràng buộc, giám sát chất
  lượng, yêu cầu truy cập: đọc được nhưng chưa có đối tượng thật nào để thao tác.
- Lineage và kiểm toán: chưa chạy được do thiếu quyền system tables (mục 4).
- Chưa đăng nhập bằng `edison696996@gmail.com` (vai trò viewer) trên app thật;
  luồng chỉ đọc mới được kiểm thử bằng test tự động.

---

## 7. Việc nên làm tiếp

1. **Đặt `GOVERNANCE_ENVIRONMENT`.** Đang để trống nên app ghi “Môi trường: chưa
   khai báo”. Đặt `DEV` / `UAT` / `PROD` để bật dấu hiệu nhận biết, đặc biệt là
   cảnh báo PROD.
2. **Cân nhắc `GOVERNANCE_CATALOGS`.** Đang trống, nghĩa là thao tác ghi áp dụng
   được cho mọi catalog service principal quản lý được. App có cảnh báo, nhưng
   giới hạn phạm vi vẫn an toàn hơn.
3. **Quyết định về system tables.** Nếu cần lineage và nhật ký kiểm toán, làm
   theo bảng ở mục 4 — và cân nhắc rằng việc đó mở dữ liệu kiểm toán cho mọi
   người dùng app.
4. **Kiểm thử cấp và thu hồi thật** trên một catalog sandbox, theo danh sách 14
   bước trong [`DEPLOY.md`](DEPLOY.md) mục 4.
5. **Xem lại vai trò.** `edison696996@gmail.com` đang là `viewer`; đổi trong
   `GOVERNANCE_ROLES` rồi deploy lại nếu cần khác.
