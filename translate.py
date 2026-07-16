"""
Query translation (step 2): rewrite a search query from one bibliographic
database's syntax into another's, via an LLM given both databases' field-tag
rules as guidance.

LLM-assisted with human review (see SPEC §9 decision 2): the translated string is
saved to the SearchQuery for the target database and always shown for editing
before it's used. One synchronous Anthropic call per translation. The *source*
database is the workspace's primary (the one the canonical query is authored in),
so translation is source-aware, not hard-wired to PubMed.

All prompt text lives in prompts.py (DB_RULES, TRANSLATE_SYSTEM, translate_user);
this module keeps only the API call and the defensive fence-stripping.
"""
from prompts import DB_RULES, TRANSLATE_SYSTEM, translate_user

DEFAULT_MODEL = "claude-sonnet-5"


def _strip_fences(s: str) -> str:
    """Defensively remove a ```-fenced wrapper the model sometimes adds despite
    the instruction not to."""
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s.strip("`").strip()


def translate_query(api_key: str, source_query: str, target_db: str,
                    source_db: str = "pubmed", year_from: int | None = None,
                    year_to: int | None = None, apply_years: bool = False,
                    model: str = DEFAULT_MODEL) -> str:
    if target_db not in DB_RULES:
        raise ValueError(f"Unsupported target database: {target_db}")
    if source_db == target_db:
        return source_query
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    prompt = translate_user(source_db, source_query, target_db, year_from, year_to, apply_years)
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=TRANSLATE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return _strip_fences(text)
