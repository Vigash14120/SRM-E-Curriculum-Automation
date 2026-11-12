# eCurricula helper

Semi-automatic helper for the SRMIST eCurricula portal (Behavioral Psychology, 21GNH401TA).
For each session it can answer the MCQs and the worksheets with an AI model, make a shared
Google Doc per worksheet, and paste the link into the portal.

**You stay in control.** You review every answer before it is used, you type `SUBMIT`
for the one-way MCQ submit, and you type your own authenticator code in the terminal
(the script never sees your password or your authenticator secret).

> Check your university's rules on AI-assisted work before using this. You are responsible
> for what gets submitted under your name. Review every answer.

## Setup (Windows / macOS / Linux, Python 3.10+)

1. Install dependencies
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
2. **Gemini key**: create a free key at https://aistudio.google.com. The script asks for it on
   first run and stores it in `config.json` on your computer (git-ignored).
   Optional: use a local model instead by setting `PROVIDER = "ollama"` in `solve_mcqs.py`.
3. **Google Drive (for the worksheet links)**: one-time, about 10 minutes, with your own Gmail
   - console.cloud.google.com > create a project
   - APIs & Services > Library > enable **Google Drive API**
   - OAuth consent screen / Audience: External, add your Gmail as a **Test user**
     (or click *Publish app*; the script only uses the `drive.file` scope)
   - Credentials > Create credentials > OAuth client ID > **Desktop app**
   - Download the JSON, save it as `credentials.json` next to the scripts
4. Run
   ```
   python ecurricula_auto.py
   ```
   First run asks for your name and register number (printed at the top of every answer doc).

## Using it

- Log in: the portal opens; type your username/password in the browser, the 6-digit code in the terminal.
- Pick a mode (MCQs + worksheets, MCQs only, or worksheets only).
- Press **Enter** to open and process the next session automatically (S1, S2, ...),
  or type `2 4` to jump to Unit 2 Session 4, `c` to use the page that is open, `q` to quit.
- Each worksheet link needs one authenticator code, typed in the terminal.
- A log of what was done is written to `run_out/log.csv`.

## Files

| File | Purpose |
|---|---|
| `ecurricula_auto.py` | Main script (run this) |
| `solve_mcqs.py` | MCQ reading/answering and LLM settings |
| `solve_worksheets.py` | Worksheet parsing and Google Drive helpers |

## Never commit these

`config.json`, `credentials.json`, `token.json` and `browser_profile/` hold your key, Google
access and your logged-in portal session. They are in `.gitignore`; check `git status` before
every push.

## Known limits

- Built against the portal layout as of Nov 2026; selectors may need small fixes if it changes.
- Worksheets can be `.docx`, PDF-only, or written-answer; odd layouts may need a manual skip (`s`).
- Free-tier Gemini has rate limits; the script retries automatically.
