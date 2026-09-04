#!/usr/bin/env python3
"""RK Group cold outreach sequencer."""

import argparse
import json
import os
import re
import sys

import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing. Run: pip install pyyaml")

CONFIG_PATH = "config.yaml"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path):
    if not os.path.exists(path):
        sys.exit(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# google sheet
# --------------------------------------------------------------------------

def sheets_api():
    """Service account auth. Works locally and on GitHub Actions."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    elif os.path.exists("service_account.json"):
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    else:
        sys.exit("no credentials: set GOOGLE_SERVICE_ACCOUNT_JSON or add service_account.json")

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(cfg):
    """Return every data row as a dict, with its real sheet row number."""
    api = sheets_api()
    tab = cfg["sheet"]["tab"]
    values = api.spreadsheets().values().get(
        spreadsheetId=cfg["sheet"]["id"],
        range=f"{tab}!A2:G",
    ).execute().get("values", [])

    rows = []
    for i, row in enumerate(values):
        row = list(row) + [""] * (7 - len(row))
        rows.append({
            "row": i + 2,
            "company": row[0].strip(),
            "email": row[3].strip(),
            "website": row[4].strip(),
            "status": row[5].strip(),
            "notes": row[6].strip(),
        })
    return rows


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
LOWER_THEN_UPPER = re.compile(r"[a-z][A-Z]")

# Never send. Complaint magnets, and nobody there buys anything.
HARD_ROLE = {
    "noreply", "no-reply", "donotreply", "postmaster", "abuse", "spam",
    "webmaster", "privacy", "legal", "compliance", "dmca", "unsubscribe",
    "mailer-daemon", "bounce", "bounces",
}

# Low quality but sendable.
SOFT_ROLE = {
    "info", "admin", "contact", "office", "hello", "help", "support",
    "sales", "team", "mail", "enquiries", "inquiries", "service",
    "customerservice", "membership", "accounts", "accounting", "billing",
    "careers", "hr", "general", "frontdesk", "reception",
}


def validate(row):
    """Return (email, tier, reason). email is None when the row must be skipped."""
    company = row["company"]
    raw = row["email"]

    if not raw:
        return None, None, "no_email"
    if not company:
        return None, None, "no_company"

    # some cells hold two or three addresses; take the first
    parts = [p for p in re.split(r"[,;/\s]+", raw) if p]
    multi = len(parts) > 1
    first = parts[0]
    addr = first.lower()

    if not EMAIL_RE.match(addr):
        return None, None, "invalid_email"

    local = addr.split("@")[0]

    # scraper artifacts like jIXSK@... - lowercase then uppercase, no separator
    if LOWER_THEN_UPPER.search(first.split("@")[0]) and not re.search(r"[._\-]", local):
        return None, None, "suspicious_email"

    base = re.sub(r"[^a-z]", "", local.split("+")[0])
    if base in HARD_ROLE:
        return None, None, "blocked_role_account"

    tier = "role" if base in SOFT_ROLE else "named"
    return addr, tier, ("ok_multi_email" if multi else "ok")



def build_message(cfg, mailbox, company, to_email):
    step = cfg["sequence"][0]
    body = step["body"].replace("{{company}}", company).rstrip()
    body += "\n\n" + cfg["footer"].strip() + "\n"

    msg = EmailMessage()
    msg["From"] = formataddr((mailbox["from_name"], mailbox["address"]))
    msg["To"] = to_email
    msg["Subject"] = step["subject"].replace("{{company}}", company)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=mailbox["address"].split("@")[1])
    msg["List-Unsubscribe"] = f"<mailto:{mailbox['address']}?subject=unsubscribe>"
    msg.set_content(body)
    return msg


def send_smtp(mailbox, msg):
    password = os.environ.get(mailbox["password_env"])
    if not password:
        raise RuntimeError(f"{mailbox['password_env']} is not set")
    with smtplib.SMTP(mailbox["smtp_host"], mailbox["smtp_port"], timeout=45) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(mailbox["address"], password)
        s.send_message(msg)


def cmd_test(args, cfg):
    mailbox = cfg["mailboxes"][0]
    msg = build_message(cfg, mailbox, args.company, args.to)
    print(f"From:    {msg['From']}")
    print(f"To:      {msg['To']}")
    print(f"Subject: {msg['Subject']}\n")
    print(msg.get_content())
    if args.dry_run:
        print("[dry run - nothing sent]")
        return
    send_smtp(mailbox, msg)
    print(f"sent to {args.to}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_check(args, cfg):
    print("config loaded OK\n")
    print(f"  spreadsheet id : {cfg['sheet']['id']}")
    print(f"  tab            : {cfg['sheet']['tab']}")
    print(f"  mailboxes      : {len(cfg['mailboxes'])}")
    for mb in cfg["mailboxes"]:
        print(f"     - {mb['address']}  (password from ${mb['password_env']})")
    w = cfg["send_window"]
    print(f"  send window    : {', '.join(w['days'])}  {w['start']}-{w['end']}  {w['timezone']}")
    print(f"  sequence steps : {len(cfg['sequence'])}")

    problems = []
    sid = str(cfg["sheet"]["id"])
    if "PUT_SPREADSHEET_ID" in sid:
        problems.append("sheet.id is still the placeholder")
    elif "/" in sid or "http" in sid:
        problems.append("sheet.id is a full URL - use only the part between /d/ and /edit")
    elif len(sid) < 40:
        problems.append(f"sheet.id is {len(sid)} chars; native Sheets are ~44 (Office upload?)")
    if "PUT FULL STREET ADDRESS" in cfg["footer"]:
        problems.append("footer has no physical address (CAN-SPAM requires one)")
    for mb in cfg["mailboxes"]:
        if not os.environ.get(mb["password_env"]):
            problems.append(f"env var {mb['password_env']} is not set")

    print()
    if problems:
        print("TODO before sending:")
        for p in problems:
            print(f"  ! {p}")
    else:
        print("no blockers found")


def cmd_preview(args, cfg):
    step = cfg["sequence"][0]
    company = args.company
    print("Subject:", step["subject"].replace("{{company}}", company))
    print()
    print(step["body"].replace("{{company}}", company).rstrip())
    print()
    print(cfg["footer"].strip())


def cmd_rows(args, cfg):
    rows = read_sheet(cfg)
    pending = [r for r in rows if not r["status"] and not r["notes"]]
    print(f"total rows: {len(rows)}   pending: {len(pending)}\n")
    for r in rows[:args.limit]:
        state = r["status"] or r["notes"] or "-"
        print(f"  {r['row']:4}  {r['company'][:30]:32} {r['email'][:34]:36} {state}")


def cmd_validate(args, cfg):
    rows = read_sheet(cfg)
    counts = {}
    skipped = []
    for r in rows:
        addr, tier, reason = validate(r)
        key = tier if addr else reason
        counts[key] = counts.get(key, 0) + 1
        if not addr:
            skipped.append((r["row"], r["company"], r["email"], reason))

    print(f"{len(rows)} rows\n")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:24} {v}")

    sendable = sum(v for k, v in counts.items() if k in ("named", "role"))
    print(f"\nsendable: {sendable}")

    print(f"\nfirst {args.limit} skipped:")
    for row, company, email, reason in skipped[:args.limit]:
        print(f"  {row:4}  {company[:28]:30} {email[:32]:34} {reason}")


def main():
    parser = argparse.ArgumentParser(description="RK Group cold outreach")
    parser.add_argument("--config", default=CONFIG_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="validate config.yaml").set_defaults(fn=cmd_check)

    p = sub.add_parser("preview", help="render the email for one company")
    p.add_argument("company", nargs="?", default="Account On Us")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("rows", help="read the sheet")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_rows)

    p = sub.add_parser("validate", help="classify every row, send nothing")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_validate)


    p = sub.add_parser("test", help="send one email to an address you control")
    p.add_argument("to")
    p.add_argument("--company", default="Test Accounting LLC")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_test)

    args = parser.parse_args()
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()