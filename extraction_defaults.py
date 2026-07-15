"""
Built-in (stable) extraction fields, seeded into every workspace and editable
afterwards. Mirrors RevMaster's fixed columns: country, study year, study type,
and a methodology whose options depend on study type — modelled here as two
conditional fields gated by `show_if`.
"""
from pathlib import Path

_CFG = Path(__file__).parent / "configs"


def _countries() -> list[str]:
    try:
        return [ln.strip() for ln in (_CFG / "countries.txt").read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    except OSError:
        return []


STUDY_TYPES = ["Theoretical", "Empirical", "Literature review", "Viewpoint/commentary", "Other"]

METHODOLOGY_EMPIRICAL = ["Observational", "Experimental", "Descriptive", "Analytical",
                         "Quantitative", "Qualitative", "Mixed methods", "Longitudinal",
                         "Cross-sectional"]

METHODOLOGY_LITREV = ["Traditional (narrative) review", "Rapid review", "Scoping review",
                      "Systematic review", "Meta analysis"]


def builtin_fields() -> list[dict]:
    """Ordered builtin field definitions. `show_if` gates a field on another
    field's value (that's how 'study type opens other options' is expressed)."""
    return [
        {"key": "country", "label": "Country", "field_type": "multiselect",
         "options": _countries(), "help": "Country/countries the study concerns."},
        {"key": "study_year", "label": "Study year", "field_type": "number",
         "help": "Year the study was conducted (may differ from publication year)."},
        {"key": "study_type", "label": "Study type", "field_type": "select",
         "options": STUDY_TYPES},
        {"key": "methodology_empirical", "label": "Methodology (empirical)",
         "field_type": "select", "options": METHODOLOGY_EMPIRICAL,
         "show_if_key": "study_type", "show_if_values": ["Empirical"]},
        {"key": "methodology_litrev", "label": "Methodology (literature review)",
         "field_type": "select", "options": METHODOLOGY_LITREV,
         "show_if_key": "study_type", "show_if_values": ["Literature review"]},
    ]
