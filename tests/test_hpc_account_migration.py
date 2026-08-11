from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from hpc_account_migration import (
    account_migration_is_encrypted,
    export_account_migration,
    import_account_migration,
    load_account_migration,
)
from hpc_account_store import (
    AccountStoreError,
    load_credentials,
    load_store,
    save_account_token,
    upsert_account,
    upsert_account_credential,
)


PASSPHRASE = "correct horse battery staple"


def source_stores(root: Path) -> tuple[Path, Path]:
    accounts = root / "source-accounts.json"
    credentials = root / "source-credentials.json"
    upsert_account(
        "main",
        portal_user="portal-main",
        cluster="cluster2",
        account="os-main",
        profile_dir=root / "old-machine-profile",
        set_default=True,
        path=accounts,
    )
    save_account_token("main", "test-token-main", validation={"ok": True}, path=accounts)
    upsert_account("other", portal_user="portal-other", cluster="cluster2", account="os-other", path=accounts)
    save_account_token("other", "test-token-other", path=accounts)
    upsert_account_credential(
        "main",
        login_name="portal-main",
        login_password="test-password-main",
        path=credentials,
    )
    return accounts, credentials


def test_metadata_only_round_trip_resets_profiles() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_accounts, source_credentials = source_stores(root)
        migration = root / "metadata.json"
        result = export_account_migration(
            migration,
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        assert os.stat(migration).st_mode & 0o777 == 0o600
        assert result["includes_tokens"] is False
        assert result["includes_credentials"] is False
        payload = load_account_migration(migration)
        assert "token" not in payload["accounts"]["main"]
        assert "profile_dir" not in payload["accounts"]["main"]

        target_accounts = root / "target-accounts.json"
        target_credentials = root / "target-credentials.json"
        imported = import_account_migration(
            migration,
            accounts_path=target_accounts,
            credentials_path=target_credentials,
        )
        target = load_store(target_accounts)
        assert imported["imported"] == ["main", "other"]
        assert target["default"] == "main"
        assert "token" not in target["accounts"]["main"]
        assert "profile_dir" not in target["accounts"]["main"]
        assert not target_credentials.exists()


def test_secret_round_trip_and_conflict_policies() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_accounts, source_credentials = source_stores(root)
        migration = root / "private-migration.json"
        export_account_migration(
            migration,
            include_tokens=True,
            include_credentials=True,
            passphrase=PASSPHRASE,
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        assert account_migration_is_encrypted(migration) is True
        envelope_text = migration.read_text(encoding="utf-8")
        assert "test-token-main" not in envelope_text
        assert "test-password-main" not in envelope_text
        target_accounts = root / "target-accounts.json"
        target_credentials = root / "target-credentials.json"
        import_account_migration(
            migration,
            passphrase=PASSPHRASE,
            accounts_path=target_accounts,
            credentials_path=target_credentials,
        )
        target = load_store(target_accounts)
        credentials = load_credentials(target_credentials)
        assert target["accounts"]["main"]["token"] == "test-token-main"
        assert credentials["accounts"]["main"]["login_password"] == "test-password-main"
        assert os.stat(target_accounts).st_mode & 0o777 == 0o600
        assert os.stat(target_credentials).st_mode & 0o777 == 0o600

        try:
            import_account_migration(
                migration,
                passphrase=PASSPHRASE,
                accounts_path=target_accounts,
                credentials_path=target_credentials,
            )
        except AccountStoreError as error:
            assert "import made no changes" in str(error)
        else:
            raise AssertionError("default conflict policy must fail closed")

        skipped = import_account_migration(
            migration,
            on_conflict="skip",
            passphrase=PASSPHRASE,
            accounts_path=target_accounts,
            credentials_path=target_credentials,
        )
        assert skipped["imported"] == []
        assert skipped["skipped"] == ["main", "other"]


def test_tamper_loose_permissions_and_symlink_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_accounts, source_credentials = source_stores(root)
        migration = root / "migration.json"
        export_account_migration(
            migration,
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        payload = json.loads(migration.read_text())
        payload["accounts"]["main"]["portal_user"] = "tampered"
        migration.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(migration, 0o600)
        try:
            load_account_migration(migration)
        except AccountStoreError as error:
            assert "payload_sha256 mismatch" in str(error)
        else:
            raise AssertionError("tampered migration must fail closed")

        export_account_migration(
            migration,
            include_tokens=True,
            passphrase=PASSPHRASE,
            overwrite=True,
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        os.chmod(migration, 0o644)
        try:
            load_account_migration(migration, passphrase=PASSPHRASE)
        except AccountStoreError as error:
            assert "readable by group/others" in str(error)
        else:
            raise AssertionError("loosely permissioned migration must fail closed")

        os.chmod(migration, 0o600)
        linked = root / "linked.json"
        linked.symlink_to(migration)
        try:
            load_account_migration(linked, passphrase=PASSPHRASE)
        except AccountStoreError as error:
            assert "non-symlink" in str(error)
        else:
            raise AssertionError("symlink migration input must fail closed")


def test_wrong_passphrase_and_ciphertext_tamper_fail_before_import() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_accounts, source_credentials = source_stores(root)
        migration = root / "encrypted.json"
        export_account_migration(
            migration,
            include_tokens=True,
            include_credentials=True,
            passphrase=PASSPHRASE,
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        target_accounts = root / "target-accounts.json"
        target_credentials = root / "target-credentials.json"
        try:
            import_account_migration(
                migration,
                passphrase="wrong passphrase value",
                accounts_path=target_accounts,
                credentials_path=target_credentials,
            )
        except AccountStoreError as error:
            assert "wrong or the encrypted file was modified" in str(error)
        else:
            raise AssertionError("wrong migration passphrase must fail closed")
        assert not target_accounts.exists()
        assert not target_credentials.exists()

        envelope = json.loads(migration.read_text(encoding="utf-8"))
        ciphertext = envelope["ciphertext_b64"]
        envelope["ciphertext_b64"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        migration.write_text(json.dumps(envelope), encoding="utf-8")
        os.chmod(migration, 0o600)
        try:
            load_account_migration(migration, passphrase=PASSPHRASE)
        except AccountStoreError as error:
            assert "wrong or the encrypted file was modified" in str(error)
        else:
            raise AssertionError("tampered ciphertext must fail closed")


def test_metadata_replace_preserves_target_token_and_profile() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_accounts, source_credentials = source_stores(root)
        migration = root / "metadata.json"
        export_account_migration(
            migration,
            names=["main"],
            accounts_path=source_accounts,
            credentials_path=source_credentials,
        )
        target_accounts = root / "target-accounts.json"
        target_credentials = root / "target-credentials.json"
        target_profile = root / "target-profile"
        upsert_account(
            "main",
            portal_user="old-local-value",
            cluster="cluster2",
            account="os-main",
            profile_dir=target_profile,
            set_default=True,
            path=target_accounts,
        )
        save_account_token("main", "target-token", path=target_accounts)
        import_account_migration(
            migration,
            on_conflict="replace",
            accounts_path=target_accounts,
            credentials_path=target_credentials,
        )
        target = load_store(target_accounts)["accounts"]["main"]
        assert target["portal_user"] == "portal-main"
        assert target["token"] == "target-token"
        assert target["profile_dir"] == str(target_profile)


def main() -> None:
    test_metadata_only_round_trip_resets_profiles()
    test_secret_round_trip_and_conflict_policies()
    test_tamper_loose_permissions_and_symlink_are_rejected()
    test_wrong_passphrase_and_ciphertext_tamper_fail_before_import()
    test_metadata_replace_preserves_target_token_and_profile()
    print("PASS account migration fixtures")


if __name__ == "__main__":
    main()
