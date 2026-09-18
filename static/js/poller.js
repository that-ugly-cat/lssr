// Poll a background job's status endpoint and report it next to its form.
//
// Forms post normally and the page redirects, so a run can finish before the new
// page has even loaded: always report a finished job's message, and only reload
// when this page actually watched the run go from running → done (which also
// keeps a completed job from reloading the page forever).
//
// Elements are looked up by id every time they are used, never held from the
// start. Pages that redraw in place (inplace.js) replace the contents of the
// very form these ids live in, and a poller holding the old button would spend
// the rest of the session disabling an element no longer in the page.
//
// Job shape: {status, message, total, done|downloaded}. Any status other than
// idle/done/error counts as in progress (pubmed uses searching/downloading).
//
//   poller(formId, statusId, btnId, url[, progId, fillId])
function poller(formId, statusId, btnId, url, progId, fillId) {
  const form = document.getElementById(formId);
  if (!form) return;
  const el = (id) => (id ? document.getElementById(id) : null);
  let sawRunning = false;
  let anchorT = 0, anchorDone = 0;   // for the time estimate

  const progress = (j) => (j.done !== undefined ? j.done : j.downloaded);

  function say(cls, text) {
    const box = el(statusId);
    if (!box) return;
    box.style.display = 'block';
    box.className = cls;
    box.textContent = text;
  }

  function disable(state) {
    const btn = el(btnId);
    if (btn) btn.disabled = state;
  }

  function setBar(done, total) {
    const prog = el(progId), fill = el(fillId);
    if (!prog || !fill) return;
    prog.style.display = 'flex';
    fill.style.width = (total ? Math.round((done / total) * 100) : 0) + '%';
  }

  // Rolling ETA from the rate we actually observe, so it needs no server clock
  // and self-corrects. Anchored the first time we see progress.
  function eta(done, total) {
    if (!total || !done || done >= total) return '';
    const now = Date.now();
    if (!anchorT) { anchorT = now; anchorDone = done; return ''; }
    const dd = done - anchorDone, dt = (now - anchorT) / 1000;
    if (dd <= 0 || dt < 1) return '';
    const secs = Math.round((total - done) * (dt / dd));
    const m = Math.floor(secs / 60), s = secs % 60;
    return ' · ~' + (m ? `${m}m ${s}s` : `${s}s`) + ' left';
  }

  async function poll() {
    let j;
    try {
      j = await (await fetch(url)).json();
    } catch (e) {
      return;
    }
    if (!j || j.status === 'idle') return;

    if (j.status === 'done') {
      say('flash flash-ok', j.message || 'Done.');
      if (sawRunning) {
        setBar(1, 1);
        setTimeout(() => location.reload(), 1800);
      }
    } else if (j.status === 'error') {
      say('flash flash-error', 'Error: ' + (j.message || 'unknown'));
      disable(false);
    } else {
      sawRunning = true;
      disable(true);
      const done = progress(j);
      say('flash', (j.message || j.status) + (j.total ? ` (${done}/${j.total})${eta(done, j.total)}` : ''));
      setBar(done, j.total);
      setTimeout(poll, 2000);
    }
  }

  form.addEventListener('submit', () => { sawRunning = true; setTimeout(poll, 800); });
  poll();
}

// Is a job being reported right now? An in-place redraw asks before copying a
// button's `disabled` state over: mid-run that state was set by the poller, and
// a fresh render from the server knows nothing about it.
function pollerBusy(statusId) {
  const box = document.getElementById(statusId);
  return !!box && box.style.display !== 'none';
}
