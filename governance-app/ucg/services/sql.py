"""Controlled SQL execution.

Only the modules that have no REST alternative use this: system-table lineage
and audit, and table/view ownership transfer. Everything else goes through the
typed SDK surface.

Three rules make this safe enough to ship:

* **Templates only.** Callers pick a named template; they never hand in SQL.
* **Values are bound**, identifiers are quoted through
  :func:`ucg.naming.sql_identifier`, which rejects backticks and dots. Raw
  string concatenation of user input never happens.
* **Everything is bounded** - row count, byte size and wait time all have
  ceilings, and a timeout is reported as an unknown outcome, not a rollback.

There is deliberately no free-form SQL console: this app authenticates with a
privileged identity, and a console would hand that identity to whoever can open
the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..errors import Code, GovernanceError, translate
from .base import Service, now_text

#: Hard ceiling regardless of configuration.
MAX_ROWS = 10_000
MAX_BYTES = 40 * 1024 * 1024


@dataclass
class SqlResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    truncated: bool = False
    statement_id: str = ""
    observed_at: str = ""
    warehouse_id: str = ""

    @property
    def empty(self) -> bool:
        return not self.rows


class SqlRunner(Service):
    """Executes parameterised statements against a configured SQL warehouse."""

    capability_keys = ()

    def available(self) -> bool:
        return bool(self.settings.warehouse_id)

    def require_warehouse(self) -> str:
        warehouse = (self.settings.warehouse_id or "").strip()
        if not warehouse:
            raise GovernanceError(
                Code.WAREHOUSE_REQUIRED,
                "Chức năng này cần một SQL Warehouse. Đặt GOVERNANCE_WAREHOUSE_ID "
                "trong cấu hình ứng dụng rồi deploy lại.",
            )
        return warehouse

    def warehouses(self) -> list[dict]:
        """Warehouses the executing identity can see, for the setup screen."""
        try:
            out = []
            for wh in self.w.warehouses.list():
                out.append({
                    "id": getattr(wh, "id", ""),
                    "name": getattr(wh, "name", ""),
                    "state": str(getattr(getattr(wh, "state", None), "value", "") or ""),
                })
            return out
        except Exception:
            return []

    def run(
        self,
        statement: str,
        parameters: dict | None = None,
        *,
        row_limit: int | None = None,
        catalog: str = "",
        schema: str = "",
        mutating: bool = False,
    ) -> SqlResult:
        """Execute one statement. ``statement`` must come from a template."""
        from databricks.sdk.service.sql import (
            Disposition, Format, StatementParameterListItem,
            StatementState,
        )

        warehouse = self.require_warehouse()
        settings: Settings = self.settings
        limit = min(row_limit or settings.sql_row_limit, MAX_ROWS)
        wait = max(5, min(settings.sql_timeout_seconds, 50))

        params = [
            StatementParameterListItem(name=str(k), value=None if v is None else str(v))
            for k, v in (parameters or {}).items()
        ]

        try:
            response = self.w.statement_execution.execute_statement(
                statement=statement,
                warehouse_id=warehouse,
                parameters=params or None,
                row_limit=limit,
                byte_limit=MAX_BYTES,
                catalog=catalog or None,
                schema=schema or None,
                disposition=Disposition.INLINE,
                format=Format.JSON_ARRAY,
                wait_timeout=f"{wait}s",
                on_wait_timeout="CANCEL",
            )
        except Exception as exc:
            raise translate(exc, mutating=mutating) from None

        state = getattr(getattr(response, "status", None), "state", None)
        statement_id = getattr(response, "statement_id", "") or ""

        if state == StatementState.SUCCEEDED:
            return self._collect(response, statement_id, warehouse, limit)

        if state in (StatementState.PENDING, StatementState.RUNNING):
            # We asked Databricks to cancel on timeout, but for a mutating
            # statement the outcome is genuinely unknown until it is re-read.
            raise GovernanceError(
                Code.TIMEOUT_UNKNOWN if mutating else Code.UPSTREAM_ERROR,
                "Truy vấn vượt quá thời gian chờ cho phép."
                + (" Kiểm tra trạng thái thực tế trước khi thử lại."
                   if mutating else " Hãy thu hẹp phạm vi rồi thử lại."),
                detail=statement_id,
            )

        # FAILED / CANCELED / CLOSED. The upstream message can contain data, so
        # only the documented error code is surfaced.
        error = getattr(getattr(response, "status", None), "error", None)
        code = str(getattr(getattr(error, "error_code", None), "value", "") or "")
        raise GovernanceError(
            Code.UPSTREAM_ERROR,
            "Databricks không hoàn tất được truy vấn.",
            detail=code or str(getattr(state, "value", state) or ""),
        )

    @staticmethod
    def _collect(response, statement_id: str, warehouse: str, limit: int) -> SqlResult:
        manifest = getattr(response, "manifest", None)
        schema = getattr(manifest, "schema", None)
        columns = [c.name for c in (getattr(schema, "columns", None) or [])]
        data = getattr(getattr(response, "result", None), "data_array", None) or []
        rows = [dict(zip(columns, values)) for values in data]
        return SqlResult(
            columns=columns,
            rows=rows,
            truncated=bool(getattr(manifest, "truncated", False)) or len(rows) >= limit,
            statement_id=statement_id,
            observed_at=now_text(),
            warehouse_id=warehouse,
        )
