# Runbook triển khai và vận hành

Tài liệu thao tác cho người deploy và trực vận hành ứng dụng
**Quản trị Unity Catalog**. Phần kiến trúc, vai trò và giới hạn nằm ở
[`README.md`](README.md).

---

## 1. Thông tin triển khai hiện tại

| Mục | Giá trị |
|---|---|
| Databricks App | `adser` |
| URL | `https://adser-7474654536971820.aws.databricksapps.com` |
| Workspace | `https://dbc-76001947-638a.cloud.databricks.com` |
| Nguồn | Git — `kaitosherlock/databricks-apps-cookbook-governance`, nhánh `main` |
| Source code path | `governance-app` |
| Auto-deploy | Tắt (phải bấm Deploy thủ công) |
| Service principal | `app-4ee9bw adser` |
| Compute | Medium (2 vCPU, 6 GB) |

---

## 2. Quy trình deploy

Vì app lấy nguồn từ Git, **code phải lên `main` trước khi deploy**.

### 2.1. Build giao diện trước (bắt buộc nếu có sửa `web/`)

Databricks Apps chỉ cài gói Python và **không chạy được `npm` lúc deploy**, nên
giao diện phải được build sẵn và commit.

```bash
cd web
npm install
npm run typecheck
npm run build
cd ..
```

`npm run build` ghi kết quả vào `governance-app/static/`. Nếu bỏ bước này sau khi
sửa `web/`, app sẽ deploy thành công nhưng vẫn chạy giao diện của lần build trước.

> Chạy `python governance-app/server.py` mà chưa build thì trang chủ trả về mã
> 503 kèm hướng dẫn, không phải lỗi 500 khó hiểu.

### 2.2. Đẩy lên Git

```bash
git add -A
git commit -m "<mô tả thay đổi>"
git push origin main
```

### 2.3. Chọn giao diện chạy

`governance-app/app.yaml` có đúng một dòng `command` đang hoạt động:

```yaml
command: ["python", "server.py"]          # giao diện React (mặc định)
# command: ["streamlit", "run", "app.py"] # giao diện Streamlit cũ
```

**Quy trình quay lui:** đổi chỗ hai dòng comment, commit, push, bấm Deploy. Giao
diện Streamlit cũ vẫn dùng chung backend `ucg/`, nên không mất chức năng nào ở
các màn hình nó có.

Sau đó trong Databricks:

1. Mở app → tab **Overview**.
2. Bấm **Deploy**.
3. Đợi lần lượt: *Stopped app* → *Source code downloaded* → *App spec loaded* →
   *Packages installed* → *App built* → *App started*.
4. Mở URL app và kiểm tra theo mục 4.

Hoặc bằng CLI:

```bash
databricks auth login --host https://dbc-76001947-638a.cloud.databricks.com
databricks apps deploy adser
```

> Đổi giá trị trong `governance-app/app.yaml` **chỉ có hiệu lực sau khi deploy lại**.
> Đây là nguyên nhân phổ biến nhất của “tôi đã bật ghi rồi mà app vẫn chỉ đọc”.

---

## 3. Rollback

Databricks Apps không có nút rollback. Vì app deploy từ Git, cách quay lui là
deploy lại một commit cũ:

```bash
git revert <commit-hash>        # tạo commit đảo ngược, an toàn hơn reset
git push origin main
```

rồi bấm **Deploy**. Xem lịch sử deploy ở tab **Deployments** để biết commit nào
đang chạy.

---

## 4. Danh sách kiểm tra sau deploy

Chạy lần lượt, không bỏ bước nào:

| # | Kiểm tra | Kỳ vọng |
|---|---|---|
| 1 | Mở URL app | Trang **Tìm tài sản dữ liệu** hiện ra |
| 2 | Thanh bên | Hiện đúng workspace, phạm vi catalog, vai trò của bạn |
| 3 | Thanh bên | Phân biệt rõ “Bạn đang đăng nhập” và “Yêu cầu được thực hiện bằng” |
| 4 | **Khả năng và cấu hình** → Khả năng | Không có dòng nào ở trạng thái “Chưa kiểm tra” bất thường |
| 5 | **Khả năng và cấu hình** → Cấu hình | Giá trị env đúng như `app.yaml` vừa deploy |
| 6 | Chọn một catalog → schema → bảng | Metadata và cột hiển thị |
| 7 | **Xem quyền** → tab *Quyền hiệu lực* | Cột “Nguồn quyền” phân biệt trực tiếp / kế thừa |
| 8 | Đối chiếu với Catalog Explorer | Danh sách quyền khớp |
| 9 | **Cấp / thu hồi quyền** với tài khoản viewer | Hiện “Chỉ đọc” kèm lý do cụ thể |
| 10 | **Cấp / thu hồi quyền** với tài khoản admin | Tạo được bản xem trước |
| 11 | Sửa một trường sau khi xem trước | Bản xem trước cũ bị huỷ |
| 12 | Áp dụng một grant thử nghiệm | Kết quả nêu principal, quyền, đối tượng và Event ID |
| 13 | Đối chiếu Catalog Explorer, rồi thu hồi | Trạng thái đọc lại khớp |
| 14 | **Thao tác gần đây** | Ghi đủ người thao tác, danh tính thực thi, lý do, kết quả |

---

## 5. Xử lý sự cố

### App không nhìn thấy catalog nào

Service principal chưa được cấp quyền. Trong Catalog Explorer, cấp cho
**application ID** của `app-4ee9bw adser`:

- `USE CATALOG` (+ `BROWSE`) trên catalog cần quản lý
- `USE SCHEMA` trên schema cần dùng
- `MANAGE` trên phạm vi cần quản trị grants, hoặc quyền sở hữu tương ứng

### Mọi thứ chỉ đọc dù đã bật ghi

Kiểm tra theo thứ tự — màn hình **Cấp / thu hồi quyền** sẽ nói cổng nào đang chặn:

1. `GOVERNANCE_ENABLE_WRITES` có phải `"true"` trong app.yaml **đã deploy** không?
2. Email của bạn có trong `GOVERNANCE_ROLES` với vai trò `access_admin` trở lên không?
3. Catalog có nằm trong `GOVERNANCE_CATALOGS` không (nếu biến này khác rỗng)?

### “Không đủ quyền” khi xem danh sách quyền

Databricks chỉ trả về đầy đủ ACL cho chủ sở hữu, người có `MANAGE`, hoặc metastore
admin. Danh tính khác chỉ thấy quyền của chính mình. Đây **không** phải lỗi ứng dụng.

### Lineage / Nhật ký kiểm toán báo chưa dùng được

Cần đủ bốn thứ:

1. `GOVERNANCE_WAREHOUSE_ID` đã đặt trong app.yaml và đã deploy lại.
2. Service principal có `CAN USE` trên SQL Warehouse đó.
3. System schema `access` đã được bật bởi account/metastore admin.
4. Service principal có `USE CATALOG` trên `system`, `USE SCHEMA` + `SELECT` trên
   `system.access`.

Màn hình sẽ nói cụ thể đang thiếu cái nào.

### Thao tác báo “Chưa xác định kết quả”

Yêu cầu đã gửi đi nhưng không nhận được xác nhận. **Không bấm lại.** Hãy:

1. Mở **Xem quyền** của đúng đối tượng đó.
2. Bấm **Làm mới dữ liệu**.
3. Đối chiếu trạng thái thật.
4. Chỉ thực hiện lại nếu thay đổi thực sự chưa được áp dụng.

Ứng dụng cố ý không tự thử lại để tránh thực hiện hai lần.

### Build thất bại ở bước *Packages installed*

`governance-app/requirements.txt` ghim `databricks-sdk==0.105.0`. Việc ghim là bắt
buộc: runtime của Databricks Apps cài sẵn `databricks-sdk 0.33.0`, thiếu toàn bộ
API governance mà app này dùng. Đừng gỡ ghim.

---

## 6. Xem log

- Trong app: tab **Logs**.
- Endpoint `/logz` của app.

Log của ứng dụng là JSON một dòng mỗi sự kiện, gồm `event_id`, `actor`,
`execution_identity`, `action`, `target`, `reason`, `status`. Không chứa token,
secret hay nội dung phản hồi thô.

> Databricks **không** giữ log sau khi compute của app dừng. Muốn lưu lâu dài,
> bật App telemetry để xuất sang Unity Catalog, hoặc dựa vào `system.access.audit`.

---

## 7. Thay đổi cấu hình thường gặp

| Muốn | Sửa | Nhớ |
|---|---|---|
| Thêm người quản trị | `GOVERNANCE_ROLES` | Deploy lại |
| Giới hạn phạm vi catalog | `GOVERNANCE_CATALOGS` | Deploy lại |
| Chuyển sang chỉ đọc toàn cục | `GOVERNANCE_ENABLE_WRITES=false` | Deploy lại |
| Gắn nhãn môi trường | `GOVERNANCE_ENVIRONMENT=PROD` | Deploy lại; PROD có cảnh báo riêng |
| Bật lineage / kiểm toán | `GOVERNANCE_WAREHOUSE_ID` | Deploy lại + cấp quyền ở mục 5 |

Chỉ cấp `CAN MANAGE` app cho người bảo trì tin cậy: ai sửa được `app.yaml` thì
sửa được ánh xạ vai trò.
