"""Helpers for Rivian component."""
from __future__ import annotations


def get_model_and_year(vin: str) -> tuple[str | None, str | None]:
    """Attempt to derive Rivian model information from VIN."""
    model: str | None = None
    year: str | None = None

    if vin[0:5] == "7FCTG":
        model = "R1T"
    elif vin[0:5] == "7PDSG":
        model = "R1S"

    if vin[9] == "N":
        year = "2022"
    elif vin[9] == "P":
        year = "2023"

    return model, year
