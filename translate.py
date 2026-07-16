"""
Query translation (step 2): rewrite a search query from one bibliographic
database's syntax into another's, via an LLM given both databases' field-tag
rules as guidance.

LLM-assisted with human review (see SPEC §9 decision 2): the translated string is
saved to the SearchQuery for the target database and always shown for editing
before it's used. One synchronous Anthropic call per translation. The *source*
database is the workspace's primary (the one the canonical query is authored in),
so translation is source-aware, not hard-wired to PubMed.
"""
DEFAULT_MODEL = "claude-sonnet-5"

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
        "ABSTRACT:, AUTH:, KW: (keywords), MESH: (MeSH heading), TITLE_ABS: "
        "(title or abstract). Boolean AND/OR/NOT (uppercase), grouping with "
        "parentheses. Phrases in double quotes, wildcard *. Retains MeSH "
        "(MESH:\"...\"), so MeSH concepts map almost 1:1 from PubMed. Do NOT put a "
        "year clause in the query — the tool applies the year window separately."
    ),
    "openalex": (
        "OpenAlex search string (Elasticsearch query_string over title/abstract). "
        "Boolean AND/OR/NOT must be UPPERCASE; parentheses for grouping; exact "
        "phrases in double quotes. No field tags and no controlled vocabulary — map "
        "MeSH and subject headings to free-text keyword terms. Do NOT put a year "
        "clause in the query — the tool applies the year window as a separate filter."
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

_SYSTEM = (
    "You are an expert research librarian who translates bibliographic database "
    "queries between syntaxes for systematic reviews. You preserve the search "
    "logic exactly — same concepts, same Boolean structure — and adapt only the "
    "field tags, operators, and wildcards to the target database. Controlled-"
    "vocabulary terms (MeSH, Emtree, thesaurus descriptors) become the target's "
    "equivalent controlled vocabulary, or free-text/keyword equivalents where the "
    "target has none. Return ONLY the translated query string, with no "
    "explanation, no code fences, no surrounding prose."
)


def translate_query(api_key: str, source_query: str, target_db: str,
                    source_db: str = "pubmed", model: str = DEFAULT_MODEL) -> str:
    if target_db not in DB_RULES:
        raise ValueError(f"Unsupported target database: {target_db}")
    if source_db == target_db:
        return source_query
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    source_rules = DB_RULES.get(source_db, "(unknown source syntax — infer from the query)")
    prompt = (
        f"Source database: {source_db}\n"
        f"Source syntax rules:\n{source_rules}\n\n"
        f"Target database: {target_db}\n"
        f"Target syntax rules:\n{DB_RULES[target_db]}\n\n"
        f"Query to translate (written in {source_db} syntax):\n{source_query}\n\n"
        f"Translated {target_db} query:"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
