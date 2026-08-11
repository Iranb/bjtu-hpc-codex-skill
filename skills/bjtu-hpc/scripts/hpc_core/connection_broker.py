"""Cycle-local connection reuse for BJTU portal metadata and SSH clients.

The broker never persists connection metadata or temporary certificate tokens.
It is intentionally generic: callers provide the account-scoped metadata loader
and SSH connector so existing helper contracts remain usable.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any


class ConnectionBroker:
    """Cache one connection-info object and SSH client per private account key."""

    def __init__(
        self,
        connection_loader: Callable[[str], dict[str, Any]],
        client_connector: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._connection_loader = connection_loader
        self._client_connector = client_connector
        self._infos: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._root_lock = threading.RLock()
        self.call_counts: Counter[str] = Counter()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._root_lock:
            return self._locks.setdefault(str(key), threading.RLock())

    @staticmethod
    def _client_active(client: Any) -> bool:
        try:
            transport = client.get_transport()
            return bool(transport and transport.is_active())
        except Exception:
            return False

    def connection_for(self, key: str) -> dict[str, Any]:
        key = str(key)
        with self._lock_for(key):
            if key not in self._infos:
                self.call_counts["connection_load"] += 1
                self._infos[key] = dict(self._connection_loader(key))
            return self._infos[key]

    def client_for(self, key: str) -> Any:
        key = str(key)
        with self._lock_for(key):
            client = self._clients.get(key)
            if client is not None and self._client_active(client):
                self.call_counts["client_reuse"] += 1
                return client
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
                self._clients.pop(key, None)
            info = self.connection_for(key)
            self.call_counts["ssh_connect"] += 1
            client = self._client_connector(info)
            self._clients[key] = client
            return client

    def invalidate_client(self, key: str, *, refresh_connection: bool = False) -> None:
        key = str(key)
        with self._lock_for(key):
            client = self._clients.pop(key, None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if refresh_connection:
                self._infos.pop(key, None)
            self.call_counts["client_invalidate"] += 1

    def close(self) -> None:
        with self._root_lock:
            keys = list(self._clients)
        for key in keys:
            self.invalidate_client(key)

    def redacted_counts(self) -> dict[str, int]:
        return dict(sorted(self.call_counts.items()))

    def __enter__(self) -> "ConnectionBroker":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
