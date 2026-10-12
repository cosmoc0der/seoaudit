from __future__ import annotations

import json
from pathlib import Path

from conftest import SITE
from seoaudit.checks.html import validators

OPTIONS = {"validator": "auto", "validator_pages": None, "validator_url": "https://v.test/", "validator_delay": 0}


async def test_local_validator_maps_messages_to_pages(monkeypatch):
    async def fake_vnu(cmd, env, timeout):
        folder = Path(cmd[-1])
        assert sorted(p.name for p in folder.iterdir()) == ["0.html", "1.html"]
        message = {"type": "error", "url": (folder / "1.html").as_uri(), "message": "Unclosed element"}
        return json.dumps({"messages": [message]}).encode(), b"", 0

    monkeypatch.setattr(validators, "run_process", fake_vnu)
    validator = validators.LocalValidator(["vnu"], None)
    validator.add(f"{SITE}/", "<!doctype html><title>ok</title>")
    validator.add(f"{SITE}/bad", "<p><div>")
    results, errors = await validator.run()

    assert results[f"{SITE}/"] == []
    assert [m["message"] for m in results[f"{SITE}/bad"]] == ["Unclosed element"]
    assert errors == []


async def test_missing_vnu_falls_back_to_online():
    diagnostics = []
    validator = await validators.create_validator({**OPTIONS, "vnu": "no-such-dir"}, None, diagnostics)
    assert isinstance(validator, validators.OnlineValidator)
    assert validator.max_pages == 5
    assert "no-such-dir" in diagnostics[0]
