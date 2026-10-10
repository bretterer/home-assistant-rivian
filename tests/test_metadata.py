"""Consistency checks for the integration's metadata files.

These guard against the files drifting apart: the HACS and requirements Home
Assistant floors, the services described in ``services.yaml`` versus their
translations, and the config/options flow steps in ``strings.json`` versus
``translations/en.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "rivian"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _ha_floor_from_requirements() -> str:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^homeassistant>=([0-9.]+)\s*$", text, re.MULTILINE)
    assert match, "requirements.txt must pin a homeassistant>= floor"
    return match.group(1)


def test_manifest_basics() -> None:
    manifest = _load_json(INTEGRATION / "manifest.json")
    assert manifest["domain"] == "rivian"
    assert manifest["config_flow"] is True
    assert "version" in manifest
    assert any(
        req.startswith("rivian-python-client") for req in manifest["requirements"]
    )


def test_hacs_floor_matches_requirements() -> None:
    hacs = _load_json(ROOT / "hacs.json")
    assert hacs["homeassistant"] == _ha_floor_from_requirements()


def test_translations_cover_the_same_flow_steps() -> None:
    strings = _load_json(INTEGRATION / "strings.json")
    english = _load_json(INTEGRATION / "translations" / "en.json")
    for flow in ("config", "options"):
        steps = strings.get(flow, {}).get("step", {})
        assert set(steps) == set(english.get(flow, {}).get("step", {})), flow
        for step, body in steps.items():
            assert set(body.get("data", {})) == set(
                english[flow]["step"][step].get("data", {})
            ), f"{flow}.{step}"


def test_services_are_described_and_translated() -> None:
    services_file = INTEGRATION / "services.yaml"
    declared = set(
        (yaml.safe_load(services_file.read_text(encoding="utf-8")) or {})
        if services_file.exists()
        else {}
    )
    strings = set(_load_json(INTEGRATION / "strings.json").get("services", {}))
    english = set(
        _load_json(INTEGRATION / "translations" / "en.json").get("services", {})
    )
    assert declared == strings == english
