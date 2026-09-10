# LSSR — User Guide

LSSR runs a whole scoping review inside one tool: from writing the search query to publishing the narrative synthesis, with the PRISMA flow computed from the pool as it stands. It is **living** — every step re-runs on demand, screening and extraction only touch what is new, and the human decisions already taken stay where they are.

The LLM is a first-pass assistant throughout. It pre-screens, retrieves, drafts an extraction. It never casts the vote that counts: every decision in the review carries the name of the reviewer who made it, in the web app.

---

## 1. Getting started

1. Open `https://lssr.borant.eu`. The front page is a public showcase that never looks at who is reading it; the app itself lives behind **Enter →**, which sends you through **Borant ID**, the single sign-on shared by the borant tools. There is no separate LSSR password to set up.
2. If you have never been here, your profile is created on first arrival, keyed on the immutable subject the gate knows you by and never on your email address — your reviewer id is stamped on every screening decision and every extraction row you will ever save, and an address that changes with an institution must not be what re-finds it. Access is granted by an administrator: open registration is off, so if the gate does not know you yet, ask.
3. **Log out** sends you to the gate's own logout, not just to a cleared local cookie: dropping the app's cookie alone would leave the gate's session alive, and the next click would walk straight back in.
4. There is no second factor inside LSSR. One was half-built once, advertised and never wired to a route, so it was removed rather than left as a promise; where a second factor is wanted it belongs to the gate, which has one that works.
5. Open **Profile** and save your **Anthropic API key**. It is stored encrypted at rest and spent only on runs you start. Query translation, screening, the assessment draft and the synthesis all refuse to start without it — the key is per person, so a run launched from your account never spends anyone else's budget.
6. Optionally save your **publisher credentials** (see §7). Leave one blank and that publisher is skipped entirely, so an unconfigured LSSR never calls out to it.

Every long-running action — a harvest, a screening pass, retrieval, a synthesis — runs as a background job with a progress bar. You can close the browser; the run continues and the page picks the progress back up when you return.

## 2. Reviews, members and roles

One **workspace is one review**. Create it with a name, a **research question** and a description: those two texts go into the top of the public page and into every LLM prompt as the study context, so vague ones produce vague drafts.

**Members** are added by email under Settings, by the owner or an admin, and only if the person already has an LSSR profile — there is no invitation flow, because a reviewer id has to exist before work can be attributed to it. The **owner** additionally adjudicates conflicts, sets how many reviewers each stage needs, curates the final extraction row, creates and revokes public links, publishes the synthesis and deletes the review.

**Reviewers per stage** (Settings): screening 1 takes 1–10 independent human votes to settle a record; screening 2 has its own number, and left blank it reuses screening 1's. Set it apart when the stages genuinely differ — a corpus double-screened on title and abstract is often read once on full text, and demanding two votes there would leave every record `pending` however many people had actually assessed it. Changing the number re-resolves stage 2 for every record, because the cached decision was computed against the old one.

The **model** for all LLM steps is a workspace setting: `claude-haiku-4-5` (default, cheap, fine for title/abstract), `claude-sonnet-5`, `claude-opus-4-8`. Query translation always uses Sonnet, regardless of this setting, because a mistranslated query is expensive in a way tokens are not.

**Duplicate this review** (Overview) starts a v2 from the same protocol: seven checkboxes decide what comes along — details, settings, criteria, extraction fields, members, queries, public link. Everything absent from that list is *work*: records, votes, extractions, full texts, iterations, synthesis and the done flags are never copied, because a copied decision would carry a reviewer's name into a review that reviewer never read. A duplicated public link gets a fresh token; the old one keeps pointing at the original. Whoever duplicates becomes the owner.

**Delete this review** sits in a danger zone at the bottom of Settings, is owner-only, lists what will disappear counted from the database, and asks you to type the review's name. There is no undo and no export step inside it: a review is deleted once in its life, and the button sits under a page people open for other reasons, so the guard costs more than a reflex. Downloaded PDFs go with it.

## 3. The protocol: criteria and extraction fields

Both live in **Settings**, and both are protocol rather than results — the public page shows them from the start, before any record exists.

**Exclusion criteria** drive screening 1 on title and abstract. **Inclusion criteria** drive screening 2 on the full text. Each has a label and a description; the description is guidance and it is what the model reads, so write what you would tell a new human coder. With no exclusion criteria the screening run is refused; with no inclusion criteria the assessment draft is refused. There is no criterion set for extraction — that is the field schema below.

**Extraction fields** are the columns of the review's data matrix. Eight builtins are seeded the first time you open Settings: country (multiselect over a country list), study year, study type, three single-choice axes for empirical work (design, data, timeframe), a literature-review methodology field and a free-text one for "Other". The three axes and the last two appear only for the matching study type, through a **show_if** condition on another field's value.

- Field types are `text`, `textarea`, `number`, `select`, `multiselect`. **The type decides where a field ends up**: `select`, `multiselect` and `number` fields feed the procedural "Study characteristics" block of the synthesis and the charts on the public page; `text` and `textarea` fields each get their own LLM-written narrative block. A finding you want narrated has to be free text; a variable you want counted has to be an option list.
- Options on a `select`/`multiselect` field are a constraint on the model too: a drafted value outside the list is dropped before it is stored, as is a number that is not numeric and a conditional field whose condition does not hold.
- The options of a builtin field are **per workspace**, so one review can split `country` into the four UK nations without touching `extraction_defaults.py` or any other review.
- Fields are read from the database on every request, so you can add one mid-review without a restart — but it will be blank on everything already extracted.
- The editor does **add, delete and reorder only**; there is no inline edit. A badly designed field cannot be corrected in place, which moves the weight onto the design: settle the schema *before* extraction starts. This is a practical constraint, not hygiene.

## 4. Query and translation

The Overview numbers the six pipeline tabs 1–6; this is the first.

**Start from** is restricted to **PubMed** or **OpenAlex**, and the route refuses anything else. The two are the sensible poles: PubMed has the richest syntax — MeSH plus field tags — so translating *out* of it loses the least, while OpenAlex has the broadest, most multidisciplinary corpus but only free text, so translating out of it produces free text everywhere. The breadth of OpenAlex is obtained by *harvesting* it as a target, not by authoring there.

The **year window** is a workspace setting and applies to every direct harvest. For the databases you search by hand it is folded into the translated string instead, in that database's own date syntax, and flagged for you to check.

**Include other databases** picks translation targets among fourteen: PubMed, Europe PMC, OpenAlex, ERIC, Scopus, Web of Science, CINAHL, JSTOR, Embase (Ovid and EBSCO), APA PsycInfo (Ovid and EBSCO), PhilPapers, HeinOnline.

- Four have an open API and are **harvested directly** — PubMed, Europe PMC, OpenAlex, ERIC. Each gets a **Run** button with a progress bar, and records land straight in the pool. One harvest at a time per review; a second is refused while the first is running.
- The other ten give you a translated query plus a link to that database's advanced-search page. You run it there, export, and import the file under Records. The paid databases have no automatic import: Elsevier refuses a server-side key without an institutional token, and PhilPapers has an API whose terms forbid redistributing the data.

**Translate with AI** is one call per target, source-aware: the syntax rules of both the source and the target database go into the prompt. The result is always saved editable, and you should edit it. Two known traps are already in the rules and worth verifying in the output anyway: MeSH headings must map to Europe PMC's `KW:"exact heading"` and never to its `MESH:` field, which silently collapses on multi-word headings (`"Tissue and Organ Procurement"` returned 53 records against PubMed's 22,858); and OpenAlex rejects wildcards inside a quoted phrase, so truncations become explicit OR-expansions.

**Refinement** lists the top MeSH terms and author keywords in the current pool, which is how you decide what to add to the query or exclude from it after a first pass.

## 5. Records: import, deduplicate, curate

**Import** takes BibTeX, RIS and `.nbib`/MEDLINE files, and Excel through a column-mapping step where Title is the anchor and nothing is ingested until it is mapped. Files are decoded as `utf-8-sig`, because Web of Science exports carry a byte-order mark that made the RIS parser drop the first record with no error at all.

At import you declare two things about provenance:

- **which database** the export came from, or `Other` with a free label up to 60 characters. That label survives all the way into the PRISMA diagram, so `Expert nomination: 9` appears in the Identification box next to PubMed.
- whether these references arrived **via other methods** — expert knowledge, citation chasing, grey literature, a request from the commissioner. This is a checkbox you tick, never inferred from the source label, because sooner or later someone imports a real database export under `Other` simply because that database is not in the menu, and an inferred flag would misreport the flow with nobody noticing. The free-text **note** next to it is where the methods section's *why* goes: a count says how many, never who nominated them.

**Deduplication** happens twice. At import, incrementally: exact DOI, then exact canonical key, then a fuzzy title match at 95% within the same publication year when there is no DOI to trust. On a duplicate the more complete version survives, the longer abstract wins, keywords and MeSH are unioned, and both provenances are kept on the record — which is why a paper found by three databases counts once and still reports three sources.

The **Deduplicate** button runs a pass over the existing pool, for what slipped through: it auto-merges the certain cases (same normalized DOI, same URL, or identical normalized title plus first-author surname plus year) and sends the doubtful ones — near-identical titles at 85% similarity — to a review page where you pick the survivor or mark them distinct. A dismissed pair is remembered and never asked again. A merge **soft-deletes** the loser with the reason recorded, and carries its sticky decisions and its full text to the survivor if the survivor lacked them. Because the loser row stays, counts that do not filter it out report merged duplicates as survivors: every count in the app and on the MCP surface filters it, and that is why the PRISMA arithmetic closes.

**Add record** types one in by hand: it gets `manual` provenance and lands in the other-methods arm of PRISMA, where a hand-added record belongs.

**Delete** at this stage is a hard delete and leaves no trace, single or batched. Removals are tracked from screening onward, as exclude decisions with a reason; before screening there is nothing to account for. Tables show the first 500 matches — narrow the filter to see more.

## 6. Screening 1 — title and abstract

The model pre-screens every pending record against the exclusion criteria and answers `include`, `exclude` or `maybe`. The prompt deliberately does not lean inclusive: genuine uncertainty — ambiguous scope, missing abstract — is parked as **maybe** for a human rather than waved through as include.

Two failure modes land in `maybe` as well: an answer that will not parse, and a record whose API call failed after its retries, whose reason then starts with `screening error:`. The `maybe` bucket is therefore not purely the model's uncertainty — read the reasons before drawing conclusions from its size.

- **Run on N pending** screens only records with no decision at all. **Re-run** additionally re-does the model's own past calls, and never a human's: a record any reviewer or adjudicator has voted on is theirs.
- Each estimate under the buttons is a rough upper bound — four characters to the token, ignoring prompt caching, which in practice keeps the real cost lower.
- **Voting is blind.** You cast include / maybe / exclude on the right; the other reviewers' votes are revealed to you once you have voted, once an adjudicator has closed the record, or if you are the owner. `✕` retracts your own vote.
- **Resolution** runs in one order: an adjudicator's ruling wins; otherwise human consensus once enough independent votes are in; otherwise the model's provisional decision; otherwise pending. Humans who disagree make the record a **conflict**, which only the owner can resolve. Humans who agree but are too few yet leave the model's decision standing in the meantime.
- **maybe parks a record.** It does not go to full text until someone moves it to include or exclude, because the full-text step works on the screening-1 included pool.
- Two filters are questions rather than states. **≠ divergent** lists every record where at least one voice differs from another, the model's included and every `maybe` counted; **conflict** only ever means two humans disagreeing, so a record the model reads differently never appears there. The gap is usually large: on one real review `conflict` found 0 records and `divergent` found 716. **🤖 model only** lists records standing on the pre-screener's word alone — empty on a fully screened corpus, and filling again after every refresh, which is exactly when it is worth opening.

Export to Excel gives one row per record with the resolved decision, who decided it, the reason, and a summary of every reviewer's vote.

## 7. Full text

Two passes over the screening-1 included pool, and both skip records you have already excluded at screening 2 — those are decided, and chasing their text costs time for nothing.

**Pass 1 — Fetch** needs a DOI; a record without one fails immediately. It then walks a ladder and stops at the first rung that yields verified full text:

1. **Europe PMC full-text XML**, when the article is in PMC. No PDF and no conversion — these go straight to *converted*, and for a PubMed-shaped corpus this is the highest-yield rung.
2. **Unpaywall and OpenAlex** open-access locations, repository copies before publisher ones (publisher copies are the ones behind bot walls), direct PDFs before landing pages.
3. **Landing pages**, read for the `citation_pdf_url` meta tag most publishers emit.
4. **Publisher TDM APIs** — Elsevier, Springer, Wiley — tried only for DOIs with that publisher's prefix and only when you have saved the credential, so no call is spent that is known to fail.
5. **Open-access siblings**: same-titled OpenAlex works under a different DOI, which is where the preprint copy of a paywalled article lives. OpenAlex indexes the two versions as separate works, so nothing earlier in the ladder can see them.

Each candidate is **converted as it arrives and verified against the record's own title**, because a candidate can only be checked once it is text. One that turns out to be a different paper is discarded, the ladder continues, and the record keeps a note saying what was thrown away and why. Deferring conversion would mean accepting the first PDF that downloads, whatever it contains.

Outcomes per record: **converted** (markdown in hand), **fetched** (a PDF is stored but the converter was unavailable — pass 2 finishes it), **OA link** (a location was found but no document could be verified; retrieve it by hand), **not found**.

**Pass 2 — Convert** sends every stored-but-unconverted PDF to paper2md, LSSR's own conversion service. A title mismatch here is recorded as a *warning* rather than a discard, because this path also carries your own uploads and a reviewer who deliberately attached a file outranks a heuristic. If nothing converts and some fail, the run reports an error naming the service instead of a quiet "done" you cannot act on.

**Upload** takes PDF, DOCX, Markdown or TXT for anything the ladder could not reach. A PDF is stored for the convert pass; the text formats already are the full text and go straight to converted. Your upload clears the retrieval notes on that record, since you chose the file.

Nothing here bypasses a paywall. The publisher APIs are the sanctioned route — systematic retrieval under the subscription your institution already pays for — and their credentials are yours, per person, encrypted at rest, because the entitlement follows the person and their institution and not the server:

| Credential | What it is | Caveat |
|---|---|---|
| Elsevier API key | free from dev.elsevier.com | on its own it only works from inside your institution's network; off-campus, and on a server, every request is refused |
| Elsevier institutional token | obtained by your library from Elsevier | required for any full text when not on the institution's network |
| Springer Open Access API key | from dev.springernature.com, the OA key and not the Meta one | open-access content only |
| Wiley TDM client token | from your Wiley Online Library account | returns a PDF, so it goes through the convert pass |

A credential a publisher rejects is reported in the run message: a misconfigured key is a thing to fix, not a paper that does not exist.

The **reader sees the whole text**, references included. Back matter is stripped only for the LLM, from the first references / acknowledgements / funding / declarations heading onward, and only when that heading falls after 40% of the document, so a stray early match cannot gut the text.

## 8. Assessment — screening 2 and extraction in one act

One large modal: the full text on the left, the inclusion criteria, the include/maybe/exclude decision and the extraction form on the right. Reading a paper twice — once to decide, once to extract — is the thing this step exists not to do.

**The AI draft** is one conditional call per record: the model returns the screening-2 decision and, only if that decision is include, the field values. An excluded record costs no extraction tokens. It drafts only records that are converted, still pending and untouched by a human; **Re-draft** additionally redoes its own past drafts and still never a human's. Values are validated against the schema before they are stored.

The draft pre-fills your form only when you have no row of your own; the badge says so, and it is a draft to check. Other reviewers' extractions are visible to the owner **read-only and deliberately never as a pre-fill**: adopting someone else's answers with one click would turn a second independent extraction into a copy of the first.

**The authoritative value** of a field is the owner-curated `final` row if there is one, else the most recently saved reviewer row, else the model draft. That is also a trap already paid for: because it is the *latest* row, opening a colleague's extracted record and saving an empty form used to empty it in exports and synthesis without deleting anything. Saving a blank form now cannot blank a record another reviewer has extracted; clearing your **own** existing row stays possible, since that is a deliberate act. The owner's checkbox **also set as final version** writes the curated row.

Screening 2 is multi-reviewer with the same blind voting, the same resolution order and the same owner adjudication as screening 1, using its own reviewer count.

**The modal opens even when there is no full text**, on purpose. A report nobody can obtain is a *decided* record: exclude it and say so in the reason. Leaving it pending hides it from every count, zeroes it out of PRISMA and keeps it out of the synthesis.

Two navigation filters earn their place here: **🤖 model only** (drafted by the model and confirmed by nobody — and the draft also pre-filled the extraction, so these are the ones to check) and **⌀ included, not extracted** (in the review, contributing to no field of the synthesis; an existing but empty extraction counts as a hole too, because from the reader's side it is the same hole).

Export to Excel gives the record × field matrix, with the authoritative value per field and fields hidden by an unmet condition left blank.

## 9. Synthesis

Generating the synthesis produces, over the finally included records:

- **Study characteristics** — a distribution summary of the `select`, `multiselect` and `number` fields, computed from the database with no LLM involved, so no figure in it can be miscounted. Number fields report range, median and n.
- **One narrative block per free-text field**, written by the model from the per-study values. A field no included study answered says so instead of being narrated; a value of "not addressed" is skipped.

**Citations are procedural.** The model cites only by inserting a study token — `[S1]`, `[S2]` — and is instructed never to write an author, a year, a DOI or a link. Each token is then replaced with a citation built from that record's own fields (`Surname et al., Year, https://doi.org/…`), and a token that is not in the map is dropped. A reference here cannot be invented: the model chose where a citation goes, never what it says.

Regenerating replaces the blocks and keeps the published state. **Publish** (owner only) is what makes the synthesis visible on the public link; a synthesis exists as a draft until then.

## 10. PRISMA, done flags and the public dashboard

The **PRISMA flow** is computed live from the current pool, not snapshotted when the synthesis was generated, and rendered as an SVG in Overview and on the public page, with the exact counts in a table underneath. Stages whose step is not marked done appear as dashed *pending* boxes with no numbers — a flow diagram that reported a screening nobody has run would be worse than an empty one.

When at least one reference arrived via other methods, the diagram splits into the **two columns PRISMA 2020 draws**. The membership rule is the whole design: a record is in the *other* arm only when **none** of its provenances is a database. So a nomination that a later harvest also finds moves to the left, the column measures what those routes found *that the search missed* rather than the diligence of whoever nominated, and it comes with the number saying how much of a nominated list the query had already caught. Duplicates removed is not split by arm: it is a difference, not a population, and splitting a difference the obvious way subtracts twice. Reviews that never use the manual arm have no second column and their diagram is identical to before the feature existed.

One deliberate deviation from the published template: the right column *does* have a title/abstract screening box, because LSSR screens a nominated record against exactly the same criteria as every other. Drawing the template's shape would hide a step that really happens; the promise of this diagram is that every box is calculated from the pool, not that it resembles a figure.

**Mark as done** on each tab is a human declaration, and it gates what the public page shows.

**Public link** (`/r/{token}`, created and revoked in Overview by the owner) is a read-only dashboard with no login, for a commissioner or an external reviewer who has no account here and is not meant to get one. It shows the description, research question, both criterion sets and the extraction schema from the start — protocol, not results — plus the live PRISMA. The queries, the record statistics (per-database pie, year histogram, author and keyword frequencies, publication types) and the full-text retrieval breakdown appear when their step is marked done; the screening and screening-2 decision bars appear once there is something to show; the charts over the included studies need the assessment step done; and the synthesis appears only once published. It carries aggregates and never individual votes or reviewer names.

## 11. Living: iterations and refresh

An **iteration** is the unit of *living*. The first one opens when you run your first search or import. **Start new iteration (refresh)** closes the open one and opens the next: subsequent searches and imports attach there, deduplication marks records it sees again as last seen in this iteration, genuinely new records get their first-seen there, and screening and assessment only touch records that are still pending.

The consequence is the point: a refresh re-runs the search and re-screens nothing you already decided. Overview lists every iteration with how many records it brought in.

## 12. Export

Three Excel files, from the tabs they belong to:

| File | Content |
|---|---|
| **Records** | one row per record in the live pool — the bibliography as imported, provenance (which databases returned it, which iteration first and last saw it), whether it was hand-added. No decision columns: this is the pool before any judgement |
| **Screening** | the same bibliography plus the resolved screening-1 decision, who decided it, the reason, and every reviewer's vote |
| **Assessment** | one row per screening-1-included record: bibliography, full-text status, the resolved screening-2 decision with its votes, and one column per extraction field with the authoritative value |

There is no export step inside the delete confirmation. Take one before you delete anything worth keeping.

## 13. Working from a chat (MCP)

A scoping review is something you ask questions about far more often than you edit: how many records are still unscreened, what the inclusion criteria actually say, which included studies came out of Wales, what the extraction says about study design. Those questions arrive in a conversation. LSSR exposes the same corpus there, through twelve tools over MCP.

**Getting a key.** Profile → MCP keys → New key. Paste the endpoint (`https://lssr.borant.eu/mcp`) and the key into your client as an `X-API-Key` header. Clients that cannot set headers can use `https://lssr.borant.eu/mcp/k/<your-key>` instead — there the URL *is* the credential and can end up in access logs, so use one key per client and revoke rather than share.

**It is read-only, all of it.** No tool votes, extracts, adjudicates or marks a step done. A screening decision carries a reviewer's name and belongs to a person doing the reading; a surface where a model could cast one would quietly turn the reviewer into an editor of its own output. The practical consequence is that a leaked key exposes a corpus and cannot corrupt one. Duplicating and deleting a review are not there either — a v2 is created by a person, in the UI.

**What it reaches** is exactly what you reach: the key is your identity, so a review you are not a member of does not exist as far as your assistant is concerned, and reports "no review" rather than "forbidden" so nothing can be enumerated. A review is named by id or by name, and an ambiguous name is refused with the candidates listed rather than resolved to the first hit.

**What you can ask for**: the list of your reviews and the state of one (configuration, steps done, live PRISMA including the two arms, LLM cost per step); the iterations and the imports that fed them; the queries and which databases are harvestable; the protocol — both criterion sets plus the extraction schema with its options and conditions; records, with the same filters as the UI including `divergent`, `modelonly` and `empty`; one record in full with every vote and every reviewer's extraction rows alongside the authoritative one; the full text of one record; how retrieval went across the pool; the records whose reviewers do not agree, with the votes; the distribution of extracted values; and the synthesis with its blocks.

Three details worth knowing:

- **Counts are computed, not narrated.** The extraction summary and the PRISMA numbers come out of SQL, so the model is handed figures it cannot have hallucinated — the same principle as the procedural block of the synthesis.
- **Full text is a separate, paginated call.** It is the one request that can fill a context by itself, so it does not ride along inside a record read.
- **Every vote is visible here, even the ones the UI hides.** Blinding is a discipline of the *moment of voting*, which happens in the app; this surface exists to read a corpus, and a reader that saw half the votes would mostly produce wrong totals. The cost has to be said plainly: a reviewer who reads here before voting there has read ahead. What answers that is who holds a key, not a filter that would make every count depend on its reader.

## 14. Data protection

LSSR processes what you put into it, and it is up to you, the researcher, to make sure that what you put in is appropriate to process. The facts to base that judgement on:

- **Stored on the server**, for the life of the review and visible to all its members: bibliographic records and abstracts, retrieved full texts as markdown, downloaded PDFs in the review's own folder, every reviewer's votes with their name and reason, one extraction row per reviewer per record, and the synthesis. Deleting the review removes the rows and the folder.
- **Your credentials** — the Anthropic key and the Elsevier, Springer and Wiley tokens — are per user and encrypted at rest. There is no central key, which is also why nothing about roles has to be configured: a run spends its own launcher's budget and nobody else's.
- **What reaches the Anthropic API**: the query string at translation; each record's title and abstract at screening; the full text minus its back matter, capped, at the assessment draft; the per-study extracted findings at synthesis. That is an external processor. Personal data and special categories of data should not travel through this pipeline unless your ethics approval, your data management plan and your legal basis explicitly cover it.
- **What reaches other external services**: the query goes to NCBI, Europe PMC, OpenAlex and ERIC when you harvest; a DOI goes to Unpaywall, OpenAlex and the publisher APIs during retrieval, and Unpaywall requires an email address, which is the configured one or the review owner's; landing pages are fetched from publisher sites. PDF conversion goes to paper2md, a companion service run alongside LSSR, not a third-party endpoint.
- **An MCP key is a second door onto the same corpus**, including the retrieved full texts, which means those texts reach whichever model that client talks to. It is the same judgement as the LLM steps, made once for everything you can see rather than run by run. Make it deliberately and revoke the key when the project ends.
- **A public link is public.** Anyone holding the URL reads the dashboard and, if published, the synthesis. It carries no votes and no reviewer names, but it does carry your criteria, your queries and your counts. Revoke it when it has served its purpose.
- **Full text retrieved under your institution's entitlement stays inside the review.** LSSR is not a redistribution channel, and the routes that would make it one are absent on purpose: no Sci-Hub layer, because private use does not cover a multi-user server that downloads, stores and shares; no PhilPapers harvest, because its terms forbid redistributing the data.
- Compliance with GDPR, your institutional requirements, your ethics approval and the terms of every publisher API key you save is the researcher's responsibility, not the tool's. When in doubt ask your data protection officer, before uploading rather than after.

## 15. Good practices

- **Settle the extraction schema before extracting.** There is no inline edit, and a field added later is empty on every record already done. Write the fields against the questions the review has to answer, and decide for each one whether it is a counted variable (option list or number) or a narrated finding (free text) — that choice is what puts it in the procedural block or in a narrative one.
- **Author the query in PubMed** unless the review is genuinely outside biomedicine, then harvest OpenAlex as a target for breadth. You get the controlled vocabulary and the wide corpus that way; authoring in OpenAlex gets you neither.
- **Read every translated query before you run it.** It is a suggestion from a model, and the failure mode is not an error message — it is a silently impoverished harvest. Compare the hit count each database returns against the source; a block that collapses from 549 to 29 is what a broken field tag looks like.
- **Pilot the criteria.** Screen a few dozen records, read the model's reasons rather than its verdicts, fix the criterion wording, then run the corpus. The description under each criterion is the whole prompt as far as screening is concerned.
- **Ask for the estimate first.** It is free and it appears under every run button; a cost seen afterwards is not a decision.
- **Watch `divergent`, not `conflict`**, and open `model only` after every refresh. Those two lists are where the holes that normal counts hide actually are.
- **Decide the unobtainable papers.** Exclude with a reason beats pending: a pending record is invisible to PRISMA, to the synthesis and to every count on the public page.
- **Never mark assessment done while `included, not extracted` is above zero.** Those records are in the review and in no line of its results.
- **Export before you delete**, and before any operation you would want to reconstruct. Three Excel files cost one click each.
- LSSR produces a **first pass to correct, not a finished review**. The pre-screen, the draft extraction and the narrative blocks are all material for a reviewer to work on; the judgement, the adjudication and the final text stay yours, and the tool is built so that they visibly do.
