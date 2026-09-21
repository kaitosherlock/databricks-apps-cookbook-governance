"""Reading and changing Unity Catalog privileges.

The correctness rules encoded here, all of which are easy to get wrong:

* ``grants.get`` returns **direct grants only**; only ``get_effective``
  resolves inheritance. Building an access picture from ``get`` alone is the
  classic bug, so both are exposed and the UI labels them differently.
* The two endpoints return **different shapes**: ``privileges`` is a list of
  strings in one and a list of objects in the other.
* A privilege is direct exactly when ``inherited_from_name``/``_type`` are
  *absent* - not empty strings.
* ``update`` is a **delta** (add/remove), never a replace. We only ever send
  the privileges the operator picked for the one principal they picked.
* Owners hold everything implicitly and never appear in the grant list, so an
  owner row is added separately and clearly marked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import privileges as priv
from ..errors import Code, GovernanceError
from ..naming import Target
from ..paging import Completeness, collect_pages, permissions_page_size
from ..plans import Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, now_text

DIRECT = "direct"
INHERITED = "inherited"
OWNERSHIP = "ownership"

SOURCE_LABELS = {
    DIRECT: "Cấp trực tiếp",
    INHERITED: "Kế thừa",
    OWNERSHIP: "Từ quyền sở hữu",
}


def classify_principal(identifier: str) -> str:
    """How Unity Catalog will read a principal string.

    UC does not return a principal *type*, it returns a string and interprets
    it by shape: an address is a user, a UUID is a service principal's
    applicationId, anything else is an account group. Showing that reading is
    more useful than a column of "unknown", and it is the same rule the grant
    will actually be evaluated by.
    """
    value = (identifier or "").strip()
    if not value:
        return "Chưa xác định"
    if "@" in value:
        return "Người dùng"
    parts = value.split("-")
    if (len(parts) == 5 and [len(p) for p in parts] == [8, 4, 4, 4, 12]
            and all(c in "0123456789abcdefABCDEF" for p in parts for c in p)):
        return "Service principal"
    return "Nhóm"


@dataclass
class GrantRow:
    principal: str
    privilege: str
    source: str = DIRECT
    inherited_from: str = ""
    inherited_from_type: str = ""
    principal_type: str = ""

    @property
    def source_label(self) -> str:
        if self.source == INHERITED and self.inherited_from_type:
            kind = self.inherited_from_type.lower()
            noun = {"catalog": "catalog", "schema": "schema", "metastore": "metastore"}.get(kind, kind)
            return f"Kế thừa từ {noun}"
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def revocable_here(self) -> bool:
        """Only a direct grant can be revoked on this object, and only if we
        could read which privilege it is."""
        return self.source == DIRECT and not self.unreadable

    @property
    def unreadable(self) -> bool:
        """Databricks named a privilege the pinned SDK enum does not contain.

        The SDK parses `privilege` into an enum and yields None for anything it
        has not shipped - READ METADATA, for instance, is documented but absent
        from 0.105.0. The grant is real and it does widen access, so the row is
        kept and labelled rather than dropped, which would under-report access.
        """
        return not self.privilege and self.source != OWNERSHIP

    def row(self) -> dict:
        if self.unreadable:
            label, code = "Không đọc được mã quyền", "—"
        else:
            label, code = priv.info(self.privilege).label, self.privilege
        return {
            "Principal": self.principal,
            "Loại principal": self.principal_type or classify_principal(self.principal),
            "Quyền": label,
            "Mã Databricks": code,
            "Nguồn quyền": self.source_label,
            "Cấp tại": self.inherited_from or (
                "Chính đối tượng này" if self.source == DIRECT else "—"
            ),
        }


@dataclass
class GrantView:
    """Everything the permissions screen shows about one object."""

    target: Target
    rows: list[GrantRow] = field(default_factory=list)
    owner: str = ""
    completeness: str = Completeness.COMPLETE
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def principals(self) -> list[str]:
        return sorted({r.principal for r in self.rows})

    def for_principal(self, principal: str) -> list[GrantRow]:
        return [r for r in self.rows if r.principal == principal]

    def direct_privileges(self, principal: str) -> list[str]:
        """Direct privileges we can name. Unreadable ones are excluded so they
        never end up in a revoke payload or a state fingerprint."""
        return sorted({r.privilege for r in self.rows
                       if r.principal == principal and r.source == DIRECT and r.privilege})

    @property
    def has_unreadable(self) -> bool:
        return any(r.unreadable for r in self.rows)

    def table(self) -> list[dict]:
        return [r.row() for r in self.rows]

    #: Shown only when at least one row could not be named.
    UNREADABLE_NOTE = (
        "Một số dòng hiển thị “Không đọc được mã quyền”: Databricks trả về một quyền mà "
        "phiên bản SDK đang ghim chưa biết tên (ví dụ READ METADATA). Quyền đó **có thật** "
        "và vẫn có hiệu lực — ứng dụng giữ lại dòng thay vì bỏ đi để không báo thiếu quyền. "
        "Đối chiếu trong Catalog Explorer để biết chính xác."
    )

    #: Shown under every grant table. The grant list is not the whole story.
    CAVEAT = (
        "Bảng này liệt kê quyền do Unity Catalog trả về cho danh tính thực thi. "
        "Đây **không phải** kết luận đầy đủ về mọi đường truy cập: quyền sở hữu, "
        "thành viên nhóm, ràng buộc workspace, chính sách ABAC và row filter/column mask "
        "đều có thể thay đổi kết quả thực tế."
    )


class GrantService(Service):
    capability_keys = ("grants.read", "grants.effective", "grants.write")

    # -- reading ----------------------------------------------------------
    def _page(self, api, securable_type: str, full_name: str):
        page_size = permissions_page_size(None)

        def fetch(token):
            response = api(
                securable_type=securable_type, full_name=full_name,
                max_results=page_size, page_token=token,
            )
            items = list(getattr(response, "privilege_assignments", None) or [])
            return items, getattr(response, "next_page_token", None)

        return collect_pages(fetch, max_pages=self.settings.max_pages)

    def direct(self, target: Target) -> GrantView:
        """Direct grants on this object only."""
        self.authz.require_scope(target.catalog)
        view = GrantView(target=target, observed_at=now_text())
        try:
            assignments, completeness = self._page(
                self.w.grants.get, target.securable_type, target.full_name
            )
        except Exception as exc:
            from ..errors import translate
            view.error = translate(exc)
            return view
        view.completeness = completeness
        for assignment in assignments:
            data = as_dict(assignment)
            principal = data.get("principal") or ""
            for p in data.get("privileges") or []:
                # In the direct endpoint this is a bare string.
                view.rows.append(GrantRow(principal, enum_value(p), DIRECT))
        view.rows.sort(key=lambda r: (r.principal.lower(), r.privilege))
        return view

    def effective(self, target: Target) -> GrantView:
        """Direct plus inherited, with provenance."""
        self.authz.require_scope(target.catalog)
        view = GrantView(target=target, observed_at=now_text())
        try:
            assignments, completeness = self._page(
                self.w.grants.get_effective, target.securable_type, target.full_name
            )
        except Exception as exc:
            from ..errors import translate
            view.error = translate(exc)
            return view
        view.completeness = completeness
        for assignment in assignments:
            data = as_dict(assignment)
            principal = data.get("principal") or ""
            for item in data.get("privileges") or []:
                # Here each entry is an object carrying inheritance provenance.
                entry = as_dict(item)
                inherited_name = entry.get("inherited_from_name") or ""
                inherited_type = enum_value(entry.get("inherited_from_type"))
                view.rows.append(GrantRow(
                    principal=principal,
                    privilege=enum_value(entry.get("privilege")),
                    # Absent (not empty) provenance means the grant is direct.
                    source=INHERITED if inherited_name or inherited_type else DIRECT,
                    inherited_from=inherited_name,
                    inherited_from_type=inherited_type,
                ))
        view.rows.sort(key=lambda r: (r.principal.lower(), r.source != DIRECT, r.privilege))
        return view

    def with_owner(self, view: GrantView, owner: str) -> GrantView:
        """Add the owner, who holds everything implicitly and is invisible to the API."""
        view.owner = owner or ""
        if owner:
            view.rows.insert(0, GrantRow(
                principal=owner,
                privilege="(chủ sở hữu)",
                source=OWNERSHIP,
                inherited_from=view.target.full_name,
            ))
        return view

    def available_privileges(self, target: Target, *, table_type: str = "") -> tuple[str, ...]:
        blocked = priv.NOT_GRANTABLE.get(target.securable_type)
        if blocked:
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, blocked)
        return priv.for_securable(target.securable_type, kind=target.kind, table_type=table_type)

    # -- planning ---------------------------------------------------------
    def plan_change(
        self,
        target: Target,
        principal: str,
        action: str,
        selected: list[str],
        reason: str,
        *,
        table_type: str = "",
    ) -> Plan:
        """Validate -> Authorize -> Build plan -> Preview. Nothing is sent."""
        from ..authz import Action

        act = Action.GRANT if action == "grant" else Action.REVOKE
        self.authz.require(act, target)

        principal = (principal or "").strip()
        reason = (reason or "").strip()
        if not principal or any(ord(c) < 32 for c in principal):
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Nhập principal hợp lệ: email người dùng, tên account group, "
                "hoặc application ID của service principal.",
            )
        if action not in ("grant", "revoke"):
            raise GovernanceError(Code.INVALID_INPUT, "Thao tác phải là cấp hoặc thu hồi quyền.")
        chosen = tuple(sorted({p.strip().upper() for p in selected if p and p.strip()}))
        if not chosen:
            raise GovernanceError(Code.INVALID_INPUT, "Chọn ít nhất một quyền.")
        allowed = set(self.available_privileges(target, table_type=table_type))
        invalid = [p for p in chosen if p not in allowed]
        if invalid:
            raise GovernanceError(
                Code.INVALID_PRIVILEGE,
                f"Quyền không hợp lệ với {target.label.lower()}: {', '.join(invalid)}.",
            )
        if not reason:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
        if len(reason) > 500:
            raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")

        current = self.direct(target)
        if not current.ok:
            raise current.error
        held = set(current.direct_privileges(principal))

        if action == "revoke":
            missing = [p for p in chosen if p not in held]
            if missing:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Chỉ thu hồi được quyền đang cấp trực tiếp tại đối tượng này. "
                    f"Chưa cấp trực tiếp: {', '.join(missing)}. "
                    "Quyền kế thừa phải sửa ở cấp cha.",
                )
        else:
            redundant = [p for p in chosen if p in held]
            if len(redundant) == len(chosen):
                raise GovernanceError(
                    Code.ALREADY_SATISFIED,
                    "Principal đã được cấp trực tiếp toàn bộ các quyền đã chọn.",
                )

        before = fingerprint(sorted(held))
        plan = Plan(
            action=act,
            target_key=target.key,
            target_name=target.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={"principal": principal, "privileges": list(chosen), "operation": action},
            before=before,
            reason=reason,
            summary=("Cấp quyền" if action == "grant" else "Thu hồi quyền")
            + " " + ", ".join(chosen),
        )
        plan.preview = self._preview(target, principal, action, chosen, held)
        return plan

    def _preview(self, target, principal, action, chosen, held) -> list[PreviewLine]:
        verb = "Cấp" if action == "grant" else "Thu hồi"
        lines = [
            PreviewLine(f"{verb} quyền: " + ", ".join(priv.display(p) for p in chosen), "change"),
            PreviewLine(f"Cho principal: {principal}", "change"),
            PreviewLine(f"Trên đối tượng: {target.full_name} ({target.label})", "change"),
            PreviewLine(
                "Yêu cầu sẽ được gửi bằng: "
                + ("tài khoản dịch vụ của ứng dụng"
                   if self.ctx.execution_identity == "app_service_principal"
                   else "hồ sơ đăng nhập hiện tại"),
                "info",
            ),
            PreviewLine(
                "Chỉ gửi đúng phần thay đổi (delta) cho principal này; "
                "quyền của principal khác không bị ảnh hưởng.",
                "info",
            ),
        ]
        if action == "grant":
            already = [p for p in chosen if p in held]
            if already:
                lines.append(PreviewLine(
                    "Đã có sẵn (sẽ không đổi): " + ", ".join(already), "info"))
        if target.kind in ("catalog", "schema"):
            lines.append(PreviewLine(priv.inheritance_note(target.kind), "warning"))
            lines.append(PreviewLine(
                "Số đối tượng con chịu ảnh hưởng: Chưa xác định — Unity Catalog không cung cấp "
                "phép đếm này và ứng dụng không ước lượng.",
                "unknown",
            ))
        note = priv.traversal_note(target.kind)
        if note:
            lines.append(PreviewLine(note, "warning"))
        for warning in priv.warnings_for(chosen, target.kind):
            lines.append(PreviewLine(warning, "warning"))
        if action == "revoke":
            lines.append(PreviewLine(
                "Thu hồi một quyền trực tiếp KHÔNG đảm bảo principal mất toàn bộ quyền truy cập: "
                "họ vẫn có thể còn quyền qua nhóm, qua đối tượng cha, qua quyền sở hữu "
                "hoặc qua chính sách khác.",
                "warning",
            ))
        return lines

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, target: Target, confirmation: str):
        plan.require_confirmation(confirmation)
        from databricks.sdk.service.catalog import PermissionsChange, Privilege

        principal = plan.payload["principal"]
        codes = plan.payload["privileges"]
        grant = plan.payload["operation"] == "grant"
        try:
            values = [Privilege(c) for c in codes]
        except ValueError:
            raise GovernanceError(
                Code.INVALID_PRIVILEGE,
                "Phiên bản SDK đang dùng không nhận diện được một trong các quyền đã chọn.",
            ) from None
        change = PermissionsChange(
            principal=principal,
            add=values if grant else None,
            remove=None if grant else values,
        )

        def revalidate() -> str:
            fresh = self.direct(target)
            if not fresh.ok:
                raise fresh.error
            return fingerprint(sorted(fresh.direct_privileges(principal)))

        def do():
            self.w.grants.update(
                securable_type=target.securable_type,
                full_name=target.full_name,
                changes=[change],
            )

        def verify() -> dict:
            after = self.direct(target)
            if not after.ok:
                raise after.error
            held = set(after.direct_privileges(principal))
            expected_present = set(codes) if grant else set()
            expected_absent = set() if grant else set(codes)
            return {
                "principal": principal,
                "privileges_now": sorted(held),
                "applied": expected_present.issubset(held) and not (expected_absent & held),
            }

        return self.execute(
            plan, target, do,
            revalidate=revalidate, verify=verify,
            action_label=plan.summary,
        )
