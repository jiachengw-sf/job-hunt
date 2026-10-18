// Synthetic stand-in for Lever's /apply page behavior (location search, resume parse that
// autofills fields, form POST on submit). Test fixture only.
(function () {
  const form = document.getElementById('application-form');
  const loc = document.getElementById('location-input');
  const box = document.querySelector('.dropdown-container');
  const results = box.querySelector('.dropdown-results');
  let timer = null;
  loc.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const text = loc.value.trim();
      results.innerHTML = '';
      if (text.length < 2) { box.style.display = 'none'; return; }
      const r = await fetch('/searchLocations?text=' + encodeURIComponent(text));
      const all = await r.json();
      all.filter(o => o.name.toLowerCase().includes(text.toLowerCase())).forEach((o, i) => {
        const d = document.createElement('div');
        d.className = 'dropdown-location';
        d.id = 'location-' + i;
        d.textContent = o.name;
        d.addEventListener('click', () => {
          loc.value = o.name;
          document.getElementById('selected-location').value = JSON.stringify(o);
          box.style.display = 'none';
        });
        results.appendChild(d);
      });
      box.style.display = 'flex';
    }, 120);
  });

  const resume = document.getElementById('resume-upload-input');
  resume.addEventListener('change', async () => {
    const f = resume.files[0];
    document.querySelector('.filename').textContent = f.name;
    const body = new FormData();
    body.append('resume', f);
    try {
      const r = await fetch('/parseResume', {method: 'POST', body});
      const parsed = await r.json();
      // Lever fills these from the parsed resume, overwriting whatever was typed.
      for (const k of ['name', 'email', 'phone', 'org']) {
        const el = form.querySelector('[name="' + k + '"]');
        if (el && parsed[k]) el.value = parsed[k];
      }
    } catch (e) { /* parse is best effort */ }
  });

  document.getElementById('btn-submit').addEventListener('click', () => form.submit());
})();
