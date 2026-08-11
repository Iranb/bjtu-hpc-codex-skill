from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "skills" / "bjtu-hpc" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hpc_core.connection_broker import ConnectionBroker


class FakeTransport:
    def __init__(self) -> None:
        self.active = True

    def is_active(self) -> bool:
        return self.active


class FakeClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.closed = False

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.closed = True
        self.transport.active = False


def test_connection_and_client_are_reused_per_account() -> None:
    loads: list[str] = []
    clients: list[FakeClient] = []

    def loader(key: str):
        loads.append(key)
        return {"key": key, "certificate": "secret-not-for-output"}

    def connector(info):
        client = FakeClient()
        clients.append(client)
        return client

    broker = ConnectionBroker(loader, connector)
    first = broker.client_for("a")
    second = broker.client_for("a")
    assert first is second
    assert loads == ["a"]
    assert len(clients) == 1
    assert broker.redacted_counts()["connection_load"] == 1
    assert broker.redacted_counts()["ssh_connect"] == 1

    first.transport.active = False
    third = broker.client_for("a")
    assert third is not first
    assert loads == ["a"]
    assert len(clients) == 2
    assert broker.redacted_counts()["ssh_connect"] == 2
    assert "secret" not in repr(broker.redacted_counts())

    broker.close()
    assert third.closed is True


def test_refresh_connection_discards_cached_metadata() -> None:
    load_count = 0

    def loader(key: str):
        nonlocal load_count
        load_count += 1
        return {"key": key, "generation": load_count}

    broker = ConnectionBroker(loader, lambda info: FakeClient())
    assert broker.connection_for("a")["generation"] == 1
    broker.invalidate_client("a", refresh_connection=True)
    assert broker.connection_for("a")["generation"] == 2


def main() -> None:
    test_connection_and_client_are_reused_per_account()
    test_refresh_connection_discards_cached_metadata()
    print("PASS connection broker fixtures")


if __name__ == "__main__":
    main()
