from __future__ import annotations

from collections.abc import Iterable

import pytest


WINDOWS_E2E_PREFIXES = (
    "plugins/agent-tasks-runtime/tests/test_execution.py::WindowsRealProcessTest::",
    "plugins/copilot-review-loop/tests/test_hosted_review_candidate.py::HostedDispatcherOwnershipTest::test_native_windows_timeout_reaps_owned_descendant",
)


def normalized_nodeid(item: pytest.Item) -> str:
    return item.nodeid.replace("\\", "/")


def starts_with_any(value: str, prefixes: Iterable[str]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def pyramid_marker(nodeid: str) -> str | None:
    if starts_with_any(nodeid, WINDOWS_E2E_PREFIXES):
        return "windows_e2e"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = pyramid_marker(normalized_nodeid(item))
        if marker is not None:
            item.add_marker(getattr(pytest.mark, marker))
