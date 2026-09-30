"""
Synthesis (step 10): the public deliverable.

Builds the PRISMA flow counts, then a sequence of blocks:
  • Block 0 — "Study characteristics": a procedural distribution summary of the
    structured "fixed variable" fields (select/multiselect/number: country, study
    year, study type, methodology axes…). No LLM, so no miscounted figures.
  • One block per free-text (text/textarea) field the review ticks for synthesis
    (ExtractionField.in_synthesis): the LLM aggregates the per-study findings
    into a narrative paragraph — or, with Workspace.synthesis_group_key set, one
    paragraph per value of that select/multiselect field. Citations are NOT authored
    by the LLM — it only inserts a study token ([S1], [S2]…) which we substitute
    procedurally with a number, in order of first appearance, linked to a final
    "References" block whose entries are built from the record (authors, year,
    title, journal, DOI), so a citation can never be hallucinated. With grouping,
    each group can open with a procedural counts line over chosen fields.

Values come from each record's authoritative extraction (curated final row, else
the latest reviewer's, else the model draft). Stored as Synthesis + SynthesisBlock
rows, shown on the public /r/{token} page when published. Background job, JOBS
keyed by workspace_id.
"""
import json
import re
import threading

JOBS: dict[int, dict] = {}
_lock = threading.Lock()


def get_job(workspace_id: int) -> dict | None:
    with _lock:
        return JOBS.get(workspace_id)


def _set(workspace_id: int, data: dict):
    with _lock:
        JOBS[workspace_id] = data


# ── PRISMA counts ──────────────────────────────────────────────────────────────

def compute_prisma(db, workspace_id: int) -> dict:
    from sqlalchemy import func
    from models import DB_LABELS, Record, RawReference
    R = Record
    def rc(*filters):
        return db.query(R).filter(R.workspace_id == workspace_id, *filters).count()

    # records identified per source database (one RawReference per provenance)
    by_source = {}
    for dbkey, n in (db.query(RawReference.database, func.count())
                       .filter(RawReference.workspace_id == workspace_id)
                       .group_by(RawReference.database).all()):
        by_source[DB_LABELS.get(dbkey, dbkey or "other")] = n
    identified = db.query(RawReference).filter(RawReference.workspace_id == workspace_id).count()
    # Both the automatic dedup at ingest and the manual merge pass produce
    # duplicates; the manual one soft-deletes its loser instead of dropping the
    # row. Counting rows without filtering is_removed therefore reports merged
    # duplicates as survivors, and they then vanish between 'without duplicates'
    # and 'screened' with no arrow accounting for them.
    records_total = rc(R.is_removed == False)                              # noqa: E712
    screened = records_total
    included_s1 = rc(R.is_removed == False, R.screen1_decision == "include")  # noqa: E712
    retrieved = rc(R.is_removed == False, R.screen1_decision == "include",   # noqa: E712
                   R.full_text_status == "converted")
    included_final = rc(R.is_removed == False, R.screen2_decision == "include")  # noqa: E712
    out = {
        "identified": identified,
        "by_source": by_source,
        "duplicates_removed": max(identified - records_total, 0),
        "screened": screened,
        "excluded_screen1": rc(R.is_removed == False, R.screen1_decision == "exclude"),  # noqa: E712
        "included_screen1": included_s1,
        "fulltext_sought": included_s1,
        "fulltext_retrieved": retrieved,
        "fulltext_not_retrieved": max(included_s1 - retrieved, 0),
        "assessed": rc(R.is_removed == False, R.screen1_decision == "include",  # noqa: E712
                       R.screen2_decision.in_(["include", "exclude"])),
        "excluded_screen2": rc(R.is_removed == False, R.screen2_decision == "exclude"),  # noqa: E712
        "included_final": included_final,
    }
    arms = _prisma_arms(db, workspace_id, out)
    if arms:
        out["arms"] = arms
    return out


def _prisma_arms(db, workspace_id: int, total: dict) -> dict | None:
    """Split the flow into the two arms PRISMA 2020 draws: records found by
    searching databases and registers, and records found by any other route.

    Returns None when nothing arrived by another route, which is every review
    that does not use the manual arm — the caller then renders exactly as
    before, and the feature costs those reviews nothing.

    The membership rule is the whole design: **a record belongs to the other
    arm only when none of its provenances is a database**. It is independent of
    the order things were imported in (a nomination that a later harvest also
    finds moves to the database arm, and so does the reverse), it matches how
    PRISMA counts a paper found by both, and it makes the right-hand column a
    measurement of the search strategy's recall rather than of the nominator's
    diligence: it counts what the experts found *that the query did not*.
    """
    from sqlalchemy import func
    from models import DB_LABELS, Record, RawReference
    RR, R = RawReference, Record

    other_raw = (db.query(func.count(RR.id))
                   .filter(RR.workspace_id == workspace_id,
                           RR.via_other_methods == True).scalar() or 0)  # noqa: E712
    if not other_raw:
        return None

    # Records with at least one database provenance: the left arm, by definition.
    db_rec_ids = {rid for (rid,) in
                  db.query(RR.record_id)
                    .filter(RR.workspace_id == workspace_id,
                            RR.via_other_methods == False,          # noqa: E712
                            RR.record_id.isnot(None)).distinct().all()}
    # Records any other-methods reference points at, whether or not the search
    # had already found them.
    other_touched = {rid for (rid,) in
                     db.query(RR.record_id)
                       .filter(RR.workspace_id == workspace_id,
                               RR.via_other_methods == True,        # noqa: E712
                               RR.record_id.isnot(None)).distinct().all()}

    live = {rid for (rid,) in db.query(R.id).filter(R.workspace_id == workspace_id,
                                                    R.is_removed == False).all()}  # noqa: E712
    other_ids = (other_touched - db_rec_ids) & live
    already_found = len((other_touched & db_rec_ids) & live)

    def counts(ids: set, identified: int, by_source: dict) -> dict:
        if ids:
            rows = (db.query(R.screen1_decision, R.screen2_decision, R.full_text_status)
                      .filter(R.id.in_(ids)).all())
        else:
            rows = []
        inc1 = [r for r in rows if r[0] == "include"]
        return {
            "identified": identified,
            "by_source": by_source,
            "records": len(ids),
            "screened": len(ids),
            "excluded_screen1": sum(1 for r in rows if r[0] == "exclude"),
            "included_screen1": len(inc1),
            "fulltext_sought": len(inc1),
            "fulltext_retrieved": sum(1 for r in inc1 if r[2] == "converted"),
            "fulltext_not_retrieved": sum(1 for r in inc1 if r[2] != "converted"),
            "assessed": sum(1 for r in inc1 if r[1] in ("include", "exclude")),
            "excluded_screen2": sum(1 for r in rows if r[1] == "exclude"),
            "included_final": sum(1 for r in rows if r[1] == "include"),
        }

    def by_source_for(flag: bool) -> dict:
        out = {}
        for dbkey, n in (db.query(RR.database, func.count())
                           .filter(RR.workspace_id == workspace_id,
                                   RR.via_other_methods == flag)
                           .group_by(RR.database).all()):
            out[DB_LABELS.get(dbkey, dbkey or "other")] = n
        return out

    db_ids = db_rec_ids & live
    left = counts(db_ids, total["identified"] - other_raw, by_source_for(False))
    right = counts(other_ids, other_raw, by_source_for(True))
    # Duplicates are a *difference*, not a population, and the difference does
    # not split by arm: a left-arm record can carry references from both. So the
    # left column keeps the one subtraction it always had (its own references
    # minus its own records), and the right column reports instead the two
    # numbers a methods section actually wants — how many nominations the search
    # had already found, and how many were genuinely new.
    left["duplicates_removed"] = max(left["identified"] - left["records"], 0)
    right["already_found_by_search"] = already_found
    right["duplicates_removed"] = max(right["identified"] - already_found - right["records"], 0)
    return {"db": left, "other": right}


# ── PRISMA flow diagram (inline SVG, theme-aware) ───────────────────────────────

_SVG_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
_LH = 15   # line height inside a flow box


def _esc(s):
    import html as _h
    return _h.escape(str(s))


def _box(x, y0, w, h, lines, bold=False, boldlast=False, muted=False, dashed=False):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out = [f'<rect x="{x}" y="{y0}" width="{w}" height="{h}" rx="6" '
           f'fill="var(--card)" stroke="var(--border)" stroke-width="1"{dash}/>']
    n, cx = len(lines), x + w / 2
    sy = y0 + h / 2 - (n - 1) * _LH / 2
    for i, ln in enumerate(lines):
        fw = "700" if (bold or (boldlast and i == n - 1)) else "400"
        col = "var(--muted-2)" if muted else "var(--text)"
        out.append(f'<text x="{cx:.0f}" y="{sy + i * _LH:.0f}" text-anchor="middle" '
                   f'dominant-baseline="central" font-size="12" font-weight="{fw}" '
                   f'fill="{col}">{_esc(ln)}</text>')
    return "".join(out)


def prisma_svg(prisma: dict, steps_done=None):
    """Render the PRISMA counts as a top-to-bottom flow diagram (inline SVG).
    Colours come from the page's CSS custom properties, so it follows the theme.
    With steps_done, stages whose pipeline step isn't marked done render as
    muted dashed 'pending' placeholders instead of counts.
    Returns Markup so templates can drop it in with no escaping."""
    from markupsafe import Markup
    if not prisma:
        return Markup("")
    p = prisma
    # Two columns only when something actually arrived by another route. Every
    # other review — and every snapshot frozen before this existed, which has no
    # 'arms' key at all — renders exactly as it did before.
    arms = p.get("arms") or {}
    if (arms.get("other") or {}).get("identified"):
        return _prisma_svg_two_arms(p, arms, steps_done)
    done = None if steps_done is None else set(steps_done)
    src = p.get("by_source") or {}
    ident = [f"{k}: {v}" for k, v in src.items()]
    ident += [f"Total: {p.get('identified', 0)}"] if ident else [f"Records identified: {p.get('identified', 0)}"]
    ident.append(f"Without duplicates: {p.get('identified', 0) - p.get('duplicates_removed', 0)}")
    stages = [
        {"g": "Identification", "step": "records", "pending_title": "Records identified",
         "lines": ident, "boldlast": bool(src),
         "side": [f"Duplicates removed: {p.get('duplicates_removed', 0)}"]},
        {"g": "Screening", "step": "screening", "pending_title": "Screening 1 (title/abstract)",
         "lines": ["Screened vs exclusion criteria",
                   f"(screening 1): {p.get('screened', 0)}"],
         "side": [f"Excluded: {p.get('excluded_screen1', 0)}"]},
        {"g": "Screening", "step": "fulltext", "pending_title": "Full text retrieval",
         "lines": [f"Full texts retrieved: {p.get('fulltext_retrieved', 0)}",
                   f"of {p.get('fulltext_sought', 0)} sought"],
         "side": [f"Not retrieved: {p.get('fulltext_not_retrieved', 0)}"]},
        {"g": "Screening", "step": "assessment", "pending_title": "Assessment (screening 2)",
         "lines": ["Assessed vs inclusion criteria",
                   f"(screening 2): {p.get('assessed', 0)}"],
         "side": [f"Excluded: {p.get('excluded_screen2', 0)}"]},
        {"g": "Included", "step": "assessment", "pending_title": "Studies included in the review",
         "lines": ["Studies included in the review:",
                   str(p.get('included_final', 0))], "bold": True, "side": None},
    ]
    for st in stages:
        if done is not None and st["step"] not in done:
            st.update(lines=[st["pending_title"], "pending"], side=None,
                      bold=False, boldlast=False, pending=True)
    LH, GAP, PAD = _LH, 30, 16
    SPINE_X, SPINE_W, SIDE_X, SIDE_W, LBL_X, LBL_W, WD = 64, 250, 396, 210, 6, 30, 620

    y, pos = PAD, []
    for st in stages:
        h = max(52, len(st["lines"]) * LH + 22)
        pos.append((y, h))
        y += h + GAP
    H = y - GAP + PAD

    esc, box = _esc, _box

    parts = [f'<svg viewBox="0 0 {WD} {int(H)}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="{_SVG_FONT}" style="width:100%;height:auto;max-width:640px;">',
             '<defs><marker id="pr-ah" markerWidth="9" markerHeight="9" refX="6.5" refY="3" '
             'orient="auto"><path d="M0,0 L6.5,3 L0,6 Z" fill="var(--muted-2)"/></marker></defs>']

    # left group labels spanning their stages
    groups = []
    for i, st in enumerate(stages):
        if groups and groups[-1][0] == st["g"]:
            groups[-1][2] = i
        else:
            groups.append([st["g"], i, i])
    for g, i0, i1 in groups:
        top, bot = pos[i0][0], pos[i1][0] + pos[i1][1]
        cx, cy = LBL_X + LBL_W / 2, (top + bot) / 2
        parts.append(f'<rect x="{LBL_X}" y="{top}" width="{LBL_W}" height="{bot - top}" rx="5" '
                     f'fill="var(--card-hover)" stroke="var(--border)"/>')
        parts.append(f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central" '
                     f'font-size="11" font-weight="700" fill="var(--muted)" '
                     f'transform="rotate(-90 {cx} {cy})">{esc(g)}</text>')

    # spine boxes, vertical arrows, side boxes + horizontal arrows
    for i, st in enumerate(stages):
        y0, h = pos[i]
        parts.append(box(SPINE_X, y0, SPINE_W, h, st["lines"],
                         bold=st.get("bold", False), boldlast=st.get("boldlast", False),
                         muted=st.get("pending", False), dashed=st.get("pending", False)))
        if i < len(stages) - 1:
            x = SPINE_X + SPINE_W / 2
            parts.append(f'<line x1="{x}" y1="{y0 + h}" x2="{x}" y2="{pos[i + 1][0] - 2}" '
                         f'stroke="var(--muted-2)" stroke-width="1.5" marker-end="url(#pr-ah)"/>')
        if st.get("side"):
            sh = max(38, len(st["side"]) * LH + 18)
            sy = y0 + (h - sh) / 2
            parts.append(f'<line x1="{SPINE_X + SPINE_W}" y1="{y0 + h / 2}" x2="{SIDE_X - 2}" '
                         f'y2="{y0 + h / 2}" stroke="var(--muted-2)" stroke-width="1.5" '
                         f'marker-end="url(#pr-ah)"/>')
            parts.append(box(SIDE_X, sy, SIDE_W, sh, st["side"], muted=True))

    parts.append("</svg>")
    return Markup("".join(parts))


def _prisma_svg_two_arms(p: dict, arms: dict, steps_done=None):
    """PRISMA 2020 with both arms: databases and registers down the left, other
    methods down the right, joining into one 'studies included' box.

    One deliberate departure from the published template. There the right-hand
    column has no title/abstract screening box — records found by other methods
    go straight to full-text retrieval. Here it has one, because LSSR screens a
    nominated record against the same exclusion criteria as any other, and that
    is the property that makes the manual arm honest rather than a way around
    the criteria. Drawing the template's shape would hide a step that really
    happens, and the promise this diagram makes is that every box is computed
    from the pool — not that it matches a picture.
    """
    from markupsafe import Markup
    left, right = arms.get("db") or {}, arms.get("other") or {}
    done = None if steps_done is None else set(steps_done)

    def ident(a, other):
        src = a.get("by_source") or {}
        lines = [f"{k}: {v}" for k, v in src.items()] or ["Records identified"]
        lines.append(f"Total: {a.get('identified', 0)}")
        lines.append(f"{'New records' if other else 'Without duplicates'}: {a.get('records', 0)}")
        side = []
        if other and a.get("already_found_by_search"):
            side.append(f"Already found by the search: {a['already_found_by_search']}")
        if a.get("duplicates_removed") or not side:
            side.append(f"Duplicates removed: {a.get('duplicates_removed', 0)}")
        return lines, side

    def stages_for(a, other):
        lines, side = ident(a, other)
        return [
            {"step": "records", "pending_title": "Records identified",
             "lines": lines, "boldlast": True, "side": side},
            {"step": "screening", "pending_title": "Screening 1 (title/abstract)",
             "lines": ["Screened vs exclusion criteria",
                       f"(screening 1): {a.get('screened', 0)}"],
             "side": [f"Excluded: {a.get('excluded_screen1', 0)}"]},
            {"step": "fulltext", "pending_title": "Full text retrieval",
             "lines": [f"Full texts retrieved: {a.get('fulltext_retrieved', 0)}",
                       f"of {a.get('fulltext_sought', 0)} sought"],
             "side": [f"Not retrieved: {a.get('fulltext_not_retrieved', 0)}"]},
            {"step": "assessment", "pending_title": "Assessment (screening 2)",
             "lines": ["Assessed vs inclusion criteria",
                       f"(screening 2): {a.get('assessed', 0)}"],
             "side": [f"Excluded: {a.get('excluded_screen2', 0)}"]},
        ]

    cols = [stages_for(left, False), stages_for(right, True)]
    for col in cols:
        for st in col:
            if done is not None and st["step"] not in done:
                st.update(lines=[st["pending_title"], "pending"], side=None,
                          boldlast=False, pending=True)

    GAP, PAD, HEAD = 30, 16, 26
    LBL_X, LBL_W = 6, 26
    SPINE_W, SIDE_W, SIDE_GAP, COL_GAP = 236, 172, 8, 24
    C1 = 40
    C2 = C1 + SPINE_W + SIDE_GAP + SIDE_W + COL_GAP
    WD = C2 + SPINE_W + SIDE_GAP + SIDE_W + 6
    XS = (C1, C2)

    # Rows are as tall as the taller of the two columns, so the stages stay
    # side by side and the flow reads across as well as down.
    y, pos = PAD + HEAD, []
    for i in range(4):
        h = max(52, max(len(c[i]["lines"]) for c in cols) * _LH + 22)
        pos.append((y, h))
        y += h + GAP
    final_h = 58
    final_y = y
    H = final_y + final_h + PAD

    parts = [f'<svg viewBox="0 0 {WD} {int(H)}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="{_SVG_FONT}" style="width:100%;height:auto;max-width:960px;">',
             '<defs><marker id="pr-ah" markerWidth="9" markerHeight="9" refX="6.5" refY="3" '
             'orient="auto"><path d="M0,0 L6.5,3 L0,6 Z" fill="var(--muted-2)"/></marker></defs>']

    for x, head in zip(XS, ("Identified from databases and registers",
                            "Identified via other methods")):
        parts.append(f'<text x="{x + SPINE_W / 2:.0f}" y="{PAD + 8}" text-anchor="middle" '
                     f'dominant-baseline="central" font-size="11" font-weight="700" '
                     f'fill="var(--muted)">{_esc(head)}</text>')

    for label, i0, i1 in (("Identification", 0, 0), ("Screening", 1, 3)):
        top, bot = pos[i0][0], pos[i1][0] + pos[i1][1]
        cx, cy = LBL_X + LBL_W / 2, (top + bot) / 2
        parts.append(f'<rect x="{LBL_X}" y="{top}" width="{LBL_W}" height="{bot - top}" rx="5" '
                     f'fill="var(--card-hover)" stroke="var(--border)"/>')
        parts.append(f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central" '
                     f'font-size="11" font-weight="700" fill="var(--muted)" '
                     f'transform="rotate(-90 {cx} {cy})">{_esc(label)}</text>')
    parts.append(f'<rect x="{LBL_X}" y="{final_y}" width="{LBL_W}" height="{final_h}" rx="5" '
                 f'fill="var(--card-hover)" stroke="var(--border)"/>')
    icx, icy = LBL_X + LBL_W / 2, final_y + final_h / 2
    parts.append(f'<text x="{icx}" y="{icy}" text-anchor="middle" dominant-baseline="central" '
                 f'font-size="11" font-weight="700" fill="var(--muted)" '
                 f'transform="rotate(-90 {icx} {icy})">Included</text>')

    for col, x in zip(cols, XS):
        for i, st in enumerate(col):
            y0, h = pos[i]
            parts.append(_box(x, y0, SPINE_W, h, st["lines"],
                              boldlast=st.get("boldlast", False),
                              muted=st.get("pending", False), dashed=st.get("pending", False)))
            cx = x + SPINE_W / 2
            nxt = pos[i + 1][0] - 2 if i < 3 else final_y - 2
            parts.append(f'<line x1="{cx}" y1="{y0 + h}" x2="{cx}" y2="{nxt}" '
                         f'stroke="var(--muted-2)" stroke-width="1.5" marker-end="url(#pr-ah)"/>')
            if st.get("side"):
                sh = max(38, len(st["side"]) * _LH + 18)
                sx = x + SPINE_W + SIDE_GAP
                parts.append(f'<line x1="{x + SPINE_W}" y1="{y0 + h / 2}" x2="{sx - 2}" '
                             f'y2="{y0 + h / 2}" stroke="var(--muted-2)" stroke-width="1.5" '
                             f'marker-end="url(#pr-ah)"/>')
                parts.append(_box(sx, y0 + (h - sh) / 2, SIDE_W, sh, st["side"], muted=True))

    pending_final = done is not None and "assessment" not in done
    if pending_final:
        flines = ["Studies included in the review", "pending"]
    else:
        flines = [f"Studies included in the review: {p.get('included_final', 0)}",
                  f"databases and registers: {left.get('included_final', 0)}"
                  f"  ·  other methods: {right.get('included_final', 0)}"]
    parts.append(_box(C1, final_y, C2 + SPINE_W - C1, final_h, flines,
                      bold=not pending_final, muted=pending_final, dashed=pending_final))
    parts.append("</svg>")
    return Markup("".join(parts))


# ── Citations (procedural — never authored by the LLM) ──────────────────────────

def reference_entry(rec) -> str:
    """One entry of the numbered reference list, built from the record's own
    fields (authors, year, title, journal, DOI). The LLM never writes this — it
    only places study tokens — so a reference can't be hallucinated. Returned as
    HTML, since it goes inside a raw <ol> the markdown filter passes through."""
    from html import escape
    from authors import split_authors, surname_of
    names = split_authors(rec.authors)
    shown = [surname_of(n) or n for n in names[:3]]
    who = ", ".join(shown) + (" et al." if len(names) > 3 else "") if names else "Anon."
    parts = [f"{escape(who)} ({rec.year or 'n.d.'})."]
    if rec.title:
        parts.append(escape(rec.title.rstrip(".")) + ".")
    if rec.source:
        parts.append(f"<em>{escape(rec.source)}</em>.")
    link = f"https://doi.org/{rec.doi}" if rec.doi else (rec.url or "")
    if link:
        parts.append(f'<a href="{escape(link)}">{escape(link)}</a>')
    return " ".join(parts)


# One citation cluster: [S1], [S1, S4], [S1][S4], [S1], [S4]… as the model
# happens to write it. Everything inside is one citation point in the text.
_TOKEN_CLUSTER_RE = re.compile(
    r"\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\](?:\s*[,;]?\s*\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\])*")
_TOKEN_RE = re.compile(r"S(\d+)")
_STRAY_TOKEN_RE = re.compile(r"\[[^\[\]]*\bS\d+\b[^\[\]]*\]")


def _cluster_tokens(cluster: str) -> list:
    return ["S" + d for d in _TOKEN_RE.findall(cluster)]


def number_citations(texts: list, valid: set) -> dict:
    """Token -> citation number, in order of first appearance across `texts`
    (which come in the order the reader meets them). Unknown tokens, i.e.
    hallucinated ones, get no number."""
    num = {}
    for t in texts:
        for m in _TOKEN_CLUSTER_RE.finditer(t or ""):
            for tok in _cluster_tokens(m.group(0)):
                if tok in valid and tok not in num:
                    num[tok] = len(num) + 1
    return num


def _ranges(ns: list) -> list:
    out, start, prev = [], None, None
    for n in ns:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev)); start = prev = n
    if start is not None:
        out.append((start, prev))
    return out


def _render_cluster(nums: list) -> str:
    """[1, 3–5] with each end a link to its reference. Brackets escaped so
    markdown does not read the whole thing as a link."""
    link = lambda n: f"[{n}](#ref-{n})"   # noqa: E731
    parts = [link(a) if a == b else (f"{link(a)}, {link(b)}" if b == a + 1 else f"{link(a)}–{link(b)}")
             for a, b in _ranges(sorted(set(nums)))]
    return "\\[" + ", ".join(parts) + "\\]"


def render_citations(text: str, num: dict) -> str:
    """Replace every token cluster with its numbered citation; a cluster whose
    tokens are all unknown is dropped, with the space it leaves."""
    def sub(m):
        nums = [num[t] for t in _cluster_tokens(m.group(0)) if t in num]
        return (" " + _render_cluster(nums)) if nums else ""
    out = _TOKEN_CLUSTER_RE.sub(sub, text)
    # anything token-like the cluster pattern did not recognise ("[S99=S91]",
    # "[S55… wait]") is not a citation, and must not reach the reader either
    out = _STRAY_TOKEN_RE.sub("", out)
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r"\( \\\[", "(\\[", out)
    # a dropped or moved cluster leaves a space before the punctuation after it
    return re.sub(r" +([.,;:)])", r"\1", out).strip()


def references_block(num: dict, rec_of_token: dict) -> str:
    items = sorted(num.items(), key=lambda kv: kv[1])
    lis = "\n".join(f'<li id="ref-{n}">{reference_entry(rec_of_token[tok])}</li>' for tok, n in items)
    return f'<ol class="refs">\n{lis}\n</ol>'


def counts_line(fields, recs, extracted) -> str:
    """The procedural line at the head of a group: for each chosen field, how
    the group's studies split across its values. No LLM: the paragraph under it
    cannot quietly contradict these numbers."""
    from collections import Counter
    from models import field_visible
    parts = []
    for fld in fields:
        c: Counter = Counter()
        for rec in recs:
            vals = extracted.get(rec.id, {})
            if not field_visible(fld, vals):
                continue
            v = vals.get(fld.key)
            for x in (v if isinstance(v, list) else [v]):
                if x not in (None, ""):
                    c[str(x)] += 1
        if c:
            order = [o for o in fld.options() if o in c] + sorted(k for k in c if k not in fld.options())
            parts.append(f"{fld.label}: " + " · ".join(f"{k} {c[k]}" for k in order))
    return ("_" + " — ".join(parts) + "_") if parts else ""


# ── General block: structured "fixed variables" (procedural, no LLM) ────────────

CHART_MAX_ROWS = 12
# values that mean "the study did not say": drawn in gray, since they are
# missing data rather than a category, and never folded into "Other"
_MISSING_RE = re.compile(r"(?i)^(not (reported|stated|specified|discussed|applicable|verified)|none|n/?a)$")


def _bar_rows(counts, order, answered) -> str:
    """One horizontal bar per value, width = share of the studies that answered
    the field. Long tails fold into a single 'Other' row."""
    from html import escape
    keys = [k for k in order if k in counts]
    items = [(k, counts[k], False) for k in keys]
    if len(keys) > CHART_MAX_ROWS:
        substantive = [k for k in keys if not _MISSING_RE.match(k)]
        missing = [k for k in keys if _MISSING_RE.match(k)]
        keep = max(1, CHART_MAX_ROWS - 1 - len(missing))
        rest = substantive[keep:]
        items = ([(k, counts[k], False) for k in substantive[:keep]]
                 + [(f"Other ({len(rest)} values)", sum(counts[k] for k in rest), True)]
                 + [(k, counts[k], False) for k in missing])
    rows = []
    # length compares the values with each other, so it scales to the largest;
    # the share of all answering studies is in the hover title
    top = max((n for _l, n, _o in items), default=0)
    for label, n, other in items:
        pct = round(100 * n / answered) if answered else 0
        width = round(100 * n / top) if top else 0
        miss = " is-missing" if (_MISSING_RE.match(label) and not other) else ""
        tip = escape(f"{label}: {n} of {answered} studies ({pct}%)")
        rows.append(f'<div class="pub-bar-row" title="{tip}">'
                    f'<span class="pub-bar-label{miss}">{escape(label)}</span>'
                    f'<span class="pub-bar-track"><span class="pub-bar-val{miss}" style="width:{width}%"></span></span>'
                    f'<span class="pub-bar-n">{n} <span class="pub-bar-pct">({pct}%)</span></span></div>')
    return '<div class="pub-bars">' + "".join(rows) + "</div>"


def _year_hist(nums) -> str:
    from collections import Counter
    c = Counter(int(x) for x in nums)
    lo, hi = min(c), max(c)
    top = max(c.values())
    bars = "".join(f'<div class="pub-bar" title="{y}: {c.get(y, 0)}">'
                   f'<div class="pub-bar-fill" style="height:{round(100 * c.get(y, 0) / top)}%"></div></div>'
                   for y in range(lo, hi + 1))
    return (f'<div class="pub-hist" role="img" aria-label="Studies per year, {lo} to {hi}">{bars}</div>'
            f'<div class="pub-hist-axis"><span>{lo}</span><span>{hi}</span></div>')


def general_narrative(structured_fields, extracted, included) -> str:
    """The distribution of every structured extraction field across the included
    studies, as a grid of small bar charts (a histogram for numbers). Counted
    here, no LLM, so no miscounted figure; every bar carries its count as text
    and a hover title with the share, so nothing is read from colour alone.
    Rendered as one raw HTML block, which the markdown filter passes through."""
    import statistics
    from collections import Counter
    from html import escape
    from models import field_visible

    if not included:
        return "_No studies were included in the synthesis._"
    charts = []
    for fld in structured_fields:
        counts: Counter = Counter()
        nums: list = []
        answered = 0
        for rec in included:
            vals = extracted.get(rec.id, {})
            if not field_visible(fld, vals):
                continue
            v = vals.get(fld.key)
            if v in (None, "") or (isinstance(v, list) and not v):
                continue
            answered += 1
            if fld.field_type == "number":
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, list):
                for x in v:
                    if x not in (None, ""):
                        counts[str(x)] += 1
            else:
                counts[str(v)] += 1
        title = f'<div class="syn-chart-title">{escape(fld.label)} <span class="muted small">n={answered}</span></div>'
        if fld.field_type == "number" and nums:
            med = statistics.median(nums)
            med = int(med) if med == int(med) else round(med, 1)
            charts.append(f'<div class="syn-chart">{title}{_year_hist(nums)}'
                          f'<div class="muted small">median {med}</div></div>')
        elif counts:
            order = [o for o in fld.options() if o in counts]
            order += [k for k, _n in counts.most_common() if k not in order]
            if len(order) > CHART_MAX_ROWS:   # long lists (countries): by frequency
                order = [k for k, _n in counts.most_common()]
            multi = ('<div class="muted small">several values per study possible</div>'
                     if fld.field_type == "multiselect" else "")
            charts.append(f'<div class="syn-chart">{title}{_bar_rows(counts, order, answered)}{multi}</div>')
    head = f"**{len(included)} studies** were included in the synthesis."
    return head + ("\n\n" + '<div class="syn-charts">' + "".join(charts) + "</div>" if charts else "")


# ── LLM narrative per assessment criterion (text/textarea fields) ───────────────
# Prompt text lives in prompts.py (SYNTHESIS_SYSTEM, synthesis_user); citation
# substitution below stays here — it is post-processing, not a prompt.
from prompts import SYNTHESIS_SYSTEM, synthesis_user  # noqa: E402


CUT_OFF = ("_This paragraph was cut off before the model finished it; regenerate the "
           "synthesis. Nothing partial is shown._")


def _narrative(client, model, rq, criterion, items):
    """One paragraph. Streamed with room for thinking: on current models
    (Sonnet 5, Opus 5) thinking runs by default and counts against max_tokens,
    and at the old 1500 the whole budget could go to reasoning, leaving a
    truncated or empty paragraph that went straight to the public page. A reply
    that stops for any reason other than finishing is replaced by a note."""
    with client.messages.stream(
        model=model, max_tokens=16000,
        system=[{"type": "text", "text": SYNTHESIS_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": synthesis_user(rq, criterion, items)}],
    ) as stream:
        resp = stream.get_final_message()
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    text = _last_paragraph(text)
    if resp.stop_reason != "end_turn" or not text:
        text = CUT_OFF
    return text, resp.usage.input_tokens, resp.usage.output_tokens


VERIFY_TEXT_CHARS = 60_000     # per cited study; a long review is cut, not skipped
UNVERIFIED = ("\n\n_This paragraph could not be checked against the full texts of the "
              "studies it cites; read it with care._")
_CHANGES_RE = re.compile(r"<changes>\s*(\d+)\s*</changes>")


def _verify(client, model, rq, theme, draft, studies):
    """Second pass: check the draft claim by claim against the full texts of the
    studies it cites, and correct it. Returns (text, changes, tokens_in,
    tokens_out); changes is None when the check did not complete, in which case
    the draft is returned marked as unverified rather than silently kept."""
    from prompts import VERIFY_SYSTEM, verify_user
    with client.messages.stream(
        model=model, max_tokens=32000,
        system=[{"type": "text", "text": VERIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": verify_user(rq, theme, draft, studies)}],
    ) as stream:
        resp = stream.get_final_message()
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    found = _PARAGRAPH_RE.findall(raw)
    if resp.stop_reason != "end_turn" or not found or not found[-1].strip():
        return draft + UNVERIFIED, None, resp.usage.input_tokens, resp.usage.output_tokens
    m = _CHANGES_RE.search(raw)
    return (found[-1].strip(), int(m.group(1)) if m else 0,
            resp.usage.input_tokens, resp.usage.output_tokens)


_PARAGRAPH_RE = re.compile(r"<paragraph>(.*?)</paragraph>", re.S)


def _last_paragraph(text: str) -> str:
    """The content of the last <paragraph> tag. A model that corrects itself
    mid-answer ("… wait. Actually, let me produce the final paragraph") once
    published both the abandoned draft and the aside; the tags keep only the
    version it settled on. Untagged output is taken whole, as before."""
    found = _PARAGRAPH_RE.findall(text or "")
    return found[-1].strip() if found else (text or "").strip()


NO_VALUE = "Not reported / not coded"


def _groups(group_field, included, extracted) -> list:
    """(group value, [records]) in the field's own option order, then the records
    with no value. A multiselect puts a record in every group it ticks, so the
    groups can overlap; the block says so."""
    from models import field_visible
    buckets: dict = {}
    for rec in included:
        vals = extracted.get(rec.id, {})
        v = vals.get(group_field.key) if field_visible(group_field, vals) else None
        keys = [x for x in v if x not in (None, "")] if isinstance(v, list) else (
            [v] if v not in (None, "") else [])
        for k in keys or [NO_VALUE]:
            buckets.setdefault(str(k), []).append(rec)
    order = [o for o in group_field.options() if o in buckets]
    order += sorted(k for k in buckets if k not in order and k != NO_VALUE)
    if NO_VALUE in buckets:
        order.append(NO_VALUE)
    return [(k, buckets[k]) for k in order]


def _run(workspace_id: int, api_key: str, user_id: int | None):
    from models import (Record, SessionLocal, Synthesis, SynthesisBlock, UserCostLog,
                        Workspace, authoritative_values, calc_cost, ensure_extraction_fields,
                        workspace_extraction_fields)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        ensure_extraction_fields(db, ws)
        fields = workspace_extraction_fields(db, ws)
        structured_fields = [f for f in fields if f.field_type in ("select", "multiselect", "number")]
        narrative_fields = [f for f in fields if f.field_type in ("text", "textarea")
                            and f.in_synthesis is not False]
        group_field = next((f for f in structured_fields
                            if f.key == ws.synthesis_group_key
                            and f.field_type in ("select", "multiselect")), None)
        _set(workspace_id, {"status": "running", "message": "Building synthesis…",
                            "total": len(narrative_fields), "done": 0})

        prisma = compute_prisma(db, workspace_id)

        # preserve prior published state; replace blocks
        syn = db.query(Synthesis).filter(Synthesis.workspace_id == workspace_id).first()
        published = syn.published if syn else False
        if syn:
            db.query(SynthesisBlock).filter(SynthesisBlock.synthesis_id == syn.id).delete()
            syn.prisma_json = json.dumps(prisma)
        else:
            syn = Synthesis(workspace_id=workspace_id, prisma_json=json.dumps(prisma),
                            published=published)
            db.add(syn)
        db.commit()
        db.refresh(syn)

        included = (db.query(Record)
                      .filter(Record.workspace_id == workspace_id,
                              Record.is_removed == False,               # noqa: E712
                              Record.screen2_decision == "include").all())
        extracted = {rec.id: authoritative_values(db, rec) for rec in included}
        # stable per-study token; the LLM only ever sees the token, and the
        # numbered citation and reference entry are built from the record
        tokens = {rec.id: f"S{i + 1}" for i, rec in enumerate(included)}
        rec_of_token = {tokens[rec.id]: rec for rec in included}
        try:
            summary_keys = json.loads(ws.synthesis_summary_keys_json or "[]")
        except (ValueError, TypeError):
            summary_keys = []
        summary_fields = [f for f in structured_fields
                          if f.key in summary_keys and f.field_type in ("select", "multiselect")]

        # Block 0: the general "fixed variables" summary — procedural, no LLM.
        db.add(SynthesisBlock(synthesis_id=syn.id, heading="Study characteristics",
                              narrative=general_narrative(structured_fields, extracted, included),
                              position=0))
        db.commit()

        client = anthropic.Anthropic(api_key=api_key)

        # The review's own coded fields (select/multiselect, not the builtin
        # bibliographic ones) travel with each study's free text, so a claim
        # about the direction of an effect can be held to the value the
        # reviewers coded rather than to the model's reading of a note.
        from models import field_visible
        coded_fields = [f for f in structured_fields
                        if f.field_type in ("select", "multiselect") and not f.builtin]

        def coded_for(rec):
            vals = extracted.get(rec.id, {})
            parts = []
            for f in coded_fields:
                if not field_visible(f, vals):
                    continue
                v = vals.get(f.key)
                v = ", ".join(str(x) for x in v if x not in (None, "")) if isinstance(v, list) else v
                if v not in (None, ""):
                    parts.append(f"{f.label}: {v}")
            return "; ".join(parts)

        def items_for(fld, recs):
            items = []
            for rec in recs:
                val = extracted.get(rec.id, {}).get(fld.key)
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                val = (val or "").strip() if isinstance(val, str) else ""
                if val and val.lower() != "not addressed":
                    items.append({"token": tokens[rec.id], "finding": val, "coded": coded_for(rec)})
            return items

        # Every paragraph to write, as (field index, group or None, theme, items).
        # With a grouping field each narrative field gets one paragraph per group
        # value, so the calls multiply; they run in parallel below.
        groups = _groups(group_field, included, extracted) if group_field else [(None, included)]
        jobs = []
        for i, fld in enumerate(narrative_fields):
            for gval, recs in groups:
                theme = fld.label if gval is None else f"{fld.label} — {group_field.label}: {gval}"
                jobs.append((i, gval, theme, items_for(fld, recs)))
        _set(workspace_id, {"status": "running", "message": "Writing the narratives…",
                            "total": len(jobs), "done": 0})

        from concurrent.futures import ThreadPoolExecutor
        results = {}
        tin = tout = 0
        done = 0
        # Read on this thread, before the pool starts. The commit above expired
        # `ws`; a worker touching ws.research_question would reload it through the
        # shared SQLite session from several threads at once, and SQLite answers
        # with "bad parameter or other API misuse" (the same trap screening.py
        # documents). Workers get plain values only.
        rq = ws.research_question
        # Full texts for the verification pass, read here for the same reason.
        from fulltext import strip_back_matter
        text_of = {tokens[rec.id]: strip_back_matter(rec.full_text_md or "")[:VERIFY_TEXT_CHARS]
                   for rec in included}
        verify_log = []            # changes per paragraph; None = check did not complete

        def write(job):
            i, gval, theme, items = job
            if not items:
                return job, None, 0, 0
            raw, ti, to = _narrative(client, model, rq, theme, items)
            if raw == CUT_OFF:
                return job, raw, ti, to
            cited = []
            for m in _TOKEN_CLUSTER_RE.finditer(raw):
                cited += [t for t in _cluster_tokens(m.group(0)) if t not in cited]
            by_token = {it["token"]: it for it in items}
            studies = [dict(by_token[t], full_text=text_of.get(t, "")) for t in cited if t in by_token]
            if not studies:
                return job, raw, ti, to
            final, changes, ti2, to2 = _verify(client, model, rq, theme, raw, studies)
            verify_log.append(changes)
            return job, final, ti + ti2, to + to2

        with ThreadPoolExecutor(max_workers=4) as ex:
            for job, raw, ti, to in ex.map(write, jobs):
                results[(job[0], job[1])] = (raw, len(job[3]))
                tin += ti
                tout += to
                done += 1
                _set(workspace_id, {"status": "running", "message": f"Synthesizing {job[2]}…",
                                    "total": len(jobs), "done": done})

        # Citations are numbered in the order the reader meets them: block by
        # block, group by group, the same order the loop below writes them in.
        num = number_citations([results[(i, gval)][0] or ""
                                for i in range(len(narrative_fields)) for gval, _r in groups],
                               set(rec_of_token))
        for i, fld in enumerate(narrative_fields):
            if group_field is None:
                raw, _n = results[(i, None)]
                narrative = (render_citations(raw, num) if raw
                             else "_No included studies addressed this field._")
            else:
                parts = []
                if group_field.field_type == "multiselect":
                    parts.append(f"_Grouped by {group_field.label}. A study can tick more than "
                                 f"one value, so it can appear in more than one group._")
                for gval, recs in groups:
                    raw, _n = results[(i, gval)]
                    if not raw:
                        continue
                    # the group's size, the same denominator as the counts line
                    n = len(recs)
                    head = f"**{gval}** ({n} stud{'y' if n == 1 else 'ies'})"
                    line = counts_line(summary_fields, recs, extracted)
                    parts.append(head + ("\n\n" + line if line else "") + "\n\n"
                                 + render_citations(raw, num))
                narrative = ("\n\n".join(parts) if any(r[0] for k, r in results.items() if k[0] == i)
                             else "_No included studies addressed this field._")
            db.add(SynthesisBlock(synthesis_id=syn.id, heading=fld.label,
                                  narrative=narrative, position=i + 1))
        if num:
            db.add(SynthesisBlock(synthesis_id=syn.id, heading="References",
                                  narrative=references_block(num, rec_of_token),
                                  position=len(narrative_fields) + 1))
        db.commit()

        if tin or tout:
            db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="synthesis",
                               input_tokens=tin, output_tokens=tout,
                               cost_usd=calc_cost(model, tin, tout)))
            db.commit()
        cut = sum(1 for raw, _n in results.values() if raw == CUT_OFF)
        checked = [c for c in verify_log if c is not None]
        unverified = len(verify_log) - len(checked)
        msg = (f"Synthesis ready. {len(checked)} paragraph(s) checked against the cited full "
               f"texts, {sum(1 for c in checked if c)} corrected ({sum(checked)} citations "
               f"changed).")
        if unverified:
            msg += f" {unverified} could not be checked and are marked as such."
        if cut:
            msg += f" {cut} paragraph(s) were cut off: regenerate."
        _set(workspace_id, {"status": "done", "message": msg,
                            "total": len(jobs), "done": len(jobs)})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_synthesis(workspace_id: int, api_key: str, user_id: int | None):
    _set(workspace_id, {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id), daemon=True).start()
