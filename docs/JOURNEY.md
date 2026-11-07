# jobhunt: what it can do, and how it got here

This page has two parts. The first says what jobhunt does today and where it stops. The second
is the build history, milestone by milestone. [PROGRESS.md](../PROGRESS.md) has the full
engineering log behind each step. [AGENTS.md](../AGENTS.md) has the rules every change keeps.

## What jobhunt can do today

jobhunt is a local job-hunt agent. A Python CLI does the work with no AI inside. Eight skills
let your own AI tool (Claude Code, Codex CLI or another Agent Skills reader) drive it and do the
parts that need judgment. Everything stays in `~/.jobhunt/` on your computer.

### 1. Set you up from your resume

- Reads your resume as PDF (Chrome or Word exports, LaTeX, two-column), DOCX (plain, table
  layouts, contact details in the page header), Markdown or text. Spaced-out letters, small caps,
  ligatures and wrapped links are repaired before parsing.
- **Scanned or image-only PDFs** are read with local OCR (Tesseract) when the optional `ocr`
  extra is installed. Nothing is uploaded.
- Drafts your profile (education, experience with dates, projects, skills) without your contact
  details. You correct it and confirm it. Nothing is invented.
- **The setup form fills itself from your resume.** City and state, school email, GPA and scale,
  graduation date, class standing, spoken languages and past employers are pre-filled and
  marked "From your resume: check it, then Save to confirm". Sensitive answers (work
  authorization, sponsorship, EEO and similar) are never suggested; you type them yourself.

### 2. Find jobs

- Public, documented sources only: Simplify's internship and new-grad lists, plus the
  Greenhouse, Lever (US and EU) and Ashby boards of any company on your watchlist. Any single
  posting can be added with `jobhunt add <url>`.
- Merges duplicates across sources (the same job listed twice, or a slightly different
  company name). Notices closed postings. Flags legitimacy concerns.
- The first fetch per source is a silent baseline, so old postings don't flood you.
- Live check (2026-11-06): 7,029 postings from six company boards, 0 errors, no duplicates on
  re-fetch.

### 3. Decide what's worth applying to

- **Gates** drop jobs you can't or won't take (seniority, location, pay floor, term, work mode,
  sponsorship, blocklist) and always show the reason.
- **Ranking** in stages: keyword search (BM25, optional embeddings), requirement extraction
  against your profile, then a **qualification band** (strong, good, stretch, not a fit) kept
  separate from a **preference score**. Priority combines them with freshness, deadline,
  referral and watchlist. `jobhunt show <id>` explains every number.
- Your AI tool reads the requirements ("judgment tasks"). The CLI accepts a judgment only if it
  quotes the posting and your profile accurately.
- `jobhunt calibrate` compares the bands with your real interview outcomes and suggests config
  changes. It never changes anything by itself.

### 4. Know the questions before you open a form

- Reads Greenhouse, Lever and Ashby application forms ahead of time and sorts each job into
  Ready, Needs answers, Essay or Manual.
- **An answer bank** stores each answer once. A question you already answered in other words
  is recognized by wording similarity or by your AI tool's `jobhunt answers map`, both checked by
  code. It's filled from that answer and flagged for your review. EEO answers are never filled
  this way, and sensitive questions need a much closer match.
- Questions only you can answer are asked once, in one batch.
- `jobhunt coverage` shows how many required fields your answers already cover.

### 5. Pre-fill applications for you to review

- **Greenhouse, Lever and Ashby:** opens each Ready job in a browser, fills every field from
  your saved answers, uploads your resume, reads every value back, highlights anything it left
  for you, and stops.
- **Workday:** you sign in on the employer's page yourself; jobhunt never logs in. Then it
  goes step by step:
  - reads each step and fills it from your answers;
  - adds work experience, education, languages and websites from your profile, with dates;
  - handles Workday's search-and-pick lists (for example, Campus → Career Fair for "How did you
    hear about us");
  - clicks Save and Continue only when the step is clean.

  It stops at Review. With a fake persona it reached Review on two real employer portals.
- **It never submits.** A guard blocks every submit button and every submit request while it
  fills. You review and click Submit yourself.
- Anything it can't do gets a hand-off packet (`jobhunt packet <id>`) with every answer ready to
  copy.

### 6. Write with you, never for you

- Essay drafts get exactly one review pass, and every sentence must trace back to your profile
  or a cited source. Drafts can't add numbers or skills you don't have.
- Company research: at most 5 searches and 8 page fetches. Every fact is a URL plus a short
  quote the CLI finds on that page.
- Outreach drafts (referral asks, follow-ups) are yours to edit and send. There is no send code.

### 7. Track every application

- One record per application, created when the confirmation page appears, when you say "I
  applied to …", or when you mark it in the sheet.
- Each record keeps a screenshot, the job description as it was, the resume used, what you
  submitted, and where to check status (MyGreenhouse, the company's Workday Candidate Home, or
  email).
- Views: a local dashboard, `APPLICATIONS.md`, one folder per application, and a spreadsheet
  (XLSX or Google Sheets). Your edits to the spreadsheet always win.
- jobhunt only moves a status forward; you can set any status. Applications with no reply are
  flagged after 21 days, but their status never changes on its own.

### 8. Run on its own

- `jobhunt schedule install` runs discovery every 4 hours (launchd, cron or Task Scheduler).
  The scheduled run is code only, with no AI.
- Digests and alerts go to you only, by ntfy, Discord or email.

### Safety rules, enforced in code

- No submit or send command exists. No logins, account creation, CAPTCHA solving or scraping
  of LinkedIn or Indeed.
- Sensitive answers come only from you and are never shown to the AI.
- Postings and web pages are data, never instructions. A posting that tries to instruct an AI
  is flagged.
- CI scans every file and every commit for real-looking personal data.

### Where it stops (known gaps)

- **Workday has been verified only with fake data**, on two employer portals. A run with your
  real information hasn't happened yet. Language proficiency, phone type and subsidiary
  questions are left to you. Workday jobs are found through Simplify or `jobhunt add`; there is
  no direct Workday job feed.
- **Ranking:** recall favors title-only Simplify rows, and watchlist jobs are ordered mostly by
  freshness. A fix is planned (score the title plus part of the description, and rank watchlist
  jobs too).
- `jobhunt coverage` is slow (about 50–80 seconds), because it reads around 30 live forms.
- **Codex CLI is untested**, and real day-to-day use on Windows is untested (CI passes there).
- Lever and Ashby confirmation pages are best guesses, since tests never submit.
  `jobhunt mark <id> Applied` fixes a missed one.
- Not published on PyPI. Install from a clone.

## How it got here

| Date | Step | What it added |
|---|---|---|
| 2026-10-18 | Milestone 0: Skeleton | The CLI contract: one JSON envelope per command, exit codes, `--dry-run`, output cap, `jobhunt schema`, `doctor`, CI personal-data scan |
| 2026-10-18 | Milestone 1: Discovery | Simplify, Greenhouse, Lever and Ashby sources; manual URLs; dedupe; closures; legitimacy signals; offline fixtures |
| 2026-10-18 | Milestone 2: Fit engine + tracker | Gates, BM25 recall, requirement matching, bands and priority, `eval-fit`, XLSX / Google Sheets tracker with three-way sync |
| 2026-10-18 | Milestone 3: Onboarding + answer bank | Resume to profile, local setup form for sensitive answers, answer bank, form reader and question classifier, `coverage` |
| 2026-10-18 | Milestone 4: Pre-fill | `answers.json` validator, submit guard, Greenhouse / Lever / Ashby adapters, read-back, highlights, review driver |
| 2026-10-19 | Milestone 5: Skills | The skills, `jobhunt ingest` with code-truth checks, judgment / research / essay / outreach tasks, trigger evals (Claude Code) |
| 2026-10-19 | Milestone 6: Calibrate + share | `calibrate`, README and CONTRIBUTING, demo GIF. Published on GitHub; CI green on Linux, macOS and Windows. **v0.1.0** |
| 2026-10-21 | Application tracking | Application records, status portals, timelines, dashboard, `APPLICATIONS.md`, per-application folders, local-time display. **v0.2.0** |
| 2026-10-23 | Fixes from a real-user test | Eight issues from a full v0.2.0 run: Greenhouse education block, range options, judgment for postings without descriptions, several resumes, same question in different words, setup form, tracking, scheduling |
| 2026-11-06 | Code review fixes | Nine bugs, each reproduced by a failing test and then fixed |
| 2026-11-06 | Workday pre-fill | The Workday adapter and step-by-step flow after your own sign-in, with a mock Workday portal for tests |
| 2026-11-06 | Fewer repeat questions | Answers reused across different wordings, flagged for review; `answers map`; wordings learned on submit |
| 2026-11-06 | Resumes in any layout | 16 synthetic resume layouts (PDF, LaTeX, two-column, scanned, DOCX, Markdown) all parse correctly; OCR for scans; setup form pre-filled from the resume |
| 2026-11-06 | Combined system check | Every command for all four test personas; live fetch and live form reading; fixed live inspect reading zero forms |
| 2026-11-07 | Merged (PR #1) | All of the above on `main`; CI green on all six platform and Python combinations |

### What the latest check measured (2026-11-06)

- **Tests:** 978 offline tests (1 skipped), plus lint, format and the personal-data scan, all
  clean.
- **Resumes:** 224 of 224 expected fields read correctly across 16 layouts, scans included. It
  was 134 of 224 before this round.
- **Fit eval:**
  - gate accuracy 0.975, with 0 jobs wrongly gated out;
  - recall ranking quality (nDCG@10) 0.982;
  - final ranking quality (nDCG@10) 0.959;
  - band: 54% exactly right, and 100% within one band.
- **Live:** 7,029 postings fetched with 0 errors; 15 of 15 real application forms read.
- **Answer reuse:** on a real answer bank, the questions still waiting on the user fell from
  43 to 30.
