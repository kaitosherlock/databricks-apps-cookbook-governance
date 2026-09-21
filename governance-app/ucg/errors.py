"""Stable error codes and user-facing messages.

Nothing outside this module is allowed to put a raw SDK exception in front of a
user: SDK errors can carry response bodies, and response bodies can carry
credentials. Every failure leaves here as a ``GovernanceError`` with a stable
``code`` the UI can branch on and a Vietnamese ``message`` a data steward can act
on.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

LOG = logging.getLogger("ucg.errors")


class Code:
    """Stable error codes. The strings are part of the backend contract."""

    # Caller mistakes
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_NAME = "INVALID_NAME"
    INVALID_PRIVILEGE = "INVALID_PRIVILEGE"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"

    # Authorisation inside this app (never a substitute for Unity Catalog)
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    WRITES_DISABLED = "WRITES_DISABLED"
    NO_IDENTITY = "NO_IDENTITY"
    SELF_APPROVAL = "SELF_APPROVAL"

    # Authorisation at Databricks
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # Capability / configuration
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    WAREHOUSE_REQUIRED = "WAREHOUSE_REQUIRED"

    # Concurrency and lifecycle
    STALE_PLAN = "STALE_PLAN"
    CONFLICT = "CONFLICT"

    # Transport
    TIMEOUT_UNKNOWN = "TIMEOUT_UNKNOWN"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL = "INTERNAL"


#: Codes whose result at Databricks is genuinely unknown: the caller must
#: re-read state before retrying, never blind-retry.
UNKNOWN_OUTCOME = frozenset({Code.TIMEOUT_UNKNOWN})

#: What a user should do next, keyed by code. Shown under the error message.
NEXT_STEP = {
    Code.INVALID_INPUT: "Kiểm tra lại các trường đã nhập rồi thử lại.",
    Code.INVALID_NAME: "Tên Unity Catalog chỉ gồm các thành phần phân tách bằng dấu chấm.",
    Code.INVALID_PRIVILEGE: "Chọn quyền hợp lệ với loại đối tượng này.",
    Code.NOT_FOUND: "Đối tượng có thể đã bị đổi tên hoặc xoá. Làm mới danh sách.",
    Code.ALREADY_SATISFIED: "Không cần thay đổi gì.",
    Code.FORBIDDEN_ROLE: "Liên hệ quản trị viên ứng dụng để được cấp vai trò phù hợp.",
    Code.FORBIDDEN_SCOPE: "Đối tượng nằm ngoài phạm vi catalog mà ứng dụng được phép quản lý.",
    Code.WRITES_DISABLED: "Quản trị viên cần bật ghi trong cấu hình ứng dụng rồi deploy lại.",
    Code.NO_IDENTITY: "Mở ứng dụng bằng URL Databricks Apps để proxy gắn danh tính của bạn.",
    Code.SELF_APPROVAL: "Yêu cầu người khác có thẩm quyền phê duyệt.",
    Code.PERMISSION_DENIED: (
        "Danh tính thực thi chưa đủ quyền Unity Catalog cho thao tác này. "
        "Cần cấp thêm quyền trong Catalog Explorer."
    ),
    Code.NOT_CONFIGURED: "Bổ sung cấu hình còn thiếu rồi deploy lại ứng dụng.",
    Code.CAPABILITY_UNAVAILABLE: "Workspace hoặc phiên bản hiện tại chưa hỗ trợ chức năng này.",
    Code.WAREHOUSE_REQUIRED: "Chọn một SQL Warehouse trong phần cấu hình để dùng chức năng này.",
    Code.STALE_PLAN: "Tạo lại bản xem trước để làm việc trên trạng thái mới nhất.",
    Code.CONFLICT: "Trạng thái đã thay đổi. Làm mới rồi thực hiện lại.",
    Code.TIMEOUT_UNKNOWN: (
        "Chưa xác định được kết quả. Làm mới và kiểm tra trạng thái thực tế "
        "TRƯỚC KHI thử lại, để tránh thực hiện hai lần."
    ),
    Code.UPSTREAM_ERROR: "Thử lại sau ít phút. Nếu vẫn lỗi, kiểm tra trạng thái workspace.",
    Code.INTERNAL: "Gửi mã tra cứu bên dưới cho người quản trị ứng dụng.",
}


@dataclass
class GovernanceError(Exception):
    """The only exception type this backend shows to a UI."""

    code: str
    message: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    detail: str = ""
    #: Set when the operation may or may not have taken effect upstream.
    outcome_unknown: bool = False

    def __post_init__(self):
        super().__init__(self.message)
        if self.code in UNKNOWN_OUTCOME:
            self.outcome_unknown = True

    @property
    def next_step(self) -> str:
        return NEXT_STEP.get(self.code, "")

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "next_step": self.next_step,
            "correlation_id": self.correlation_id,
            "detail": self.detail,
            "outcome_unknown": self.outcome_unknown,
        }


def invalid(message: str, detail: str = "") -> GovernanceError:
    return GovernanceError(Code.INVALID_INPUT, message, detail=detail)


#: Databricks error_code -> (our code, Vietnamese message). Anything not listed
#: falls through to UPSTREAM_ERROR, which never echoes the upstream body.
_DATABRICKS_MAP = {
    "PERMISSION_DENIED": (Code.PERMISSION_DENIED, "Danh tính thực thi không đủ quyền cho thao tác này."),
    "UNAUTHORIZED": (Code.PERMISSION_DENIED, "Yêu cầu không được xác thực tại Databricks."),
    "NOT_FOUND": (Code.NOT_FOUND, "Databricks không tìm thấy đối tượng này."),
    "RESOURCE_DOES_NOT_EXIST": (Code.NOT_FOUND, "Databricks không tìm thấy đối tượng này."),
    "RESOURCE_ALREADY_EXISTS": (Code.CONFLICT, "Đối tượng đã tồn tại."),
    "RESOURCE_CONFLICT": (Code.CONFLICT, "Trạng thái đối tượng đã thay đổi."),
    "ABORTED": (Code.CONFLICT, "Databricks đã huỷ yêu cầu do xung đột trạng thái."),
    "INVALID_PARAMETER_VALUE": (Code.INVALID_INPUT, "Databricks từ chối một giá trị trong yêu cầu."),
    "MALFORMED_REQUEST": (Code.INVALID_INPUT, "Yêu cầu gửi lên không hợp lệ."),
    "FEATURE_DISABLED": (Code.CAPABILITY_UNAVAILABLE, "Tính năng chưa được bật cho workspace này."),
    "NOT_IMPLEMENTED": (Code.CAPABILITY_UNAVAILABLE, "Workspace này chưa hỗ trợ API cần thiết."),
    "ENDPOINT_NOT_FOUND": (Code.CAPABILITY_UNAVAILABLE, "Workspace này chưa hỗ trợ API cần thiết."),
    "UNSUPPORTED_OPERATION": (Code.CAPABILITY_UNAVAILABLE, "Thao tác không được hỗ trợ cho loại đối tượng này."),
    "REQUEST_LIMIT_EXCEEDED": (Code.UPSTREAM_ERROR, "Databricks đang giới hạn tần suất yêu cầu."),
    "DEADLINE_EXCEEDED": (Code.TIMEOUT_UNKNOWN, "Databricks không phản hồi kịp thời hạn."),
    "TEMPORARILY_UNAVAILABLE": (Code.UPSTREAM_ERROR, "Dịch vụ Databricks tạm thời không khả dụng."),
}

#: Exception class names that mean "the request may have been applied".
_UNKNOWN_OUTCOME_TYPES = frozenset({
    "DeadlineExceeded", "Timeout", "ReadTimeout", "ConnectTimeout",
    "RequestsConnectionError", "ConnectionError", "ChunkedEncodingError",
})


def translate(exc: BaseException, *, mutating: bool = False) -> GovernanceError:
    """Map any exception into a ``GovernanceError``.

    ``mutating`` matters: a timeout on a read is a retryable nuisance, a timeout
    on a grant update means Databricks may already have applied the change.
    """
    if isinstance(exc, GovernanceError):
        return exc

    type_name = type(exc).__name__
    error_code = getattr(exc, "error_code", None)

    if error_code and error_code in _DATABRICKS_MAP:
        code, message = _DATABRICKS_MAP[error_code]
    elif type_name in _UNKNOWN_OUTCOME_TYPES:
        code = Code.TIMEOUT_UNKNOWN if mutating else Code.UPSTREAM_ERROR
        message = (
            "Yêu cầu hết thời gian chờ."
            if not mutating
            else "Yêu cầu hết thời gian chờ sau khi đã gửi thay đổi."
        )
    elif error_code:
        code, message = Code.UPSTREAM_ERROR, f"Databricks từ chối yêu cầu (mã {error_code})."
    else:
        code, message = Code.INTERNAL, "Ứng dụng gặp lỗi không mong đợi."

    err = GovernanceError(code, message, detail=error_code or type_name)
    if mutating and code in (Code.UPSTREAM_ERROR, Code.INTERNAL):
        # We sent a mutation and did not get a clean answer: treat as unknown.
        err.outcome_unknown = True
    # The message is never logged: it may contain response content.
    LOG.warning(
        "translated_error ref=%s code=%s upstream=%s type=%s mutating=%s",
        err.correlation_id, err.code, error_code or "-", type_name, mutating,
    )
    return err
