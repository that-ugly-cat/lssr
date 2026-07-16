"""
prompts.py — every instruction sent to an LLM, in one place.

Centralised for auditing and explainability: the exact system prompts, the
database syntax rules, and the user-message shapes for each pipeline step live
here and nowhere else. The step modules (translate, screening, assessment,
synthesis) import from here and keep only orchestration — API calls, parsing,
cost, jobs. Changing what the model reads means changing this file.

Objects passed in (criteria, extraction fields) are used by duck typing only;
this module imports nothing from the app.
"""

# ══ Query translation (translate.py) ═════════════════════════════════════════

# Concise, human-checkable syntax notes injected into the prompt per database.
# Each entry does double duty: it describes the source when a query is being
# translated *from* that database, and the target when translating *to* it.
DB_RULES = {
    "pubmed": (
        "PubMed / MEDLINE. Field tags in square brackets after the term: [tiab] "
        "(title/abstract), [ti] (title), [ab] (abstract), [mh] (MeSH Terms), [majr] "
        "(MeSH major topic), [tw] (text word), [au] (author), [pdat] (publication "
        "date). MeSH is a controlled vocabulary. Boolean AND/OR/NOT (uppercase). "
        "Truncation with * (min 4 leading chars). Phrases in double quotes."
    ),
    "europepmc": (
        "Europe PMC (Lucene-based). Field prefixes as FIELD:term — TITLE:, "
        "ABSTRACT:, AUTH:, TITLE_ABS: (title or abstract), KW: (keywords, which "
        "INCLUDE the MeSH headings). Boolean AND/OR/NOT (uppercase), grouping with "
        "parentheses. Phrases in double quotes, wildcard *. "
        "MeSH: map a PubMed MeSH heading to KW:\"<exact heading>\" — Europe PMC's "
        "keyword field reliably recovers the MeSH-indexed set for multi-word "
        "headings (including ones containing \"and\", e.g. KW:\"Tissue and Organ "
        "Procurement\"). Do NOT use the MESH: field — it silently under-matches and "
        "collapses on multi-word headings. If a heading is a single very common "
        "word (e.g. Neoplasms), KW: over-matches, so use the TITLE_ABS free-text "
        "form instead. Do NOT put a year clause in the query — the tool applies the "
        "year window separately."
    ),
    "openalex": (
        "OpenAlex search string (Elasticsearch query_string over title/abstract). "
        "Boolean AND/OR/NOT must be UPPERCASE; parentheses for grouping; exact "
        "phrases in double quotes. No field tags and no controlled vocabulary — map "
        "MeSH and subject headings to free-text keyword terms. "
        "WILDCARDS: * and ? are NOT allowed inside a quoted phrase — OpenAlex "
        "rejects them there, so never write \"deceased donor*\". OpenAlex also "
        "auto-stems, so simple plurals are already covered (donor matches donors): "
        "just drop the trailing * (write \"deceased donor\"). When a truncation "
        "spans genuinely different word forms (legislat* -> legislation / "
        "legislative / legislature; \"donation rate*\" meant to catch rate and "
        "rates), expand it into an explicit OR of the full quoted forms instead of "
        "a wildcard, e.g. (\"donation rate\" OR \"donation rates\"). A single-word "
        "wildcard outside quotes (legislat*) is tolerated but prefer OR-expansion. "
        "Do NOT put a year clause in the query — the tool applies the year window "
        "as a separate filter."
    ),
    "eric": (
        "ERIC (Solr). Field prefixes as field:term — title:, author:, "
        "description: (abstract), subject: (ERIC Thesaurus descriptor). Boolean "
        "AND/OR/NOT (uppercase), parentheses for grouping, phrases in double "
        "quotes, wildcard *. Map MeSH to ERIC descriptors where an education "
        "equivalent exists, otherwise to free-text keywords. Do NOT add a year "
        "clause — the tool applies the year window separately."
    ),
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
    "embase-ovid": (
        "Embase on Ovid. Field suffixes appended to the term: .ti. (title), .ab. "
        "(abstract), .ti,ab. (title or abstract), .mp. (multi-purpose, the default "
        "if unspecified). Emtree subject headings: exp Term/ (exploded, includes "
        "narrower terms) or Term/ (unexploded). Boolean AND/OR/NOT. Adjacency adjN "
        "(within N words, any order). Truncation *, wildcard ? (single char). Map "
        "MeSH to Emtree headings (exp Heading/)."
    ),
    "embase-ebsco": (
        "Embase on EBSCOhost. Field codes: TI (title), AB (abstract), DE (Emtree "
        "subject terms), TX (all text). Boolean AND/OR/NOT. Proximity Nn (near, any "
        "order) and Wn (within, in order). Wildcards: * (truncation), # (optional "
        "char), ? (single). Phrases in quotes. Map MeSH to DE Emtree terms."
    ),
    "psycinfo-ovid": (
        "APA PsycInfo on Ovid. Field suffixes: .ti. (title), .ab. (abstract), "
        ".ti,ab. (title or abstract), .mp. (multi-purpose default). APA Thesaurus "
        "descriptors: exp Term/ (exploded) or Term/. Boolean AND/OR/NOT, adjacency "
        "adjN, truncation *, wildcard ?. Map MeSH to APA Thesaurus of Psychological "
        "Index Terms descriptors (exp Descriptor/)."
    ),
    "psycinfo-ebsco": (
        "APA PsycInfo on EBSCOhost. Field codes: TI (title), AB (abstract), DE "
        "(descriptors — APA Thesaurus), SU (subjects). Boolean AND/OR/NOT. "
        "Proximity Nn (any order) and Wn (in order). Wildcards: * (truncation), # "
        "(optional char), ? (single). Phrases in quotes. Map MeSH to DE APA "
        "descriptors."
    ),
    "philpapers": (
        "PhilPapers search. Limited field support — treat as a free-text keyword "
        "search. Boolean AND/OR/NOT and exact phrases in double quotes. No "
        "controlled vocabulary usable in the query: map MeSH and subject headings "
        "to plain keyword terms. Aim for recall over precision."
    ),
    "heinonline": (
        "HeinOnline (Lucene-like). Field prefixes as field:term — title:, text:, "
        "creator: (author). Boolean AND/OR/NOT (uppercase), parentheses for "
        "grouping, phrases in double quotes, wildcards * and ?, proximity \"...\"~n. "
        "Legal database with no biomedical controlled vocabulary — map MeSH to "
        "free-text keyword terms."
    ),
}


# How to express a publication-year window in each translation-only database.
# Harvest databases are absent on purpose: their year window is applied by the
# harvest job in the source's own syntax, not baked into the translated string.
def db_date_syntax(db: str, yf: int, yt: int) -> str | None:
    return {
        "scopus": f"PUBYEAR > {yf - 1} AND PUBYEAR < {yt + 1}",
        "wos": f"AND PY=({yf}-{yt})",
        "cinahl": f"AND (PY {yf}-{yt}), or the EBSCO Publication Date limiter {yf}-{yt}",
        "jstor": f"restrict to {yf}-{yt} with JSTOR's date-range limiter (no reliable inline year field)",
        "embase-ovid": f'AND ({yf}:{yt}).yr., or: limit results to yr="{yf}-{yt}"',
        "embase-ebsco": f"AND (PY {yf}-{yt}), or the EBSCO Publication Date limiter",
        "psycinfo-ovid": f'AND ({yf}:{yt}).yr., or: limit results to yr="{yf}-{yt}"',
        "psycinfo-ebsco": f"AND (PY {yf}-{yt}), or the EBSCO Publication Date limiter",
        "philpapers": f"{yf}-{yt} (PhilPapers has no query-string date field — apply it in the interface)",
        "heinonline": f"{yf}-{yt} (HeinOnline date-range facet: yearlo={yf}, yearhi={yt})",
    }.get(db)


TRANSLATE_SYSTEM = (
    "You are an expert research librarian who translates bibliographic database "
    "queries between syntaxes for systematic reviews. You preserve the search "
    "logic exactly — same concepts, same Boolean structure — and adapt only the "
    "field tags, operators, and wildcards to the target database. Controlled-"
    "vocabulary terms (MeSH, Emtree, thesaurus descriptors) become the target's "
    "equivalent controlled vocabulary, or free-text/keyword equivalents where the "
    "target has none. Return ONLY the translated query string as plain text — no "
    "explanation, NO code fences, NO triple backticks (```), no surrounding prose."
)


def translate_user(source_db: str, source_query: str, target_db: str,
                   year_from: int | None, year_to: int | None,
                   apply_years: bool) -> str:
    """The user message for one translation, including the optional year note."""
    source_rules = DB_RULES.get(source_db, "(unknown source syntax — infer from the query)")
    year_note = ""
    if apply_years and year_from and year_to:
        hint = db_date_syntax(target_db, year_from, year_to)
        if hint:
            year_note = (
                f"\nAlso restrict the query to publication years {year_from}–{year_to}. "
                f"In {target_db}, express this as: {hint}. Integrate it into the query "
                f"with AND when the syntax is inline; if the database only offers a UI "
                f"date limiter, append it as a short parenthetical note rather than "
                f"inventing inline syntax.\n"
            )
    return (
        f"Source database: {source_db}\n"
        f"Source syntax rules:\n{source_rules}\n\n"
        f"Target database: {target_db}\n"
        f"Target syntax rules:\n{DB_RULES[target_db]}\n"
        f"{year_note}\n"
        f"Query to translate (written in {source_db} syntax):\n{source_query}\n\n"
        f"Translated {target_db} query:"
    )


# ══ Screening 1 — title + abstract (screening.py) ════════════════════════════

SCREENING_SYSTEM = """\
You are screening records for a scoping review at the TITLE + ABSTRACT stage.

Research question:
{rq}

Exclude a record if it meets one or more of these exclusion criteria:
{criteria}

Rules:
- This is a first-pass title/abstract screen. Apply the exclusion criteria
  whenever one is met, and exclude records clearly off-topic.
- Use "maybe" when you genuinely cannot tell from the title and abstract alone
  (missing abstract, ambiguous scope). Do NOT default to "include" out of
  caution — park the uncertain ones as "maybe" for a human to look at.
- Reserve "include" for records that clearly fit and meet no exclusion criterion.

Return ONLY a JSON object, no prose, no code fences:
  {{"decision": "include" | "exclude" | "maybe", "reason": "<one sentence; name the criterion if excluding>"}}"""


def screening_system(research_question, exclusion_criteria) -> str:
    crit = "\n".join(f"- {c.label}: {c.description or ''}".rstrip() for c in exclusion_criteria)
    return SCREENING_SYSTEM.format(rq=(research_question or "(not specified)").strip(),
                                   criteria=crit or "(no exclusion criteria defined)")


def screening_user(title, abstract) -> str:
    return f"Title: {title or '(no title)'}\n\nAbstract: {abstract or '(no abstract)'}"


# ══ Assessment — screening 2 + extraction, on full text (assessment.py) ══════

ASSESSMENT_SYSTEM = """\
You are conducting the FULL-TEXT stage of a scoping review.

Research question:
{rq}

STEP 1 — Inclusion decision. Include the study only if it meets ALL of these
inclusion criteria; exclude it if any is clearly not met. Use "maybe" only when
the full text genuinely does not settle it.
{inclusion}

STEP 2 — Data extraction (only when the decision is "include"). Fill the fields
below from the full text. Rules:
- Use the exact allowed values for select/multiselect fields; multiselect values
  must be a JSON array of those strings.
- Omit any field you cannot ground in the text — never guess.
- Fill a conditional field only when its stated condition holds.
- For free-text fields (text/textarea), where possible support your answer with a
  short EXACT quote from the article, copied verbatim inside «guillemets», after
  your answer.
{fields}

Return ONLY a JSON object, no prose, no code fences:
  {{"inclusion_decision": "include" | "exclude" | "maybe",
    "inclusion_reason": "<one sentence>",
    "fields": {{"<field key>": <value>, ...}}}}
Use an empty object for "fields" when the decision is not "include"."""


def _fields_spec(fields) -> str:
    lines = []
    for f in fields:
        parts = [f"- {f.key} ({f.field_type}) — {f.label}"]
        if f.help:
            parts.append(f"note: {f.help}")
        opts = f.options()
        if opts:
            parts.append("allowed values: " + " | ".join(opts))
        if f.show_if_key:
            parts.append(f"only when {f.show_if_key} is one of: " + ", ".join(f.show_if_values()))
        lines.append("\n    ".join(parts))
    return "\n".join(lines) or "(no extraction fields defined)"


def assessment_system(rq, inclusion_criteria, fields) -> str:
    inc = "\n".join(f"- {c.label}: {c.description or ''}".rstrip() for c in inclusion_criteria)
    return ASSESSMENT_SYSTEM.format(rq=(rq or "(not specified)").strip(),
                                    inclusion=inc or "(no inclusion criteria defined)",
                                    fields=_fields_spec(fields))


def assessment_user(full_text) -> str:
    return f"Full text:\n\n{full_text}"


# ══ Synthesis — narrative per assessment criterion (synthesis.py) ════════════

SYNTHESIS_SYSTEM = """\
You are writing the results section of a scoping review. For the theme below,
synthesize the provided per-study findings into one coherent narrative paragraph
(or a few, if warranted). Cite each study you draw on by inserting ITS TOKEN
exactly as given, in square brackets, e.g. [S1]; put the token right after the
statement it supports. Do NOT write author names, years, DOIs, or links yourself —
only the token. Do not invent findings or tokens; use only the material provided.
Be concise and neutral.

Return only the narrative prose, no headings, no preamble."""


def synthesis_user(research_question, theme, items) -> str:
    body = "\n\n".join(f"[{it['token']}] {it['finding']}" for it in items)
    return (f"Research question: {research_question or '(not specified)'}\n\n"
            f"Theme (assessment criterion): {theme}\n\n"
            f"Findings to synthesize (each prefixed by its study token):\n{body}")
