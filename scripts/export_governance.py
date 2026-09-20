"""Refresh standalone app from the canonical Cookbook governance module."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
dest = root / "governance-app"
shutil.copytree(root / "streamlit/governance", dest / "governance", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copyfile(root / "streamlit/governance_app.py", dest / "app.py")
print(dest)
