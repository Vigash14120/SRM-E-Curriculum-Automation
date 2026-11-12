"""
Step 3: worksheet answer links, one session at a time.

For each SLO worksheet in the session you have open:
  1. downloads the worksheet .docx and reads its MCQs
  2. gets answers from the LLM (reuses solve_mcqs.py, keep both files together)
  3. you review the answers
  4. creates a Google Doc with the answers, shared as "anyone with the link can view"
  5. pastes the link into the portal and clicks UPDATE
  6. the portal then asks for your authenticator code: YOU type it in the portal
     popup. The script just waits for the popup to close.

One-time Google setup (about 10 minutes) - see the notes printed if credentials.json
is missing. Install:
    pip install google-api-python-client google-auth-oauthlib
Run:
    python solve_worksheets.py
"""
import re
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

import solve_mcqs as base   # LLM call, logging, session label, settings

WS_DIR = base.OUT_DIR / "worksheets"
WS_DIR.mkdir(parents=True, exist_ok=True)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ---------- worksheet parsing (no extra libraries) ----------
def docx_text(path):
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf8")
    xml = re.sub(r"<w:br\s*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/>", " ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return text


def parse_worksheet(path):
    """Return (heading, [{'question', 'options'}])."""
    text = docx_text(path)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    heading = lines[0] if lines else ""
    body = "\n".join(lines)
    blocks = re.split(r"(?m)^\s*\d+\.\s+", body)[1:]
    mcqs = []
    for b in blocks:
        parts = re.split(r"(?m)^\s*[a-dA-D]\)\s*", b)
        if len(parts) < 3:   # options inline on one line: fall back
            parts = re.split(r"\s*(?<![A-Za-z])[a-dA-D]\)\s+", b)
        q = parts[0].strip()
        opts = [p.strip() for p in parts[1:] if p.strip()]
        if q and len(opts) >= 2:
            mcqs.append({"question": q, "options": opts})
    return heading, mcqs


def answer_doc_text(session, slo, heading, mcqs, answers):
    out = [f"{heading}", f"{session} - SLO {slo}", ""]
    for i, (m, a) in enumerate(zip(mcqs, answers), 1):
        letter = "abcd"[a - 1]
        out.append(f"{i}. {m['question']}")
        out.append(f"Answer: {letter}) {m['options'][a - 1]}")
        out.append("")
    return "\n".join(out)


# ---------- Google Drive ----------
def drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not Path("credentials.json").exists():
        raise RuntimeError(
            "credentials.json missing. One-time setup:\n"
            " 1. console.cloud.google.com > create a project\n"
            " 2. APIs & Services > Library > enable 'Google Drive API'\n"
            " 3. OAuth consent screen: External, add your own Gmail as a Test user\n"
            " 4. Credentials > Create credentials > OAuth client ID > Desktop app\n"
            " 5. Download the JSON, rename it credentials.json, put it next to this script"
        )
    creds = None
    if Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
        Path("token.json").write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def make_shared_doc(svc, title, text):
    import io
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain")
    f = svc.files().create(
        body={"name": title, "mimeType": "application/vnd.google-apps.document"},
        media_body=media, fields="id",
    ).execute()
    svc.permissions().create(fileId=f["id"], body={"type": "anyone", "role": "reader"}).execute()
    return f"https://docs.google.com/document/d/{f['id']}/edit?usp=sharing"


# ---------- portal ----------
def open_practice_panels(page):
    heads = page.locator(".ant-collapse-title", has_text="Learning Practice")
    for i in range(heads.count()):
        h = heads.nth(i)
        # only click if its worksheet buttons are not visible yet
        try:
            h.scroll_into_view_if_needed()
            h.click()
            page.wait_for_timeout(1000)
        except Exception as e:
            print("  could not open a Learning Practice panel:", e)


def download_worksheet(page, idx, session, slo):
    btn = page.get_by_role("button", name="Download Docx").nth(idx)
    btn.scroll_into_view_if_needed()
    with page.expect_download(timeout=30000) as dl:
        btn.click()
    d = dl.value
    safe = re.sub(r"[^A-Za-z0-9]+", "_", session)
    path = WS_DIR / f"{safe}_SLO{slo}_{d.suggested_filename}"
    d.save_as(str(path))
    return path


def submit_link(page, idx, link):
    inp = page.get_by_placeholder(re.compile("Paste", re.I)).nth(idx)
    inp.scroll_into_view_if_needed()
    inp.fill(link)
    btn = page.locator("button", has_text=re.compile("UPDATE", re.I)).nth(idx)
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(500)   # let the page enable the button after the link is typed
    btn.click()
    print("\n>>> The portal should now show 'Verify with your authenticator'.")
    print(">>> Type the 6-digit code from Google Authenticator INTO THE BROWSER POPUP and click 'Verify and submit'.")
    try:
        page.get_by_text("Verify with your authenticator").wait_for(timeout=15000)
    except Exception:
        print("    (popup not seen - maybe no code needed, or the click failed; check the browser)")
    try:
        page.get_by_text("Verify with your authenticator").wait_for(state="hidden", timeout=240000)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def main():
    svc = None
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(base.PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(input("Portal URL: ").strip())
        print("Log in (+2FA) yourself in the browser window.")

        while True:
            input("\nOpen a session page (Unit X & Session Y), then press Enter... ")
            session = base.session_label(page)
            print("Session:", session)
            try:
                open_practice_panels(page)
                n = page.get_by_role("button", name="Download Docx").count()
                print(f"Found {n} worksheet(s).")
                for idx in range(n):
                    slo = idx + 1
                    existing = page.get_by_placeholder(re.compile("Paste", re.I)).nth(idx).input_value()
                    if existing.strip():
                        print(f"SLO {slo}: a link is already there, skipping.")
                        continue
                    path = download_worksheet(page, idx, session, slo)
                    heading, mcqs = parse_worksheet(path)
                    print(f"\n=== SLO {slo} worksheet: '{heading}' ({len(mcqs)} questions) ===")
                    if len(mcqs) == 0:
                        print("Could not read questions from", path, "- skipping.")
                        base.log(session + f" SLO{slo}", [], [], "worksheet parse error")
                        continue
                    answers = base.ask_llm(mcqs)
                    base.show(mcqs, answers)
                    ov = input("\nEnter = accept, '2:3 5:1' = override, s = skip: ").strip()
                    if ov.lower() == "s":
                        base.log(session + f" SLO{slo}", mcqs, answers, "worksheet skipped")
                        continue
                    if ov:
                        answers = base.apply_overrides(ov, answers, mcqs)
                    if svc is None:
                        svc = drive_service()
                    text = answer_doc_text(session, slo, heading, mcqs, answers)
                    link = make_shared_doc(svc, f"{session} - SLO {slo} answers", text)
                    print("Created doc:", link)
                    if input("Paste this link into the portal now? (Enter = yes, n = no): ").strip().lower() == "n":
                        base.log(session + f" SLO{slo}", mcqs, answers, "doc made, not submitted: " + link)
                        continue
                    ok = submit_link(page, idx, link)
                    print("Submitted." if ok else "Verification popup did not close - check the browser.")
                    base.log(session + f" SLO{slo}", mcqs, answers, ("link submitted: " if ok else "link NOT confirmed: ") + link)
            except Exception as e:
                print("ERROR:", e)
                base.log(session, [], [], f"worksheet error: {e}")
            if input("Another session? (Enter = yes, q = quit): ").strip().lower() == "q":
                break
        ctx.close()


if __name__ == "__main__":
    main()
