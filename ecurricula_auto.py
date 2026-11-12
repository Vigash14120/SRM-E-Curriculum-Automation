"""
All-in-one: MCQs + worksheet links, with 2FA codes typed in this terminal.

Keep these three files in the same folder (the two older ones are NOT modified):
    solve_mcqs.py        (holds your Gemini key + LLM settings)
    solve_worksheets.py  (worksheet reading + Google Drive helpers)
    ecurricula_auto.py   (this file)

Run:
    python ecurricula_auto.py

Per session you open, it can: answer + submit the MCQs, and for each SLO worksheet
answer it, make a shared Google Doc, paste the link and verify with your code.
Every one-way action (MCQ Submit) still waits for you to type SUBMIT.
"""
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import solve_mcqs as mc
import solve_worksheets as ws

PORTAL_URL = "https://dld.srmist.edu.in/ktretecurricula/#/"
STUDENT_LINE = ""            # set from config.json at start-up; printed at the top of every doc
CONFIG_FILE = Path("config.json")


# ======================= terminal OTP =======================
def ask_code(prompt):
    """Ask for a 6-digit authenticator code in the terminal. Returns None if cancelled."""
    while True:
        c = input(prompt).strip().replace(" ", "")
        if c.lower() == "c":
            return None
        if re.fullmatch(r"\d{6}", c):
            return c
        print("  Type exactly 6 digits (or c to cancel).")


def login(page):
    """Open the portal; handle the login 2FA screen via the terminal."""
    page.goto(PORTAL_URL)
    print("Opening portal. If you are asked for username/password, type them in the browser.")
    print("The 6-digit code, if asked, is requested here in the terminal.")
    start = time.time()
    while time.time() - start < 600:
        logout = page.locator("button", has_text="Logout")
        if logout.count() and logout.first.is_visible():
            print("Logged in.")
            return True
        box = page.get_by_placeholder(re.compile("6-digit", re.I))
        if box.count() and box.first.is_visible():
            code = ask_code("Login code from Google Authenticator (6 digits, c = cancel): ")
            if code is None:
                return False
            box.first.fill(code)
            page.locator("button", has_text=re.compile(r"^\s*Verify\s*$")).first.click()
            for _ in range(12):     # wait for success (Logout shows) or rejection (box stays)
                page.wait_for_timeout(1000)
                if page.locator("button", has_text="Logout").count():
                    break
            continue
        page.wait_for_timeout(1000)
    print("Login timed out.")
    return False


def verify_popup(page):
    """Fill the 'Verify with your authenticator' popup from a code typed in the terminal."""
    dlg = page.get_by_role("dialog").filter(has_text="Verify with your authenticator")
    try:
        dlg.wait_for(state="visible", timeout=15000)
    except Exception:
        print("  The verification popup did not appear - check the browser.")
        return False
    for attempt in range(3):
        code = ask_code("Link code from Google Authenticator (6 digits, c = cancel): ")
        if code is None:
            try:
                dlg.locator("button", has_text="Cancel").click()
            except Exception:
                pass
            return False
        inputs = dlg.locator("input")
        n = inputs.count()
        typed_ok = False
        if n >= 6:
            for i, d in enumerate(code):
                inputs.nth(i).fill(d)
            typed_ok = "".join(inputs.nth(i).input_value() for i in range(6)) == code
        elif n == 1:
            inputs.first.fill(code)
            typed_ok = inputs.first.input_value() == code
        if not typed_ok:        # fall back to real key presses
            inputs.first.click()
            page.keyboard.type(code, delay=80)
        dlg.locator("button", has_text="Verify and submit").click()
        try:
            dlg.wait_for(state="hidden", timeout=12000)
            page.wait_for_timeout(1000)
            return True
        except Exception:
            print("  Code was not accepted (expired or wrong). Enter a fresh one.")
    return False


# ======================= worksheet parsing =======================
Q_RE = re.compile(r"^\s*(?:Q(?:uestion)?\s*)?(\d+)\s*[.):\-]\s*(.+)$", re.I)
OPT_START = re.compile(r"^\s*\(?([A-Da-d])[.)]\s+")
OPT_SPLIT = re.compile(r"(?:^|(?<=\s))\(?[A-Da-d][.)]\s+")


def parse_flex(lines):
    mcqs, cur, pending = [], None, None     # pending = last plain line, i.e. a possible unnumbered Q1
    for ln in lines:
        m = Q_RE.match(ln)
        if m and not OPT_START.match(ln):
            if cur:
                mcqs.append(cur)
            rest = m.group(2).strip()
            parts = [x.strip() for x in OPT_SPLIT.split(rest)]
            if len(parts) >= 3 and parts[0]:      # options written inline after the question
                cur = {"question": parts[0], "options": [x for x in parts[1:] if x]}
            else:
                cur = {"question": rest, "options": []}
        elif OPT_START.match(ln):
            if cur is None:
                if not pending:
                    continue
                cur = {"question": pending, "options": []}   # first question had no number
            parts = [p.strip() for p in OPT_SPLIT.split(ln) if p.strip()]
            cur["options"].extend(parts)
        elif cur is not None and not cur["options"]:
            cur["question"] += " " + ln
        elif cur is None:
            pending = ln
    if cur:
        mcqs.append(cur)
    return [q for q in mcqs if q["question"] and len(q["options"]) >= 2]


def _raw_llm(prompt, temperature, as_json):
    """One model call; returns the reply text."""
    if mc.PROVIDER == "gemini":
        key = mc.GEMINI_API_KEY.strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mc.GEMINI_MODEL}:generateContent"
        cfg = {"temperature": temperature}
        if as_json:
            cfg["responseMimeType"] = "application/json"
        r = mc._post(url, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                     {"x-goog-api-key": key})
        parts = r["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    body = {"model": mc.OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "options": {"temperature": temperature}}
    if as_json:
        body["format"] = "json"
    r = mc._post("http://localhost:11434/api/chat", body, {}, timeout=300)
    return r["message"]["content"]


def llm_text(prompt, temperature=0.3):
    return _raw_llm(prompt, temperature, as_json=False)


def llm_json(prompt, temperature=0, tries=3):
    last = None
    for attempt in range(tries):
        text = _raw_llm(prompt, temperature, as_json=True).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as e:
            last = e
            print(f"  Model reply was not valid JSON ({e}); retrying...")
            temperature = min(0.5, temperature + 0.2)
    raise last


def llm_extract(text):
    prompt = (
        "Below is the text of a university worksheet. Extract every multiple-choice question "
        "with its answer options, in order. Do NOT answer them. Remove option letters/numbers. "
        'Reply ONLY with JSON: {"questions":[{"question":"...","options":["...","..."]}]}\n\n'
        + text[:12000]
    )
    data = llm_json(prompt)
    out = []
    for q in data.get("questions", []):
        opts = [str(o).strip() for o in q.get("options", []) if str(o).strip()]
        if q.get("question") and len(opts) >= 2:
            out.append({"question": str(q["question"]).strip(), "options": opts})
    return out


def _has_options(joined):
    return bool(re.search(r"(?:^|\s)\(?[Aa][.)]\s", joined) and re.search(r"(?:^|\s)\(?[Bb][.)]\s", joined))


def pdf_text(path):
    """Text of a PDF via pypdf (pip install pypdf). None if unavailable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception as e:
        print("  Could not read the PDF text:", e)
        return None


def worksheet_text(path):
    if str(path).lower().endswith(".pdf"):
        return pdf_text(path) or ""
    return ws.docx_text(path)


def llm_extract_pdf(path):
    """No pypdf installed: let Gemini read the PDF itself. Returns (title, mcqs)."""
    import base64
    if mc.PROVIDER != "gemini":
        return "", []
    data = base64.b64encode(Path(path).read_bytes()).decode()
    prompt = (
        "This PDF is a university worksheet. Extract every multiple-choice question with its "
        "answer options, in order (the first question may have no number). Do NOT answer them. "
        "Remove option letters. Reply ONLY with JSON: "
        '{"title":"<worksheet title>","questions":[{"question":"...","options":["...","..."]}]}'
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mc.GEMINI_MODEL}:generateContent"
    try:
        r = mc._post(url, {"contents": [{"parts": [{"inlineData": {"mimeType": "application/pdf", "data": data}},
                                                   {"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
                     {"x-goog-api-key": mc.GEMINI_API_KEY.strip()}, timeout=120)
        txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"]).strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
        d = json.loads(txt, strict=False)
    except Exception as e:
        print("  Gemini could not read the PDF:", e)
        return "", []
    out = []
    for q in d.get("questions", []):
        opts = [str(o).strip() for o in q.get("options", []) if str(o).strip()]
        if q.get("question") and len(opts) >= 2:
            out.append({"question": str(q["question"]).strip(), "options": opts})
    return str(d.get("title", "")).strip() or "Worksheet", out


def parse_pdf(path):
    text = pdf_text(path)
    if text and text.strip():
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        heading = lines[0]
        joined = "\n".join(lines)
        if not _has_options(joined) and "mcq" not in heading.lower():
            return heading, [], "not-mcq"
        mcqs = parse_flex(lines)
        a_marks = len(re.findall(r"(?:^|\s)\(?[Aa][.)]\s", joined))
        if mcqs and len(mcqs) == a_marks:      # every option block accounted for: skip the extra LLM call
            return heading, mcqs, "PDF text + parser"
        try:
            llm = llm_extract(joined)          # counts disagree: let the LLM read the layout
        except Exception as e:
            print("  LLM extraction failed:", e)
            llm = []
        if llm:
            return heading, llm, "PDF text + LLM (check the questions look right)"
        return heading, mcqs, ("PDF text + parser (check the count!)" if mcqs else "none")
    title, mcqs = llm_extract_pdf(path)
    if mcqs:
        return title, mcqs, "Gemini read the PDF (check the questions look right)"
    print("  Tip: run  pip install pypdf  so PDFs can be read as text.")
    return title, [], "none"


def parse_robust(path):
    """Try the original parser, then a flexible one, then the LLM. Returns (heading, mcqs, how)."""
    if str(path).lower().endswith(".pdf"):
        return parse_pdf(path)
    heading, mcqs = ws.parse_worksheet(path)
    if mcqs:
        return heading, mcqs, "standard parser"
    text = ws.docx_text(path)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    heading = lines[0] if lines else ""
    mcqs = parse_flex(lines)
    if mcqs:
        return heading, mcqs, "flexible parser"
    joined = "\n".join(lines)
    has_options = re.search(r"(?:^|\s)\(?[Aa][.)]\s", joined) and re.search(r"(?:^|\s)\(?[Bb][.)]\s", joined)
    if not has_options and "mcq" not in heading.lower():
        return heading, [], "not-mcq"      # no answer options anywhere: a written-answer worksheet
    if lines:
        try:
            mcqs = llm_extract("\n".join(lines))
        except Exception as e:
            print("  LLM extraction failed:", e)
            mcqs = []
        if mcqs:
            return heading, mcqs, "LLM extraction (check the questions look right)"
    dump = Path(str(path) + ".txt")
    dump.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Could not find MCQs. Extracted text saved to {dump}")
    for l in lines[:25]:
        print("   |", l[:110])
    return heading, [], "none"


def _as_text(v):
    if isinstance(v, list):
        return "\n".join("- " + str(x).strip() for x in v)
    return str(v).strip()


def written_answers(text, temperature=0.3):
    """For worksheets with open-ended tasks (no answer options)."""
    prompt = (
        "You are a university student completing a worksheet for a Behavioral Psychology course. "
        "Below is the worksheet text. Answer EVERY numbered task. Follow each task's instructions "
        "exactly (for example, 'three ideas' means exactly three, one short line each). Keep EVERY "
        "answer SHORT: at most 4-5 short lines (roughly 50-70 words), specific to the scenario, "
        "in plain text with no markdown. Never write long paragraphs. If a task asks about the "
        "student's own experience, give a short realistic example.\n"
        "Reply in EXACTLY this plain-text format and nothing else (no JSON, no markdown):\n"
        "### 1\nTASK: <the task text copied from the worksheet>\nANSWER: <your answer, may use several lines>\n"
        "### 2\nTASK: ...\nANSWER: ...\n\n" + text[:12000]
    )
    reply = llm_text(prompt, temperature)
    return parse_written_reply(reply)


def parse_written_reply(reply):
    reply = reply.replace("**", "")
    blocks = re.split(r"(?m)^\s*#{2,4}\s*(?:Task\s*)?\d+.*$", reply)[1:]
    items = []
    for b in blocks:
        b = b.strip()
        m = re.search(r"TASK:\s*(.*?)\n\s*ANSWER:\s*(.*)", b, re.S | re.I)
        if m:
            task, ans = m.group(1).strip(), m.group(2).strip()
        else:
            task, ans = "", b
        if ans:
            items.append({"task": task, "answer": ans})
    return items


def written_doc_text(session, slo, heading, items):
    out = [STUDENT_LINE, "", heading, f"{session} - SLO {slo}", ""]
    for i, it in enumerate(items, 1):
        out += [f"{i}. {it['task']}", it["answer"], ""]
    return "\n".join(out)


# ======================= portal actions =======================
def ensure_practice_open(page):
    heads = page.locator(".ant-collapse-header", has_text="Learning Practice")
    for i in range(heads.count()):
        h = heads.nth(i)
        if h.get_attribute("aria-expanded") != "true":
            h.scroll_into_view_if_needed()
            h.click()
            page.wait_for_timeout(1000)


def submit_link(page, idx, link):
    inp = page.get_by_placeholder(re.compile("Paste", re.I)).nth(idx)
    inp.scroll_into_view_if_needed()
    inp.fill(link)
    btn = page.locator("button", has_text=re.compile("UPDATE", re.I)).nth(idx)
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    btn.click()
    return verify_popup(page)


def do_mcqs(page, session):
    mc.expand_mcq_panel(page)
    if mc.already_attempted(page):
        print("MCQs already submitted - skipping.")
        mc.log(session, [], [], "mcq already submitted")
        return
    mcqs = mc.extract_mcqs(page)
    if not mcqs or any(len(m["options"]) < 2 or not m["question"] for m in mcqs):
        print("Could not read the MCQs properly:", mcqs)
        mc.log(session, mcqs, [], "mcq read error")
        return
    print(f"\n--- MCQs: {len(mcqs)} questions ---")
    answers = mc.ask_llm(mcqs)
    mc.show(mcqs, answers)
    ov = input("\nEnter = accept, '2:3 5:1' = override, s = skip MCQs: ").strip()
    if ov.lower() == "s":
        mc.log(session, mcqs, answers, "mcq skipped by user")
        return
    if ov:
        answers = mc.apply_overrides(ov, answers, mcqs)
    bad = mc.fill_answers(page, answers)
    if bad:
        print("Could not tick questions:", bad, "- check the browser.")
    print("\nAnswers are ticked. Submit is ONE-WAY (single attempt).")
    if input("Type SUBMIT to submit, anything else to leave it: ").strip() == "SUBMIT":
        mc.click_submit(page)
        print("MCQs submitted.")
        mc.log(session, mcqs, answers, "mcq submitted")
    else:
        mc.log(session, mcqs, answers, "mcq filled, not submitted")


def do_written(page, idx, session, slo, heading, path, state):
    tag = f"{session} SLO{slo}"
    print(f"\n=== SLO {slo}: '{heading}' - written-answer worksheet ===")
    text = "\n".join(l.strip() for l in worksheet_text(path).splitlines() if l.strip())
    if not text:
        print("Could not read this worksheet's text (for PDFs run: pip install pypdf). Skipping.")
        mc.log(f"{session} SLO{slo}", [], [], "written worksheet unreadable")
        return
    temp = 0.3
    while True:
        items = written_answers(text, temp)
        if not items:
            print("Could not generate answers for this worksheet.")
            mc.log(tag, [], [], "written worksheet: no answers")
            return
        for i, it in enumerate(items, 1):
            print(f"\n{i}. {it['task']}\n   -> {it['answer']}")
        c = input("\nEnter = accept, r = regenerate, s = skip this worksheet: ").strip().lower()
        if c == "s":
            mc.log(tag, [], [], "written worksheet skipped")
            return
        if c == "r":
            temp = min(1.0, temp + 0.3)
            continue
        break
    if state.get("svc") is None:
        state["svc"] = ws.drive_service()
    link = ws.make_shared_doc(state["svc"], f"{session} - SLO {slo} answers",
                              written_doc_text(session, slo, heading, items))
    print("Created doc:", link)
    if input("Paste this link into the portal now? (Enter = yes, n = no): ").strip().lower() == "n":
        mc.log(tag, [], [], "written doc made, not submitted: " + link)
        return
    ok = submit_link(page, idx, link)
    print("Link submitted and verified." if ok else "Not confirmed - check the browser.")
    mc.log(tag, [], [], ("written link submitted: " if ok else "written link NOT confirmed: ") + link)


def fetch_worksheet(page, idx, session, slo, kind="Docx", wait_s=30):
    """Click 'Download <kind>'. The file arrives as a normal download, or the portal opens
    it in a new tab (like the PDF viewer); then we fetch that tab's URL directly."""
    got, pops = {}, []
    ctx = page.context

    def on_dl(d):
        got.setdefault("d", d)

    def on_page(pg):
        pops.append(pg)
        pg.on("download", on_dl)

    safe = re.sub(r"[^A-Za-z0-9]+", "_", session)
    path = None
    page.on("download", on_dl)
    ctx.on("page", on_page)
    try:
        btn = page.get_by_role("button", name=f"Download {kind}").nth(idx)
        btn.scroll_into_view_if_needed()
        btn.click()
        for _ in range(wait_s * 2):
            if "d" in got:
                break
            if any(p.url not in ("", "about:blank") for p in pops):
                page.wait_for_timeout(1000)          # grace period in case a download follows
                break
            page.wait_for_timeout(500)
        d = got.get("d")
        if d is not None:
            path = ws.WS_DIR / f"{safe}_SLO{slo}_{d.suggested_filename}"
            d.save_as(str(path))
        else:
            for pg in pops:
                url = pg.url
                if url in ("", "about:blank"):
                    continue
                print("  The portal opened a new tab:", url)
                try:
                    r = ctx.request.get(url)
                    body = r.body()
                    if r.ok and body:
                        name = Path(url.split("?")[0]).name or ("worksheet." + kind.lower())
                        path = ws.WS_DIR / f"{safe}_SLO{slo}_{name}"
                        path.write_bytes(body)
                        break
                    print("  Could not fetch it directly (HTTP", r.status, ")")
                except Exception as e:
                    print("  Could not fetch it directly:", e)
    finally:
        page.remove_listener("download", on_dl)
        ctx.remove_listener("page", on_page)
        for pg in pops:
            try:
                pg.close()
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
    if path is None:
        msgs = page.locator(".ant-message-notice, .ant-notification-notice").all_inner_texts()
        print(f"  No {kind} file arrived." + (" Portal says: " + " | ".join(msgs) if msgs else ""))
        try:
            page.screenshot(path=str(ws.WS_DIR / f"{safe}_SLO{slo}_{kind}_failed.png"))
        except Exception:
            pass
    return path


def _one_slo(page, idx, session, state):
    slo = idx + 1
    tag = f"{session} SLO{slo}"
    if page.get_by_placeholder(re.compile("Paste", re.I)).nth(idx).input_value().strip():
        print(f"SLO {slo}: a link is already there, skipping.")
        return
    # try the format that worked last time first (saves the wait when .docx is missing)
    order = ["PDF", "Docx"] if state.get("pdf_first") else ["Docx", "PDF"]
    path = None
    for kind in order:
        path = fetch_worksheet(page, idx, session, slo, kind, wait_s=12 if kind == "Docx" else 25)
        if path is not None:
            break
        print(f"  No {kind} here - trying the other format...")
    if path is not None:
        state["pdf_first"] = path.suffix.lower() == ".pdf"
    if path is None:
        print("  Could not get this worksheet as .docx or PDF. Try the buttons by hand: if nothing opens for you\n"
              "  either, it is not available yet. Skipping this SLO.")
        mc.log(tag, [], [], "worksheet download failed")
        return
    heading, mcqs, how = parse_robust(path)
    if how == "not-mcq":
        do_written(page, idx, session, slo, heading, path, state)
        return
    print(f"\n=== SLO {slo}: '{heading}' - {len(mcqs)} questions [{how}] ===")
    if not mcqs:
        mc.log(tag, [], [], "worksheet parse error")
        return
    answers = mc.ask_llm(mcqs)
    mc.show(mcqs, answers)
    ov = input("\nEnter = accept, '2:3 5:1' = override, s = skip this worksheet: ").strip()
    if ov.lower() == "s":
        mc.log(tag, mcqs, answers, "worksheet skipped")
        return
    if ov:
        answers = mc.apply_overrides(ov, answers, mcqs)
    if state.get("svc") is None:
        state["svc"] = ws.drive_service()
    text = STUDENT_LINE + "\n\n" + ws.answer_doc_text(session, slo, heading, mcqs, answers)
    link = ws.make_shared_doc(state["svc"], f"{session} - SLO {slo} answers", text)
    print("Created doc:", link)
    if input("Paste this link into the portal now? (Enter = yes, n = no): ").strip().lower() == "n":
        mc.log(tag, mcqs, answers, "doc made, not submitted: " + link)
        return
    ok = submit_link(page, idx, link)
    print("Link submitted and verified." if ok else "Not confirmed - check the browser.")
    mc.log(tag, mcqs, answers, ("link submitted: " if ok else "link NOT confirmed: ") + link)


def do_worksheets(page, session, state):
    ensure_practice_open(page)
    n = page.get_by_role("button", name="Download Docx").count()
    print(f"\n--- Worksheets: found {n} ---")
    for idx in range(n):
        try:
            _one_slo(page, idx, session, state)
        except Exception as e:
            print(f"SLO {idx + 1} ERROR: {e}")
            mc.log(f"{session} SLO{idx + 1}", [], [], f"worksheet error: {e}")


# ======================= session navigation =======================
SESSION_RE = re.compile(r"Learning Unit\s*:\s*(\d+)\s*&\s*Session\s*:\s*(\d+)")
UNITS, SESSIONS = 5, 9


def current_session(page):
    """(unit, session) of the session page that is open right now, or None."""
    loc = page.get_by_text(SESSION_RE)
    if loc.count() == 0:
        return None
    m = SESSION_RE.search(loc.first.inner_text())
    return (int(m.group(1)), int(m.group(2))) if m else None


def next_after(cur):
    u, s = cur
    if s < SESSIONS:
        return (u, s + 1)
    if u < UNITS:
        return (u + 1, 1)
    return None


def go_to_session(page, u, s):
    """Click the session in the circular chart. SVG arc ids look like mainArc-1021
    (unit 1, session 02, SLO 1). Falls back to the session-ring arc."""
    if current_session(page) == (u, s):
        return True
    try:
        page.locator("path.sunburst-main-arc").first.wait_for(timeout=15000)
    except Exception:
        print("  The course chart is not on screen. In the browser, open the course page "
              "(the one with the circular chart), then try again.")
        return False
    arcs = [
        page.locator(f"#mainArc-{u}{s:02d}1"),                                   # SLO 1 arc
        page.locator("path.sunburst-main-arc").nth(6 + (u - 1) * SESSIONS + (s - 1)),   # session ring
        page.locator(f"#mainArc-{u}{s:02d}2"),                                   # SLO 2 arc
    ]
    for arc in arcs:
        try:
            if arc.count() == 0:
                continue
            arc.scroll_into_view_if_needed()
            try:
                arc.click(timeout=4000)
            except Exception:
                arc.dispatch_event("click")
        except Exception:
            continue
        for _ in range(16):                       # wait up to ~8 s for the session header
            page.wait_for_timeout(500)
            if current_session(page) == (u, s):
                page.wait_for_timeout(2000)       # let the session content finish loading
                return True
    return False


def process_session(page, state, mode):
    session = mc.session_label(page)
    print("Session:", session)
    try:
        if mode in ("b", "m"):
            do_mcqs(page, session)
        if mode in ("b", "w"):
            do_worksheets(page, session, state)
    except Exception as e:
        print("ERROR:", e)
        mc.log(session, [], [], f"error: {e}")


def load_config():
    """First run asks for your name/register number and Gemini key, then saves them to
    config.json (git-ignored, so it never gets published)."""
    global STUDENT_LINE
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    if not cfg.get("student_line"):
        name = input("Your name (printed at the top of every answer doc): ").strip()
        reg = input("Your register number: ").strip()
        cfg["student_line"] = f"{name} ({reg})" if reg else name
    if "gemini_api_key" not in cfg:
        key = mc.GEMINI_API_KEY.strip()
        if key in ("", "PASTE_YOUR_KEY_HERE"):
            key = input("Your Gemini API key from aistudio.google.com (Enter to skip if using Ollama): ").strip()
        cfg["gemini_api_key"] = key
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    STUDENT_LINE = cfg["student_line"]
    if cfg["gemini_api_key"]:
        mc.GEMINI_API_KEY = cfg["gemini_api_key"]


def main():
    load_config()
    state = {"svc": None}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(mc.PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not login(page):
            ctx.close()
            return
        names = {"b": "MCQs + worksheets", "m": "MCQs only", "w": "worksheets only"}
        m0 = input("\nMode: Enter = MCQs + worksheets, m = MCQs only, w = worksheets only: ").strip().lower()
        mode = m0 if m0 in ("m", "w") else "b"
        last = None
        while True:
            cur = current_session(page)
            target = next_after(last) if last else (cur or (1, 1))
            if target is None:
                print("\nAll sessions are done.")
                break
            print(f"\nNext: Unit {target[0]} Session {target[1]}   [mode: {names[mode]}]")
            c = input("Enter = go | 'U S' (e.g. '2 4') = jump | c = use the page open now | "
                      "m/w/b = change mode | q = quit: ").strip().lower()
            if c == "q":
                break
            if c in ("m", "w", "b"):
                mode = c
                continue
            if c == "c":
                cur = current_session(page)
                if not cur:
                    print("No session page is open in the browser.")
                    continue
                target = cur
            elif c:
                jm = re.fullmatch(r"(\d)\D+(\d)", c)
                if not jm or not (1 <= int(jm.group(1)) <= UNITS and 1 <= int(jm.group(2)) <= SESSIONS):
                    print(f"Type two numbers like '2 4' (unit 1-{UNITS}, session 1-{SESSIONS}).")
                    continue
                target = (int(jm.group(1)), int(jm.group(2)))
            if c != "c":
                print(f"Opening Unit {target[0]} Session {target[1]}...")
                if not go_to_session(page, *target):
                    print("Could not open it automatically. Click the session yourself, then type c.")
                    continue
            process_session(page, state, mode)
            last = target
        ctx.close()


if __name__ == "__main__":
    main()
