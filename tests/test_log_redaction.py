"""Logs never carry a full VIN."""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from custom_components.rivian.helpers import redact_vin

COMPONENT = Path(__file__).parent.parent / "custom_components" / "rivian"
VIN = "TESTVIN0000000001"
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
# A log argument whose source mentions one of these may hold a VIN: the VIN
# itself, or an id built from it (drive and session ids, meta keys).
SENSITIVE = re.compile(r"vin|drive_id|session_id", re.IGNORECASE)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (VIN, "…000001"),
        (f"{VIN}_1700000000", "…000001_1700000000"),
        (f"{VIN}_idle", "…000001_idle"),
        (f"energy_model:{VIN}", "energy_model:…000001"),
        (f"inferred:{VIN}:1700000000", "inferred:…000001:1700000000"),
        ("rivian:testvin0000000001_energy_kwh", "rivian:…000001_energy_kwh"),
        ("DEMO0R2EAGLE00001", "…E00001"),
        ([VIN, "OTHERVIN000000002"], "['…000001', '…000002']"),
        (None, "None"),
        ("1700000000", "1700000000"),
        ("a-shorter-id", "a-shorter-id"),
        ("ABCDEFGHIJKLMNOPQR", "ABCDEFGHIJKLMNOPQR"),  # 18 characters
    ],
)
def test_redact_vin(value, expected) -> None:
    """A VIN, alone or inside an id, keeps only its last 6 characters."""
    assert redact_vin(value) == expected


def _unredacted_log_arguments() -> list[str]:
    found = []
    for path in sorted(COMPONENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOGGER"
                and node.func.attr in LOG_METHODS
            ):
                continue
            if (
                node.args
                and isinstance(node.args[0], ast.JoinedStr)
                and SENSITIVE.search(ast.unparse(node.args[0]))
            ):
                found.append(f"{path.name}:{node.lineno} (f-string message)")
            for arg in node.args[1:]:
                if not SENSITIVE.search(ast.unparse(arg)):
                    continue
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id in {"redact_vin", "len"}
                ):
                    continue
                found.append(f"{path.name}:{node.lineno} {ast.unparse(arg)}")
    return found


def test_every_logged_vin_is_redacted() -> None:
    """Each log argument that may hold a VIN goes through redact_vin."""
    assert _unredacted_log_arguments() == []
