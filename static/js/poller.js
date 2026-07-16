// Poll a background job's status endpoint and report it next to its form.
//
// Forms post normally and the page redirects, so a run can finish before the new
// page has even loaded: always report a finished job's message, and only reload
// when this page actually watched the run go from running → done (which also
// keeps a completed job from reloading the page forever).
//
// Job shape: {status, message, total, done|downloaded}. Any status other than
// idle/done/error counts as in progress (pubmed uses searching/downloading).
//
//   poller(formId, statusId, btnId, url[, progId, fillId])
function poller(formId, statusId, btnId, url, progId, fillId) {
  const form = document.getElementById(formId);
  if (!form) return;
  const box = document.getElementById(statusId);
  const btn = btnId ? document.getElementById(btnId) : null;
  const prog = progId ? document.getElementById(progId) : null;
  const fill = fillId ? document.getElementById(fillId) : null;
  let sawRunning = false;
  let anchorT = 0, anchorDone = 0;   // for the time estimate

  const progress = (j) => (j.done !== undefined ? j.done : j.downloaded);

  function setBar(done, total) {
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
      box.style.display = 'block';
      box.className = 'flash flash-ok';
      box.textContent = j.message || 'Done.';
      if (sawRunning) {
        setBar(1, 1);
        setTimeout(() => location.reload(), 1800);
      }
    } else if (j.status === 'error') {
      box.style.display = 'block';
      box.className = 'flash flash-error';
      box.textContent = 'Error: ' + (j.message || 'unknown');
      if (btn) btn.disabled = false;
    } else {
      sawRunning = true;
      if (btn) btn.disabled = true;
      box.style.display = 'block';
      box.className = 'flash';
      const done = progress(j);
      box.textContent = (j.message || j.status) + (j.total ? ` (${done}/${j.total})${eta(done, j.total)}` : '');
      setBar(done, j.total);
      setTimeout(poll, 2000);
    }
  }

  form.addEventListener('submit', () => { sawRunning = true; setTimeout(poll, 800); });
  poll();
}
