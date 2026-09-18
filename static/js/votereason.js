// Alt-click a vote button to say why.
//
// Screening 1 has three buttons and no reason field, and that is deliberate:
// the buttons are made to be pressed at a glance, and a text box on every row
// of a five-hundred-row table is a five-hundred-row invitation to slow down.
// But a reason is sometimes the whole point — a conflict, a boundary case, a
// criterion applied against the obvious reading — and until now the only way
// to write one at this stage was through the MCP surface. On one real review
// that shows up as 708 reasons written from a chat and 2 from the app.
//
// So: plain click votes as it always did and sends no reason field at all,
// which the route reads as "leave whatever is there alone". Alt-click opens a
// box instead of voting, and the vote is cast when the box is saved.
//
// The shortcut is invisible by nature, so the page says it in the help text
// above the table. That sentence is the feature as much as this file is.
function wireVoteReasons(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;

  function boxFor(row) {
    return row && row.querySelector('.vote-reason');
  }

  function open(row, decision) {
    const box = boxFor(row);
    if (!box) return;
    box.hidden = false;
    box.dataset.decision = decision;
    const ta = box.querySelector('textarea');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  function cast(row) {
    const box = boxFor(row);
    const decision = box.dataset.decision;
    const form = row.querySelector(`form[action$="/screen1/vote"] input[value="${decision}"]`)?.form;
    if (!form) return;
    // Added here and only here: a form that never carries these fields is a
    // vote that cannot touch the reason attached to it. Two fields and not
    // one, because an empty `reason` reaches the route as None, exactly like
    // an absent one — so "clear it" needs a word of its own.
    const put = (name, value) => {
      let el = form.querySelector(`input[name="${name}"]`);
      if (!el) {
        el = document.createElement('input');
        el.type = 'hidden';
        el.name = name;
        form.append(el);
      }
      el.value = value;
    };
    put('reason', box.querySelector('textarea').value);
    put('set_reason', '1');
    box.hidden = true;
    form.requestSubmit();
  }

  root.addEventListener('click', (e) => {
    const btn = e.target.closest('form[action$="/screen1/vote"] button[type="submit"]');
    if (!btn || !e.altKey) return;
    const decision = btn.form.querySelector('input[name="decision"]').value;
    if (decision === 'clear') return;       // retracting takes no argument
    e.preventDefault();
    open(btn.closest('tr'), decision);
  });

  root.addEventListener('keydown', (e) => {
    const ta = e.target.closest('.vote-reason textarea');
    if (!ta) return;
    if (e.key === 'Escape') { e.preventDefault(); ta.closest('.vote-reason').hidden = true; return; }
    if (e.key !== 'Enter' || e.shiftKey) return;   // Shift+Enter breaks the line
    e.preventDefault();
    cast(ta.closest('tr'));
  });

  root.addEventListener('click', (e) => {
    if (e.target.matches('.vote-reason .vote-reason-save')) cast(e.target.closest('tr'));
    if (e.target.matches('.vote-reason .vote-reason-cancel')) {
      e.target.closest('.vote-reason').hidden = true;
    }
  });
}
