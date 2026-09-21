"""Native access requests (RFA).

The most important thing this module does is refuse to pretend. Databricks'
Request-for-Access API has exactly three operations - read destinations,
replace destinations, submit requests. There is **no** endpoint to list pending
requests, **no** endpoint to approve or deny, and Databricks never executes the
grant itself: an approver follows the notification into the Databricks UI and
performs an ordinary grant by hand.

So there is no approve method here, not even one that raises. Building an
approval queue on top of this API would be inventing a workflow the platform
does not have, and a governance tool that misrepresents its own guarantees is
worse than one that does less.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from ..authz import Action
from ..errors import Code, GovernanceError
from ..naming import Target, validate_component
from ..plans import Plan, PreviewLine, fingerprint
from .base import Service, as_dict, enum_value, now_text

#: Documented ceilings. Enforced locally so a rejection is explained in
#: Vietnamese rather than arriving as an opaque 400.
MAX_EMAIL_DESTINATIONS = 5
MAX_EXTERNAL_DESTINATIONS = 5
MAX_REQUESTS_PER_BATCH = 30
MAX_SECURABLES_PER_PRINCIPAL = 30
MAX_COMMENT = 200

EMAIL = "EMAIL"
URL = "URL"
EXTERNAL_TYPES = frozenset({"SLACK", "MICROSOFT_TEAMS", "GENERIC_WEBHOOK"})

DESTINATION_LABELS = {
    EMAIL: "Email",
    URL: "Chuyển tới hệ thống ngoài (URL)",
    "SLACK": "Slack",
    "MICROSOFT_TEAMS": "Microsoft Teams",
    "GENERIC_WEBHOOK": "Webhook",
}

NO_APPROVAL_API = (
    "Databricks **không** cung cấp API để liệt kê, phê duyệt hoặc từ chối yêu cầu truy cập, "
    "và cũng không tự động cấp quyền khi được duyệt. Người phê duyệt mở thông báo, vào giao "
    "diện Databricks và cấp quyền thủ công. Vì vậy ứng dụng này chỉ cấu hình nơi nhận yêu cầu "
    "và gửi yêu cầu — không có hàng đợi phê duyệt."
)
NO_REQUEST_ID = (
    "API gửi yêu cầu **không trả về mã yêu cầu**, và không có gì để tra cứu hay đối soát về sau. "
    "Ứng dụng tự sinh một mã đối chiếu và nhúng vào phần ghi chú; đây là cơ chế của ứng dụng, "
    "không phải của Databricks, và Databricks không phản hồi lại mã đó."
)
ROUTING_IS_SERVER_SIDE = (
    "Danh sách dưới đây chỉ là nơi nhận được đặt **trực tiếp** trên đối tượng này. "
    "Databricks định tuyến theo bốn mức: đối tượng → chủ sở hữu → đối tượng cha gần nhất có "
    "cấu hình → cấp metastore. Việc danh sách rỗng **không** có nghĩa là yêu cầu không đến ai."
)
REPLACE_SEMANTICS = (
    "Cập nhật nơi nhận sẽ **thay thế toàn bộ** danh sách hiện có. Ứng dụng luôn đọc trước, "
    "hợp nhất, rồi gửi đủ danh sách để không xoá nhầm cấu hình của nhóm khác."
)
URL_EXCLUSIVE = (
    "Nơi nhận kiểu URL loại trừ mọi kiểu khác và **tắt hẳn** biểu mẫu yêu cầu trong Databricks: "
    "người dùng sẽ bị chuyển sang hệ thống ngoài. Đặt URL ở catalog sẽ tắt yêu cầu truy cập "
    "cho mọi đối tượng bên dưới chưa có cấu hình riêng."
)
PREREQUISITE_FANOUT = (
    "Yêu cầu SELECT sẽ tự sinh thêm yêu cầu cho USE CATALOG / USE SCHEMA còn thiếu, và các "
    "yêu cầu đó được gửi tới người duyệt của **đối tượng cha**. Một lần gửi có thể thông báo "
    "cho nhiều nhóm khác nhau."
)
APP_NOT_A_NOTIFIER = (
    "Ứng dụng không tự gửi email, Slack hay Teams. Ứng dụng chỉ yêu cầu Databricks thông báo "
    "tới những nơi nhận đã được cấu hình."
)
NO_TIME_BOUND_GRANTS = (
    "Ứng dụng **không** cung cấp quyền có thời hạn. Unity Catalog không có cơ chế GRANT kèm hạn "
    "sử dụng, còn Databricks Apps không giữ được trạng thái hay tiến trình nền qua các lần khởi "
    "động lại. Muốn có quyền hết hạn tự động thì phải xây scheduler và nơi lưu trữ bền vững "
    "(ví dụ Lakeflow Job cộng bảng Unity Catalog), không làm trong tiến trình của app."
)
WEBHOOK_ID_UNVERIFIED = (
    "Với Slack / Teams / Webhook, Databricks không công bố định dạng của mã nơi nhận. "
    "Hãy đối chiếu với phần Notification destinations của workspace trước khi dùng."
)


@dataclass
class Destination:
    destination_type: str
    destination_id: str = ""
    special: str = ""

    @property
    def type_label(self) -> str:
        if self.special:
            return "Chủ sở hữu đối tượng"
        return DESTINATION_LABELS.get(self.destination_type, self.destination_type)

    def row(self) -> dict:
        return {
            "Kiểu": self.type_label,
            "Địa chỉ / mã": self.destination_id or self.special or "—",
        }


@dataclass
class DestinationView:
    target: Target
    destinations: list[Destination] = field(default_factory=list)
    any_hidden: bool = False
    inherited_from: str = ""
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def configured_here(self) -> bool:
        return bool(self.destinations)

    def rows(self) -> list[dict]:
        return [d.row() for d in self.destinations]


@dataclass
class RequestDraft:
    """One access request, before it is sent."""

    principal: str
    target: Target
    privileges: tuple[str, ...]
    comment: str
    correlation_id: str = field(default_factory=lambda: uuid4().hex[:8])

    @property
    def full_comment(self) -> str:
        """Comment with the app's own correlation marker, within the 200-char cap."""
        marker = f"[UCG:{self.correlation_id}] "
        room = MAX_COMMENT - len(marker)
        return marker + self.comment[:room]


class AccessRequestService(Service):
    capability_keys = ("rfa.destinations", "rfa.submit")

    NO_APPROVAL_API = NO_APPROVAL_API
    NO_REQUEST_ID = NO_REQUEST_ID
    ROUTING_IS_SERVER_SIDE = ROUTING_IS_SERVER_SIDE
    REPLACE_SEMANTICS = REPLACE_SEMANTICS
    URL_EXCLUSIVE = URL_EXCLUSIVE
    PREREQUISITE_FANOUT = PREREQUISITE_FANOUT
    APP_NOT_A_NOTIFIER = APP_NOT_A_NOTIFIER
    NO_TIME_BOUND_GRANTS = NO_TIME_BOUND_GRANTS
    WEBHOOK_ID_UNVERIFIED = WEBHOOK_ID_UNVERIFIED

    # -- reads ------------------------------------------------------------
    def destinations(self, target: Target) -> DestinationView:
        self.authz.require_scope(target.catalog)
        view = DestinationView(target=target, observed_at=now_text())
        try:
            response = self.w.rfa.get_access_request_destinations(
                securable_type=target.securable_type.lower(),
                full_name=target.full_name,
            )
        except Exception as exc:
            from ..errors import translate
            view.error = translate(exc)
            return view

        data = as_dict(response)
        view.any_hidden = bool(data.get("are_any_destinations_hidden"))
        source = as_dict(data.get("destination_source_securable") or {})
        view.inherited_from = source.get("full_name") or ""
        for entry in data.get("destinations") or []:
            item = as_dict(entry)
            view.destinations.append(Destination(
                destination_type=enum_value(item.get("destination_type")),
                destination_id=item.get("destination_id") or "",
                special=enum_value(item.get("special_destination")),
            ))
        return view

    # -- planning: routing ------------------------------------------------
    def plan_set_destinations(self, target: Target, destinations: list[dict],
                              reason: str) -> Plan:
        """Replace the destination list on one securable, read-modify-write."""
        self.authz.require(Action.MANAGE_REQUEST_ROUTING, target)
        reason = _reason(reason)

        parsed = [self.validate_destination(d) for d in (destinations or [])]
        self._check_destination_set(parsed)

        current = self.destinations(target)
        if not current.ok:
            raise current.error
        before_set = _destination_key(current.destinations)
        after_set = _destination_key(parsed)
        if before_set == after_set:
            raise GovernanceError(Code.ALREADY_SATISFIED,
                                  "Danh sách nơi nhận không thay đổi.")

        plan = Plan(
            action=Action.MANAGE_REQUEST_ROUTING,
            target_key=f"rfa:{target.key}",
            target_name=target.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": "destinations",
                "securable_type": target.securable_type.lower(),
                "full_name": target.full_name,
                "destinations": [
                    {"type": d.destination_type, "id": d.destination_id} for d in parsed
                ],
            },
            before=fingerprint(sorted(before_set)),
            reason=reason,
            summary=f"Cập nhật nơi nhận yêu cầu truy cập cho {target.full_name}",
        )
        removed = [d for d in current.destinations
                   if (d.destination_type, d.destination_id) not in after_set]
        plan.preview = [
            PreviewLine(f"Đặt lại danh sách nơi nhận yêu cầu cho `{target.full_name}`", "change"),
            PreviewLine(
                "Danh sách sau khi áp dụng: "
                + (", ".join(f"{d.type_label} {d.destination_id}" for d in parsed)
                   or "(rỗng)"),
                "change",
            ),
            self._identity_line(),
            PreviewLine(REPLACE_SEMANTICS, "warning"),
        ]
        if removed:
            plan.preview.append(PreviewLine(
                "Sẽ bị gỡ khỏi danh sách: "
                + ", ".join(f"{d.type_label} {d.destination_id}" for d in removed),
                "warning",
            ))
        if any(d.destination_type == URL for d in parsed):
            plan.preview.append(PreviewLine(URL_EXCLUSIVE, "warning"))
        if any(d.destination_type in EXTERNAL_TYPES for d in parsed):
            plan.preview.append(PreviewLine(WEBHOOK_ID_UNVERIFIED, "unknown"))
        plan.preview.append(PreviewLine(ROUTING_IS_SERVER_SIDE, "info"))
        return plan

    def validate_destination(self, raw: dict) -> Destination:
        kind = str(raw.get("type") or "").strip().upper()
        value = str(raw.get("id") or "").strip()
        if kind not in DESTINATION_LABELS:
            raise GovernanceError(Code.INVALID_INPUT,
                                  f"Kiểu nơi nhận không hợp lệ: {kind or '(trống)'}.")
        if not value:
            raise GovernanceError(Code.INVALID_INPUT,
                                  f"Thiếu địa chỉ hoặc mã cho nơi nhận kiểu {kind}.")
        if any(ord(c) < 32 for c in value):
            raise GovernanceError(Code.INVALID_INPUT, "Giá trị nơi nhận chứa ký tự không hợp lệ.")
        if kind == EMAIL and ("@" not in value or " " in value):
            raise GovernanceError(Code.INVALID_INPUT, f"Địa chỉ email không hợp lệ: {value}.")
        if kind == URL and not value.lower().startswith("https://"):
            # A client-supplied endpoint is never accepted loosely.
            raise GovernanceError(Code.INVALID_INPUT,
                                  "Địa chỉ URL phải bắt đầu bằng https://.")
        return Destination(destination_type=kind, destination_id=value)

    def _check_destination_set(self, parsed: list[Destination]):
        if any(d.destination_type == URL for d in parsed) and len(parsed) > 1:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Nơi nhận kiểu URL loại trừ mọi kiểu khác. " + URL_EXCLUSIVE,
            )
        emails = sum(1 for d in parsed if d.destination_type == EMAIL)
        if emails > MAX_EMAIL_DESTINATIONS:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Tối đa {MAX_EMAIL_DESTINATIONS} địa chỉ email cho một đối tượng.",
            )
        external = sum(1 for d in parsed if d.destination_type in EXTERNAL_TYPES)
        if external > MAX_EXTERNAL_DESTINATIONS:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Tối đa {MAX_EXTERNAL_DESTINATIONS} nơi nhận bên ngoài cho một đối tượng.",
            )
        seen = set()
        for d in parsed:
            key = (d.destination_type, d.destination_id)
            if key in seen:
                raise GovernanceError(Code.INVALID_INPUT,
                                      f"Nơi nhận bị trùng: {d.destination_id}.")
            seen.add(key)

    # -- planning: submitting a request ----------------------------------
    def plan_submit_request(self, target: Target, principal: str, privileges: list[str],
                            comment: str) -> Plan:
        """Ask the object's approvers for access. No approval happens here."""
        self.authz.require(Action.SUBMIT_REQUEST, target)
        principal = (principal or "").strip()
        if not principal:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập principal sẽ nhận quyền.")
        chosen = tuple(sorted({p.strip().upper() for p in privileges if p and p.strip()}))
        if not chosen:
            raise GovernanceError(Code.INVALID_INPUT, "Chọn ít nhất một quyền cần xin.")
        comment = (comment or "").strip()
        if not comment:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do cần quyền truy cập.")

        draft = RequestDraft(principal=principal, target=target,
                             privileges=chosen, comment=comment)
        if len(draft.full_comment) > MAX_COMMENT:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Ghi chú tối đa {MAX_COMMENT} ký tự kể cả mã đối chiếu của ứng dụng.",
            )

        routing = self.destinations(target)
        plan = Plan(
            action=Action.SUBMIT_REQUEST,
            target_key=f"rfa-submit:{target.key}",
            target_name=target.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": "submit",
                "securable_type": target.securable_type.lower(),
                "full_name": target.full_name,
                "principal": principal,
                "privileges": list(chosen),
                "comment": draft.full_comment,
                "correlation_id": draft.correlation_id,
            },
            # Submitting does not depend on prior state; the fingerprint binds
            # the plan to its own content so an edited draft cannot be sent.
            before=fingerprint({"principal": principal, "privileges": list(chosen),
                                "comment": draft.full_comment}),
            reason=comment,
            summary=f"Gửi yêu cầu {', '.join(chosen)} trên {target.full_name}",
        )
        plan.preview = [
            PreviewLine(f"Xin quyền: {', '.join(chosen)}", "change"),
            PreviewLine(f"Cho principal: {principal}", "change"),
            PreviewLine(f"Trên đối tượng: {target.full_name}", "change"),
            self._identity_line(),
            PreviewLine(NO_APPROVAL_API, "warning"),
            PreviewLine(NO_REQUEST_ID, "warning"),
            PreviewLine(APP_NOT_A_NOTIFIER, "info"),
        ]
        if "SELECT" in chosen:
            plan.preview.append(PreviewLine(PREREQUISITE_FANOUT, "info"))
        if routing.ok and not routing.configured_here:
            plan.preview.append(PreviewLine(
                "Đối tượng này chưa có nơi nhận đặt trực tiếp. " + ROUTING_IS_SERVER_SIDE,
                "unknown",
            ))
        plan.preview.append(PreviewLine(
            f"Mã đối chiếu của ứng dụng: {draft.correlation_id} "
            "(chỉ dùng để tra trong nhật ký thao tác của ứng dụng).",
            "info",
        ))
        return plan

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, target: Target, confirmation: str | None = None):
        operation = plan.payload.get("operation")
        if operation == "destinations":
            if confirmation is not None:
                plan.require_confirmation(confirmation)
            return self._apply_destinations(plan, target)
        if operation == "submit":
            return self._apply_submit(plan, target)
        raise GovernanceError(Code.INVALID_INPUT, "Bản xem trước không hợp lệ.")

    def _apply_destinations(self, plan: Plan, target: Target):
        from databricks.sdk.service.catalog import (
            AccessRequestDestinations, DestinationType, NotificationDestination, Securable,
        )

        payload = plan.payload
        entries = []
        for item in payload["destinations"]:
            try:
                kind = DestinationType(item["type"])
            except ValueError:
                raise GovernanceError(
                    Code.CAPABILITY_UNAVAILABLE,
                    f"SDK đang dùng không hỗ trợ kiểu nơi nhận {item['type']}.",
                ) from None
            entries.append(NotificationDestination(
                destination_id=item["id"], destination_type=kind,
            ))

        request = AccessRequestDestinations(
            securable=Securable(full_name=payload["full_name"],
                                type=payload["securable_type"]),
            destinations=entries,
        )

        def revalidate() -> str:
            fresh = self.destinations(target)
            if not fresh.ok:
                raise fresh.error
            return fingerprint(sorted(_destination_key(fresh.destinations)))

        def do():
            self.w.rfa.update_access_request_destinations(
                access_request_destinations=request,
                update_mask="destinations",
            )

        def verify() -> dict:
            after = self.destinations(target)
            if not after.ok:
                raise after.error
            return {
                "destinations": [f"{d.destination_type}:{d.destination_id}"
                                 for d in after.destinations],
            }

        return self.execute(plan, target, do, revalidate=revalidate, verify=verify,
                            action_label=plan.summary)

    def _apply_submit(self, plan: Plan, target: Target):
        from databricks.sdk.service.catalog import (
            CreateAccessRequest, Principal, Securable, SecurablePermissions,
        )

        payload = plan.payload
        principal = payload["principal"]
        request = CreateAccessRequest(
            behalf_of=Principal(id=principal),
            comment=payload["comment"],
            securable_permissions=[SecurablePermissions(
                securable=Securable(full_name=payload["full_name"],
                                    type=payload["securable_type"]),
                permissions=list(payload["privileges"]),
            )],
        )

        def do():
            self.w.rfa.batch_create_access_requests(requests=[request])

        # There is nothing to verify: the API returns no request id and exposes
        # no queue to read back. Claiming verification here would be a lie, so
        # the outcome stops at "sent".
        return self.execute(plan, target, do, revalidate=None, verify=None,
                            action_label=plan.summary)

    # -- helpers ----------------------------------------------------------
    def _identity_line(self) -> PreviewLine:
        from ..identity import ExecutionIdentity
        return PreviewLine(
            "Yêu cầu sẽ được gửi bằng: "
            + ExecutionIdentity.LABELS.get(self.ctx.execution_identity,
                                           self.ctx.execution_identity),
            "info",
        )


def _destination_key(destinations: list[Destination]) -> set[tuple[str, str]]:
    return {(d.destination_type, d.destination_id) for d in destinations}


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
    if len(reason) > 500:
        raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
    return reason
