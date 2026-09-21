"""Storage credentials, service credentials, external locations and workspace isolation.

Two things in this area are routinely got wrong, so they are structural here:

* **Isolation is two settings, not one.** A securable is reachable from every
  workspace until its ``isolation_mode`` is ISOLATED; the binding list alone
  protects nothing. Every read returns both, and the UI is expected to show
  them together - "bound to 2 workspaces" while the mode is OPEN is a false
  sense of security.
* **A storage credential's binding is only checked when an external location is
  created.** Re-binding the credential later does not retroactively restrict
  locations already built on it.

``generate_temporary_service_credential`` is deliberately never called: it
returns live cloud credentials (access key, session token, SAS) rather than a
health verdict, and putting that behind a button would republish the underlying
cloud role to anyone who can click it. Health checks use ``validate`` instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..authz import Action
from ..errors import Code, GovernanceError
from ..naming import Target, validate_component
from ..paging import take
from ..plans import Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

PAGE_LIMIT = 300

#: Binding vocabulary is the binding API's own, and differs from the grants
#: securable_type: a SERVICE credential binds as "credential".
BINDING_TYPES = {
    "storage_credential": "storage_credential",
    "service_credential": "credential",
    "external_location": "external_location",
    "catalog": "catalog",
}

KIND_LABELS = {
    "storage_credential": "Storage credential",
    "service_credential": "Service credential",
    "external_location": "External location",
    "catalog": "Catalog",
}

READ_WRITE = "BINDING_TYPE_READ_WRITE"
READ_ONLY = "BINDING_TYPE_READ_ONLY"

#: Databricks does not support read-only binding for external locations.
NO_READ_ONLY_BINDING = frozenset({"external_location"})

ISOLATED_VALUES = frozenset({"ISOLATED", "ISOLATION_MODE_ISOLATED"})


# -- explanations the UI shows ------------------------------------------
TWO_STEP_NOTE = (
    "Cách ly theo workspace gồm **hai** thiết lập: chế độ cách ly của đối tượng và "
    "danh sách workspace được gán. Nếu chế độ vẫn là “Mở cho mọi workspace” thì danh "
    "sách gán không có tác dụng bảo vệ."
)
CREDENTIAL_BINDING_TIMING = (
    "Ràng buộc workspace của storage credential chỉ được kiểm tra **lúc tạo** external "
    "location. Thu hẹp ràng buộc của credential sau đó KHÔNG tự động hạn chế các "
    "external location đã tạo trước — phải ràng buộc chính các external location đó."
)
UNBIND_EFFECT_UNKNOWN = (
    "Databricks không công bố điều gì xảy ra với cluster, job hoặc phiên truy vấn "
    "ĐANG CHẠY khi gỡ ràng buộc. Không xem đây là thu hồi tức thì. Nếu credential bị "
    "lộ, phải xoay vòng credential phía cloud, không chỉ gỡ ràng buộc."
)
NOT_IAM = (
    "Quyền Unity Catalog khác với IAM/RBAC của nhà cung cấp cloud. Kiểm tra kết nối "
    "thành công chỉ chứng minh đường truy cập hoạt động, không phải chứng nhận an toàn."
)
NO_CREATE_CREDENTIAL = (
    "Ứng dụng không tạo credential. Quyền CREATE SERVICE CREDENTIAL không uỷ quyền được "
    "cho service principal, nên việc tạo phải do người có thẩm quyền thực hiện trong "
    "Databricks. Ứng dụng chỉ kiểm kê, kiểm tra và ràng buộc."
)
FORCE_NOTE = (
    "Tuỳ chọn “bỏ qua phụ thuộc” (force) thực sự phá huỷ: Databricks sẽ tiếp tục dù "
    "còn external location, bảng external hoặc mount phụ thuộc, khiến chúng ngừng "
    "dùng được. Ứng dụng không bật tuỳ chọn này."
)
DEFAULT_WORKSPACE_CATALOG = (
    "Catalog mặc định của workspace do nhóm workspace-local “workspace admins” sở hữu. "
    "Đổi ràng buộc của nó có thể khiến nhóm đó mất quyền, vì nhóm workspace-local không "
    "dùng được ở workspace khác."
)
FALLBACK_FINDING = (
    "fallback=true cho phép truy cập rơi về credential của cluster khi credential Unity "
    "Catalog không đủ — tức là đi vòng qua kiểm soát của Unity Catalog."
)


@dataclass
class SecurableRef:
    kind: str
    name: str
    owner: str = ""
    comment: str = ""
    isolation_mode: str = ""
    read_only: bool = False
    url: str = ""
    credential_name: str = ""
    purpose: str = ""
    fallback: bool = False
    used_for_managed_storage: bool = False
    created: str = ""
    updated: str = ""
    #: Non-secret summary of how the credential authenticates.
    auth_summary: str = ""

    @property
    def isolated(self) -> bool:
        return (self.isolation_mode or "").upper() in ISOLATED_VALUES

    @property
    def isolation_label(self) -> str:
        if not self.isolation_mode:
            return "Chưa xác định"
        return "Chỉ workspace được gán" if self.isolated else "Mở cho mọi workspace"

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def row(self) -> dict:
        return {
            "Tên": self.name,
            "Loại": self.kind_label,
            "Chủ sở hữu": self.owner or "—",
            "Chế độ cách ly": self.isolation_label,
            "Chỉ đọc": "Có" if self.read_only else "Không",
            "Xác thực": self.auth_summary or "—",
            "Đường dẫn": self.url or "—",
        }


@dataclass
class BindingView:
    securable_kind: str
    securable_name: str
    isolation_mode: str = ""
    bindings: list[dict] = field(default_factory=list)
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def isolated(self) -> bool:
        return (self.isolation_mode or "").upper() in ISOLATED_VALUES

    @property
    def effective_note(self) -> str:
        if not self.isolated:
            return (
                "Đối tượng đang **mở cho mọi workspace**. Danh sách gán bên dưới "
                "hiện KHÔNG hạn chế truy cập."
            )
        if not self.bindings:
            return (
                "Đối tượng đang cách ly nhưng chưa gán workspace nào — hiện không "
                "workspace nào dùng được."
            )
        return f"Đối tượng chỉ dùng được từ {len(self.bindings)} workspace được gán."

    def rows(self) -> list[dict]:
        return [
            {
                "Workspace ID": b.get("workspace_id"),
                "Chế độ": "Chỉ đọc" if b.get("binding_type") == READ_ONLY else "Đọc và ghi",
            }
            for b in self.bindings
        ]


@dataclass
class Finding:
    securable: str
    title: str
    detail: str

    def row(self) -> dict:
        return {"Đối tượng": self.securable, "Vấn đề cần xem xét": self.title,
                "Giải thích": self.detail}


class StorageService(Service):
    capability_keys = ("storage.credentials", "storage.service_credentials",
                       "storage.locations", "storage.bindings")

    TWO_STEP_NOTE = TWO_STEP_NOTE
    CREDENTIAL_BINDING_TIMING = CREDENTIAL_BINDING_TIMING
    UNBIND_EFFECT_UNKNOWN = UNBIND_EFFECT_UNKNOWN
    NOT_IAM = NOT_IAM
    NO_CREATE_CREDENTIAL = NO_CREATE_CREDENTIAL
    FORCE_NOTE = FORCE_NOTE
    DEFAULT_WORKSPACE_CATALOG = DEFAULT_WORKSPACE_CATALOG

    # -- inventory --------------------------------------------------------
    def storage_credentials(self) -> Listing:
        def run() -> Listing:
            # include_unbound is essential for an inventory: without it any
            # ISOLATED credential not bound to this workspace is invisible, and
            # a compliance report would read clean while gaps sit elsewhere.
            items, more = take(
                self.w.storage_credentials.list(include_unbound=True, max_results=0),
                PAGE_LIMIT,
            )
            refs = [self._credential_ref(as_dict(c), "storage_credential") for c in items]
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text(),
                           note=CREDENTIAL_BINDING_TIMING)

        return self.listing(run)

    def service_credentials(self) -> Listing:
        def run() -> Listing:
            items, more = take(
                self.w.credentials.list_credentials(include_unbound=True, max_results=0),
                PAGE_LIMIT,
            )
            refs = []
            for c in items:
                data = as_dict(c)
                ref = self._credential_ref(data, "service_credential")
                ref.purpose = enum_value(data.get("purpose"))
                refs.append(ref)
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text(),
                           note=NO_CREATE_CREDENTIAL)

        return self.listing(run)

    def external_locations(self) -> Listing:
        def run() -> Listing:
            items, more = take(
                self.w.external_locations.list(include_unbound=True, include_browse=True,
                                               max_results=0),
                PAGE_LIMIT,
            )
            refs = []
            for loc in items:
                data = as_dict(loc)
                refs.append(SecurableRef(
                    kind="external_location",
                    name=data.get("name") or "",
                    owner=data.get("owner") or "",
                    comment=data.get("comment") or "",
                    isolation_mode=enum_value(data.get("isolation_mode")),
                    read_only=bool(data.get("read_only")),
                    url=data.get("url") or "",
                    credential_name=data.get("credential_name") or "",
                    fallback=bool(data.get("fallback")),
                    created=millis_to_text(data.get("created_at")),
                    updated=millis_to_text(data.get("updated_at")),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text())

        return self.listing(run)

    def _credential_ref(self, data: dict, kind: str) -> SecurableRef:
        return SecurableRef(
            kind=kind,
            name=data.get("name") or "",
            owner=data.get("owner") or "",
            comment=data.get("comment") or "",
            isolation_mode=enum_value(data.get("isolation_mode")),
            read_only=bool(data.get("read_only")),
            used_for_managed_storage=bool(data.get("used_for_managed_storage")),
            created=millis_to_text(data.get("created_at")),
            updated=millis_to_text(data.get("updated_at")),
            auth_summary=_auth_summary(data),
        )

    def detail(self, kind: str, name: str) -> SecurableRef:
        name = validate_component(name, "Tên đối tượng")
        if kind == "storage_credential":
            data = as_dict(self.read(lambda: self.w.storage_credentials.get(name=name)))
            return self._credential_ref(data, kind)
        if kind == "service_credential":
            data = as_dict(self.read(lambda: self.w.credentials.get_credential(name_arg=name)))
            ref = self._credential_ref(data, kind)
            ref.purpose = enum_value(data.get("purpose"))
            return ref
        if kind == "external_location":
            data = as_dict(self.read(
                lambda: self.w.external_locations.get(name=name, include_browse=True)))
            return SecurableRef(
                kind=kind, name=data.get("name") or "",
                owner=data.get("owner") or "", comment=data.get("comment") or "",
                isolation_mode=enum_value(data.get("isolation_mode")),
                read_only=bool(data.get("read_only")), url=data.get("url") or "",
                credential_name=data.get("credential_name") or "",
                fallback=bool(data.get("fallback")),
                created=millis_to_text(data.get("created_at")),
                updated=millis_to_text(data.get("updated_at")),
            )
        if kind == "catalog":
            data = as_dict(self.read(lambda: self.w.catalogs.get(name=name)))
            return SecurableRef(
                kind=kind, name=data.get("name") or "",
                owner=data.get("owner") or "", comment=data.get("comment") or "",
                isolation_mode=enum_value(data.get("isolation_mode")),
            )
        raise GovernanceError(Code.INVALID_INPUT, f"Loại đối tượng không hỗ trợ: {kind}.")

    # -- bindings ---------------------------------------------------------
    def bindings(self, kind: str, name: str) -> BindingView:
        securable_type = BINDING_TYPES.get(kind)
        if not securable_type:
            raise GovernanceError(Code.INVALID_INPUT,
                                  f"Loại đối tượng không hỗ trợ ràng buộc workspace: {kind}.")
        name = validate_component(name, "Tên đối tượng")
        view = BindingView(securable_kind=kind, securable_name=name, observed_at=now_text())
        try:
            ref = self.detail(kind, name)
            view.isolation_mode = ref.isolation_mode
        except GovernanceError as exc:
            view.error = exc
            return view
        try:
            items, _ = take(
                self.w.workspace_bindings.get_bindings(
                    securable_type=securable_type, securable_name=name, max_results=0
                ),
                PAGE_LIMIT,
            )
        except Exception as exc:
            from ..errors import translate
            view.error = translate(exc)
            return view
        for binding in items:
            data = as_dict(binding)
            view.bindings.append({
                "workspace_id": data.get("workspace_id"),
                "binding_type": enum_value(data.get("binding_type")) or READ_WRITE,
            })
        view.bindings.sort(key=lambda b: str(b["workspace_id"]))
        return view

    def read_only_binding_supported(self, kind: str) -> bool:
        return kind not in NO_READ_ONLY_BINDING

    # -- planning ---------------------------------------------------------
    def plan_set_isolation(self, kind: str, name: str, isolated: bool, reason: str) -> Plan:
        """Step one of isolation: flip the securable's own mode."""
        self.authz.require(Action.MANAGE_BINDINGS, self._target(kind, name))
        reason = _reason(reason)
        current = self.bindings(kind, name)
        if not current.ok:
            raise current.error
        if current.isolated == isolated:
            raise GovernanceError(
                Code.ALREADY_SATISFIED,
                "Chế độ cách ly hiện tại đã đúng như mong muốn.",
            )

        plan = self._plan(
            kind, name, reason,
            payload={"operation": "isolation", "kind": kind, "name": name,
                     "isolated": bool(isolated)},
            before=_binding_fingerprint(current),
            summary=("Bật cách ly workspace" if isolated else "Mở cho mọi workspace")
            + f" cho {name}",
        )
        if isolated:
            plan.preview = [
                PreviewLine(f"Chuyển {KIND_LABELS.get(kind, kind)} `{name}` sang chế độ "
                            "chỉ dùng được từ workspace được gán.", "change"),
                self._identity_line(),
                PreviewLine(
                    f"Số workspace đang được gán: {len(current.bindings)}. "
                    + ("Sau khi bật cách ly mà chưa gán workspace nào, đối tượng sẽ "
                       "không dùng được ở đâu cả." if not current.bindings else ""),
                    "warning" if not current.bindings else "info",
                ),
                PreviewLine(UNBIND_EFFECT_UNKNOWN, "warning"),
            ]
        else:
            plan.preview = [
                PreviewLine(f"Mở {KIND_LABELS.get(kind, kind)} `{name}` cho **mọi** workspace "
                            "trong metastore.", "change"),
                self._identity_line(),
                PreviewLine(
                    "Đây là thao tác MỞ RỘNG phạm vi truy cập, không phải thu hẹp.",
                    "warning",
                ),
            ]
        if kind == "storage_credential":
            plan.preview.append(PreviewLine(CREDENTIAL_BINDING_TIMING, "warning"))
        return plan

    def plan_bind(self, kind: str, name: str, workspace_ids: list[int],
                  binding_type: str, reason: str) -> Plan:
        return self._plan_binding(kind, name, workspace_ids, binding_type, reason, add=True)

    def plan_unbind(self, kind: str, name: str, workspace_ids: list[int], reason: str) -> Plan:
        return self._plan_binding(kind, name, workspace_ids, READ_WRITE, reason, add=False)

    def _plan_binding(self, kind: str, name: str, workspace_ids: list[int],
                      binding_type: str, reason: str, *, add: bool) -> Plan:
        self.authz.require(Action.MANAGE_BINDINGS, self._target(kind, name))
        reason = _reason(reason)
        ids = _workspace_ids(workspace_ids)
        if binding_type not in (READ_WRITE, READ_ONLY):
            raise GovernanceError(Code.INVALID_INPUT, "Chế độ ràng buộc không hợp lệ.")
        if binding_type == READ_ONLY and not self.read_only_binding_supported(kind):
            raise GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                "Databricks không hỗ trợ ràng buộc chỉ đọc cho external location. "
                "Dùng thuộc tính read_only của chính external location thay thế.",
            )

        current = self.bindings(kind, name)
        if not current.ok:
            raise current.error
        existing = {int(b["workspace_id"]): b["binding_type"] for b in current.bindings}
        if add:
            changing = [i for i in ids if existing.get(i) != binding_type]
        else:
            changing = [i for i in ids if i in existing]
        if not changing:
            raise GovernanceError(Code.ALREADY_SATISFIED,
                                  "Danh sách ràng buộc đã ở trạng thái mong muốn.")

        plan = self._plan(
            kind, name, reason,
            payload={"operation": "bind" if add else "unbind", "kind": kind, "name": name,
                     "workspace_ids": changing, "binding_type": binding_type},
            before=_binding_fingerprint(current),
            summary=("Gán" if add else "Gỡ") + f" {len(changing)} workspace cho {name}",
        )
        mode = "đọc và ghi" if binding_type == READ_WRITE else "chỉ đọc"
        plan.preview = [
            PreviewLine(
                ("Gán quyền dùng " + mode if add else "Gỡ quyền dùng")
                + f" {KIND_LABELS.get(kind, kind)} `{name}`", "change"),
            PreviewLine("Workspace: " + ", ".join(str(i) for i in changing), "change"),
            self._identity_line(),
        ]
        if not current.isolated:
            plan.preview.append(PreviewLine(
                "Đối tượng đang mở cho mọi workspace, nên thay đổi danh sách này "
                "CHƯA có tác dụng hạn chế. Bật chế độ cách ly trước.",
                "warning",
            ))
        if not add:
            plan.preview.append(PreviewLine(
                "Phạm vi truy cập có thể bị gián đoạn: mọi workload trong workspace bị gỡ "
                "sẽ không còn dùng được đối tượng này.", "warning",
            ))
            plan.preview.append(PreviewLine(UNBIND_EFFECT_UNKNOWN, "unknown"))
        if kind == "catalog":
            plan.preview.append(PreviewLine(DEFAULT_WORKSPACE_CATALOG, "warning"))
        if kind == "storage_credential":
            plan.preview.append(PreviewLine(CREDENTIAL_BINDING_TIMING, "warning"))
        return plan

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, confirmation: str):
        plan.require_confirmation(confirmation)
        payload = plan.payload
        kind, name = payload["kind"], payload["name"]
        target = self._target(kind, name)

        def revalidate() -> str:
            fresh = self.bindings(kind, name)
            if not fresh.ok:
                raise fresh.error
            return _binding_fingerprint(fresh)

        if payload["operation"] == "isolation":
            do = self._isolation_call(kind, name, payload["isolated"])
        else:
            do = self._binding_call(kind, name, payload)

        def verify() -> dict:
            after = self.bindings(kind, name)
            if not after.ok:
                raise after.error
            return {
                "isolation_mode": after.isolation_mode,
                "isolated": after.isolated,
                "workspaces": [b["workspace_id"] for b in after.bindings],
            }

        return self.execute(plan, target, do, revalidate=revalidate, verify=verify,
                            action_label=plan.summary)

    def _isolation_call(self, kind: str, name: str, isolated: bool):
        from databricks.sdk.service.catalog import CatalogIsolationMode, IsolationMode

        def do():
            if kind == "catalog":
                mode = CatalogIsolationMode.ISOLATED if isolated else CatalogIsolationMode.OPEN
                self.w.catalogs.update(name=name, isolation_mode=mode)
                return
            mode = (IsolationMode.ISOLATION_MODE_ISOLATED if isolated
                    else IsolationMode.ISOLATION_MODE_OPEN)
            if kind == "storage_credential":
                self.w.storage_credentials.update(name=name, isolation_mode=mode)
            elif kind == "service_credential":
                self.w.credentials.update_credential(name_arg=name, isolation_mode=mode)
            elif kind == "external_location":
                self.w.external_locations.update(name=name, isolation_mode=mode)
            else:
                raise GovernanceError(Code.INVALID_INPUT, "Loại đối tượng không hỗ trợ.")

        return do

    def _binding_call(self, kind: str, name: str, payload: dict):
        from databricks.sdk.service.catalog import (
            WorkspaceBinding, WorkspaceBindingBindingType,
        )

        securable_type = BINDING_TYPES[kind]
        btype = (WorkspaceBindingBindingType.BINDING_TYPE_READ_ONLY
                 if payload["binding_type"] == READ_ONLY
                 else WorkspaceBindingBindingType.BINDING_TYPE_READ_WRITE)
        entries = [WorkspaceBinding(workspace_id=int(i), binding_type=btype)
                   for i in payload["workspace_ids"]]
        adding = payload["operation"] == "bind"

        def do():
            # get_bindings/update_bindings, not the deprecated get/update pair:
            # the old one is catalog-only and cannot express binding_type at
            # all, so it silently creates read-write bindings.
            self.w.workspace_bindings.update_bindings(
                securable_type=securable_type,
                securable_name=name,
                add=entries if adding else None,
                remove=None if adding else entries,
            )

        return do

    # -- validation and findings -----------------------------------------
    def validate(self, kind: str, name: str) -> dict:
        """Connectivity check. Not a security certification - see :data:`NOT_IAM`."""
        self.authz.require(Action.READ_STORAGE)
        name = validate_component(name, "Tên đối tượng")
        if kind == "storage_credential":
            result = self.read(lambda: self.w.storage_credentials.validate(
                storage_credential_name=name))
        elif kind == "external_location":
            result = self.read(lambda: self.w.storage_credentials.validate(
                external_location_name=name))
        elif kind == "service_credential":
            result = self.read(lambda: self.w.credentials.validate_credential(
                credential_name=name))
        else:
            raise GovernanceError(Code.INVALID_INPUT, "Loại đối tượng không hỗ trợ kiểm tra.")
        data = as_dict(result)
        # The response can carry per-operation detail but never a credential;
        # only documented result fields are surfaced.
        return {
            "observed_at": now_text(),
            "results": [as_dict(r) for r in (data.get("results") or [])],
            "is_dir": data.get("is_dir"),
            "note": NOT_IAM,
        }

    def findings(self) -> Listing:
        """Configuration worth a human look. Signals, not compliance verdicts."""
        found: list[Finding] = []
        partial = False

        locations = self.external_locations()
        if locations.ok:
            for loc in locations.items:
                if loc.fallback:
                    found.append(Finding(loc.name, "Cho phép fallback credential", FALLBACK_FINDING))
                if not loc.isolated:
                    found.append(Finding(
                        loc.name, "Mở cho mọi workspace",
                        "External location này dùng được từ mọi workspace trong metastore.",
                    ))
                if not loc.owner:
                    found.append(Finding(loc.name, "Chưa xác định chủ sở hữu",
                                         "Không đọc được chủ sở hữu của external location."))
        else:
            partial = True

        for listing in (self.storage_credentials(), self.service_credentials()):
            if not listing.ok:
                partial = True
                continue
            for cred in listing.items:
                if not cred.isolated:
                    found.append(Finding(
                        cred.name, "Mở cho mọi workspace",
                        f"{cred.kind_label} này dùng được từ mọi workspace trong metastore.",
                    ))

        return Listing(
            found,
            "partial_permission" if partial else "complete",
            now_text(),
            note=(
                "Đây là các điểm cần xem xét, KHÔNG phải kết luận vi phạm. "
                "Ứng dụng không tự thay đổi cấu hình từ các mục này."
            ),
        )

    # -- helpers ----------------------------------------------------------
    def _target(self, kind: str, name: str) -> Target | None:
        """Bindable securables other than catalogs live at metastore level."""
        if kind == "catalog":
            return Target("catalog", validate_component(name, "Tên catalog"))
        return None

    def _plan(self, kind: str, name: str, reason: str, *, payload: dict,
              before: str, summary: str) -> Plan:
        return Plan(
            action=Action.MANAGE_BINDINGS,
            target_key=f"{kind}:{name}",
            target_name=name,
            target_type=KIND_LABELS.get(kind, kind),
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


def _auth_summary(data: dict) -> str:
    """Non-secret description of how a credential authenticates."""
    aws = data.get("aws_iam_role") or {}
    if aws:
        return f"AWS IAM role {as_dict(aws).get('role_arn', '')}".strip()
    azure_mi = data.get("azure_managed_identity") or {}
    if azure_mi:
        item = as_dict(azure_mi)
        return "Azure managed identity " + (
            item.get("managed_identity_id") or item.get("access_connector_id") or ""
        )
    azure_sp = data.get("azure_service_principal") or {}
    if azure_sp:
        # application_id is an identifier; client_secret is never surfaced.
        return "Azure service principal " + (as_dict(azure_sp).get("application_id") or "")
    gcp = data.get("databricks_gcp_service_account") or {}
    if gcp:
        return "GCP service account " + (as_dict(gcp).get("email") or "")
    if data.get("cloudflare_api_token"):
        return "Cloudflare API token (không hiển thị giá trị)"
    return ""


def _binding_fingerprint(view: BindingView) -> str:
    return fingerprint({
        "isolation_mode": view.isolation_mode,
        "bindings": sorted(
            (str(b["workspace_id"]), b["binding_type"]) for b in view.bindings
        ),
    })


def _workspace_ids(values) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise GovernanceError(Code.INVALID_INPUT,
                                  f"Workspace ID không hợp lệ: {value!r}.") from None
        if number <= 0:
            raise GovernanceError(Code.INVALID_INPUT, "Workspace ID phải là số dương.")
        if number not in ids:
            ids.append(number)
    if not ids:
        raise GovernanceError(Code.INVALID_INPUT, "Chọn ít nhất một workspace.")
    if len(ids) > 100:
        raise GovernanceError(Code.INVALID_INPUT, "Tối đa 100 workspace trong một thao tác.")
    return ids


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
    if len(reason) > 500:
        raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
    return reason
