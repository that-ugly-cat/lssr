// The earmark: the dot, and the note beside it.
//
// Lifted out of the two templates that had grown identical copies of it, which
// is where a second line's worth of keyboard handling would have been written
// twice and then diverged once.
//
// The dot paints before the round trip: a marker meant to be used carelessly
// cannot make you wait to see that it worked. The redraw that follows keeps
// the ⚑ count on the filter tabs honest, and the server's answer is what the
// row ends up drawn from.
//
//   wireEarmarks(rootId)   — rootId is a container that survives the redraw
// A note is one line most of the time and two when it needs to be, so the
// field is a textarea that starts one line high and follows what is typed. It
// is not a box you are invited to fill: growing is what it does when you ask,
// not an offer made before you have written anything.
function growEarmarkNote(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 96) + 'px';
}

// Every visible note field, sized to its content. Called after an in-place
// redraw: the fields that come back are new elements, and a two-line note
// would otherwise return one line high with its second line out of sight.
function sizeEarmarkNotes() {
  document.querySelectorAll('.earmark-note:not([hidden]) [name="note"]')
          .forEach(growEarmarkNote);
}

function wireEarmarks(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const grow = growEarmarkNote;

  root.addEventListener('click', (e) => {
    const btn = e.target.closest('.earmark-form .earmark');
    if (!btn) return;
    const on = btn.classList.toggle('on');
    // The note field lives beside the dot and is shown with it. Clearing the
    // dot deletes the row server-side, so the text goes too — leaving it in a
    // hidden field would put it back on screen next time, unsaved.
    const scope = btn.closest('td, .review-head');
    const note = scope && scope.querySelector('.earmark-note');
    if (note) {
      note.hidden = !on;
      const box = note.querySelector('[name="note"]');
      if (!on) box.value = '';
      else { grow(box); box.focus(); }
    }
  });

  // Saved on leaving the field — `change` fires on blur only when the value
  // actually differs, so clicking away without typing costs nothing.
  root.addEventListener('change', (e) => {
    if (e.target.matches('.earmark-note [name="note"]')) e.target.form.requestSubmit();
  });

  root.addEventListener('input', (e) => {
    if (e.target.matches('.earmark-note [name="note"]')) grow(e.target);
  });

  // Enter saves, Shift+Enter breaks the line. That way round because saving is
  // what you do every time and a second line is what you do sometimes — and
  // because a textarea whose Enter inserted a newline would silently drop the
  // habit built by the version before it, where Enter submitted.
  root.addEventListener('keydown', (e) => {
    if (!e.target.matches('.earmark-note [name="note"]')) return;
    if (e.key !== 'Enter' || e.shiftKey) return;
    e.preventDefault();
    e.target.form.requestSubmit();
  });

  sizeEarmarkNotes();
}
