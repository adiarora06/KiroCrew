"""Regression coverage for issue #7742.

PR #7439 (`fix(sandbox): mask the crew governance tree at the OS gate`)
classified `cron-history` as one of `sandbox.py`'s `_CREW_HIDDEN_LEAVES` on
the premise that nothing running inside the sandbox touches it: bind-masked
on Linux, and both read- and write-denied by Seatbelt on macOS.

That premise did not hold. `mcp_cron.py`'s `_call_tool_inner` builds a fresh
`CronService(base_dir=config_dir())` on *every* tool call -- including pure
reads like `cron_list` -- and `CronService.__init__` always constructs a
`CronHistoryStore`, whose `__init__` used to `mkdir(parents=True,
exist_ok=True)` that masked directory unconditionally, with no exception
handling. `mcp_cron` is spawned by kiro-cli under the sandbox launcher, so
every one of those tool calls performed a write against a path the OS gate
hides. Harmless on Linux (the launcher's bind-mount pre-creates the target,
so `exist_ok=True` swallows the resulting `FileExistsError`), but a
`PermissionError` under a properly enforced macOS Seatbelt deny would not be
swallowed and would take down every `mcp_cron` tool.

These tests pin the fix (construction is inert; the directory is created
lazily, only by an actual write) at the exact call path the issue names:
`_call_tool_inner`. They are platform-agnostic -- they assert the directory
is never touched by a read, which holds regardless of what the OS gate does
with it -- so they cover the Linux-observable behavior change described in
the issue. They cannot exercise the macOS Seatbelt deny itself (a real
kernel decision, not something `tmp_path` can simulate); see the PR
description for what still needs manual macOS verification.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.mcp_cron import _call_tool_inner


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """State cron_add's precondition; see the fixture's own docstring."""


def _history_dir(tmp_path):
    return tmp_path / "cron-history"


class TestReadOnlyToolCallsDoNotTouchHistoryDir:
    """The exact call path the issue names: a read-only `mcp_cron` tool."""

    def test_cron_list_on_empty_store_leaves_history_dir_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        result = _call_tool_inner("cron_list", {})

        assert isinstance(result, str)
        assert not _history_dir(tmp_path).exists()

    def test_cron_list_with_jobs_still_leaves_history_dir_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        job_name = f"job-{uuid.uuid4().hex[:8]}"
        _call_tool_inner("cron_add", {"name": job_name, "message": "hi", "every": 120})
        assert not _history_dir(tmp_path).exists()

        result = _call_tool_inner("cron_list", {})

        assert job_name in result or "cron job" in result.lower()
        assert not _history_dir(tmp_path).exists()

    def test_repeated_read_only_calls_never_create_history_dir(self, monkeypatch, tmp_path):
        """Every `_call_tool_inner` invocation builds a brand-new `CronService`
        (and therefore a brand-new `CronHistoryStore`) from scratch -- this is
        not a "first call is expensive, rest are free" story, so repeat it.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        for _ in range(5):
            _call_tool_inner("cron_list", {})

        assert not _history_dir(tmp_path).exists()


def test_cron_add_does_not_touch_history_dir(monkeypatch, tmp_path):
    """A write to the job store proper is still not a write to history."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    result = _call_tool_inner(
        "cron_add", {"name": f"job-{uuid.uuid4().hex[:8]}", "message": "hi", "every": 60}
    )

    assert "Added job" in result
    assert not _history_dir(tmp_path).exists()
