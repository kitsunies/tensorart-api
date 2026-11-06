import json

from tensorart_api import cli, service

from .fake import fake_upstream


def use_fake(monkeypatch):
    monkeypatch.setattr(cli, "Upstream", lambda: fake_upstream())


def test_search_text(monkeypatch, capsys):
    use_fake(monkeypatch)
    assert cli.main(["characters", "item", "-s", "tensorhub", "-n", "2"]) == 0
    out, err = capsys.readouterr()
    assert out.count("\n") == 2 and "[character]" in out and "tensorhub  ok" in out
    assert "--cursor" in err


def test_search_json(monkeypatch, capsys):
    use_fake(monkeypatch)
    assert cli.main(["search", "item", "-t", "lora,dora", "-g", "blocked", "-n", "3", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert len(body["items"]) == 3 and not any(p["generation"]["allowed"] for p in body["items"])


def test_check_exit_codes(monkeypatch, capsys):
    use_fake(monkeypatch)
    assert cli.main(["check", "m1"]) == 0
    assert cli.main(["check", "m1", "self1"]) == 3
    assert "blocked:owner_only" in capsys.readouterr().out
    assert cli.main(["model", "missing"]) == 2


def test_facets(capsys):
    assert cli.main(["facets", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == service.facets().model_dump()
