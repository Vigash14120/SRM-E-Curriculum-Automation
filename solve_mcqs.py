"""
Step 2: solve the MCQs of ONE session at a time, with you in control.

Flow per session:
  1. You open a session page in the browser (Unit X > Session Y) and press Enter.
  2. Script expands the MCQ panel and reads questions + options.
  3. An LLM (Gemini free tier, or local Ollama) proposes answers.
  4. You review them (accept, or override like "2:3 5:1").
  5. Script ticks the radio buttons, then STOPS. Submit is one-way, so you must
     type SUBMIT to click it (and the confirm dialog).
  6. Everything is logged to log.csv.

Setup (PowerShell):
    pip install playwright
    playwright install chromium
    setx GEMINI_API_KEY "your-key-from-aistudio"     (then open a NEW terminal)
Run:
    python solve_mcqs.py
"""
import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------- settings ----------------
GEMINI_API_KEY = "PASTE_YOUR_KEY_HERE"   # <- paste your key between the quotes
PROVIDER = "gemini"                 # "gemini" or "ollama"
GEMINI_MODEL = "gemini-2.5-flash"   # change if Google AI Studio lists a newer Flash model
OLLAMA_MODEL = "llama3.1:8b"        # any model you've pulled with `ollama pull`
COURSE_CONTEXT = "Behavioral Psychology (self-awareness, self-concept, self-esteem, self-efficacy, Johari Window)"
PROFILE_DIR = Path("browser_profile")
OUT_DIR = Path("run_out")
OUT_DIR.mkdir(exist_ok=True)
LOG_FILE = OUT_DIR / "log.csv"
# ------------------------------------------


# ---------- page reading ----------
def expand_mcq_panel(page):
    """Open the 'Multiple Choice Questions' dropdown if it is closed."""
    if page.locator('div[role="radiogroup"]').count() > 0:
        return
    head = page.locator(".ant-collapse-title", has_text="Multiple Choice Questions").first
    head.scroll_into_view_if_needed()
    head.click()
    try:
        page.wait_for_selector('div[role="radiogroup"]', timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def extract_mcqs(page):
    """Return [{'question': str, 'options': [str, ...]}, ...]"""
    mcqs = []
    for group in page.locator('div[role="radiogroup"]').all():
        q = group.evaluate(
            """el => {
                const li = el.closest('li.ant-list-item');
                const t = li && li.querySelector('.ant-list-item-meta-title');
                return t ? t.innerText.trim() : '';
            }"""
        )
        # each option = one direct ant-space-item of the .mcq space; its text
        # lives in a sibling of the radio label, so read the whole item.
        opts = group.evaluate(
            """el => Array.from(el.querySelectorAll(':scope > .ant-space > .ant-space-item'))
                      .map(i => i.innerText.replace(/\\s+/g, ' ').trim())"""
        )
        mcqs.append({"question": q, "options": opts})
    return mcqs


def session_label(page):
    try:
        txt = page.get_by_text(re.compile(r"Learning Unit\s*:\s*\d+")).first.inner_text()
        return re.sub(r"\s+", " ", txt).strip()
    except Exception:
        return "unknown session"


def already_attempted(page):
    radios = page.locator('div[role="radiogroup"] input.ant-radio-input')
    if radios.count() == 0:
        return False
    return radios.first.is_disabled()


def fill_answers(page, answers):
    groups = page.locator('div[role="radiogroup"]')
    for i, a in enumerate(answers):
        label = groups.nth(i).locator("label.ant-radio-wrapper").nth(a - 1)
        label.scroll_into_view_if_needed()
        label.click()
        page.wait_for_timeout(250)
    # verify
    bad = []
    for i, a in enumerate(answers):
        ok = groups.nth(i).locator("input.ant-radio-input").nth(a - 1).is_checked()
        if not ok:
            bad.append(i + 1)
    return bad


def click_submit(page):
    btn = page.get_by_role("button", name="Submit", exact=True).locator("visible=true")
    # the assessment Submit is the last visible one on the page
    btn.last.scroll_into_view_if_needed()
    if btn.last.is_disabled():
        raise RuntimeError("Submit button is disabled (not all questions answered?)")
    btn.last.click()
    dlg = page.get_by_role("dialog")
    dlg.wait_for(timeout=8000)
    dlg.get_by_role("button", name="Submit", exact=True).click()
    page.wait_for_timeout(2500)


# ---------- LLM ----------
def build_prompt(mcqs):
    lines = []
    for i, m in enumerate(mcqs, 1):
        lines.append(f"Q{i}. {m['question']}")
        for j, o in enumerate(m["options"], 1):
            lines.append(f"   {j}) {o}")
    return (
        f"You are answering multiple-choice questions for a university course on {COURSE_CONTEXT}.\n"
        "Pick the single best option for each question.\n"
        'Reply with ONLY JSON like {"answers":[2,1,3]} where each number is the 1-based option '
        "index, one per question, in order.\n\n" + "\n".join(lines)
    )


def _post(url, body, headers, timeout=90, retries=4):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"  API busy ({e.code}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API error {e.code}: {e.read().decode(errors='ignore')[:300]}")


def ask_llm(mcqs):
    prompt = build_prompt(mcqs)
    if PROVIDER == "gemini":
        key = GEMINI_API_KEY.strip()
        if not key or key == "PASTE_YOUR_KEY_HERE":
            raise RuntimeError("Paste your Gemini key into GEMINI_API_KEY near the top of solve_mcqs.py")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        resp = _post(
            url,
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
            },
            {"x-goog-api-key": key},
        )
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
    else:
        resp = _post(
            "http://localhost:11434/api/chat",
            {
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            {},
            timeout=300,
        )
        text = resp["message"]["content"]

    answers = json.loads(text)["answers"]
    answers = [int(a) for a in answers]
    if len(answers) != len(mcqs):
        raise ValueError(f"model returned {len(answers)} answers for {len(mcqs)} questions")
    for a, m in zip(answers, mcqs):
        if not 1 <= a <= len(m["options"]):
            raise ValueError(f"answer {a} out of range")
    return answers


# ---------- helpers ----------
def show(mcqs, answers):
    for i, (m, a) in enumerate(zip(mcqs, answers), 1):
        print(f"\n{i}. {m['question']}")
        for j, o in enumerate(m["options"], 1):
            print(f"   {'>>' if j == a else '  '} {j}) {o}")


def apply_overrides(text, answers, mcqs):
    for pair in text.split():
        q, _, a = pair.partition(":")
        q, a = int(q), int(a)
        if 1 <= q <= len(answers) and 1 <= a <= len(mcqs[q - 1]["options"]):
            answers[q - 1] = a
    return answers


def log(session, mcqs, answers, status):
    new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "session", "answers", "status", "questions"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), session,
                    " ".join(map(str, answers)), status,
                    " | ".join(m["question"] for m in mcqs)])


# ---------- main ----------
def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(input("Portal URL: ").strip())
        print("Log in (+2FA) yourself in the browser window.")

        while True:
            input("\nOpen a session page (Unit X & Session Y), then press Enter... ")
            session = session_label(page)
            print("Session:", session)
            try:
                expand_mcq_panel(page)
                if already_attempted(page):
                    print("MCQs already submitted for this session - skipping.")
                    log(session, [], [], "already submitted")
                else:
                    mcqs = extract_mcqs(page)
                    if not mcqs or any(len(m["options"]) < 2 or not m["question"] for m in mcqs):
                        print("Could not read the questions properly:", mcqs)
                        log(session, mcqs, [], "read error")
                    else:
                        print(f"Read {len(mcqs)} questions. Asking {PROVIDER}...")
                        answers = ask_llm(mcqs)
                        show(mcqs, answers)
                        ov = input("\nPress Enter to accept, or type overrides like '2:3 5:1' (s = skip): ").strip()
                        if ov.lower() == "s":
                            log(session, mcqs, answers, "skipped by user")
                        else:
                            if ov:
                                answers = apply_overrides(ov, answers, mcqs)
                            bad = fill_answers(page, answers)
                            if bad:
                                print("Could not tick questions:", bad, "- check the browser.")
                            page.screenshot(path=str(OUT_DIR / f"{re.sub(r'[^A-Za-z0-9]+', '_', session)}_filled.png"), full_page=True)
                            print("\nAnswers are ticked in the browser. Submit is ONE-WAY (single attempt).")
                            if input("Type SUBMIT to submit, anything else to leave it: ").strip() == "SUBMIT":
                                click_submit(page)
                                print("Submitted.")
                                log(session, mcqs, answers, "submitted")
                            else:
                                log(session, mcqs, answers, "filled, not submitted")
            except Exception as e:
                print("ERROR:", e)
                log(session, [], [], f"error: {e}")

            if input("Another session? (Enter = yes, q = quit): ").strip().lower() == "q":
                break
        ctx.close()


if __name__ == "__main__":
    main()
