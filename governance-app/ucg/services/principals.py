"""Looking up the identities a privilege can be granted to.

Unity Catalog accepts **account-level** identities only, addressed as:

* a user  -> their email address;
* a group -> the account group name;
* a service principal -> its ``applicationId`` (a UUID), never its display name.

Workspace-local groups and the workspace ``users``/``admins`` system groups are
**not** valid Unity Catalog principals, so anything this module surfaces from
the workspace SCIM API is labelled with whether it can legally be granted.

Databricks also stopped returning members from the account group *list*
endpoint, so group membership is a per-group fetch. When we cannot establish
membership we say so instead of implying a group is empty.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import translate
from ..paging import take
from .base import Listing, Service, as_dict, now_text

USER = "user"
GROUP = "group"
SERVICE_PRINCIPAL = "service_principal"

TYPE_LABELS = {
    USER: "Người dùng",
    GROUP: "Nhóm",
    SERVICE_PRINCIPAL: "Service principal",
}

#: Account-level system group that IS a valid UC principal (note the space).
ACCOUNT_USERS = "account users"
#: Workspace system groups that are NOT valid UC principals.
WORKSPACE_SYSTEM_GROUPS = frozenset({"users", "admins"})

LOOKUP_LIMIT = 200


@dataclass
class Principal:
    identifier: str
    kind: str
    display_name: str = ""
    #: False for workspace-local groups, which UC refuses.
    valid_for_uc: bool = True
    note: str = ""
    active: bool = True

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.kind, self.kind)

    @property
    def label(self) -> str:
        if self.display_name and self.display_name != self.identifier:
            return f"{self.display_name} · {self.identifier}"
        return self.identifier

    def row(self) -> dict:
        return {
            "Định danh dùng để cấp quyền": self.identifier,
            "Tên hiển thị": self.display_name or "—",
            "Loại": self.type_label,
            "Dùng được cho Unity Catalog": "Có" if self.valid_for_uc else "Không",
            "Ghi chú": self.note,
        }


class PrincipalService(Service):
    capability_keys = ("principals.lookup",)

    #: Displayed wherever a principal picker appears.
    SCOPE_NOTE = (
        "Danh sách lấy từ SCIM cấp workspace. Unity Catalog chỉ chấp nhận danh tính "
        "cấp account: nhóm workspace-local và hai nhóm hệ thống `users`/`admins` "
        "không cấp quyền được. Nếu không tìm thấy principal cần dùng, hãy nhập trực tiếp "
        "email, tên account group hoặc application ID."
    )

    def search(self, term: str) -> Listing:
        """Search users, groups and service principals by name."""
        term = (term or "").strip()
        found: list[Principal] = []
        partial = False

        for fetch in (self._users, self._groups, self._service_principals):
            try:
                found.extend(fetch(term))
            except Exception:
                partial = True

        found.sort(key=lambda p: (not p.valid_for_uc, p.kind, p.label.lower()))
        return Listing(
            found,
            "partial_permission" if partial else "complete",
            now_text(),
            note="Một phần danh sách không đọc được do thiếu quyền." if partial else "",
        )

    def _filter(self, field: str, term: str) -> str | None:
        """SCIM filter. The term is escaped so it cannot break out of the literal."""
        if not term:
            return None
        safe = term.replace("\\", "\\\\").replace('"', '\\"')
        return f'{field} co "{safe}"'

    def _users(self, term: str) -> list[Principal]:
        items, _ = take(
            self.w.users.list(filter=self._filter("userName", term), count=100),
            LOOKUP_LIMIT,
        )
        out = []
        for u in items:
            data = as_dict(u)
            username = data.get("user_name") or ""
            if not username:
                continue
            out.append(Principal(
                identifier=username,
                kind=USER,
                display_name=data.get("display_name") or "",
                active=data.get("active") is not False,
                note="" if data.get("active") is not False else "Tài khoản đang bị vô hiệu hoá.",
            ))
        return out

    def _groups(self, term: str) -> list[Principal]:
        items, _ = take(
            self.w.groups.list(filter=self._filter("displayName", term), count=100),
            LOOKUP_LIMIT,
        )
        out = []
        for g in items:
            data = as_dict(g)
            name = data.get("display_name") or ""
            if not name:
                continue
            local = name.lower() in WORKSPACE_SYSTEM_GROUPS
            out.append(Principal(
                identifier=name,
                kind=GROUP,
                display_name=name,
                valid_for_uc=not local,
                note=(
                    "Nhóm hệ thống của workspace — Unity Catalog không nhận nhóm này."
                    if local else
                    "Kiểm tra đây là account group; nhóm workspace-local không cấp quyền được."
                ),
            ))
        return out

    def _service_principals(self, term: str) -> list[Principal]:
        items, _ = take(
            self.w.service_principals.list(filter=self._filter("displayName", term), count=100),
            LOOKUP_LIMIT,
        )
        out = []
        for sp in items:
            data = as_dict(sp)
            app_id = data.get("application_id") or ""
            if not app_id:
                continue
            out.append(Principal(
                identifier=app_id,
                kind=SERVICE_PRINCIPAL,
                display_name=data.get("display_name") or "",
                note="Unity Catalog dùng application ID, không dùng tên hiển thị.",
            ))
        return out

    # -- membership -------------------------------------------------------
    def membership(self, group_name: str) -> Listing:
        """Members of one group, when the workspace SCIM API can see it.

        Returns an explicit "unknown" listing rather than an empty one when the
        group cannot be read, because "no members returned" and "we could not
        look" must not render the same way.
        """
        def run() -> Listing:
            groups, _ = take(
                self.w.groups.list(filter=self._filter("displayName", group_name), count=10), 10
            )
            match = None
            for g in groups:
                data = as_dict(g)
                if (data.get("display_name") or "").lower() == group_name.lower():
                    match = data
                    break
            if not match or not match.get("id"):
                return Listing(
                    [], "partial_permission", now_text(),
                    note=(
                        "Không tra cứu được nhóm này bằng SCIM cấp workspace. "
                        "Account group thường không hiển thị ở đây, nên đây KHÔNG phải "
                        "bằng chứng nhóm rỗng hay không tồn tại."
                    ),
                )
            detail = as_dict(self.w.groups.get(id=match["id"]))
            members = []
            for m in detail.get("members") or []:
                member = as_dict(m)
                members.append({
                    "Thành viên": member.get("display") or member.get("value") or "",
                    "Định danh": member.get("value") or "",
                })
            return Listing(members, "complete", now_text())

        return self.listing(run)

    def describe(self, identifier: str) -> Principal:
        """Best-effort classification of a typed-in principal string."""
        value = (identifier or "").strip()
        if not value:
            return Principal("", USER, valid_for_uc=False, note="Chưa nhập principal.")
        if "@" in value:
            return Principal(value, USER, note="Định dạng email — sẽ được hiểu là người dùng.")
        if _looks_like_uuid(value):
            return Principal(value, SERVICE_PRINCIPAL,
                             note="Định dạng UUID — sẽ được hiểu là application ID của service principal.")
        if value.lower() in WORKSPACE_SYSTEM_GROUPS:
            return Principal(value, GROUP, valid_for_uc=False,
                             note="Nhóm hệ thống của workspace — Unity Catalog không nhận.")
        return Principal(value, GROUP, note="Sẽ được hiểu là tên account group.")


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return (
        len(parts) == 5
        and [len(p) for p in parts] == [8, 4, 4, 4, 12]
        and all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)
    )
