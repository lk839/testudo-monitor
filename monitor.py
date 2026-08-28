#!/usr/bin/env python3
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TERM = os.getenv("TESTUDO_TERM", "202608")
TIMEZONE = ZoneInfo("America/New_York")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
META_PATH = Path("meta.json")

REQUEST_TIMEOUT = 20
FULL_SCAN_DELAY_MIN = 3
FULL_SCAN_DELAY_MAX = 5

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150 Safari/537.36"
    )
})

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default

def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def course_url(course):
    dept = re.match(r"^[A-Z]+", course).group(0)
    return f"https://app.testudo.umd.edu/soc/{TERM}/{dept}/{course}"

def get_page(url):
    headers = {
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    }

    # Unique query parameter prevents an old cached Testudo page
    # from being reused by an intermediate cache/CDN.
    params = {
        "_fresh": str(int(time.time()))
    }

    r = SESSION.get(
        url,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )

    if r.status_code in (403, 429):
        raise RuntimeError(f"ACCESS_CONTROL_{r.status_code}")
    if 500 <= r.status_code <= 599:
        raise RuntimeError(f"SERVER_{r.status_code}")

    r.raise_for_status()
    return r.text

def parse_page(html, course=None):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    stamp = None
    m = re.search(
        r"Open Seats as of\s+(\d{1,2}/\d{1,2}/\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M)",
        text, re.I
    )
    if m:
        stamp = " ".join(m.group(1).split())

    # IMPORTANT: a Testudo URL can contain more than one related course.
    # Example: the BSCI331 page also contains BSCI331H. Both have section 0101.
    # Scope the text to the exact requested course before reading section seats.
    scoped_text = text
    if course:
        course = course.upper()

        # Locate the actual heading for the requested course. On Testudo the
        # heading is followed by the course title and "Syllabus Repository".
        start_re = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(course)}(?![A-Za-z0-9]).{{0,250}}?Syllabus Repository",
            re.I | re.S
        )
        sm = start_re.search(text)
        if sm:
            start = sm.start()
            end = len(text)

            # Related variants such as BSCI331H, BSCI222H, etc. may follow.
            # Stop before the first heading beginning with the requested course
            # code plus an alphabetic suffix.
            variant_re = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(course)}[A-Z]+(?![A-Za-z0-9]).{{0,250}}?Syllabus Repository",
                re.I | re.S
            )
            vm = variant_re.search(text, sm.end())
            if vm:
                end = vm.start()

            scoped_text = text[start:end]

    sections = {}

    rx = re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z0-9]{4})(?![A-Za-z0-9])"
        r"\s+"
        r"([A-Za-zÀ-ÖØ-öø-ÿ.'’\- ]{2,100}?)"
        r"\s+Seats\s*\(\s*Total:\s*\d+\s*,\s*Open:\s*(\d+)",
        re.I | re.S
    )

    for section, instructor, count in rx.findall(scoped_text):
        sections[section.upper()] = int(count)

    return stamp, sections

def send_ntfy(course, section, open_now):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC missing")
    msg = "SEAT AVAILABLE" if open_now else "SEAT NO LONGER AVAILABLE"
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode(),
        headers={"Title": f"{course} {section}", "Priority": "high", "Tags": "school"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()

def sections_to_check(item, found):
    wanted = [str(x).upper() for x in item.get("sections", ["*"])]
    excluded = {str(x).upper() for x in item.get("exclude_sections", [])}
    chosen = sorted(found.keys()) if "*" in wanted else wanted
    return [s for s in chosen if s not in excluded and s in found]

def scan_all(config, state, baseline=False):
    changed = False
    for i, item in enumerate(config["watch"]):
        course = item["course"].upper()
        html = get_page(course_url(course))
        _, found = parse_page(html, course)
        if not found:
            print(f"WARNING: no sections parsed for {course}", file=sys.stderr)
            continue

        for section in sections_to_check(item, found):
            key = f"{course}-{section}"
            open_now = found[section] > 0
            old = state.get(key)

            if old is None:
                print(f"BASELINE {key}: {'OPEN' if open_now else 'CLOSED'}")
                state[key] = open_now
                changed = True
            elif bool(old) != open_now:
                if not baseline:
                    send_ntfy(course, section, open_now)
                    print(f"ALERT {key}: {'OPEN' if open_now else 'CLOSED'}")
                state[key] = open_now
                changed = True

        if i < len(config["watch"]) - 1:
            time.sleep(random.uniform(FULL_SCAN_DELAY_MIN, FULL_SCAN_DELAY_MAX))

    return changed

def due_now(now, meta):
    md = (now.month, now.day)
    h = now.hour
    daytime = 7 <= h < 23

    if md > (9, 14):
        return False, None

    if (8, 28) <= md <= (8, 30):
        mins = 10 if daytime else 30
    elif (8, 31) <= md <= (9, 4):
        mins = 5 if daytime else 15
    elif (9, 5) <= md <= (9, 13):
        mins = 10 if daytime else 30
    elif md == (9, 14):
        mins = 5 if daytime else 15
    else:
        mins = 30

    last = meta.get("last_check_epoch")
    if last is None:
        return True, mins

    elapsed = now.timestamp() - float(last)
    return elapsed >= mins * 60 - 30, mins

def main():
    now = datetime.now(TIMEZONE)
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    meta = load_json(META_PATH, {})

    due, interval = due_now(now, meta)
    print(f"Local time: {now.isoformat()}")
    print(f"Target interval: {interval} min")

    if not due:
        print("Not due. Exiting without contacting Testudo.")
        return 0

    blocked_until = float(meta.get("blocked_until_epoch", 0))
    if now.timestamp() < blocked_until:
        print("Conservative pause still active. Exiting.")
        return 0

    sentinel = config.get("sentinel_course", "BSCI222").upper()

    try:
        stamp, _ = parse_page(get_page(course_url(sentinel)), sentinel)
        print(f"Sentinel snapshot: {stamp}")

        last_stamp = meta.get("last_snapshot")
        first_run = not bool(state)

        if first_run:
            print("First run: establishing baseline.")
            scan_all(config, state, baseline=True)
            save_json(STATE_PATH, state)
        elif stamp and last_stamp and stamp != last_stamp:
            print("Snapshot changed. Running full scan.")
            changed = scan_all(config, state, baseline=False)
            if changed:
                save_json(STATE_PATH, state)
        elif stamp is None:
            last_full = float(meta.get("last_full_scan_epoch", 0))
            if now.timestamp() - last_full >= 3600:
                print("No timestamp found; conservative fallback full scan.")
                changed = scan_all(config, state, baseline=False)
                if changed:
                    save_json(STATE_PATH, state)
                meta["last_full_scan_epoch"] = now.timestamp()
        else:
            print("Snapshot unchanged. No full scan.")

        if stamp:
            meta["last_snapshot"] = stamp
        meta["last_check_epoch"] = now.timestamp()
        meta.pop("blocked_until_epoch", None)
        save_json(META_PATH, meta)
        return 0

    except RuntimeError as e:
        text = str(e)
        print(f"ERROR: {text}", file=sys.stderr)
        meta["last_check_epoch"] = now.timestamp()
        meta["blocked_until_epoch"] = now.timestamp() + (7200 if text.startswith("ACCESS_CONTROL_") else 1800)
        save_json(META_PATH, meta)
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        meta["last_check_epoch"] = now.timestamp()
        meta["blocked_until_epoch"] = now.timestamp() + 1800
        save_json(META_PATH, meta)
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
