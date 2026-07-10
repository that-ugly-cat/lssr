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
[borant](https://borant.eu) toolchain instead of reimplementing it.

## The pipeline

A workspace is one living review. Users can belong to several workspaces, and
each workspace can expose read-only public links to its published synthesis.

1. **Query & refinement** — a PubMed search, with MeSH/keyword frequencies to
   refine the query.
2. **Query translation** — LLM-assisted translation to Scopus / Web of Science /
   CINAHL / JSTOR syntax, always human-editable.
3. **Import** — BibTeX / RIS uploads from runs on other databases, normalized to
   one schema; plus manual entry.
4. **Deduplication** — incremental (DOI-exact then fuzzy title+year), keeping the
   most complete version and merging provenance.
5. **Screening 1** — title + abstract vs exclusion criteria (LLM, errs toward
   inclusion; decisions are sticky).
6. **Full text** — open-access download via Unpaywall, plus manual PDF upload.
7. **paper2md** — PDFs cleaned to markdown via the
   [paper2md](https://github.com/that-ugly-cat/paper2md) service.
8. **Screening 2 + 9. Assessment** — one conditional LLM call per full text:
   the inclusion decision, and only if included, a finding + citation per
   assessment criterion (so excluded records cost no assessment tokens).
10. **Synthesis** — a narrative block per assessment criterion with inline
    citations, plus a PRISMA flow, on a public page.

Press **Refresh** to open a new iteration: it re-runs the searches and
re-deduplicates, screening and assessing only the newly found records — the
living-review loop.

## Stack

FastAPI + Jinja2 + SQLAlchemy/SQLite, JWT cookie auth, per-user Anthropic key
(Fernet-encrypted), background jobs with status polling. Ships as a Docker
container on port **8013**. See [DEPLOY.md](DEPLOY.md) and [SPEC.md](SPEC.md).

## License

[AGPL-3.0](LICENSE).
