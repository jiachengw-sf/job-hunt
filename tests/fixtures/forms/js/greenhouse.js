// Synthetic stand-in for Greenhouse's job-board application form (react-select comboboxes,
// location autocomplete, phone country, S3 resume upload, JS-driven submit). Test fixture only.
(function () {
  const LOCATIONS = JSON.parse(document.getElementById('fx-locations').textContent);
  const SCHOOLS = JSON.parse(document.getElementById('fx-schools').textContent);

  function menuFor(container) {
    let m = container.querySelector('.select__menu');
    if (!m) {
      m = document.createElement('div');
      m.className = 'select__menu';
      m.setAttribute('role', 'listbox');
      container.querySelector('.select-shell').appendChild(m);
    }
    return m;
  }
  function close(container) {
    const m = container.querySelector('.select__menu');
    if (m) m.remove();
    const input = container.querySelector('input[role=combobox]');
    input.setAttribute('aria-expanded', 'false');
    input.value = '';
  }
  function options(container, text) {
    const t = text.trim().toLowerCase();
    if (container.dataset.kind === 'location' || container.dataset.kind === 'school') {
      if (t.length < 2) return [];  // both load by search as you type
      const all = container.dataset.kind === 'school' ? SCHOOLS : LOCATIONS;
      return all.filter(o => o.toLowerCase().includes(t));
    }
    const all = JSON.parse(container.dataset.options);
    return all.filter(o => o.toLowerCase().includes(t));
  }
  function render(container) {
    const input = container.querySelector('input[role=combobox]');
    const id = input.id;
    const m = menuFor(container);
    m.innerHTML = '';
    input.setAttribute('aria-expanded', 'true');
    options(container, input.value).forEach((o, i) => {
      const d = document.createElement('div');
      d.className = 'select__option';
      d.setAttribute('role', 'option');
      d.id = 'react-select-' + id + '-option-' + i;
      d.textContent = o;
      d.addEventListener('click', () => choose(container, o));
      m.appendChild(d);
    });
  }
  function choose(container, text) {
    const vc = container.querySelector('.select__value-container');
    if (container.dataset.multi === '1') {
      const mv = document.createElement('div');
      mv.className = 'select__multi-value';
      mv.innerHTML = '<div class="select__multi-value__label"></div>';
      mv.firstChild.textContent = text;
      vc.insertBefore(mv, vc.querySelector('.select__input-container'));
    } else {
      let sv = vc.querySelector('.select__single-value');
      if (!sv) {
        sv = document.createElement('div');
        sv.className = 'select__single-value';
        vc.insertBefore(sv, vc.querySelector('.select__input-container'));
      }
      sv.textContent = text;
      if (container.dataset.kind === 'phone-country') {
        const name = text.replace(/\s*\+\d+$/, '');
        document.querySelector('.iti__selected-country').setAttribute('title', name);
      }
    }
    close(container);
    if (container.querySelector('input[role=combobox]').id === 'hispanic_ethnicity') {
      revealRace(container, text === 'No');
    }
  }
  // Race exists only once Hispanic/Latino is answered No, as on the live page.
  function revealRace(hisp, show) {
    const race = document.getElementById('race');
    if (race && !show) race.closest('.select__container').remove();
    if (!race && show) {
      const tpl = document.getElementById('fx-race');
      hisp.insertAdjacentElement('afterend', tpl.content.firstElementChild.cloneNode(true));
      bind(hisp.nextElementSibling);
    }
  }
  function bind(c) {
    const input = c.querySelector('input[role=combobox]');
    input.addEventListener('click', () => render(c));
    input.addEventListener('focus', () => render(c));
    input.addEventListener('input', () => {
      if (c.dataset.kind === 'location' || c.dataset.kind === 'school') {
        setTimeout(() => render(c), 150);
      } else render(c);
    });
    input.addEventListener('keydown', e => { if (e.key === 'Escape') close(c); });
  }
  document.querySelectorAll('.select__container').forEach(bind);

  // Education: "Add another" appends entry N (ids end in --N), "Remove education" drops it.
  const addEdu = document.querySelector('.add-another-button');
  if (addEdu) {
    addEdu.addEventListener('click', () => {
      const n = document.querySelectorAll('.education--form').length;
      const tpl = document.getElementById('fx-education').innerHTML
        .replace(/--1(?=["-])/g, '--' + n);
      addEdu.insertAdjacentHTML('beforebegin', tpl);
      const entry = addEdu.previousElementSibling;
      entry.dataset.entry = String(n);
      entry.querySelectorAll('.select__container').forEach(bind);
      entry.querySelector('[aria-label="Remove education"]').addEventListener('click',
        () => entry.remove());
    });
  }
  document.addEventListener('mousedown', e => {
    document.querySelectorAll('.select__container').forEach(c => {
      if (!c.contains(e.target)) close(c);
    });
  });

  document.querySelectorAll('.file-upload input[type=file]').forEach(inp => {
    inp.addEventListener('change', async () => {
      const f = inp.files[0];
      const body = new FormData();
      body.append('file', f);
      const wrap = inp.closest('.file-upload').querySelector('.file-upload__wrapper');
      try {
        const r = await fetch(document.body.dataset.uploadUrl, {method: 'POST', body});
        if (!r.ok) throw new Error('upload failed');
        wrap.innerHTML = '<div class="file-upload__filename"></div>' +
          '<button type="button" class="btn btn--pill">Remove</button>';
        wrap.firstChild.textContent = f.name;
      } catch (e) {
        wrap.insertAdjacentHTML('beforeend', '<p class="error">Upload failed</p>');
      }
    });
  });

  document.getElementById('application-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await fetch(location.pathname, {method: 'POST', body: new FormData(e.target)});
      if (!r.ok) throw new Error('rejected');
      location.href = location.pathname + '?application_id=4242001';
    } catch (err) {
      document.getElementById('fx-status').textContent = 'There was a problem submitting.';
    }
  });
})();
