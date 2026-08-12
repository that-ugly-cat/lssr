"""Author-string handling.

Canonical storage format: authors separated by "; ", each author kept in the
form the source provides — "Rossi, Mario", "Rossi M", or "Mario Rossi".
split_authors() also copes with legacy comma-joined strings (heuristically:
multi-word surnames in "Surname, Given, Surname, Given" lists defeat it);
author_key() merges the different forms of the same person for counting.
"""
import re

_INITIALS = re.compile(r"^[A-Z]{1,3}$")


def is_initials(tok: str) -> bool:
    """True for an initials token: 'M', 'M.', 'MJ', 'M.J.', 'J.-P.'."""
    core = re.sub(r"[.\-‐\s]", "", tok)
    return bool(_INITIALS.match(core))


def split_authors(raw: str | None) -> list[str]:
    """One authors string → list of single-author strings."""
    if not raw or not raw.strip():
        return []
    raw = raw.strip().rstrip(".")
    if ";" in raw:
        return [a.strip() for a in raw.split(";") if a.strip()]
    pieces = [p.strip() for p in re.split(r",|\s+and\s+", raw) if p.strip()]
    authors = []
    for p in pieces:
        if authors and is_initials(p):     # 'Rossi' + 'M' → 'Rossi, M'
            authors[-1] += ", " + p
        else:
            authors.append(p)
    # ERIC-style 'Surname, Given, Surname, Given': every piece a single
    # full word and the count is even → pair them up
    if (len(authors) >= 2 and len(authors) % 2 == 0
            and all(len(a.split()) == 1 and "," not in a for a in authors)):
        authors = [f"{authors[i]}, {authors[i + 1]}"
                   for i in range(0, len(authors), 2)]
    return authors


def join_authors(names) -> str:
    """List of single-author strings → canonical '; '-separated string."""
    return "; ".join(n for n in (str(x).strip().rstrip(",") for x in names) if n)


def canonicalize(raw: str | None) -> str:
    """Legacy authors string → canonical '; '-separated string."""
    return join_authors(split_authors(raw))


def _parts(name: str) -> tuple[str, str]:
    """Single-author string → (surname, given), best effort."""
    name = name.strip()
    if "," in name:
        surname, given = (x.strip() for x in name.split(",", 1))
        return surname, given
    toks = name.split()
    if len(toks) == 1:
        return toks[0], ""
    if is_initials(toks[-1]):              # 'Rossi M', 'van der Berg JW'
        return " ".join(toks[:-1]), toks[-1]
    return toks[-1], " ".join(toks[:-1])   # 'Mario Rossi'


def surname_of(name: str) -> str:
    return _parts(name)[0]


def author_key(name: str) -> str:
    """Merge key: 'Mario Rossi', 'Rossi M' and 'Rossi, Mario' all → 'rossi|m'."""
    surname, given = _parts(name)
    return f"{surname.lower()}|{given[:1].lower()}"
