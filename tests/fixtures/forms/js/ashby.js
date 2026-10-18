// Synthetic stand-in for Ashby's hosted application form (no <form>; yes/no toggles, radio and
// checkbox groups, location combobox, upload -> parse -> autofill, GraphQL submit). Fixture only.
(function () {
  const GQL = '/api/non-user-graphql?op=';
  const post = (op, body) => fetch(GQL + op, {method: 'POST', body: JSON.stringify(body || {}),
    headers: {'content-type': 'application/json'}});

  // Ashby saves the draft server-side as fields change.
  document.querySelectorAll('[data-field-path] input, [data-field-path] textarea').forEach(el => {
    if (el.type === 'file') return;
    el.addEventListener('change', () => post('ApiSetFormValue', {path: el.name || el.id}));
  });

  document.querySelectorAll('button[data-option]').forEach(b => {
    b.addEventListener('click', () => {
      b.parentElement.querySelectorAll('button[data-option]').forEach(o =>
        o.setAttribute('aria-pressed', o === b ? 'true' : 'false'));
      b.parentElement.querySelector('input[type=checkbox]').checked = b.dataset.option === 'yes';
    });
  });

  document.querySelectorAll('.fx-location').forEach(entry => {
    const input = entry.querySelector('input[role=combobox]');
    const list = entry.querySelector('[role=listbox]');
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        list.innerHTML = '';
        const text = input.value.trim().toLowerCase();
        if (text.length < 2) return;
        const r = await post('ApiAutocompleteGeoLocation', {text});
        const all = (await r.json()).data.result;
        all.filter(o => o.toLowerCase().includes(text)).forEach(o => {
          const d = document.createElement('div');
          d.setAttribute('role', 'option');
          d.textContent = o;
          d.addEventListener('click', () => { input.value = o; list.innerHTML = ''; });
          list.appendChild(d);
        });
      }, 120);
    });
  });

  const resume = document.getElementById('_systemfield_resume');
  if (resume) resume.addEventListener('change', async () => {
    const f = resume.files[0];
    const entry = resume.closest('[data-field-path]');
    entry.querySelector('.fx-file-name').textContent = 'Uploading...';
    await post('ApiCreateFileUploadHandle', {filename: f.name});
    const body = new FormData();
    body.append('file', f);
    await fetch(document.body.dataset.uploadUrl, {method: 'POST', body});
    const linked = await post('ApiSetFormValueToFile', {path: '_systemfield_resume'});
    if (!linked.ok) throw new Error('not attached');
    entry.querySelector('.fx-file-name').textContent = f.name;
    const r = await post('ApiAutofillApplicationFormWithUploadedResume', {});
    const parsed = (await r.json()).data.autofill;
    // Ashby's parse lands a moment later and overwrites the fields it knows.
    setTimeout(() => {
      const set = (sel, v) => { const el = document.querySelector(sel); if (el && v) el.value = v; };
      set('#_systemfield_name', parsed.name);
      set('#_systemfield_email', parsed.email);
      set('input[type=tel]', parsed.phone);
    }, 300);
  });

  document.querySelector('.ashby-application-form-submit-button').addEventListener('click',
    async () => {
      try {
        const r = await post('ApiSubmitSingleApplicationFormAction', {});
        if (!r.ok) throw new Error('rejected');
        document.querySelector('.ashby-application-form-container').outerHTML =
          '<div class="ashby-application-form-success-container"><h2>Thank you for applying' +
          '</h2><p>Your application was successfully submitted.</p></div>';
      } catch (e) {
        document.getElementById('fx-status').textContent = 'Something went wrong.';
      }
    });
})();
