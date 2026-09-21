"""Delta Sharing: shares, recipients and providers.

This is the highest-consequence area in the app, because it moves data outside
the organisation. Three rules are enforced structurally:

* **Credentials never leave this module.** ``RecipientInfo.tokens[]``,
  ``activation_url``, ``sharing_code`` and ``ProviderInfo.recipient_profile*``
  are credentials, not identifiers - an activation link yields a one-time
  download that grants the recipient's access. Everything here returns derived
  booleans and dates instead, and nothing is logged or exported.
* **Protections do not travel.** A table with a *table-level* row filter or
  column mask cannot be shared at all; a table with ABAC policies can be shared
  only by an owner exempt from them, which means the recipient sees the data
  unfiltered. That is reported as a data-egress finding, never as "still
  protected".
* **A share rots silently.** Its contents depend on the share owner keeping the
  underlying privilege, so a shared object flips to PERMISSION_DENIED when an
  owner is offboarded. :meth:`SharingService.findings` surfaces that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..authz import Action
from ..errors import Code, GovernanceError
from ..naming import validate_component
from ..paging import take
from ..plans import Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

PAGE_LIMIT = 300

#: Recipients on these authentication types are outside Databricks.
OPEN_SHARING_AUTH = frozenset({"TOKEN", "OIDC_FEDERATION", "OAUTH_CLIENT_CREDENTIALS"})

#: Asset types an open-sharing (non-Databricks) recipient can actually receive.
TABULAR_ONLY = frozenset({"TABLE", "MATERIALIZED_VIEW", "STREAMING_TABLE", "VIEW",
                          "FOREIGN_TABLE", "SCHEMA"})

CREDENTIALS_NEVER_SHOWN = (
    "Ứng dụng không hiển thị link kích hoạt, token hay hồ sơ recipient. "
    "Đó là **thông tin xác thực**, không phải mã định danh: ai có link đều tải được "
    "tệp credential và truy cập dữ liệu. Link kích hoạt chỉ tải được một lần; "
    "nếu mất, cách duy nhất là xoay vòng token (thao tác này vô hiệu hoá credential cũ)."
)
PROTECTION_DOES_NOT_TRAVEL = (
    "Row filter, column mask và chính sách ABAC **không** đi theo dữ liệu được chia sẻ. "
    "Bảng có row filter/column mask gắn trực tiếp thì không chia sẻ được. Bảng có chính sách "
    "ABAC chỉ chia sẻ được bởi chủ share được miễn trừ khỏi chính sách — nghĩa là recipient "
    "nhận dữ liệu **chưa bị che**. Cơ chế lọc theo recipient được Databricks hỗ trợ là dynamic view."
)
OWNER_PRIVILEGE_DEPENDENCY = (
    "Nội dung của share phụ thuộc liên tục vào quyền của **chủ sở hữu share**. "
    "Nếu chủ share mất quyền trên tài sản, recipient mất truy cập và trạng thái tài sản "
    "chuyển sang PERMISSION_DENIED."
)
LIST_MAY_BE_PARTIAL = (
    "Khi thiếu quyền USE SHARE, Databricks trả về **chỉ các share do danh tính thực thi sở hữu** "
    "thay vì báo lỗi. Danh sách rỗng hoặc ngắn không phải bằng chứng không có share nào khác."
)
OPEN_SHARING_LIMIT = (
    "Recipient kiểu mở (token/OIDC) chỉ nhận được dữ liệu dạng bảng. Notebook, volume, "
    "mô hình và Genie agent yêu cầu chia sẻ Databricks-to-Databricks."
)
RECIPIENT_PROPERTIES_OVERRIDE = (
    "Thuộc tính recipient được **ghi đè**, không hợp nhất. Vì dynamic view lọc dữ liệu theo "
    "chính các thuộc tính này, một lần cập nhật thiếu cẩn thận có thể MỞ RỘNG phạm vi dữ liệu "
    "recipient nhìn thấy."
)


@dataclass
class ShareRef:
    name: str
    owner: str = ""
    comment: str = ""
    created: str = ""
    created_by: str = ""
    object_count: int = 0
    denied_count: int = 0

    def row(self) -> dict:
        return {
            "Share": self.name,
            "Chủ sở hữu": self.owner or "—",
            "Số tài sản": self.object_count,
            "Tài sản bị từ chối quyền": self.denied_count,
            "Mô tả": self.comment or "—",
        }


@dataclass
class SharedObject:
    name: str
    object_type: str = ""
    status: str = ""
    shared_as: str = ""
    added_by: str = ""
    added_at: str = ""
    comment: str = ""

    @property
    def active(self) -> bool:
        return (self.status or "ACTIVE").upper() == "ACTIVE"

    def row(self) -> dict:
        return {
            "Tài sản": self.name,
            "Loại": self.object_type or "—",
            "Chia sẻ dưới tên": self.shared_as or self.name,
            "Trạng thái": "Đang hoạt động" if self.active else "Chủ share không còn quyền",
            "Thêm bởi": self.added_by or "—",
            "Thêm lúc": self.added_at or "—",
        }


@dataclass
class RecipientRef:
    """Recipient facts with every credential field deliberately absent."""

    name: str
    authentication_type: str = ""
    owner: str = ""
    comment: str = ""
    activated: bool = False
    token_count: int = 0
    token_expires: str = ""
    token_expiring_soon: bool = False
    has_ip_allowlist: bool = False
    expiration_time: str = ""
    created: str = ""

    @property
    def external(self) -> bool:
        return (self.authentication_type or "").upper() in OPEN_SHARING_AUTH

    @property
    def kind_label(self) -> str:
        if (self.authentication_type or "").upper() == "DATABRICKS":
            return "Databricks-to-Databricks"
        return f"Chia sẻ mở ({self.authentication_type or 'không xác định'})"

    def row(self) -> dict:
        return {
            "Recipient": self.name,
            "Hình thức": self.kind_label,
            "Ngoài tổ chức": "Có" if self.external else "Không",
            "Đã kích hoạt": "Rồi" if self.activated else "Chưa",
            "Token": self.token_count,
            "Token hết hạn": self.token_expires or "—",
            "Chặn theo IP": "Có" if self.has_ip_allowlist else "Không",
            "Chủ sở hữu": self.owner or "—",
        }


@dataclass
class ProviderRef:
    name: str
    authentication_type: str = ""
    owner: str = ""
    comment: str = ""
    region: str = ""
    created: str = ""

    def row(self) -> dict:
        return {
            "Provider": self.name,
            "Hình thức": self.authentication_type or "—",
            "Chủ sở hữu": self.owner or "—",
            "Khu vực": self.region or "—",
        }


@dataclass
class ShareDetail:
    ref: ShareRef
    objects: list[SharedObject] = field(default_factory=list)
    recipients: list[dict] = field(default_factory=list)
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Finding:
    subject: str
    title: str
    detail: str
    severity: str = "review"

    def row(self) -> dict:
        return {"Đối tượng": self.subject, "Cần xem xét": self.title, "Giải thích": self.detail}


class SharingService(Service):
    capability_keys = ("sharing.shares", "sharing.recipients", "sharing.providers")

    CREDENTIALS_NEVER_SHOWN = CREDENTIALS_NEVER_SHOWN
    PROTECTION_DOES_NOT_TRAVEL = PROTECTION_DOES_NOT_TRAVEL
    OWNER_PRIVILEGE_DEPENDENCY = OWNER_PRIVILEGE_DEPENDENCY
    LIST_MAY_BE_PARTIAL = LIST_MAY_BE_PARTIAL
    OPEN_SHARING_LIMIT = OPEN_SHARING_LIMIT
    RECIPIENT_PROPERTIES_OVERRIDE = RECIPIENT_PROPERTIES_OVERRIDE

    # -- reads ------------------------------------------------------------
    def shares(self) -> Listing:
        def run() -> Listing:
            items, more = take(self.w.shares.list_shares(max_results=0), PAGE_LIMIT)
            refs = []
            for share in items:
                data = as_dict(share)
                objects = data.get("objects") or []
                refs.append(ShareRef(
                    name=data.get("name") or "",
                    owner=data.get("owner") or "",
                    comment=data.get("comment") or "",
                    created=millis_to_text(data.get("created_at")),
                    created_by=data.get("created_by") or "",
                    object_count=len(objects),
                    denied_count=sum(
                        1 for o in objects
                        if str(enum_value(as_dict(o).get("status")) or "ACTIVE").upper() != "ACTIVE"
                    ),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text(),
                           note=LIST_MAY_BE_PARTIAL)

        return self.listing(run)

    def share_detail(self, name: str) -> ShareDetail:
        name = validate_component(name, "Tên share")
        detail = ShareDetail(ref=ShareRef(name=name), observed_at=now_text())
        try:
            data = as_dict(self.w.shares.get(name=name, include_shared_data=True))
        except Exception as exc:
            from ..errors import translate
            detail.error = translate(exc)
            return detail

        detail.ref = ShareRef(
            name=data.get("name") or name,
            owner=data.get("owner") or "",
            comment=data.get("comment") or "",
            created=millis_to_text(data.get("created_at")),
            created_by=data.get("created_by") or "",
        )
        for obj in data.get("objects") or []:
            item = as_dict(obj)
            detail.objects.append(SharedObject(
                name=item.get("name") or "",
                object_type=enum_value(item.get("data_object_type")),
                status=enum_value(item.get("status")) or "ACTIVE",
                shared_as=item.get("shared_as") or item.get("string_shared_as") or "",
                added_by=item.get("added_by") or "",
                added_at=millis_to_text(item.get("added_at")),
                comment=item.get("comment") or "",
            ))
        detail.ref.object_count = len(detail.objects)
        detail.ref.denied_count = sum(1 for o in detail.objects if not o.active)
        detail.recipients = self._share_permission_rows(name)
        return detail

    def _share_permission_rows(self, name: str) -> list[dict]:
        try:
            response = self.w.shares.share_permissions(name=name, max_results=0)
        except Exception:
            # A refused read is reported by the caller's own error path; here an
            # empty list is acceptable because the note explains the limit.
            return []
        rows = []
        for assignment in (getattr(response, "privilege_assignments", None) or []):
            data = as_dict(assignment)
            rows.append({
                "Recipient": data.get("principal") or "",
                "Quyền": ", ".join(enum_value(p) for p in (data.get("privileges") or [])),
            })
        return rows

    def recipients(self) -> Listing:
        def run() -> Listing:
            items, more = take(self.w.recipients.list(max_results=0), PAGE_LIMIT)
            refs = [self._recipient_ref(as_dict(r)) for r in items]
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text(),
                           note=CREDENTIALS_NEVER_SHOWN)

        return self.listing(run)

    def recipient_detail(self, name: str) -> RecipientRef:
        name = validate_component(name, "Tên recipient")
        data = as_dict(self.read(lambda: self.w.recipients.get(name=name)))
        return self._recipient_ref(data)

    def _recipient_ref(self, data: dict) -> RecipientRef:
        """Build a recipient view with every credential field dropped.

        ``tokens[].activation_url``, ``activation_url`` and ``sharing_code`` are
        read here only to derive counts and dates; the values themselves never
        enter the returned object.
        """
        tokens = [as_dict(t) for t in (data.get("tokens") or [])]
        expiries = [t.get("expiration_time") for t in tokens if t.get("expiration_time")]
        soonest = min(expiries) if expiries else None
        expiring_soon = False
        if soonest:
            try:
                from datetime import datetime, timedelta, timezone
                when = datetime.fromtimestamp(int(soonest) / 1000, tz=timezone.utc)
                expiring_soon = when - datetime.now(timezone.utc) < timedelta(days=30)
            except Exception:
                expiring_soon = False
        ip_list = as_dict(data.get("ip_access_list") or {})
        return RecipientRef(
            name=data.get("name") or "",
            authentication_type=enum_value(data.get("authentication_type")),
            owner=data.get("owner") or "",
            comment=data.get("comment") or "",
            activated=bool(data.get("activated")),
            token_count=len(tokens),
            token_expires=millis_to_text(soonest),
            token_expiring_soon=expiring_soon,
            has_ip_allowlist=bool(ip_list.get("allowed_ip_addresses")),
            expiration_time=millis_to_text(data.get("expiration_time")),
            created=millis_to_text(data.get("created_at")),
        )

    def providers(self) -> Listing:
        def run() -> Listing:
            items, more = take(self.w.providers.list(max_results=0), PAGE_LIMIT)
            refs = []
            for provider in items:
                data = as_dict(provider)
                # recipient_profile / recipient_profile_str carry a bearer token.
                refs.append(ProviderRef(
                    name=data.get("name") or "",
                    authentication_type=enum_value(data.get("authentication_type")),
                    owner=data.get("owner") or "",
                    comment=data.get("comment") or "",
                    region=data.get("region") or "",
                    created=millis_to_text(data.get("created_at")),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text(),
                           note=CREDENTIALS_NEVER_SHOWN)

        return self.listing(run)

    def provider_shares(self, name: str) -> Listing:
        name = validate_component(name, "Tên provider")

        def run() -> Listing:
            items, more = take(self.w.providers.list_shares(name=name, max_results=0), PAGE_LIMIT)
            rows = [{"Share": as_dict(s).get("name") or ""} for s in items]
            return Listing(rows, "truncated" if more else "complete", now_text())

        return self.listing(run)

    # -- planning ---------------------------------------------------------
    def plan_remove_object(self, share: str, object_name: str, reason: str) -> Plan:
        """Remove one asset from a share. Narrowing, but still a change."""
        self.authz.require(Action.MANAGE_SHARING)
        share = validate_component(share, "Tên share")
        reason = _reason(reason)
        detail = self.share_detail(share)
        if not detail.ok:
            raise detail.error
        present = {o.name for o in detail.objects}
        if object_name not in present:
            raise GovernanceError(Code.NOT_FOUND,
                                  f"Share `{share}` không chứa tài sản `{object_name}`.")

        plan = self._plan(
            share, reason,
            payload={"operation": "remove_object", "share": share, "object": object_name},
            before=_share_fingerprint(detail),
            summary=f"Gỡ {object_name} khỏi share {share}",
        )
        plan.preview = [
            PreviewLine(f"Gỡ tài sản `{object_name}` khỏi share `{share}`", "change"),
            self._identity_line(),
            PreviewLine(
                "Mọi recipient của share này sẽ mất truy cập tới tài sản đó.",
                "warning",
            ),
            PreviewLine(
                "Recipient nào chịu ảnh hưởng: xem danh sách recipient của share. "
                "Ứng dụng không ước lượng số người dùng cuối phía recipient.",
                "unknown",
            ),
        ]
        return plan

    def plan_add_object(self, share: str, object_name: str, object_type: str,
                        reason: str, *, protections: dict | None = None) -> Plan:
        """Add an asset to a share. This is the data-egress direction."""
        self.authz.require(Action.MANAGE_SHARING)
        share = validate_component(share, "Tên share")
        reason = _reason(reason)
        object_type = (object_type or "TABLE").upper()
        parts = [p for p in (object_name or "").split(".") if p]
        if len(parts) not in (2, 3):
            raise GovernanceError(
                Code.INVALID_NAME,
                "Nhập tên đầy đủ của tài sản, ví dụ catalog.schema.table.",
            )
        for part in parts:
            validate_component(part)

        detail = self.share_detail(share)
        if not detail.ok:
            raise detail.error
        if object_name in {o.name for o in detail.objects}:
            raise GovernanceError(Code.ALREADY_SATISFIED,
                                  "Tài sản này đã có trong share.")

        protections = protections or {}
        if protections.get("row_filter") or protections.get("column_masks"):
            raise GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                "Databricks không cho chia sẻ bảng có row filter hoặc column mask gắn "
                "trực tiếp. Đây là giới hạn của sản phẩm, không phải vấn đề quyền.",
            )

        external = [r for r in self._recipients_of(share) if r.external]
        plan = self._plan(
            share, reason,
            payload={"operation": "add_object", "share": share, "object": object_name,
                     "object_type": object_type},
            before=_share_fingerprint(detail),
            summary=f"Thêm {object_name} vào share {share}",
        )
        plan.preview = [
            PreviewLine(f"Thêm `{object_name}` ({object_type}) vào share `{share}`", "change"),
            self._identity_line(),
            PreviewLine(PROTECTION_DOES_NOT_TRAVEL, "warning"),
            PreviewLine(OWNER_PRIVILEGE_DEPENDENCY, "warning"),
        ]
        if external:
            names = ", ".join(r.name for r in external[:5])
            plan.preview.insert(1, PreviewLine(
                f"**Dữ liệu sẽ ra ngoài phạm vi tổ chức.** Share này có "
                f"{len(external)} recipient chia sẻ mở: {names}.",
                "warning",
            ))
            if object_type not in TABULAR_ONLY:
                plan.preview.append(PreviewLine(
                    OPEN_SHARING_LIMIT + f" Loại `{object_type}` có thể không đến được "
                    "các recipient đó.", "warning",
                ))
        else:
            plan.preview.append(PreviewLine(
                "Chưa xác định được recipient nào ngoài tổ chức từ dữ liệu đọc được. "
                "Hãy kiểm tra danh sách recipient trước khi áp dụng.",
                "unknown",
            ))
        return plan

    def plan_share_permission(self, share: str, recipient: str, grant: bool,
                              reason: str) -> Plan:
        """Give or remove a recipient's SELECT on a share."""
        self.authz.require(Action.MANAGE_SHARING)
        share = validate_component(share, "Tên share")
        recipient = validate_component(recipient, "Tên recipient")
        reason = _reason(reason)

        detail = self.share_detail(share)
        if not detail.ok:
            raise detail.error
        current = {row["Recipient"] for row in detail.recipients}
        if grant and recipient in current:
            raise GovernanceError(Code.ALREADY_SATISFIED,
                                  "Recipient đã có quyền trên share này.")
        if not grant and recipient not in current:
            raise GovernanceError(Code.NOT_FOUND,
                                  "Recipient chưa có quyền trên share này.")

        info = None
        try:
            info = self.recipient_detail(recipient)
        except GovernanceError:
            info = None

        plan = self._plan(
            share, reason,
            payload={"operation": "share_permission", "share": share,
                     "recipient": recipient, "grant": bool(grant)},
            before=_share_fingerprint(detail),
            summary=("Cấp" if grant else "Thu hồi") + f" quyền share {share} cho {recipient}",
        )
        plan.preview = [
            PreviewLine(
                ("Cấp SELECT trên share " if grant else "Thu hồi SELECT trên share ")
                + f"`{share}` cho recipient `{recipient}`",
                "change",
            ),
            PreviewLine(f"Share đang chứa {len(detail.objects)} tài sản.", "info"),
            self._identity_line(),
        ]
        if grant:
            if info is not None and info.external:
                plan.preview.insert(1, PreviewLine(
                    f"**Mở dữ liệu ra ngoài tổ chức.** Recipient `{recipient}` dùng "
                    f"{info.kind_label}.",
                    "warning",
                ))
            plan.preview.append(PreviewLine(PROTECTION_DOES_NOT_TRAVEL, "warning"))
        else:
            plan.preview.append(PreviewLine(
                "Recipient sẽ mất truy cập tới toàn bộ tài sản trong share này.",
                "warning",
            ))
        return plan

    def _recipients_of(self, share: str) -> list[RecipientRef]:
        detail = self.share_detail(share)
        if not detail.ok:
            return []
        out = []
        for row in detail.recipients:
            name = row.get("Recipient") or ""
            if not name:
                continue
            try:
                out.append(self.recipient_detail(name))
            except GovernanceError:
                continue
        return out

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, confirmation: str):
        plan.require_confirmation(confirmation)
        payload = plan.payload
        share = payload["share"]

        def revalidate() -> str:
            fresh = self.share_detail(share)
            if not fresh.ok:
                raise fresh.error
            return _share_fingerprint(fresh)

        operation = payload["operation"]
        if operation in ("add_object", "remove_object"):
            do = self._object_call(payload)
        elif operation == "share_permission":
            do = self._permission_call(payload)
        else:
            raise GovernanceError(Code.INVALID_INPUT, "Bản xem trước không hợp lệ.")

        def verify() -> dict:
            after = self.share_detail(share)
            if not after.ok:
                raise after.error
            return {
                "share": share,
                "objects": [o.name for o in after.objects],
                "recipients": [r["Recipient"] for r in after.recipients],
                "inactive_objects": [o.name for o in after.objects if not o.active],
            }

        return self.execute(plan, None, do, revalidate=revalidate, verify=verify,
                            action_label=plan.summary)

    def _object_call(self, payload: dict):
        from databricks.sdk.service.sharing import (
            SharedDataObject, SharedDataObjectDataObjectType, SharedDataObjectUpdate,
            SharedDataObjectUpdateAction,
        )

        adding = payload["operation"] == "add_object"
        try:
            object_type = SharedDataObjectDataObjectType(payload.get("object_type", "TABLE"))
        except ValueError:
            object_type = None
        update = SharedDataObjectUpdate(
            action=(SharedDataObjectUpdateAction.ADD if adding
                    else SharedDataObjectUpdateAction.REMOVE),
            data_object=SharedDataObject(
                name=payload["object"],
                data_object_type=object_type if adding else None,
            ),
        )

        def do():
            self.w.shares.update(name=payload["share"], updates=[update])

        return do

    def _permission_call(self, payload: dict):
        from databricks.sdk.service.sharing import PermissionsChange, Privilege

        grant = payload["grant"]
        change = PermissionsChange(
            principal=payload["recipient"],
            add=[Privilege.SELECT] if grant else None,
            remove=None if grant else [Privilege.SELECT],
        )

        def do():
            self.w.shares.update_permissions(name=payload["share"], changes=[change])

        return do

    # -- findings ---------------------------------------------------------
    def findings(self) -> Listing:
        """Signals worth reviewing. Not compliance verdicts, and never auto-fixed."""
        found: list[Finding] = []
        partial = False

        share_list = self.shares()
        if share_list.ok:
            for ref in share_list.items:
                if ref.denied_count:
                    found.append(Finding(
                        ref.name,
                        f"{ref.denied_count} tài sản không còn truy cập được",
                        "Chủ sở hữu share đã mất quyền trên các tài sản này, nên recipient "
                        "không dùng được. " + OWNER_PRIVILEGE_DEPENDENCY,
                    ))
                if not ref.owner:
                    found.append(Finding(ref.name, "Chưa xác định chủ sở hữu share",
                                         "Không đọc được chủ sở hữu của share."))
        else:
            partial = True

        recipient_list = self.recipients()
        if recipient_list.ok:
            for ref in recipient_list.items:
                if ref.external:
                    found.append(Finding(
                        ref.name, "Recipient ngoài tổ chức",
                        f"{ref.kind_label}. Dữ liệu chia sẻ tới recipient này ra ngoài "
                        "phạm vi kiểm soát của workspace.",
                    ))
                if ref.token_expiring_soon:
                    found.append(Finding(
                        ref.name, "Token sắp hết hạn",
                        f"Token hết hạn vào {ref.token_expires}. Lên kế hoạch xoay vòng "
                        "trước thời điểm đó để tránh gián đoạn.",
                    ))
                if ref.external and not ref.has_ip_allowlist:
                    found.append(Finding(
                        ref.name, "Chưa giới hạn theo IP",
                        "Recipient ngoài tổ chức nhưng chưa cấu hình danh sách IP cho phép.",
                    ))
        else:
            partial = True

        return Listing(
            found,
            "partial_permission" if partial else "complete",
            now_text(),
            note=(
                "Đây là các điểm cần xem xét, KHÔNG phải kết luận vi phạm. "
                + LIST_MAY_BE_PARTIAL
            ),
        )

    # -- helpers ----------------------------------------------------------
    def _plan(self, share: str, reason: str, *, payload: dict, before: str,
              summary: str) -> Plan:
        return Plan(
            action=Action.MANAGE_SHARING,
            target_key=f"share:{share}",
            target_name=share,
            target_type="Share",
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload=payload,
            before=before,
            reason=reason,
            summary=summary,
        )

    def _identity_line(self) -> PreviewLine:
        from ..identity import ExecutionIdentity
        return PreviewLine(
            "Yêu cầu sẽ được gửi bằng: "
            + ExecutionIdentity.LABELS.get(self.ctx.execution_identity,
                                           self.ctx.execution_identity),
            "info",
        )


def _share_fingerprint(detail: ShareDetail) -> str:
    return fingerprint({
        "objects": sorted((o.name, o.object_type, o.status) for o in detail.objects),
        "recipients": sorted(r.get("Recipient", "") for r in detail.recipients),
        "owner": detail.ref.owner,
    })


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
    if len(reason) > 500:
        raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
    return reason
