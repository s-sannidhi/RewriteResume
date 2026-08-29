# Resume Rewriter

A local job-application assistant. It reads a job posting from your browser, writes a tailored
one-page résumé and cover letter with a **local** LLM, tracks every application, and helps you find
and message real people at the companies you apply to.

Everything runs on your machine. No cloud API, no account, no data leaves the computer.

**Windows, macOS and Linux.**

> **Your personal data never belongs in this repo.** The app stores your profile, skills, application
> history and documents in `~/ResumeRewriter/` (outside git). A pre-commit hook blocks emails,
> phone numbers, addresses, keys and data files from being committed. To publish a fresh public
> history (old private commits scrubbed): `./scripts/make_public_clean.sh --push --public`.

---

## Quickstart

```bash
git clone https://github.com/w8asec7/RewriteResume.git
cd RewriteResume
python setup.py          # installs everything and tells you what's missing
```

Then `run.bat` (Windows) or `./run.sh` (macOS/Linux) and open
**http://127.0.0.1:8765/app/**.

Fill in your profile on the Resume tab. Everything you type is saved under `~/ResumeRewriter/` —
not in the clone.

---

## Setup, step by step

**1. Install the two prerequisites**

- [Python 3.10+](https://www.python.org/downloads/) — on Windows, tick *"Add Python to PATH"* in
  the installer.
- [Ollama](https://ollama.com/download) — runs the language model locally. Install it and leave it
  running.

**2. Get the code and run setup**

```bash
git clone https://github.com/w8asec7/RewriteResume.git
cd RewriteResume
python setup.py
```

On macOS/Linux use `python3` if `python` isn't found.

`setup.py` creates the virtualenv, installs dependencies, downloads the browser used for PDF
rendering (~150 MB), checks Ollama and offers to pull the models (several GB — say yes), creates
your data folder (`~/ResumeRewriter`) with an empty `profile.json` and a starter
`skills_verified.yaml`, and installs a guard that stops personal data being committed. It prints
`[ ok ]` or `[note]` per step and tells you exactly what to do about anything it couldn't finish.
Re-running it is safe — it never overwrites data you already have.

**3. Start the server**

| | |
|---|---|
| **Windows** | `run.bat` |
| **macOS / Linux** | `./run.sh` |

Leave it running and open **http://127.0.0.1:8765/app/**.

**4. Fill in your profile**

The **Resume** tab is your profile: identity, education, work, projects. Every fact on a generated
résumé comes from here — the model rewrites wording, it never invents facts, so anything blank here
simply won't appear. Résumé and cover-letter generation refuse to run until there's something in
the profile.

---

## Moving to another machine

Export a zip (you carry it — USB, private drive — never commit it), then restore on the new machine:

```bash
python scripts/backup_data.py export
# … copy the zip …
python setup.py --restore ~/resumerewriter-backup-YYYY-MM-DD.zip
```

Optional: `python scripts/backup_data.py sync` copies `~/ResumeRewriter` into a local `data/`
folder next to the code. That folder is **gitignored** — a private on-disk mirror only.

---

## Where your data lives

Everything personal sits in `~/ResumeRewriter/` (on Windows, `C:\Users\<you>\ResumeRewriter\`),
deliberately **outside** this repo:

| File | What it is |
|---|---|
| `profile.json` | your identity, experience, projects — the source of every fact |
| `skills_verified.yaml` | the only skills allowed on a résumé (see below) |
| `rr.db` | application tracker, Q&A memory, discovered companies |
| `resumes/` | every generated résumé and cover letter |
| `documents/` | transcript, schedule, anything you attach often |

Passwords are never stored here. They go to the OS credential store (Windows Credential Manager,
macOS Keychain, Linux Secret Service) via `keyring`, or an environment variable
(`RR_GMAIL_APP_PASSWORD`, `RR_LOGIN_PASSWORD`).

This repo is meant to be public. `scripts/check_no_personal_data.py` runs on every commit and
blocks anything that looks like an email, phone number, address, key or data file. Run it yourself
any time:

```bash
python scripts/check_no_personal_data.py --all
```

If this repo once contained a private `data/` snapshot in git history, scrub it before making the
GitHub repo public (deleting files in a new commit is not enough):

```bash
./scripts/make_public_clean.sh              # dry-run
./scripts/make_public_clean.sh --push --public
```

Day-to-day after that: edit code → commit → `git push`. Your `~/ResumeRewriter` profile is never
part of those commits.

**5. Fill in your verified-skills file**

`setup.py` leaves a starter `~/ResumeRewriter/skills_verified.yaml`, deliberately empty with a
commented example to edit. The Skills section stays empty until you list skills there — nothing
else on earth can add to it, not the posting and not the model. See
[Verified skills](#verified-skills) below for the format and why.

### The browser extension

The website at **http://127.0.0.1:8765/app/** works in any browser, including Firefox. The
extension (reads the posting off the page, autofills, attaches PDFs) works in **Chrome / Edge**
and **Firefox 128+**.

**Chrome / Edge:** `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → select
the `extension` folder. Click the toolbar icon to open the side panel.

**Firefox** — do not use `about:addons` or “Install Add-on From File” (that path expects a signed
`.xpi` and will reject this folder). Load it as a temporary add-on:

1. In the address bar open `about:debugging#/runtime/this-firefox`.
2. Click **This Firefox** in the left sidebar if you aren’t already there.
3. Click **Load Temporary Add-on…**
4. Select **any file inside** the `extension` folder — `manifest.json` is the usual choice.
   Picking the folder itself does nothing; it has to be a file.
5. Open the panel: click the **Resume Rewriter** icon on the toolbar, **or**
   **View → Sidebar → Resume Rewriter**. Firefox puts it in the sidebar (left or right), not
   Chrome’s side-panel tray.
6. Pin the icon (toolbar puzzle-piece → pin) so you can find it next time.

Firefox unloads temporary add-ons when the browser quits. After a restart, repeat steps 1–4
(your settings and generated docs are on disk; only the add-on itself needs reloading).

Same features as Chrome for reading JDs, generating docs, attaching files, and filling ordinary
forms. Workday *work experience* and *education* fills need Chrome (trusted typing). Workday
*skills* and non-Workday applications work in Firefox.

### Models

Pulled for you by `setup.py`, or manually:

```bash
ollama pull gemma3:12b        # writes the prose
ollama pull nomic-embed-text  # ranks your evidence against the posting
```

`gemma3:12b` needs roughly 8 GB of RAM. On a smaller machine, set `LLM_MODEL` in
`backend/config.py` to something lighter (e.g. `gemma3:4b`) — quality drops but it works.

---

## Verified skills

The Skills section reads from **one hand-maintained file**, `~/ResumeRewriter/skills_verified.yaml`.
The job description can reorder it; nothing can add to it. Not the LLM, not a keyword match.

```yaml
verified:
  programming_languages:
    - name: "Python"
      evidence: ["ML Racecar", "AI Resume & Outreach Agent"]
    - name: "Java"
      evidence: ["Data Structures"]      # coursework counts
needs_review:                            # ignored by the generator
  databases:
    - name: "PostgreSQL"
      evidence: []
```

List only what you could defend in an interview. `evidence:` names the project, job or course that
demonstrates it — you get a warning at generation time if a listed skill has no evidence, or if its
evidence isn't on that particular résumé.

---

## How it works

The split that keeps it honest: **Python owns the facts, the LLM only owns the wording.**

- **Selection is deterministic** — which bullets, which two projects, which skills, and their
  order are chosen in Python from your profile using embedding relevance plus an impact score.
- **The model rewrites prose only**, and may never introduce a technology outside the entry's own
  tech list, no matter what the posting asks for.
- **Checks before render** — duplicate phrasing inside a bullet, two bullets restating one fact,
  invented numbers or tech, and unverifiable claims are stripped or flagged.
- **One page, always** — the PDF renderer binary-searches the type scale for the largest that
  still fits, and only trims a bullet if the smallest scale still overflows.

```
extension/   MV3 side panel (Chrome) / sidebar (Firefox): reads postings, drives generation, autofills forms
backend/     FastAPI app (127.0.0.1 only)
  resume/    builder → quality checks → Playwright PDF
  discovery/ company + contact discovery, outreach drafting
  store/     SQLite: tracker, Q&A memory, companies, contacts
frontend/    the web UI at /app
scripts/     setup helpers, backup, scrapers
tests/       pytest — run: .venv/bin/python -m pytest tests/ -q
```

---

## Troubleshooting

**"Couldn't reach the backend"** — the server isn't running. `run.bat` / `./run.sh`.

**Résumé generation hangs or errors** — Ollama isn't running or the model isn't pulled.
Check `http://127.0.0.1:8765/health`; it reports both.

**PDF generation fails** — the Playwright browser is missing:
`.venv/bin/python -m playwright install chromium` (Windows: `.venv\Scripts\python.exe -m playwright install chromium`).

**Skills section is empty** — your `~/ResumeRewriter/skills_verified.yaml` has nothing under
`verified:`. That is deliberate: the generator will not guess which skills you can defend.

**"Your profile is empty" / the Resume tab looks blank** — expected on a machine you just cloned
onto. Your profile is not in this repo (deliberately); it lives in `~/ResumeRewriter/profile.json`.
Either fill in the Resume tab, or bring your data over:
`python scripts/backup_data.py import <your-backup>.zip`. `http://127.0.0.1:8765/health` reports
`profile_ready` and `skills_file` if you want to check what the server can see.

**Port 8765 in use** — pass a different one: `./run.sh --port 8799`.

**Firefox: “This add-on could not be installed” / no Load Temporary Add-on** — you went through
`about:addons`. Use `about:debugging` instead (see [The browser extension](#the-browser-extension)).
If the add-on vanished after you closed Firefox, that is expected: load it again the same way.
