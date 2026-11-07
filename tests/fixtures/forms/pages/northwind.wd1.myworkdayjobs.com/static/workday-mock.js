// A small stand-in for a Workday application, for jobhunt's offline tests. Every company, name
// and option here is synthetic. It copies the behaviours the pre-fill adapter has to cope with:
//  - sign-in first (the human's job), then steps in a progress bar;
//  - questions in [data-automation-id="formField-<name>"] boxes, labels ending in "*";
//  - dropdowns: button[aria-haspopup=listbox] whose options arrive late;
//  - search prompts: categories ("Campus" -> "Campus: Career Fair"), a list that opens only via
//    "All", and a school search that runs only on Enter in [data-automation-id=searchBox];
//  - MM / YYYY dates whose inputs sit under an overlay, so clicks never reach them;
//  - Add / Add Another sections with ids like workExperience-11--jobTitle;
//  - autosave POSTs per field, a questionnaire definition POST the questions need, PUT step
//    saves, a sticky footer over the bottom of the page, and Review with a Submit endpoint.
(() => {
  const T = "northwind", APP = "a1b2c3d4";
  const CALYPSO = `/wday/calypso/cxs/jobapplication/${T}/jobapplication/${APP}`;
  const CXS = `/wday/cxs/${T}/jobapplication/${APP}`;
  const STEPS = ["My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Review"];
  const root = document.getElementById("root"), footer = document.getElementById("footer");
  const S = { stage: -1, v: { "phoneNumber--countryPhoneCode": ["United States of America (+1)"] },
              work: [11], edu: [], lang: [], web: [], resume: null, questions: false, submitted: false };
  window.__wd = S;

  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const save = section => fetch(`${CALYPSO}/${section}`, { method: "POST", body: "{}" }).catch(() => {});
  let popup = null;
  const closePopup = () => { if (popup) { popup.remove(); popup = null; } };
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePopup(); });
  document.addEventListener("click", e => {
    if (!e.target.isConnected) return;  // the clicked option replaced its own list
    if (popup && !popup.contains(e.target) && !e.target.closest("[data-owner]")) closePopup();
  });

  // ---------- widgets: each returns HTML; wire() attaches behaviour after render ----------
  const req = r => (r ? "*" : "");
  const box = (name, inner) => `<div data-automation-id="formField-${name}">${inner}</div>`;
  const text = (name, id, label, r, area) => box(name, `<label for="${id}">${esc(label)}${req(r)}</label>` +
    (area ? `<textarea id="${id}" data-save="${name}">${esc(S.v[id] || "")}</textarea>`
          : `<input type="text" id="${id}" data-save="${name}" value="${esc(S.v[id] || "")}">`));
  const listbox = (name, id, label, r, opts, dflt) => box(name, `<label for="${id}">${esc(label)}${req(r)}</label>` +
    `<button type="button" aria-haspopup="listbox" id="${id}" data-owner="1" data-opts='${esc(JSON.stringify(opts))}'>` +
    `${esc(S.v[id] || dflt || "Select One")}</button>`);
  const radio = (name, label, r, opts) => box(name, `<fieldset><legend>${esc(label)}${req(r)}</legend>` +
    opts.map((o, i) => `<input type="radio" name="${name}" id="${name}-r${i}" value="${esc(o)}"` +
      `${S.v[name] === o ? " checked" : ""}><label for="${name}-r${i}">${esc(o)}</label>`).join("") + `</fieldset>`);
  const check = (name, id, label, r) => box(name, `<label for="${id}">${esc(label)}${req(r)}</label>` +
    `<input type="checkbox" id="${id}"${S.v[id] ? " checked" : ""}>`);
  const pills = id => (S.v[id] || []).map(p => `<div data-automation-id="selectedItem" role="option">${esc(p)}</div>`).join("");
  const prompt = (name, id, label, r) => box(name, `<label for="${id}">${esc(label)}${req(r)}</label>` +
    `<div data-automation-id="selectedItemList">${pills(id)}</div>` +
    `<input type="text" data-automation-id="searchBox" id="${id}" data-owner="1" data-prompt="${name}">` +
    `<span data-automation-id="promptIcon">≡</span>`);
  const date = (name, prefix, which, label, r) => box(name, `<label>${esc(label)}${req(r)}</label><span class="datewrap">` +
    `<input role="spinbutton" aria-label="Month" id="${prefix}--${which}-dateSectionMonth-input" maxlength="2" value="${esc(S.v[`${prefix}--${which}-m`] || "")}">/` +
    `<input role="spinbutton" aria-label="Year" id="${prefix}--${which}-dateSectionYear-input" maxlength="4" value="${esc(S.v[`${prefix}--${which}-y`] || "")}">` +
    `<div class="display" data-automation-id="dateInputWrapper"></div></span>`);
  const add = (title, kind, label) => `<section><h3>${title}</h3>${(S[kind] || []).map(n => entry(kind, n)).join("")}` +
    `<button type="button" data-automation-id="add-button" data-add="${kind}">${label}</button></section>`;

  // prompt option sources
  const SOURCE = { Campus: ["Campus: Career Board", "Campus: Career Fair", "Campus: Info Session"],
                   "Job Board": ["Job Board: Handshake", "Job Board: Other"], "Company Website": null, Referral: null };
  const SCHOOLS = ["Northfield State University", "Northfield Technical College", "Southridge University", "Lakeside College"];
  const FIELDS = [];
  for (let i = 0; i < 160; i++) FIELDS.push(`Field ${String(i).padStart(3, "0")}`);
  FIELDS.splice(70, 0, "Computer Science", "Computer Engineering");
  const PHONE = ["United States of America (+1)", "Canada (+1)", "India (+91)"];

  function entry(kind, n) {
    const p = `${kind}-${n}`;
    if (kind === "work") {
      const q = `workExperience-${n}`;
      const cur = !!S.v[`${q}--currentlyWorkHere`];
      return `<div class="entry"><h4>Work Experience</h4>` +
        text("jobTitle", `${q}--jobTitle`, "Job Title", true) +
        text("companyName", `${q}--companyName`, "Company", true) +
        text("location", `${q}--location`, "Location", false) +
        check("currentlyWorkHere", `${q}--currentlyWorkHere`, "I currently work here", false) +
        date("startDate", q, "startDate", "From", true) + (cur ? "" : date("endDate", q, "endDate", "To", true)) +
        text("roleDescription", `${q}--roleDescription`, "Role Description", false, true) + `</div>`;
    }
    if (kind === "edu") {
      const q = `education-${n}`;
      return `<div class="entry"><h4>Education</h4>` +
        prompt("school", `${q}--school`, "School or University", true) +
        listbox("degree", `${q}--degree`, "Degree", true, ["High School Diploma", "Associates", "Bachelors", "Masters", "Doctorate"]) +
        prompt("fieldOfStudy", `${q}--fieldOfStudy`, "Field of Study", false) +
        text("gradeAverage", `${q}--gradeAverage`, "Overall Result (GPA)", false) + `</div>`;
    }
    if (kind === "lang") {
      const q = `language-${n}`;
      return `<div class="entry">` + listbox("language", `${q}--language`, "Language", true, ["English", "Spanish", "French"]) +
        listbox("9f00aa", `${q}--9f00aa`, "Reading", true, ["1 - Low", "2 - Medium", "3 - High"]) + `</div>`;
    }
    if (kind === "web") return `<div class="entry">` + text("url", `webAddress-${n}--url`, "URL", true) + `</div>`;
    return p;
  }

  // ---------- steps ----------
  function stepHTML(i) {
    if (i === 0) return `<h2>My Information</h2>` +
      prompt("source", "source--source", "How Did You Hear About Us?", true) +
      radio("candidateIsPreviousWorker", "Have You Been Previously Employed By Northwind?", true, ["Yes", "No"]) +
      listbox("country", "country--country", "Country", true, ["Canada", "India", "United States of America"], "United States of America") +
      text("legalName--firstName", "name--legalName--firstName", "First Name", true) +
      text("legalName--lastName", "name--legalName--lastName", "Last Name", true) +
      text("addressLine1", "address--addressLine1", "Address Line 1", false) +
      text("city", "address--city", "City", false) +
      listbox("phoneType", "phoneNumber--phoneType", "Phone Device Type", true, ["Home", "Mobile", "Work"], "Mobile") +
      prompt("countryPhoneCode", "phoneNumber--countryPhoneCode", "Country Phone Code", true) +
      text("phoneNumber", "phoneNumber--phoneNumber", "Phone Number", true);
    if (i === 1) return `<h2>My Experience</h2>` + add("Work Experience", "work", "Add Another") +
      add("Education", "edu", "Add") + add("Languages", "lang", "Add") +
      `<section><h3>Resume/CV</h3>` + box("", `<label>Upload a file (5MB max)</label>` +
        `<input type="file" data-automation-id="file-upload-input-ref" accept=".pdf,.doc,.docx">` +
        (S.resume ? `<div data-automation-id="file-upload-successful">${esc(S.resume)} Successfully Uploaded!</div>` : "")) +
      `</section>` + add("Websites", "web", "Add");
    if (i === 2) {
      if (!S.questions) return `<h2>Application Questions</h2><p id="loading">Loading…</p>`;
      return `<h2>Application Questions</h2>` +
        listbox("q0a1", "primaryQuestionnaire--q0a1", "Are you legally authorized to work in the United States?", true, ["Yes", "No"]) +
        listbox("q0a2", "primaryQuestionnaire--q0a2", "Will you now or in the future require sponsorship for employment visa status?", true, ["Yes", "No"]) +
        listbox("q0a3", "primaryQuestionnaire--q0a3", "Are you at least 18 years old?", true, ["Yes", "No"]) +
        check("q0a4", "primaryQuestionnaire--q0a4", "I certify that the information in this application is true and complete.", true);
    }
    if (i === 3) return `<h2>Voluntary Disclosures</h2>` +
      listbox("gender", "personalInfoUS--gender", "Please select your gender:", false, ["Female", "Male", "I do not wish to answer"]) +
      listbox("veteranStatus", "personalInfoUS--veteranStatus", "Please select your veteran status:", false,
        ["I am a protected veteran", "I am not a protected veteran", "I do not wish to self-identify"]) +
      check("acceptTermsAndAgreements", "termsAndConditions--acceptTermsAndAgreements", "Yes, I have read and consent to the terms and conditions.", true);
    return `<h2>Review</h2><p>Review your application, then submit it.</p><div id="summary"></div>`;
  }

  function render() {
    closePopup();
    if (S.submitted) {
      root.innerHTML = `<h1>Application Submitted</h1><p>Thank you for applying to Northwind.</p>`;
      footer.innerHTML = "";
      return;
    }
    if (S.stage < 0) {
      root.innerHTML = `<div data-automation-id="applyFlowPage"><div data-automation-id="progressBar">` +
        `<div data-automation-id="progressBarActiveStep">current step 1 of 6 Create Account/Sign In</div></div>` +
        `<div data-automation-id="signInContent"><button type="button" id="human-sign-in">Sign in with email</button></div></div>`;
      footer.innerHTML = "";
      document.getElementById("human-sign-in").onclick = () =>
        fetch(`/wday/cxs/${T}/careers/login`, { method: "POST", body: "{}" }).then(() => { S.stage = 0; render(); });
      return;
    }
    const i = S.stage;
    root.innerHTML = `<div data-automation-id="applyFlowPage"><div data-automation-id="progressBar">` +
      STEPS.map((s, k) => k === i ? `<div data-automation-id="progressBarActiveStep">current step ${k + 1} of ${STEPS.length}\n${s}</div>`
                                  : `<div data-automation-id="progressBarInactiveStep">${s}</div>`).join("") +
      `</div><div id="errors"></div>${stepHTML(i)}</div>`;
    const label = i === STEPS.length - 1 ? "Submit" : "Save and Continue";
    footer.innerHTML = (i > 0 ? `<button type="button" data-automation-id="pageFooterBackButton">Back</button>` : "") +
      `<button type="button" data-automation-id="pageFooterNextButton">${label}</button>`;
    wire();
    if (i === 2 && !S.questions) {
      fetch(`/wday/calypso/cxs/common/${T}/questionnaire/q0/definition?type=primary`, { method: "POST", body: "{}" })
        .then(r => { if (!r.ok) throw new Error("x"); S.questions = true; render(); })
        .catch(() => { S.stage = 0; render(); });  // like Workday: a failed load restarts the flow
    }
  }

  function wire() {
    root.querySelectorAll("[data-save]").forEach(el => {
      el.addEventListener("input", () => { S.v[el.id] = el.value; });
      el.addEventListener("blur", () => save(el.dataset.save));
    });
    root.querySelectorAll("input[type=radio]").forEach(el => el.addEventListener("change", () => {
      S.v[el.name] = el.value; save(el.name);
    }));
    root.querySelectorAll("input[type=checkbox]").forEach(el => el.addEventListener("change", () => {
      S.v[el.id] = el.checked; save(el.id); if (el.id.endsWith("currentlyWorkHere")) render();
    }));
    root.querySelectorAll("button[aria-haspopup=listbox]").forEach(btn => btn.addEventListener("click", () => {
      closePopup();
      const pop = document.createElement("ul");
      pop.className = "popup"; pop.setAttribute("role", "listbox");
      pop.style.top = (btn.getBoundingClientRect().bottom + window.scrollY) + "px";
      pop.style.left = (btn.getBoundingClientRect().left + window.scrollX) + "px";
      pop.innerHTML = `<li role="option">Select One</li>`;
      document.body.appendChild(pop); popup = pop;
      setTimeout(() => {  // options arrive late, as on Workday
        if (popup !== pop) return;
        pop.innerHTML = [`Select One`, ...JSON.parse(btn.dataset.opts)].map(o => `<li role="option">${esc(o)}</li>`).join("");
        pop.querySelectorAll("li").forEach(li => li.addEventListener("click", () => {
          if (li.textContent === "Select One") return;
          S.v[btn.id] = li.textContent; btn.textContent = li.textContent; save(btn.id); closePopup();
        }));
      }, 350);
    }));
    root.querySelectorAll("input[data-prompt]").forEach(inp => {
      const name = inp.dataset.prompt;
      const open = items => {
        closePopup();
        const pop = document.createElement("div");
        pop.className = "popup"; pop.setAttribute("data-automation-id", "activeListContainer"); pop.setAttribute("role", "listbox");
        pop.style.top = (inp.getBoundingClientRect().bottom + window.scrollY) + "px";
        pop.style.left = (inp.getBoundingClientRect().left + window.scrollX) + "px";
        pop.innerHTML = (items.length ? items : ["No Items."]).map(o =>
          `<div role="option" data-automation-id="menuItem">${esc(o)}</div>`).join("");
        document.body.appendChild(pop); popup = pop;
        pop.querySelectorAll("[role=option]").forEach(el => el.addEventListener("click", () => pick(el.textContent)));
      };
      const choose = v => {
        const multi = false; S.v[inp.id] = multi ? [...(S.v[inp.id] || []), v] : [v];
        save(name); closePopup(); render();
      };
      const pick = t => {
        if (t === "No Items.") return;
        if (name === "source" && SOURCE[t]) return open(SOURCE[t]);
        if (name === "fieldOfStudy" && t === "All") return open(FIELDS);
        if (name === "fieldOfStudy" && t === "Partial List") return open(FIELDS.slice(0, 20));
        choose(t);
      };
      const top = () => name === "source" ? Object.keys(SOURCE)
        : name === "fieldOfStudy" ? ["Partial List", "All"]
        : name === "countryPhoneCode" ? PHONE : [];
      inp.addEventListener("click", () => open(top()));
      inp.addEventListener("input", () => {
        const q = inp.value.trim().toLowerCase();
        if (!q) return open(top());
        if (name === "school") return open([]);  // the school search runs only on Enter
        const all = name === "source" ? Object.entries(SOURCE).flatMap(([k, v]) => v || [k])
          : name === "fieldOfStudy" ? [] : name === "countryPhoneCode" ? PHONE : [];
        open(all.filter(x => x.toLowerCase().includes(q)));
      });
      inp.addEventListener("keydown", e => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const q = inp.value.trim().toLowerCase();
        if (name === "school") open(SCHOOLS.filter(x => x.toLowerCase().includes(q)));
      });
    });
    root.querySelectorAll(".datewrap input").forEach(el => {
      el.addEventListener("input", () => {
        el.value = el.value.replace(/\D/g, "").slice(0, el.maxLength);
        const key = el.id.replace(/-dateSection(Month|Year)-input$/, m => m.includes("Month") ? "-m" : "-y");
        S.v[key] = el.value;
      });
      el.addEventListener("blur", () => save("workexperiences"));
    });
    root.querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => {
      const kind = b.dataset.add, base = { work: 11, edu: 21, lang: 31, web: 41 }[kind];
      S[kind].push(base + S[kind].length + (kind === "work" ? 0 : 0)); render();
    }));
    const file = root.querySelector("[data-automation-id=file-upload-input-ref]");
    if (file) file.addEventListener("change", () => {
      const f = file.files[0]; if (!f) return;
      fetch(`${CALYPSO}/resumeattachments`, { method: "POST", body: "{}" }).then(() => { S.resume = f.name; render(); });
    });
    const next = footer.querySelector("[data-automation-id=pageFooterNextButton]");
    next.addEventListener("click", () => {
      if (S.stage === STEPS.length - 1) {
        fetch(`${CXS}/submit`, { method: "POST", body: "{}" }).then(() => { S.submitted = true; render(); });
        return;
      }
      const missing = [];
      root.querySelectorAll("[data-automation-id^=formField-]").forEach(b => {
        const lab = b.querySelector("label, legend");
        if (!lab || !/\*$/.test(lab.textContent.trim())) return;
        const radios = b.querySelectorAll("input[type=radio]"), cb = b.querySelector("input[type=checkbox]");
        const btn = b.querySelector("button[aria-haspopup]"), sel = b.querySelectorAll("[data-automation-id=selectedItem]");
        const inp = b.querySelector("input[type=text], textarea"), y = b.querySelector("[id$=dateSectionYear-input]");
        let ok = true;
        if (radios.length) ok = [...radios].some(r => r.checked);
        else if (b.querySelector("[data-prompt]")) ok = sel.length > 0;
        else if (btn) ok = btn.textContent.trim() !== "Select One";
        else if (cb) ok = cb.checked;
        else if (y) ok = !!y.value;
        else if (inp) ok = !!inp.value;
        if (!ok) missing.push(lab.textContent.replace(/\*$/, ""));
      });
      if (missing.length) {
        document.getElementById("errors").innerHTML = missing.map(m =>
          `<div data-automation-id="errorMessage">${esc(m)} is required</div>`).join("");
        return;
      }
      const section = ["myinformation", "myexperience", "questionnaire", "personalinfous"][S.stage];
      fetch(`${CXS}/${section}`, { method: "PUT", body: "{}" }).then(r => {
        if (!r.ok) return;
        S.stage += 1; render();
      }).catch(() => {});
    });
    const back = footer.querySelector("[data-automation-id=pageFooterBackButton]");
    if (back) back.addEventListener("click", () => { S.stage -= 1; render(); });
  }

  render();
})();
