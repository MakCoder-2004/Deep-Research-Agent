"""Test-wide safeguards for provider-mocked suites."""

from __future__ import annotations

import inspect
import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject socket connections so tests cannot accidentally use live services."""

    original_connect = socket.socket.connect

    def deny_connect(sock: socket.socket, address: Any) -> None:
        # Windows' Proactor loop creates its internal socketpair through this
        # same method before the test body starts; it is not external traffic.
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "_fallback_socketpair":
                original_connect(sock, address)
                return
            frame = frame.f_back
        raise AssertionError("Live network access is disabled in tests.")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
