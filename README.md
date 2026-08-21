<p align="center">
  <b>LSSR — Living Systematic Scoping Review</b><br>
  From a query to a living, published scoping review — in one place.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: AGPL v3" src="https://img.shields.io/badge/License-AGPLv3-blue.svg"></a>
</p>

---

LSSR is a self-hosted web app that carries an entire scoping review — from the
search query to the published narrative synthesis — through a single tool, and
keeps it **living**: every step is re-runnable on demand to update the results
without redoing the human work already done.

It extends the review process both upstream (query building and translation) and
downstream (synthesis and publication), and reuses the rest of the
borant toolchain instead of reimplementing it.

## The pipeline

A workspace is one living review. Users belong to one or more workspaces; each
step can be marked *done* (a ✓ on its tab and Overview), and a workspace can
expose read-only public links to its dashboard and published synthesis.

1. **Query** — author the canonical query in one source database: **PubMed**
   (richest syntax, the recommended default) or **OpenAlex** (broadest,
   multidisciplinary corpus). MeSH/keyword frequencies help refine it, and a
   shared publication-year window applies to every source.
2. **Translation & sources** — LLM-assisted, **source-aware** translation into
   any of 14 databases (Scopus, Web of Science, Embase, APA PsycInfo, CINAHL,
   JSTOR, PhilPapers, HeinOnline, ERIC…), always human-editable, each with a link
   to that database's search page. Four of them have open APIs and can be
   **harvested directly** (PubMed, Europe PMC, OpenAlex, ERIC); the rest you
   export and import under Records. Deduplication merges provenance across all
   sources, so each database grafts onto the same pool.
3. **Records** — BibTeX / RIS / Excel imports (Excel with a column-mapping step),
   plus manual entry and editing; incremental **deduplication** (DOI-exact then
   fuzzy title+year, keeping the most complete version and merging provenance).
   Author strings are normalised to a canonical `; `-separated format on every
   ingest path, whatever the source database's own convention.
4. **Screening 1** — title + abstract vs the **exclusion criteria**. The LLM
   pre-screens (include / exclude / **maybe**); reviewers then vote **blind**
   (they see others' votes only after voting). N independent votes settle a
   record (configurable, and screening 2 can require a different number from
   screening 1 — a corpus double-screened on title and abstract is often read
   once on full text); disagreement becomes a **conflict** the owner
   adjudicates. Decisions are sticky across iterations.

   The decision column carries **every voice** — 🤖 model, 👤 reviewer,
   ⚖️ adjudicator — with a `≠` marker when they disagree, still behind the blind
   rule. Two filters beyond the decisions: **≠ divergent**, every record where at
   least one voice differs from another, the model's included and `maybe`
   counting as a difference — much wider than *conflict*, which only ever means
   two humans disagreeing; and **🤖 model only**, records standing on the
   pre-screener's word with no human vote behind them, which reads zero on a
   fully screened corpus and fills again after every refresh.
5. **Full text** — a retrieval ladder that stops at the first source yielding
   real full text: **Europe PMC** JATS (clean, no conversion needed) → Unpaywall
   & OpenAlex locations (repository copies first) → landing pages read for
   `citation_pdf_url` → **publisher TDM APIs** (Elsevier / Springer / Wiley, per
   the reviewer's own keys) → **OA siblings**, same-titled OpenAlex works under a
   different DOI, which is where the preprint copy of a paywalled article lives.
   PDFs are converted to markdown by the
   [paper2md](https://github.com/that-ugly-cat/paper2md) service.

   Conversion happens **inline**, because a candidate can only be checked once it
   is text: every converted candidate is verified against the record's own title,
   and one that turns out to be a different paper is discarded and the ladder
   continues. Retrieval warnings (a discarded candidate, the sibling DOI a text
   actually came from) are kept on the record and shown in the full-text table.
   Manual upload accepts **PDF / DOCX / Markdown / TXT**, and a reviewer's own
   file is never discarded — a title mismatch there is only flagged. The reader
   keeps the whole text; references and back matter are stripped only when the
   LLM reads it.
6. **Assessment** — screening 2 **and** structured extraction in one pass, in a
   large review modal: the full text beside the **inclusion criteria**, the
   include/maybe/exclude decision, and the extraction form. The LLM drafts both
   (a model *draft* that never overrides a human); reviewers confirm or edit,
   AI-assisted. Screen 2 is multi-reviewer with conflict/adjudication like
   screen 1.

   The modal opens **whether or not the full text is in hand**: a report nobody
   can obtain still needs a decision — *exclude, not retrievable* is one — and
   leaving it pending would read as "not looked at yet" rather than "looked for
   and not found". The owner also sees **what other reviewers extracted**, in a
   read-only panel: never as a pre-fill, since adopting a colleague's answers
   with one click would turn a second independent extraction into a copy of the
   first. The *model's* draft does pre-fill, which is the intended asymmetry —
   a tool's output is made to be corrected, a colleague's judgement is not.
   Two more filters: **🤖 model only** (drafted by the model, confirmed by
   nobody — it matters more here, since the same draft also fills the fields the
   reviewer is about to read) and **⌀ included, not extracted** (in the review,
   contributing to no field of the synthesis — a hole the include count hides).

   Extraction fields are configurable (text / textarea / number /
   select / multiselect, with `show_if` conditions); builtin fields cover
   country, study year, study type and the three empirical-methodology axes
   (design / data / timeframe). Each record's authoritative values are the
   owner-curated *final* row, else the latest reviewer's, else the model draft.
7. **Synthesis** — a "Study characteristics" block summarising the structured
   fields' distributions (computed, not written by the LLM), then one narrative
   block per free-text criterion. Citations are built **procedurally** from each
   record (author, year, DOI/link): the LLM only places a study token, so a
   citation can't be hallucinated.

The **PRISMA flow** is computed live from the current pool and rendered as a
flow diagram on the Overview and the public page; stages whose pipeline step
isn't marked done yet are drawn as dashed *pending* boxes, without counts.

The screening and assessment tables (and full text) carry filters; screening and
assessment export to **Excel** (the assessment sheet is the record × field
extraction matrix).

Press **Refresh** to open a new iteration: it re-runs the searches and
re-deduplicates, screening and assessing only the newly found records — the
living-review loop.

## Public page

Each active share link (`/r/{token}`) is a structured dashboard with a clickable
section index: the review's description and research question, per-step progress,
the live PRISMA flow, the queries, record stats (records-per-database pie, year
histogram, top authors — merged across name formats, keyword cloud, type counts),
a full-text retrieval pie, the screening and full-text decision bars, and — once
assessment is done — charts over the included papers (study type, country, study
year, methodology axes). Apart from the PRISMA flow, which is always shown with
its pending stages, each section appears only when its step is marked done.

## Stack

FastAPI + Jinja2 + SQLAlchemy/SQLite, JWT cookie auth. Per-user credentials
(Anthropic key, publisher TDM keys) are Fernet-encrypted at rest and set in the
profile. Admin user management at `/admin`. Background jobs with status polling,
a progress bar and a rolling time estimate; per-run cost estimates on the LLM
steps. Every prompt sent to a model lives in a single `prompts.py`, for auditing
and explainability. Ships as a Docker container on port **8013**. See
[DEPLOY.md](DEPLOY.md).

## Roadmap

Automated import from the paywalled databases (Scopus / Web of Science / Embase /
APA PsycInfo / CINAHL / JSTOR / HeinOnline) via their institutional APIs — the
query translation and manual import already exist; direct ingest needs
institutional credentials (Scopus/Embase = Elsevier, which requires an
institutional token from a server).

## License

[AGPL-3.0](LICENSE).

## Optional: behind an SSO gate

`AUTH_MODE=gateway` hands reviewer identity to an upstream `forward_auth` gate
instead of the local password. Reviewers are matched by an immutable subject and
not by email address, because a reviewer's id is the `reviewer_id` on every
screening decision and extraction they have made: an address that changes with
an institution is the wrong thing to re-find someone by.

**Public share pages are untouched.** `/r/{token}` is how someone outside the
project reads a review, and it needs no account in either mode.

`local` is the default and stays fully supported. Details, and the one-off
linking script to run before switching, in `DEPLOY.md`.
