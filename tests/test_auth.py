import json
import time

from .conftest import configure, create_owner, import_owner


def test_legacy_import_pins_account_and_keeps_secrets_out_of_output(cli):
    path = import_owner(cli)
    assert (path / "secrets.json").stat().st_mode & 0o777 == 0o600
    code, result = cli("status", "--live")
    assert code == 0 and result["account_id"] == "user-alice"
    assert "access-alice" not in json.dumps(result)
    assert result["auth"]["refresh_available"] is False


def test_importing_a_different_account_cannot_replace_pinned_owner(cli):
    path = import_owner(cli)
    envfile = path / "wrong.env"
    envfile.write_text("OURA_ACCESS_TOKEN=access-bob\n")
    code, result = cli("auth", "import-legacy", "--env-file", str(envfile))
    assert code != 0 and result["error"]["code"] == "account_mismatch"
    assert cli("status", "--live")[1]["account_id"] == "user-alice"


def test_expired_access_refreshes_and_rotates_refresh_token(cli, provider):
    path = import_owner(cli)
    configure(cli)
    f = path / "secrets.json"
    j = json.loads(f.read_text())
    j.update(
        access_token="expired", refresh_token="refresh-old", expires_at=time.time() - 100, token_kind="oauth"
    )
    f.write_text(json.dumps(j))
    code, result = cli("status", "--live")
    assert code == 0 and result["account_id"] == "user-alice"
    saved = json.loads(f.read_text())
    assert saved["refresh_token"] == "refresh-new" and saved["access_token"] == "access-rotated"
    assert provider["refreshes"] == 1
    assert "refresh-new" not in json.dumps(result)


def test_invalid_refresh_requires_login_without_destroying_old_grant(cli):
    path = import_owner(cli)
    configure(cli)
    f = path / "secrets.json"
    j = json.loads(f.read_text())
    j.update(access_token="expired", refresh_token="invalid", expires_at=0)
    f.write_text(json.dumps(j))
    code, result = cli("status", "--live")
    assert code != 0 and result["error"]["code"] == "login_required"
    assert json.loads(f.read_text())["refresh_token"] == "invalid"


def test_no_account_id_from_provider_never_binds_a_profile(cli, provider):
    create_owner(cli)
    provider["identities"]["access-alice"] = None
    f = cli.home / "candidate.env"
    f.write_text("OURA_ACCESS_TOKEN=access-alice")
    code, result = cli("auth", "import-legacy", "--env-file", str(f))
    assert code != 0 and result["error"]["code"] == "invalid_identity"
    assert not (cli.home / "owners/alice/secrets.json").exists()


def test_scope_denial_does_not_rotate_a_valid_refresh_token(cli, provider):
    path = import_owner(cli)
    configure(cli)
    p = path / "secrets.json"
    j = json.loads(p.read_text())
    j.update(token_kind="oauth", refresh_token="refresh-old", expires_at=time.time() + 3600)
    p.write_text(json.dumps(j))
    provider["scope_denied"] = {"heartrate"}
    code, value = cli("sync", "--endpoint", "heartrate")
    assert code != 0 and value["failed"]["heartrate"]["code"] == "permission_missing"
    assert provider["refreshes"] == 0
    assert json.loads(p.read_text())["refresh_token"] == "refresh-old"
