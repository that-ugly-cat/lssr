// Act on one record without losing your place.
//
// Screening, full text and assessment are all the same shape: a long list you
// work down, and an action on one row. Every one of those actions used to POST,
// redirect and redraw the whole page — back to the top, scroll position gone,
// and on Assessment the filter thrown away with it. Here the action goes out by
// fetch, the regions whose numbers moved are redrawn from a fresh render of the
// same URL, and a line at the bottom says what was recorded.
//
// Nothing here is required for the app to work: the forms are ordinary POSTs to
// routes that still redirect, and this file only intercepts what it can finish
// itself. Anything unexpected — a 500, an expired session, a redraw that fails
// — falls back to the plain behaviour or to a reload.
//
//   const live = inplace({
//     regions: ['scr-summary', 'scr-table'],   // swapped by innerHTML
//     jobs:    [['scr-status', 'scr-btn']],    // keep a running job's button disabled
//     roots:   ['scr-table'],                  // where row forms are listened for
//     match:   (action) => action.endsWith('/vote') ? 'vote' : null,
//     message: (kind, form, data) => '…',
//   });
//
// Regions are swapped by innerHTML, never replaced: the container node keeps
// its identity and its listeners, and only its contents come from the server.
function inplace(opts) {
  const regions = opts.regions || [];
  const jobs = opts.jobs || [];

  // ── the toast ──
  const toastEl = document.getElementById('toast');
  const toastText = toastEl && toastEl.querySelector('.toasttext');
  let toastTimer = null;

  // `action` is {label, run}: a button the notice carries, for the one thing
  // that cannot wait — an undo is only an undo while it is still on screen.
  function toast(message, action) {
    if (!toastEl) return;
    clearTimeout(toastTimer);
    toastText.textContent = message;
    const old = toastEl.querySelector('.toastaction');
    if (old) old.remove();
    if (action) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-small toastaction';
      btn.textContent = action.label;
      btn.addEventListener('click', () => {
        clearTimeout(toastTimer);
        toastEl.classList.add('hidden');
        action.run();
      });
      toastEl.append(btn);
    }
    toastEl.classList.remove('hidden');
    // Long enough to read one line, short enough not to sit over the next row
    // — and longer when there is a button, because reading it is only half of
    // what the reader has to do.
    toastTimer = setTimeout(() => toastEl.classList.add('hidden'), action ? 9000 : 3500);
  }

  // ── the redraw ──
  //
  // Whole regions, not single rows: one action changes the counts in the
  // headline, in the filter tabs and on the run buttons, and can move the
  // record out of the filter being looked at. Re-fetching the current URL keeps
  // the filter, the search and the sort exactly as they are, so the counts
  // cannot drift away from the rows they count.
  function redraw(flashRid) {
    return fetch(location.href, { headers: { 'X-Requested-With': 'fetch' } })
      .then((r) => (r.ok ? r.text() : Promise.reject(r)))
      .then((html) => {
        const fresh = new DOMParser().parseFromString(html, 'text/html');
        // An expired session answers with the login page, and pasting that into
        // the table would be a silent lie. Reload, and let the app say so.
        if (regions.length && !fresh.getElementById(regions[0])) return location.reload();
        for (const id of regions) {
          const from = fresh.getElementById(id);
          const here = document.getElementById(id);
          if (from && here) here.innerHTML = from.innerHTML;
        }
        // A run button inside a redrawn region comes back as the server drew
        // it, which is not how the job poller left it: mid-run the poller had
        // disabled it, and the server has no idea a run is going.
        for (const [statusId, btnId] of jobs) {
          const btn = document.getElementById(btnId);
          if (btn && pollerBusy(statusId)) btn.disabled = true;
        }
        flashRow(flashRid);
        if (opts.lastTouch) markLastTouch();
        // Whatever the page holds that the server never saw — a set of ticked
        // checkboxes, say — is restored here, after the new rows are in.
        if (opts.afterRedraw) opts.afterRedraw();
      });
  }

  // The row that was just acted on, briefly. Under most filters a settled
  // record leaves the list, so this fires only when the row is still there —
  // which is the case that needs it, because nothing else on a page of
  // near-identical rows says where the change landed.
  function flashRow(rid) {
    if (!rid) return;
    const row = document.querySelector(`tr[data-rid="${rid}"]`);
    if (!row) return;
    row.classList.remove('row-flash');
    void row.offsetWidth;              // restart the animation on a second act
    row.classList.add('row-flash');
  }


  // ── where you were ──
  //
  // The last row you acted on here, kept so that coming back to a list of
  // five hundred does not mean finding your place by eye. Every action on
  // every one of these tables already funnels through act() below, which
  // knows the record — so this needs no route, no column and no per-page
  // wiring beyond naming where to hang the chip.
  //
  // It lives in this browser and nowhere else. That is the whole bargain: a
  // bookmark for yourself is not worth a table, it must never be visible to
  // anybody else, and on a different machine it simply starts empty rather
  // than lying. Every access is wrapped, because private windows and blocked
  // site data make localStorage throw rather than return nothing.
  const TOUCH_KEY = 'lssr-last:' + location.pathname;

  function readTouch() {
    try {
      const raw = localStorage.getItem(TOUCH_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function rememberTouch(rid) {
    if (!rid) return;
    try {
      localStorage.setItem(TOUCH_KEY, JSON.stringify({ id: String(rid), at: Date.now() }));
    } catch (e) { /* nothing to do, and nothing worth saying */ }
  }

  function lastRow() {
    const t = readTouch();
    return t && document.querySelector(`tr[data-rid="${t.id}"]`);
  }

  // The chip is rebuilt when missing, not created once: every page hangs it
  // off its filter bar, and every one of those bars is a redrawn region — so
  // the first action on the page would otherwise sweep it away.
  function ensureChip() {
    let chip = document.getElementById('last-touch');
    if (chip) return chip;
    const anchor = document.getElementById(opts.lastTouch);
    if (!anchor) return null;
    chip = document.createElement('button');
    chip.type = 'button';
    chip.id = 'last-touch';
    chip.className = 'last-touch';
    chip.hidden = true;
    chip.addEventListener('click', () => {
      const row = lastRow();
      if (!row) return;
      row.scrollIntoView({ block: 'center', behavior: 'smooth' });
      flashRow(readTouch().id);
    });
    anchor.append(chip);
    return chip;
  }

  // Re-applied after every redraw: the rows that come back are new elements.
  function markLastTouch() {
    const t = readTouch();
    document.querySelectorAll('tr.row-last').forEach((r) => r.classList.remove('row-last'));
    const chip = ensureChip();
    if (!t) { if (chip) chip.hidden = true; return; }
    const row = lastRow();
    if (row) row.classList.add('row-last');
    if (!chip) return;
    chip.hidden = false;
    chip.textContent = '↩ #' + t.id;
    chip.classList.toggle('muted', !row);
    // Said rather than guessed at: a chip that scrolls nowhere is worse than
    // one that explains why it cannot.
    chip.title = row
      ? 'The last record you acted on here — click to go back to it'
      : 'The last record you acted on here is not in the current filter';
  }

  if (opts.lastTouch) markLastTouch();


  // ── posting a form in place ──
  //
  // Resolves with the route's JSON, or rejects — and a rejection means the
  // action did not land, so the caller may safely fall back to submitting the
  // form the ordinary way.
  function post(form) {
    const multipart = (form.enctype || '').includes('multipart');
    const data = new FormData(form);
    return fetch(form.getAttribute('action') || location.href, {
      method: 'POST',
      // A multipart body carries its own boundary, which only the browser can
      // write: setting Content-Type by hand here corrupts the upload.
      headers: multipart
        ? { 'X-Requested-With': 'fetch' }
        : { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'fetch' },
      body: multipart ? data : new URLSearchParams(data),
    }).then((r) => (r.ok ? r.json() : Promise.reject(r)));
  }

  // Which record a form is about, when the form is not inside its row. The
  // edit and add forms live in a modal and the review form lives in another,
  // so closest('tr') finds nothing for exactly the actions most worth
  // remembering. The id is in the action either way.
  function ridFromAction(action) {
    const m = /\/(?:records|assessment)\/(\d+)\//.exec(action || '');
    return m ? m[1] : null;
  }

  // Post, report, redraw — the whole cycle, with the fallbacks in one place.
  function act(form, kind, rid) {
    const row = form.closest('tr');
    const flashRid = rid || (row && row.dataset.rid)
                         || ridFromAction(form.getAttribute('action'));
    // Remembered before the round trip, not after: what makes this the row you
    // were on is that you acted on it, whatever the server then answers.
    rememberTouch(flashRid);
    if (row) row.classList.add('row-busy');
    return post(form)
      .then((data) => {
        // A message may come back as a string, or as {text, action} when the
        // page wants a button beside it.
        const said = opts.message ? opts.message(kind, form, data) : 'Saved';
        if (typeof said === 'string') toast(said);
        else if (said) toast(said.text, said.action);
        if (opts.onDone) opts.onDone(kind, form, data);
        // Past this line the action is recorded, so a redraw that fails must
        // not fall through to the retry below and post it a second time.
        // Reload instead: the state is on the server, only the picture is old.
        return redraw(flashRid).catch(() => location.reload());
      })
      .catch((err) => {
        if (err instanceof Error) throw err;   // a bug here, not a failed POST
        form.submit();                          // let the plain path report it
      })
      .finally(() => { if (row) row.classList.remove('row-busy'); });
  }

  // ── delegation ──
  //
  // On the container and not on each form: the rows are replaced on every
  // redraw, and a listener bound to a form would go with them.
  for (const rootId of opts.roots || []) {
    const root = document.getElementById(rootId);
    if (!root) continue;
    root.addEventListener('submit', (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      const kind = opts.match ? opts.match(form.getAttribute('action') || '', form) : null;
      if (!kind) return;
      e.preventDefault();
      act(form, kind);
    });
  }

  return { toast, redraw, post, act };
}

// ── what a screening vote is called, once it has landed ──
//
// Named from the *record's* state and not from the button pressed, because
// those two come apart exactly where it matters: below the required number of
// reviewers the record still stands on the model's decision, and two reviewers
// who differ turn it into a conflict nobody asked for by name. Screening 1 and
// screening 2 resolve by the same rules, so they say the same things.
inplace.EMOJI = { include: '✅', exclude: '❌', maybe: '🤔', conflict: '⚠️', pending: '❓' };

inplace.voteMessage = function (vote, data, opening) {
  const lead = opening || 'Vote recorded';
  if (vote === 'clear') return 'Vote retracted';
  const settled = data && data.decision;
  const by = data && data.by;
  const msg = `${lead}: ${vote}`;
  const reads = (d) => `${inplace.EMOJI[d] || ''} ${d}`;
  if (!settled || settled === vote) return msg;
  if (settled === 'conflict') return `${msg} — the reviewers disagree, ⚠️ conflict`;
  // Nobody has pre-screened this one and the quorum is not in: the record is
  // genuinely still pending, which on its own would read as if the vote had not
  // registered at all.
  if (settled === 'pending') return `${msg} — waiting for the other reviewers`;
  if (by === 'model') return `${msg} — ${reads(settled)} stands until enough reviewers vote`;
  if (by === 'adjudicator') return `${msg} — the adjudicator's ${reads(settled)} stands`;
  return `${msg} — record now ${reads(settled)}`;
};
