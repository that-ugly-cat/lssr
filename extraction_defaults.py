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

# An empirical study is classified on three independent axes, one choice each.
METH_DESIGN = ["Observational", "Experimental", "Descriptive", "Analytical"]
METH_DATA   = ["Quantitative", "Qualitative", "Mixed methods"]
METH_TIME   = ["Longitudinal", "Cross-sectional"]

METHODOLOGY_LITREV = ["Traditional (narrative) review", "Rapid review", "Scoping review",
                      "Systematic review", "Meta analysis"]


def builtin_fields() -> list[dict]:
    """Ordered builtin field definitions. `show_if` gates a field on another
    field's value (that's how 'study type opens other options' is expressed).
    Empirical methodology is three single-choice axes, not one flat list."""
    return [
        {"key": "country", "label": "Country", "field_type": "multiselect",
         "options": _countries(), "help": "Country/countries the study concerns."},
        {"key": "study_year", "label": "Study year", "field_type": "number",
         "help": "Year the study was conducted (may differ from publication year)."},
        {"key": "study_type", "label": "Study type", "field_type": "select",
         "options": STUDY_TYPES},
        {"key": "methodology_design", "label": "Methodology — design", "field_type": "select",
         "options": METH_DESIGN, "show_if_key": "study_type", "show_if_values": ["Empirical"],
         "help": "Empirical study design (one)."},
        {"key": "methodology_data", "label": "Methodology — data", "field_type": "select",
         "options": METH_DATA, "show_if_key": "study_type", "show_if_values": ["Empirical"],
         "help": "Nature of the data (one)."},
        {"key": "methodology_time", "label": "Methodology — timeframe", "field_type": "select",
         "options": METH_TIME, "show_if_key": "study_type", "show_if_values": ["Empirical"],
         "help": "Temporal design (one)."},
        {"key": "methodology_litrev", "label": "Methodology (literature review)",
         "field_type": "select", "options": METHODOLOGY_LITREV,
         "show_if_key": "study_type", "show_if_values": ["Literature review"]},
        {"key": "methodology_other", "label": "Methodology (other)", "field_type": "text",
         "show_if_key": "study_type", "show_if_values": ["Other"],
         "help": "Free text — only for study type 'Other'."},
    ]
