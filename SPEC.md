# LSSR — Living Systematic Scoping Review

*Spec & design — bozza per validazione*
*v0.1 — 2026-07-10*

---

## 1. Visione

LSSR è un tool borant che porta un'intera scoping review — dalla query alla
sintesi pubblicata — dentro un unico software, e la rende **living**: ogni
passo è iterabile on demand per aggiornare i risultati senza rifare il lavoro
umano già fatto.

È un'estensione del processo di RevMaster **a valle e a monte**: a monte prende
in carico la costruzione e la traduzione della query (oggi TopicTracker +
Zotero), a valle la sintesi narrativa e la pubblicazione (oggi a mano).

Deliverable finale di una review: **una pagina pubblica** con la sintesi
narrativa per criterio (sempre citata), i metadati (date delle varie
iterazioni) e lo schema PRISMA aggiornato.

---

## 2. Stack e convenzioni (standard borant)

Identico agli altri tool della famiglia — nessuna deviazione.

| Aspetto | Scelta |
|---|---|
| Backend | FastAPI + uvicorn |
| Template | Jinja2 |
| DB | SQLAlchemy + SQLite (`./data/lssr.db`, volume Docker) |
| Auth | JWT in cookie httpOnly (`bcrypt`), `is_admin`, TOTP opzionale (pattern AutoCode) |
| LLM | `anthropic`, chiave per-utente Fernet-encrypted (pattern AutoCode) |
| Job lunghi | thread di background + dict `JOBS` globale + polling di stato (pattern TopicTracker) |
| Stile | `static/css/style.css` — palette scura borant, copiata da AutoCode |
| Deploy | Docker + Caddy, `/opt/apps/lssr/`, clone git |
| **Porta** | **8013** (prossima libera in sequenza FastAPI) |
| **Sottodominio** | **`lssr.borant.eu`** |

Riuso diretto di codice/servizi esistenti:

- **TopicTracker** → `pipeline.py` (esearch/efetch PubMed, parse MEDLINE) per il passo 1 e 6.
- **paper2md** → API `POST /convert` (già live-ready su :8008) per il passo 7. Nessuna reimplementazione.
- **AutoCode** → pattern chiamata Anthropic, cost log, worker paralleli, codebook/criteri per i passi 5, 8, 9.
- **RevMaster** → `pdf_fetch.py` (download full text) e il modello mentale dei criteri di screening/assessment per i passi 6, 8, 9.

---

## 3. Modello concettuale

**Un workspace = una living review.** Tutto ciò che serve alla review vive nel
workspace: query, criteri, record, decisioni, sintesi.

**Il record è l'oggetto persistente.** Un `Record` (paper / libro / capitolo /
letteratura grigia) vive a livello di workspace e **accumula attraverso le
iterazioni**. Non viene ributtato via a ogni refresh.

**Le decisioni umane sono sticky.** Una volta che un record è stato deciso
(incluso/escluso a screening 1 o 2, o valutato), quella decisione sopravvive
alle iterazioni successive. Il refresh tocca solo ciò che è nuovo o ancora
pendente.

**L'iterazione è l'unità del "living".** Ogni `Iteration` è un passaggio
completo (o parziale) nella pipeline: ri-esegue le ricerche, importa il nuovo,
deduplica, e sottopone a screening/assessment **solo i record nuovi**. Genera un
PRISMA aggiornato e può rigenerare la sintesi. Le iterazioni sono un registro:
"quando abbiamo aggiornato, cosa è entrato".

```
Workspace (una review)
 ├─ SearchQuery[]        una per database (PubMed = primaria, le altre tradotte)
 ├─ ExclusionCriterion[] (screening 1: titolo+abstract)
 ├─ InclusionCriterion[] (screening 2: full text)
 ├─ AssessmentCriterion[] (passo 9)
 ├─ Member[]             utenti col diritto di lavorarci
 ├─ PublicShare[]        link read-only per la pagina pubblica
 ├─ Iteration[]          i refresh (n=1,2,3…)
 ├─ Record[]             i documenti, persistenti e accumulati
 │   ├─ RawReference[]   righe grezze pre-dedup (provenienza)
 │   └─ Assessment[]     esiti passo 9, per criterio
 └─ Synthesis           l'output pubblico (blocchi per criterio + PRISMA)
```

---

## 4. Data model (entità proposte)

Fondazione già scritta in `models.py`: **User, Workspace, WorkspaceMember,
PublicShare**. Il resto qui sotto è la proposta da validare prima di scriverlo.

**User** *(fatto)* — come AutoCode: `email, name, hashed_password,
api_key_encrypted, totp_*, is_admin, is_active`.

**Workspace** *(fatto)* — `name, description, research_question, owner_id,
created_at`. I criteri e le query sono tabelle figlie (sotto), non JSON, così la
UI li edita singolarmente e l'assessment li cita per id.

**WorkspaceMember** *(fatto)* — `(workspace_id, user_id)`. Un utente sta in ≥1 workspace.

**PublicShare** *(fatto)* — `token, workspace_id, created_by, active, created_at`.
Link read-only alla pagina di sintesi pubblica. Revocabile.

**SearchQuery** — `workspace_id, database (pubmed|scopus|wos|cinahl|jstor),
query_string, is_primary, updated_at`. PubMed è la primaria; le altre sono le
traduzioni (passo 2).

**ExclusionCriterion / InclusionCriterion / AssessmentCriterion** —
`workspace_id, label, description, order`. Tre tabelle omogenee (o una sola con
`kind`). Le prime due guidano lo screening; l'ultima l'assessment (il
`description` diventa il prompt del criterio).

**Iteration** — `workspace_id, number, status, started_at, completed_at`,
+ snapshot JSON di query e criteri al lancio (audit). `status` traccia il punto
della pipeline.

**Record** — il cuore. Campi bibliografici minimi condivisi (paper/libro/
capitolo/grigia):
`workspace_id, type, authors, year, title, abstract, doi, url, source (journal/
publisher), keywords_json, mesh_json, language`.
Provenienza + ciclo di vita:
`source_dbs_json, canonical_key (doi normalizzato o title+year), first_seen_iteration_id,
last_seen_iteration_id, added_manually, is_removed, removed_reason`.
Full text: `full_text_path, full_text_md`.
Decisioni sticky:
`screen1_decision (include|exclude|pending), screen1_reason, screen1_by (model|user), screen1_at,
screen2_decision, screen2_reason, screen2_by, screen2_at`.

**RawReference** — riga grezza da un import, pre-dedup: `record_id (merge target),
import_id, raw_json`. Serve a "tenere il più completo" nel dedup e a ricostruire
la provenienza.

**Import** — evento di import: `workspace_id, iteration_id, database, source_file,
format (bibtex|ris|csv|api), count, imported_at`.

**Assessment** — `record_id, criterion_id, iteration_id, result_text (finding +
citazione), model, tokens_in, tokens_out, cost_usd, created_at`. Ri-eseguibile.

**Synthesis** + **SynthesisBlock** — `Synthesis(workspace_id, iteration_id,
prisma_json, generated_at, published)`; `SynthesisBlock(synthesis_id,
criterion_id, narrative_text)`. Un blocco per criterio, con citazioni.

**UserCostLog** — identica ad AutoCode, per tracciare la spesa LLM dei passi 5/8/9/10.

---

## 5. La pipeline (10 passi) mappata sull'architettura

| # | Passo | Come | Riuso |
|---|---|---|---|
| 1 | **Query PubMed + raffinamento** | run esearch/efetch; analisi NLP di titoli/abstract/keyword/MeSH (frequenze) per suggerire keyword da escludere o aggiungere; loop di raffinamento | TopicTracker `pipeline.py` |
| 2 | **Traduzione query** | PubMed → sintassi Scopus/WoS (e CINAHL/JSTOR) via LLM con few-shot delle regole di sintassi note; sempre con revisione umana | nuovo (LLM) |
| 3 | **Run + import risultati** | import file standard (BibTeX/RIS/CSV) da run manuali sui DB; parser dedicati; API automation (Scopus/WoS) come fase 2 opzionale. Teniamo solo i field utili (autori, anno, titolo, abstract, DOI, link, giornale, keyword, MeSH) | nuovo (`bibtexparser`/`rispy`) |
| 4 | **Deduplication** | normalizzazione DOI + fuzzy match su title+year; in caso di duplicati **tieni il record più completo**, merge della provenienza (`source_dbs`); aggiunta/rimozione manuale | nuovo |
| 5 | **Screening 1 (titolo+abstract)** | LLM valuta ogni record vs `ExclusionCriterion[]`; output include/exclude + motivazione; worker paralleli + cost log | pattern AutoCode |
| 6 | **Download full text** | download automatico (Unpaywall/pdf_fetch) + upload manuale del singolo PDF | RevMaster `pdf_fetch.py`, TopicTracker |
| 7 | **paper2md** | `POST /convert` al servizio paper2md; salva `full_text_md` | paper2md API |
| 8 | **Screening 2 (full text)** | LLM valuta il full text vs `InclusionCriterion[]` | pattern AutoCode |
| 9 | **Assessment** | LLM estrae, per ogni `AssessmentCriterion`, il finding + citazione dal full text | pattern AutoCode (coding) |
| 10 | **Sintesi** | LLM aggrega i finding per criterio in un blocco narrativo citato; pagina pubblica con PRISMA + date | nuovo (LLM) |

### 5b. Integrazione passi 8 + 9 (ottimizzazione token)

Domanda esplicita di Spit: "come e se integrare 8 e 9 per ottimizzare i token".

**Raccomandazione: chiamata unica condizionale sul full text.** Un solo prompt
che riceve il full text una volta e ritorna structured output:

```json
{
  "inclusion_decision": "include" | "exclude",
  "inclusion_reason": "...",
  "assessments": [ { "criterion_id": N, "finding": "...", "citation": "..." }, ... ]
}
```

Il modello popola `assessments` **solo se** `inclusion_decision == "include"`.
Così il full text (la parte cara del prompt) viene letto una volta sola per
entrambi i passi, e sui record esclusi non si spende un token di assessment.

Trade-off: accoppia due decisioni concettualmente distinte in una chiamata, e se
i criteri di assessment sono tanti il prompt di output cresce. Alternativa più
semplice ma più cara: due chiamate separate (screening 2, poi assessment solo
sugli inclusi). Le due strade condividono lo stesso data model — si può partire
con la separata e passare all'integrata dietro un flag di workspace.

---

## 6. Multiutente, multiworkspace, sharing pubblico

- **Utenti in ≥1 workspace** via `WorkspaceMember`. Owner + membri; l'owner
  gestisce membri e criteri. (Ruoli fini rimandabili — per ora owner/member.)
- **Public read-only sharing**: per ogni workspace si generano `PublicShare` con
  un token opaco. La rotta pubblica `/r/{token}` mostra **solo** la pagina di
  sintesi (passo 10): blocchi per criterio, PRISMA, date. Nessun login, nessun
  accesso ai record grezzi o ai criteri interni. Revocabile e rigenerabile.
- La sintesi è pubblica solo se `Synthesis.published == true` **e** esiste uno
  share attivo — doppio interruttore.

---

## 7. Il modello "living" / iterabilità

Premere "Refresh" crea una nuova `Iteration` ed esegue:

1. Ri-run delle `SearchQuery` (passo 1/3) → nuovi `RawReference`.
2. Dedup (passo 4) contro i `Record` esistenti: i già visti aggiornano solo
   `last_seen_iteration_id`; i nuovi diventano `Record` con decisioni `pending`.
3. Screening 1/2 e assessment (5/8/9) **solo sui record `pending`** — il lavoro
   umano/LLM già fatto non si ripete.
4. Rigenerazione di `Synthesis` + PRISMA con i conteggi aggiornati.

Ogni iterazione lascia una traccia (cosa è entrato, quando), così la pagina
pubblica può mostrare "ultimo aggiornamento: …" e l'evoluzione dei conteggi.

---

## 8. Roadmap di sviluppo

- **Fase 0 — Fondazione** *(fatta)*: repo, stack, auth, multiutente,
  multiworkspace, public share, UI base, deploy skeleton.
- **Fase 1 — Ingest & dedup** (passi 1–4) *(fatta)*: query PubMed +
  raffinamento (frequenze MeSH/keyword), traduzione LLM, import BibTeX/RIS,
  dedup incrementale (DOI + fuzzy, tiene il più completo), add/remove manuale,
  chiave Anthropic per-utente. Moduli: `pubmed.py`, `ingest.py`, `translate.py`,
  `crypto.py`.
- **Fase 2 — Screening** (passi 5–8): screening 1, download full text, paper2md,
  screening 2.
- **Fase 3 — Assessment & sintesi** (passi 9–10): assessment, sintesi narrativa,
  pagina pubblica + PRISMA.
- **Fase 4 — Living**: iterazioni, decisioni sticky, refresh on demand.
- **Fase 5 — Automazione DB** (opzionale): API Scopus/WoS per il passo 3.

---

## 9. Decisioni aperte (da validare con Spit)

1. **Screening 2 + assessment**: chiamata unica condizionale (raccomandata) vs
   due chiamate separate? O flag configurabile per workspace?
2. **Traduzione query (passo 2)**: LLM-assisted con revisione umana va bene come
   punto di partenza, o vuoi anche un motore a regole per Scopus/WoS?
3. **Run dei DB (passo 3)**: import manuale di file (BibTeX/RIS) come MVP, con
   automazione API rimandata alla fase 5 — d'accordo?
4. **Ruoli nel workspace**: basta owner/member per ora, o servono ruoli più fini
   (es. screener vs revisore)?
5. **Granularità dell'iterazione**: un refresh ri-esegue sempre tutta la
   pipeline, o vuoi poter rilanciare singoli passi in modo indipendente?
6. **Lingua UI**: inglese come gli altri tool, o multilingua (i18n AutoCode)?
