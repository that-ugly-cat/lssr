"""
Synthesis (step 10): the public deliverable.

Builds the PRISMA flow counts, then a sequence of blocks:
  • Block 0 — "Study characteristics": a procedural distribution summary of the
    structured "fixed variable" fields (select/multiselect/number: country, study
    year, study type, methodology axes…). No LLM, so no miscounted figures.
  • One block per assessment criterion (text/textarea field): the LLM aggregates
    the per-study findings into a narrative paragraph. Citations are NOT authored
    by the LLM — it only inserts a study token ([S1], [S2]…) which we substitute
    procedurally with a citation built from the record (Surname et al., Year,
    DOI/link), so a citation can never be hallucinated.

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

def citation(rec) -> str:
    """Full inline citation built from the record's own fields:
    'Surname et al., Year, https://doi.org/…'. The LLM never writes this — it only
    emits a study token that we substitute here, so citations can't be hallucinated."""
    from authors import split_authors, surname_of
    names = split_authors(rec.authors)
    year = rec.year or "n.d."
    if names:
        surname = surname_of(names[0]) or "Anon"
        who = f"{surname} et al." if len(names) > 1 else surname
    else:
        who = "Anon"
    link = f"https://doi.org/{rec.doi}" if rec.doi else (rec.url or "")
    parts = [who, str(year)] + ([link] if link else [])
    return ", ".join(parts)


_TOKEN_RE = re.compile(r"\[(S\d+)\]")


def _substitute_citations(text: str, token_cite: dict) -> str:
    """Replace each [S#] study token the LLM placed with the procedural citation;
    drop any token that isn't in the map (a hallucinated reference)."""
    out = _TOKEN_RE.sub(lambda m: f"({token_cite[m.group(1)]})"
                        if m.group(1) in token_cite else "", text)
    return re.sub(r" {2,}", " ", out).strip()


# ── General block: structured "fixed variables" (procedural, no LLM) ────────────

def general_narrative(structured_fields, extracted, included) -> str:
    """A deterministic distribution summary of the structured extraction fields
    across the included studies. No LLM, so no risk of a miscounted figure."""
    import statistics
    from collections import Counter
    from models import field_visible

    if not included:
        return "_No studies were included in the synthesis._"
    lines = [f"**{len(included)} studies** were included in the synthesis."]
    for fld in structured_fields:
        counts: Counter = Counter()
        nums: list = []
        for rec in included:
            vals = extracted.get(rec.id, {})
            if not field_visible(fld, vals):
                continue
            v = vals.get(fld.key)
            if v in (None, "") or (isinstance(v, list) and not v):
                continue
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
        if fld.field_type == "number" and nums:
            lo, hi = int(min(nums)), int(max(nums))
            med = statistics.median(nums)
            med = int(med) if med == int(med) else round(med, 1)
            span = f"{lo}" if lo == hi else f"{lo}–{hi}"
            lines.append(f"- **{fld.label}:** {span} (median {med}, n={len(nums)})")
        elif counts:
            parts = ", ".join(f"{k} ({n})" for k, n in counts.most_common())
            lines.append(f"- **{fld.label}:** {parts}")
    return "\n".join(lines)


# ── LLM narrative per assessment criterion (text/textarea fields) ───────────────
# Prompt text lives in prompts.py (SYNTHESIS_SYSTEM, synthesis_user); citation
# substitution below stays here — it is post-processing, not a prompt.
from prompts import SYNTHESIS_SYSTEM, synthesis_user  # noqa: E402


def _narrative(client, model, rq, criterion, items):
    resp = client.messages.create(
        model=model, max_tokens=1500,
        system=[{"type": "text", "text": SYNTHESIS_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": synthesis_user(rq, criterion, items)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return text, resp.usage.input_tokens, resp.usage.output_tokens


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
        narrative_fields = [f for f in fields if f.field_type in ("text", "textarea")]
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
        # stable per-study token → procedural citation (LLM only ever sees the token)
        tokens = {rec.id: f"S{i + 1}" for i, rec in enumerate(included)}
        token_cite = {tokens[rec.id]: citation(rec) for rec in included}

        # Block 0: the general "fixed variables" summary — procedural, no LLM.
        db.add(SynthesisBlock(synthesis_id=syn.id, heading="Study characteristics",
                              narrative=general_narrative(structured_fields, extracted, included),
                              position=0))
        db.commit()

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        for i, fld in enumerate(narrative_fields):
            items = []
            for rec in included:
                val = extracted.get(rec.id, {}).get(fld.key)
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                val = (val or "").strip() if isinstance(val, str) else ""
                if val and val.lower() != "not addressed":
                    items.append({"token": tokens[rec.id], "finding": val})
            if items:
                raw, ti, to = _narrative(client, model, ws.research_question, fld.label, items)
                narrative = _substitute_citations(raw, token_cite)
                tin += ti
                tout += to
            else:
                narrative = "_No included studies addressed this field._"
            db.add(SynthesisBlock(synthesis_id=syn.id, heading=fld.label,
                                  narrative=narrative, position=i + 1))
            db.commit()
            _set(workspace_id, {"status": "running", "message": f"Synthesizing {fld.label}…",
                                "total": len(narrative_fields), "done": i + 1})

        if tin or tout:
            db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="synthesis",
                               input_tokens=tin, output_tokens=tout,
                               cost_usd=calc_cost(model, tin, tout)))
            db.commit()
        _set(workspace_id, {"status": "done", "message": "Synthesis ready.",
                            "total": len(narrative_fields), "done": len(narrative_fields)})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_synthesis(workspace_id: int, api_key: str, user_id: int | None):
    _set(workspace_id, {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id), daemon=True).start()
