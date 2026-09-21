# Giao diện (front end)

React / TypeScript / Next.js / Tailwind cho ứng dụng **Quản trị Unity Catalog**.

Thư mục này **không** được deploy. Nó biên dịch ra `governance-app/static/`, và
`governance-app/server.py` phục vụ thư mục đó.

---

## 1. Vì sao lại là bản build tĩnh

Databricks Apps chạy **đúng một lệnh** và chỉ cài gói Python. Không có Node lúc
chạy, cũng không có bước `npm install` khi deploy. Vì vậy:

```
web/  ──(npm run build)──>  governance-app/static/  ──(FastAPI phục vụ)──>  trình duyệt
         chạy trên máy dev                commit vào git
         hoặc trong CI
```

Hệ quả bắt buộc: **`governance-app/static/` phải được commit.** Đó là lý do
`.gitignore` ở thư mục gốc có ghi chú riêng cho nó.

---

## 2. Yêu cầu

- Node.js 20 trở lên
- Python 3.11+ với các gói trong `governance-app/requirements.txt`

---

## 3. Phát triển

Cần **hai** tiến trình. API và giao diện chạy riêng khi dev, nhưng cùng một
origin khi chạy thật.

```bash
# Cửa sổ 1 — API Python
cd governance-app
python server.py            # http://127.0.0.1:8000

# Cửa sổ 2 — giao diện
cd web
npm install
npm run dev                 # http://localhost:3000
```

`next.config.mjs` chuyển tiếp `/api/*` sang cổng 8000 **chỉ khi dev**. Khi build
thật, hai thứ dùng chung một origin nên không cần chuyển tiếp.

Muốn chạy ở máy cá nhân bằng hồ sơ Databricks CLI (chỉ đọc):

```bash
GOVERNANCE_LOCAL=true python server.py
```

---

## 4. Build

```bash
cd web
npm run build
```

Lệnh này chạy `next build` rồi `scripts/sync-static.mjs`, chép `out/` sang
`governance-app/static/`. Sau đó:

```bash
git add governance-app/static web
git commit -m "Build giao diện"
git push origin main
```

rồi bấm **Deploy** trong Databricks.

Kiểm tra trước khi commit:

```bash
npm run typecheck    # tsc --noEmit
npm run lint
```

---

## 5. Cấu trúc

```
src/
├── app/                     một thư mục = một route
│   ├── layout.tsx           font, theme, khung ứng dụng
│   ├── providers.tsx        React Query
│   ├── tim-tai-san/         tìm tài sản dữ liệu
│   ├── tai-san/             thông tin một tài sản
│   ├── quyen/               xem quyền
│   ├── thay-doi-quyen/      cấp / thu hồi quyền
│   ├── thao-tac/            nhật ký thao tác
│   └── he-thong/            khả năng và cấu hình
├── components/
│   ├── ui/                  primitive: button, field, select, table, alert…
│   ├── shell/               khung cố định: điều hướng, danh tính, môi trường
│   └── governance/          thành phần theo nghiệp vụ
└── lib/
    ├── types.ts             hợp đồng API, viết tay theo api/routers
    ├── api.ts               nơi duy nhất gọi máy chủ
    ├── queries.ts           React Query: khoá cache và invalidate
    └── selection.ts         đối tượng đang chọn, lưu trong URL
```

---

## 6. Vài quy ước quan trọng

**Đối tượng đang chọn nằm trong URL.** Nhờ vậy một màn hình có thể bookmark,
tải lại, hoặc dán vào ticket cho đồng nghiệp mở đúng chỗ đó.

**Ba mức thông báo, không hơn** (`components/ui/alert.tsx`):

| Mức | Dùng khi |
|---|---|
| `danger` | Thao tác có thể để lộ dữ liệu hoặc không hoàn tác được |
| `caution` | Kết quả sẽ khác với điều người dùng có thể đang nghĩ |
| `info` / `Note` | Bối cảnh. Chữ nhỏ, không phải hộp màu |

Giải thích dài đặt trong `<Explain>`, không đặt thành hộp cảnh báo.

**Không thứ gì truyền đạt bằng màu sắc đơn thuần.** Mọi trạng thái đều có chữ và
biểu tượng, để đọc được trên ảnh chụp đen trắng và với người mù màu.

**Ba lý do một danh sách rỗng phải khác nhau**
(`components/governance/states.tsx`): không có gì / không đủ quyền để xem / đọc
thất bại. Chỉ trường hợp đầu mới có nghĩa là đối tượng không tồn tại.

**Bản xem trước (plan) nằm ở máy chủ.** Trình duyệt chỉ giữ một `plan_id` mờ.
Nội dung thay đổi — principal, danh sách quyền, đối tượng — được máy chủ đọc lại
từ bản lưu của chính nó, nên client bị sửa đổi không thể áp dụng thứ khác với
thứ đã xem trước.

**Ô xác nhận không có nút sao chép.** Phải gõ lại tên đầy đủ là một bước dừng có
chủ đích; cho sao chép một cú nhấp sẽ vô hiệu hoá chính bước đó.

---

## 7. Phông chữ

`Be Vietnam Pro` cho chữ thường, `IBM Plex Mono` cho định danh.

Be Vietnam Pro được thiết kế riêng cho tiếng Việt: toàn bộ ứng dụng là tiếng
Việt và các bảng khá dày, nên dấu chồng (ế ự ỗ ằ) phải nằm đúng chỗ ở cỡ 13px.
IBM Plex Mono dùng cho tên đầy đủ, mã quyền và event ID — những thứ người dùng
đối chiếu từng ký tự với Catalog Explorer.

`next/font` tự host cả hai lúc build, nên ứng dụng đã deploy **không** gọi ra
CDN phông chữ. Điều này quan trọng với workspace bị chặn egress.
