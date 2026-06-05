#!/usr/bin/env python3
"""
Startup validation script for saas-service.
Checks Python version, dependencies, module imports, and configuration.
"""

import sys
from pathlib import Path

# 把 saas-service/ 加入 sys.path，让 import app.xxx 能解析
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importlib import import_module
from typing import Tuple


def check_python_version() -> Tuple[bool, str]:
    """Check Python version >= 3.12"""
    version = sys.version_info
    if version.major == 3 and version.minor >= 12:
        return True, f"OK ({version.major}.{version.minor}.{version.micro})"
    return False, f"FAIL (found {version.major}.{version.minor}.{version.micro}, need 3.12+)"


def check_dependency(pkg_name: str) -> Tuple[bool, str]:
    """Check if a Python package is installed"""
    try:
        import_module(pkg_name)
        return True, "OK"
    except ImportError:
        return False, "FAIL (not installed)"


def check_module_import(module_path: str) -> Tuple[bool, str]:
    """Check if a module can be imported"""
    try:
        import_module(module_path)
        return True, "OK"
    except Exception as e:
        return False, f"FAIL ({e})"


def check_config_load() -> Tuple[bool, str]:
    """Check if Settings can be loaded"""
    try:
        from app.config import Settings, settings
        _ = settings
        return True, "OK"
    except Exception as e:
        return False, f"FAIL ({e})"


def check_database_config() -> Tuple[bool, str]:
    """Check database configuration (without connecting)"""
    try:
        from app.config import settings
        db_url = settings.database_url
        if db_url:
            return True, f"OK (url: {db_url[:50]}...)"
        return False, "FAIL (database_url not set)"
    except Exception as e:
        return False, f"FAIL ({e})"


def check_celery_config() -> Tuple[bool, str]:
    """Check Celery configuration (without starting worker)"""
    try:
        from app.config import settings
        broker = settings.celery_broker_url
        backend = settings.celery_result_backend
        if broker and backend:
            return True, f"OK (broker: {broker[:50]}..., backend: {backend[:50]}...)"
        return False, "FAIL (celery broker/backend not set)"
    except Exception as e:
        return False, f"FAIL ({e})"


def main():
    print("Starting saas-service startup validation...\n")

    checks = []

    # 1. Python version
    ok, msg = check_python_version()
    checks.append(("Python 3.12+", ok, msg))

    # 2. Key dependencies
    deps = [
        "fastapi",
        "sqlalchemy",
        "celery",
        "redis",
        "pydantic_settings",
        "uvicorn",
        "asyncpg",
        "passlib",
    ]
    for dep in deps:
        ok, msg = check_dependency(dep)
        checks.append((dep, ok, msg))

    # 3. Module imports
    modules = [
        "app.main",
        "app.database",
        "app.models",
        "app.config",
        "app.api.routers",
        "app.api.routers.auth",
        "app.api.routers.tenants",
        "app.api.routers.users",
        "app.api.routers.pipelines",
        "app.api.routers.signals",
        "app.api.routers.reports",
        "app.api.routers.configs",
        "app.api.routers.experiments",
    ]
    for mod in modules:
        ok, msg = check_module_import(mod)
        checks.append((f"{mod} import", ok, msg))

    # 4. Config load
    ok, msg = check_config_load()
    checks.append(("app.config.Settings load", ok, msg))

    # 5. Database config (no connection)
    ok, msg = check_database_config()
    checks.append(("Database config", ok, msg))

    # 6. Celery config (no worker)
    ok, msg = check_celery_config()
    checks.append(("Celery config", ok, msg))

    # Print results
    passed = 0
    failed = 0
    for name, ok, msg in checks:
        status = "OK" if ok else "FAIL"
        print(f"[CHECK] {name} ... {status} ({msg})")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n[SUMMARY] {passed} checks passed, {failed} checks failed.")

    if failed > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
