"""
Query translation (step 2): PubMed syntax → Scopus / Web of Science / CINAHL /
JSTOR syntax, via an LLM with the target database's field-tag rules as guidance.

LLM-assisted with human review (see SPEC §9 decision 2): the translated string is
saved to the SearchQuery for the target database and always shown for editing
before it's used. One synchronous Anthropic call per translation.
"""
DEFAULT_MODEL = "claude-sonnet-5"

# Concise, human-checkable syntax notes injected into the prompt per target DB.
DB_RULES = {
    "scopus": (
        "Scopus Advanced Search. Field tags: TITLE-ABS-KEY( ) for title/abstract/"
        "keywords, TITLE( ), ABS( ), KEY( ), AUTH( ). Boolean AND/OR/AND NOT "
        "(uppercase). Proximity W/n and PRE/n. Wildcards: * (multi), ? (single). "
        "Phrases in double quotes. No MeSH — map MeSH concepts to keyword terms."
    ),
    "wos": (
        "Web of Science Core Collection Advanced Search. Field tags with '=': "
        "TS= (topic: title/abstract/author-keywords/keywords-plus), TI= (title), "
        "AB= (abstract), AK= (author keywords). Boolean AND/OR/NOT (uppercase). "
        "Proximity NEAR/n. Wildcards: * (0+ chars), ? (1 char), $ (0-1). Phrases in "
        "double quotes. No MeSH — map MeSH concepts to topic terms."
    ),
    "cinahl": (
        "CINAHL (EBSCOhost). Field codes: TI (title), AB (abstract), MW/MH (subject "
        "headings — CINAHL headings, not MeSH), TX (all text). Boolean AND/OR/NOT. "
        "Wildcards: * (truncation), # (optional char), ? (single). Phrases in quotes."
    ),
    "jstor": (
        "JSTOR Advanced Search. Field prefixes: ti:(title), ab:(abstract), "
        "au:(author). Boolean AND/OR/NOT (uppercase). Proximity \"...\"~n. Wildcards: "
        "* and ?. Phrases in double quotes. No controlled vocabulary."
    ),
}

_SYSTEM = (
    "You are an expert research librarian who translates bibliographic database "
    "queries between syntaxes for systematic reviews. You preserve the search "
    "logic exactly — same concepts, same Boolean structure — and adapt only the "
    "field tags, operators, and wildcards to the target database. MeSH terms "
    "become free-text/keyword equivalents where the target has no controlled "
    "vocabulary. Return ONLY the translated query string, with no explanation, "
    "no code fences, no surrounding prose."
)


def translate_query(api_key: str, pubmed_query: str, target_db: str,
                    model: str = DEFAULT_MODEL) -> str:
    if target_db not in DB_RULES:
        raise ValueError(f"Unsupported target database: {target_db}")
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"Target database: {target_db}\n"
        f"Target syntax rules:\n{DB_RULES[target_db]}\n\n"
        f"PubMed query to translate:\n{pubmed_query}\n\n"
        f"Translated {target_db} query:"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
