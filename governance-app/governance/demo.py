"""Offline, read-only sample workspace for UI exploration. Never calls Databricks."""
from types import SimpleNamespace
from databricks.sdk.service.catalog import (
    CatalogInfo, SchemaInfo, TableInfo, FunctionInfo, ColumnInfo,
    GetPermissionsResponse, EffectivePermissionsList,
)


def demo_client():
    catalog = CatalogInfo(name="demo_governance", owner="data-platform-admins", comment="Dữ liệu minh hoạ, không kết nối workspace.")
    schema = SchemaInfo(name="curated", catalog_name=catalog.name,
                        full_name="demo_governance.curated", owner="data-stewards", comment="Curated analytics")
    table = TableInfo(name="customers", catalog_name=catalog.name, schema_name=schema.name,
                      full_name="demo_governance.curated.customers", owner="data-stewards",
                      comment="Customer profile — dữ liệu minh hoạ",
                      columns=[ColumnInfo(name="customer_id", type_text="BIGINT", comment="Surrogate key"),
                               ColumnInfo(name="email", type_text="STRING", comment="PII: contact email")])
    function = FunctionInfo(name="normalize_email", catalog_name=catalog.name, schema_name=schema.name,
                            full_name="demo_governance.curated.normalize_email", owner="data-stewards",
                            comment="Function minh hoạ; app chỉ quản lý metadata/quyền.")
    direct = GetPermissionsResponse.from_dict({"privilege_assignments": [
        {"principal": "analysts", "privileges": ["SELECT"]},
        {"principal": "data-engineers", "privileges": ["SELECT", "MODIFY"]},
    ]})
    effective = EffectivePermissionsList.from_dict({"privilege_assignments": [
        {"principal": "analysts", "privileges": [{"privilege": "SELECT"}]},
        {"principal": "data-engineers", "privileges": [{"privilege": "SELECT"}, {"privilege": "MODIFY"}]},
        {"principal": "reporting", "privileges": [{"privilege": "SELECT", "inherited_from_name": "demo_governance.curated", "inherited_from_type": "SCHEMA"}]},
    ]})
    def table_grants(**kwargs):
        return direct if kwargs['securable_type'] == 'table' else GetPermissionsResponse(privilege_assignments=[])
    def effective_grants(**kwargs):
        return effective if kwargs['securable_type'] == 'table' else EffectivePermissionsList(privilege_assignments=[])
    def api(item):
        return SimpleNamespace(list=lambda **kwargs: iter([item]), get=lambda **kwargs: item)
    return SimpleNamespace(catalogs=api(catalog), schemas=api(schema), tables=api(table), functions=api(function),
                           grants=SimpleNamespace(get=table_grants, get_effective=effective_grants))
