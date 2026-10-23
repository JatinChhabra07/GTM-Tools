import json

import yaml
from typer.testing import CliRunner

from gtm import config
from gtm.cli import app

runner = CliRunner()


def _create(slug="acme"):
    return runner.invoke(app, ["product", "create", "--slug", slug, "--name", "Acme", "--url", "https://acme.com"])


def test_create_show_and_list():
    r = _create()
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["signal_codes_seeded"] > 20
    assert "PRODUCT: acme | Acme | https://acme.com" in runner.invoke(app, ["product", "show", "--product", "acme"]).output
    assert json.loads(runner.invoke(app, ["product", "list"]).output) == ["acme"]
    assert _create().exit_code == 1  # no silent overwrite


def test_bad_slug_refused():
    assert _create("../etc").exit_code == 1


def test_defaults_are_safe():
    _create()
    cfg = config.load("acme")
    assert cfg.usage.profile == "max5x"
    assert cfg.geo_policy["IN"].status == "blocked"
    assert cfg.geo_policy["default"].status == "blocked"
    assert not hasattr(cfg.sending, "auto_send")


def test_merge_validates_and_blocks_unsafe_changes(tmp_path):
    _create()
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump({"usage": {"profile": "max20x"}, "voice": {"sender_name": "Ravi"}}))
    assert runner.invoke(app, ["config", "merge", "--product", "acme", "--file", str(ok)]).exit_code == 0
    assert config.load("acme").usage.profile == "max20x"

    india = tmp_path / "in.yaml"
    india.write_text(yaml.safe_dump({"geo_policy": {"IN": {"status": "allowed_with_notice", "notice": "n"}}}))
    r = runner.invoke(app, ["config", "merge", "--product", "acme", "--file", str(india)])
    assert r.exit_code == 1 and "legal_signoff" in r.output

    autosend = tmp_path / "auto.yaml"
    autosend.write_text(yaml.safe_dump({"sending": {"auto_send": True}}))
    assert runner.invoke(app, ["config", "merge", "--product", "acme", "--file", str(autosend)]).exit_code == 1

    slug = tmp_path / "slug.yaml"
    slug.write_text(yaml.safe_dump({"product": {"slug": "other"}}))
    assert runner.invoke(app, ["config", "merge", "--product", "acme", "--file", str(slug)]).exit_code == 1


def test_human_only_commands_refuse_inside_claude_code(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    for cmd in (["keys", "init"], ["secrets", "set"], ["keys", "change-pin"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 1 and "your own terminal" in r.output


def test_setup_status_never_prints_secret_values(monkeypatch):
    from gtm import secrets
    secrets.set_value("APIFY_API_TOKEN", "apify_api_SUPERSECRET")
    _create()
    out = runner.invoke(app, ["setup", "status", "--product", "acme"]).output
    assert "SUPERSECRET" not in out
    data = json.loads(out)
    assert data["secrets"]["APIFY_API_TOKEN"] == "set"
    assert any("clients.csv" in m for m in data["product"]["missing"])
