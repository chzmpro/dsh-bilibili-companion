"""数据目录解析测试。

本机实测：``%LOCALAPPDATA%`` 下**无法**创建任何新目录/文件
（``PermissionError [WinError 5]``，``pwsh`` 同样失败，尽管 ACL 显示 FullControl）。
因此路径**不能写死**，必须探测可写性并有可用的回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import paths


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式指定 ``COMPANION_DATA_DIR`` 时必须照做，不做回退。"""
    target = tmp_path / "explicit"
    monkeypatch.setenv("COMPANION_DATA_DIR", str(target))
    paths.reset_cache()

    assert paths.data_dir() == target
    assert paths.data_dir_source() == "env:COMPANION_DATA_DIR"
    assert paths.db_path() == target / "companion.db"
    paths.reset_cache()


def test_falls_back_when_localappdata_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCALAPPDATA 不可写时，必须回退到可写目录而不是崩掉。"""
    monkeypatch.delenv("COMPANION_DATA_DIR", raising=False)
    # 指向一个不可能创建的"目录"（把文件当目录用）
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))
    monkeypatch.setenv("APPDATA", str(blocker))
    paths.reset_cache()

    resolved = paths.data_dir()
    assert resolved.exists()
    assert paths.data_dir_source() in ("project-local", "temp-fallback")
    paths.reset_cache()


def test_project_local_fallback_is_used_when_appdata_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPANION_DATA_DIR", raising=False)
    blocker = tmp_path / "blocked"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))
    monkeypatch.setenv("APPDATA", str(blocker))
    paths.reset_cache()

    source = paths.data_dir_source()
    assert source == "project-local"
    paths.reset_cache()


def test_report_contains_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_DATA_DIR", str(tmp_path / "d"))
    paths.reset_cache()
    report = paths.data_dir_report()
    assert set(report) == {"path", "source", "writable"}
    assert report["writable"] is True
    paths.reset_cache()


def test_resolution_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_DATA_DIR", str(tmp_path / "cached"))
    paths.reset_cache()
    assert paths.data_dir() == paths.data_dir()
    paths.reset_cache()


def test_all_derived_paths_live_under_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库/凭据/日志/锁**都**必须在数据目录下，不能散落各处。"""
    target = tmp_path / "d"
    monkeypatch.setenv("COMPANION_DATA_DIR", str(target))
    paths.reset_cache()

    base = paths.data_dir()
    for derived in (paths.db_path(), paths.cookie_path(), paths.log_path(), paths.lock_path()):
        assert derived.parent == base, derived
    assert paths.cookie_path().name == "cookie.txt"
    paths.reset_cache()


def test_project_root_helpers() -> None:
    assert (paths.project_root() / "pyproject.toml").is_file()
    assert (paths.project_root() / "core" / "paths.py").is_file()
    assert paths.example_config_path().name == "config.example.yaml"
    assert paths.example_config_path().parent == paths.project_root()


def test_config_path_honours_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ ``COMPANION_CONFIG_PATH`` 必须生效。

    测试必须能隔离配置，否则会读到开发者本机的真实 ``config.yaml``，
    "零配置只读启动"这类断言就失去意义（本项目踩过这个坑）。
    """
    target = tmp_path / "custom.yaml"
    monkeypatch.setenv("COMPANION_CONFIG_PATH", str(target))
    assert paths.default_config_path() == target


def test_config_path_defaults_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPANION_CONFIG_PATH", raising=False)
    assert paths.default_config_path().name == "config.yaml"
    assert paths.default_config_path().parent == paths.project_root()
