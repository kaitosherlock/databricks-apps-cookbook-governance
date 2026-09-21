"""Unity Catalog governance backend.

UI-independent by construction: nothing in this package imports Streamlit, so
the same services can be driven by tests, a job, or a different front end.
"""
__all__ = [
    "audit", "authz", "capabilities", "clients", "config", "errors",
    "identity", "naming", "paging", "plans", "privileges", "services",
]
