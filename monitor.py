#!/usr/bin/env python3
"""
Watches several internship repos and sends a phone push (ntfy or Pushover) for
every NEW role that appears across any of them.

Source types:
  * cvrve_json      -> SimplifyJobs & vanshb03. One listings.json feeds both a
                       repo's main and off-season README, so watching the JSON
                       covers those README pages at once.
  * markdown_table  -> speedyapply & jobright. No pollable JSON is published, so
                       we parse the README table and key each job by its apply /
                       job link (path only, so tracking params don't re-alert).

State (already-seen roles) lives in seen_ids.json so you're pinged once per role.
The first run is silent: it records what's already there. If a source fails on a
run, only that source is skipped and its state is preserved (no false flood).

Standard library only.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ---------------------------------------------------------------------------
# Sources. "key" namespaces each source's IDs so repos can't collide.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "key": "simplify2026",
        "type": "cvrve_json",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    },
    {
        "key": "vansh2027",
        "type": "cvrve_json",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    },
    {
        "key": "speedy2027swe",
        "type": "markdown_table",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
    },
    {
        "key": "jobright2026swe",
        "type": "markdown_table",
        "url": "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-Internship/master/README.md",
    },
]

STATE_FILE = os.environ.get("STATE_FILE", "seen_ids.json")
MAX_NOTIFS = int(os.environ.get("MAX_NOTIFS", "25"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

URL_RE = re.compile(r'https?://[^\s)"\'<>|]+')
# Infrastructure/badge domains that are never the actual apply/job link.
BADGE_DOMAINS = (
    "shields.io", "githubusercontent.com", "github.com", "discord.gg",
    "discord.com", "speedyapply.com", "buymeacoffee", "linkedin.com/company",
    "img.shields", "simplify.jobs",
)
# First-cell markers meaning "same company as the row above".
DITTO = {"", "↳", "->", "…", "..", "..."}


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "internship-monitor"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Parsers -> each returns list of {id, company, title, location, url}
# ---------------------------------------------------------------------------
def parse_cvrve_json(key, text):
    data = json.loads(text)
    items = []
    for l in data:
        if not (l.get("active") and l.get("is_visible") and l.get("id")):
            continue
        items.append({
            "id": f"{key}|{l['id']}",
            "company": l.get("company_name", "Unknown"),
            "title": l.get("title", "Internship"),
            "location": ", ".join(l.get("locations", []) or []),
            "url": l.get("url", ""),
        })
    return items


def _clean_cell(s):
    s = re.sub(r'<[^>]+>', '', s)                      # html tags
    s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)         # markdown images
    s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)     # markdown links -> text
    return s.replace('*', '').replace('`', '').strip()


def _key_from_url(u):
    """Stable dedup key: drop query string and fragment (keeps the job-id path)."""
    return u.split('?', 1)[0].split('#', 1)[0].rstrip('/').rstrip(').,')


def parse_markdown_table(key, text):
    items = []
    last_company = "Unknown"
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        if "🔒" in line:                               # closed/expired role
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        bare = "".join(cells)
        if not bare or set(bare) <= set("-: "):        # separator row
            continue
        low = " ".join(cells).lower()
        if "company" in low and any(w in low for w in
                                    ("location", "position", "role", "title", "application")):
            continue                                    # header row
        urls = URL_RE.findall(line)
        candidates = [u for u in urls if not any(b in u for b in BADGE_DOMAINS)]
        if not candidates:
            continue                                    # nav/badge row, not a job
        click_url = candidates[-1].rstrip(').,')        # last real link = the job
        key_url = _key_from_url(candidates[-1])

        texts = [t for t in (_clean_cell(c) for c in cells)
                 if t and not URL_RE.search(t)]
        company = texts[0] if texts else "↳"
        if company in DITTO or company == "↳":
            company = last_company
        else:
            last_company = company
        title = texts[1] if len(texts) > 1 else ""
        location = texts[2] if len(texts) > 2 else ""

        items.append({
            "id": f"{key}|{key_url}",
            "company": company,
            "title": title,
            "location": location,
            "url": click_url,
        })
    return items


PARSERS = {"cvrve_json": parse_cvrve_json, "markdown_table": parse_markdown_table}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_seen():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # first run


def save_seen(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify(title, message, url):
    if DRY_RUN:
        print(f"[DRY_RUN] {title} | {message} | {url}")
        return
    po_token, po_user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if po_token and po_user:
        data = urllib.parse.urlencode({
            "token": po_token, "user": po_user, "title": title,
            "message": message, "url": url, "url_title": "Apply now",
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json", data=data),
            timeout=30).read()
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("ERROR: set NTFY_TOPIC (or PUSHOVER_TOKEN + PUSHOVER_USER).", file=sys.stderr)
        sys.exit(1)
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "tags": ["briefcase"]}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Apply", "url": url}]
    urllib.request.urlopen(
        urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json"}),
        timeout=30).read()


def push_item(it):
    company = it["company"] or "Unknown"
    title = it["title"] or "New role"
    loc = it["location"] or "Location N/A"
    notify(f"{company} — {title}", loc, it["url"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    seen = load_seen()
    first_run = seen is None
    if seen is None:
        seen = set()

    all_new = []
    ok_current = {}

    for src in SOURCES:
        key = src["key"]
        try:
            text = http_get(src["url"])
            items = PARSERS[src["type"]](key, text)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"[{key}] fetch/parse failed, skipping this run: {e}", file=sys.stderr)
            continue
        cur_ids = {it["id"] for it in items}
        ok_current[key] = cur_ids
        print(f"[{key}] {len(items)} active listings.")
        if not first_run:
            all_new.extend(it for it in items if it["id"] not in seen)

    next_seen = set(seen)
    for key, cur_ids in ok_current.items():
        prefix = f"{key}|"
        next_seen = {i for i in next_seen if not i.startswith(prefix)} | cur_ids

    if first_run:
        save_seen(next_seen)
        print(f"First run: seeded {len(next_seen)} listings. No alerts sent.")
        return

    print(f"{len(all_new)} new listing(s) across all sources.")
    sent = 0
    for it in all_new:
        if sent >= MAX_NOTIFS:
            break
        try:
            push_item(it)
            sent += 1
            time.sleep(0.4)
        except urllib.error.URLError as e:
            print(f"Notify failed: {e}", file=sys.stderr)

    if len(all_new) > MAX_NOTIFS:
        notify(f"+{len(all_new) - sent} more new roles",
               "A large batch was posted. Open the repos to see them all.",
               "https://github.com/SimplifyJobs/Summer2026-Internships")

    save_seen(next_seen)


if __name__ == "__main__":
    main()
