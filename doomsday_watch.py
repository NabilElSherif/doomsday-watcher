#!/usr/bin/env python3
"""
Avengers: Doomsday ticket watcher - Scene Cinemas District 5
------------------------------------------------------------
Plain HTTP polling, no AI, no tokens, no third-party packages (Python 3.8+).

What it does every ~2 minutes:
  1. Loads the "Now Showing" and "Coming Soon" pages and looks for Doomsday.
  2. Opens any Doomsday movie page it finds (plus a few likely URLs) and looks
     for showtime links (<branch>.scenecinemas.com/showtime-...).
  3. Alerts you:
       - heads-up when Doomsday first appears on the site
       - URGENT when showtimes (bookable times) appear, and again if new ones are added
     via a phone push (ntfy app), a desktop notification, a beep, and it opens
     the booking page in your browser.

Usage:
  python3 doomsday_watch.py               # run forever
  python3 doomsday_watch.py --test        # send a test notification and exit
  python3 doomsday_watch.py --once        # do a single check and print the result
  python3 doomsday_watch.py --ci          # GitHub Actions mode: checks every minute for ~20 min
"""

import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

# ----------------------------------------------------------------- settings --
# Phone push: install the free "ntfy" app (iOS / Android), tap +, and subscribe
# to this exact topic name. Change it to something only you know if you like.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"   # running on GitHub Actions?
# On GitHub the topic comes from a repository secret, so it isn't public.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or ("" if IN_CI else "doomsday-d5-k7q2xw9m")

CHECK_EVERY_SECONDS = 120        # base interval
JITTER_SECONDS = 20              # +/- random wobble so requests aren't robotic
OPEN_BROWSER_ON_BOOKING = True   # open the movie page automatically when bookable

# Scene branches to watch (same site layout). Remove one you don't care about.
BRANCHES = {
    "District 5": "https://district5.scenecinemas.com",
    # "CFC (Cairo Festival City)": "https://cfc.scenecinemas.com",  # uncomment to also watch CFC
}
BASE = BRANCHES["District 5"]  # used for the --test link
# Likely URLs for the film's page, tried directly in case it isn't linked yet.
GUESSED_SLUGS = ["avengers-doomsday", "avengers-dooms-day", "avengers-5-doomsday"]

KEYWORD = re.compile(r"dooms\s*-?\s*day", re.I)
MOVIE_LINK = re.compile(r"/movie-details/([A-Za-z0-9_\-]+)\.html")
SHOWTIME_LINK = re.compile(r"/showtime-([0-9a-fA-F]{16,40})")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".doomsday_state.json")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ------------------------------------------------------------------ helpers --
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url):
    """Return (final_url, html) or (None, None) on 404. Raises on other errors."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.geturl(), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None, None
        raise


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ------------------------------------------------------------ notifications --
def push_phone(title, message, url=None, urgent=False):
    if not NTFY_TOPIC:
        return
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": "urgent" if urgent else "default",
        "Tags": "rotating_light,movie_camera" if urgent else "movie_camera",
    }
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Book now, {url}"
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                 data=message.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"  (phone push failed: {e})")


def notify_desktop(title, message):
    system = platform.system()
    try:
        if system == "Darwin":
            safe = lambda s: s.replace('"', "'")
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{safe(message)}" with title "{safe(title)}" sound name "Glass"'])
        elif system == "Windows":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "[System.Media.SystemSounds]::Exclamation.Play();"
                  f"[System.Windows.Forms.MessageBox]::Show('{message.replace(chr(39), '')}',"
                  f"'{title.replace(chr(39), '')}')")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        else:
            subprocess.Popen(["notify-send", "-u", "critical", title, message])
    except Exception as e:
        log(f"  (desktop notification failed: {e})")
    print("\a", end="", flush=True)  # terminal beep


def alert(title, message, url=None, urgent=False):
    log(f"*** {title}: {message} {url or ''}")
    push_phone(title, message, url, urgent)
    if IN_CI:
        return  # no desktop or browser on GitHub's servers
    notify_desktop(title, message)
    if urgent and url and OPEN_BROWSER_ON_BOOKING:
        webbrowser.open(url)


# -------------------------------------------------------------------- check --
def check(base):
    """Check one branch. Return dict: listed (bool), movie_url (str|None), showtimes (list of URLs)."""
    listed = False
    slugs = set(GUESSED_SLUGS)

    for page in (base + "/home", base + "/next_releasing"):
        _, html = fetch(page)
        if page.endswith("/home") and (not html or "movie-details" not in html):
            raise RuntimeError(f"{page} didn't look like the cinema homepage (site down or blocking us?)")
        if not html:
            continue
        if KEYWORD.search(html):
            listed = True
        for slug in MOVIE_LINK.findall(html):
            if KEYWORD.search(slug.replace("-", " ")) or "doomsday" in slug.lower():
                slugs.add(slug)

    movie_url, showtimes = None, []
    for slug in sorted(slugs):
        url = f"{base}/movie-details/{slug}.html"
        final_url, html = fetch(url)
        # Ignore 404s, redirects back to the homepage, and pages not about Doomsday.
        if not html or "movie-details" not in (final_url or "") or not KEYWORD.search(html):
            continue
        listed = True
        movie_url = url
        ids = sorted(set(SHOWTIME_LINK.findall(html)))
        if ids:
            showtimes = [f"{base}/showtime-{i}" for i in ids]
            break
    return {"listed": listed, "movie_url": movie_url, "showtimes": showtimes}


def run_once_branch(name, base, state):
    st = state.setdefault(name, {"listed": False, "showtimes": []})
    result = check(base)
    n = len(result["showtimes"])
    log(f"{name}: listed={result['listed']}  showtimes={n}  page={result['movie_url'] or '-'}")

    if result["showtimes"]:
        new = set(result["showtimes"]) - set(st["showtimes"])
        if new:
            first_time = not st["showtimes"]
            alert(f"DOOMSDAY BOOKING OPEN - {name}" if first_time else f"More Doomsday showtimes - {name}",
                  f"{n} showtime(s) at Scene {name}. Book now!",
                  result["movie_url"] or result["showtimes"][0], urgent=True)
            st["showtimes"] = sorted(set(st["showtimes"]) | set(result["showtimes"]))
    elif result["listed"] and not st["listed"]:
        alert(f"Doomsday is on the Scene {name} site",
              "Listed but no bookable showtimes yet. Still watching.",
              result["movie_url"] or base + "/next_releasing")

    st["listed"] = st["listed"] or result["listed"]
    return result


def run_once(state):
    errors = []
    for name, base in BRANCHES.items():
        try:
            run_once_branch(name, base, state)
        except Exception as e:
            errors.append(f"{name}: {e}")
    save_state(state)
    if len(errors) == len(BRANCHES):
        raise RuntimeError("; ".join(errors))
    for err in errors:
        log(f"check failed for {err}")


def main():
    args = sys.argv[1:]
    if "--test" in args:
        alert("Doomsday watcher test", "If you see this, notifications work.", BASE)
        return
    if "--ci" in args:
        # GitHub Actions: each run keeps checking about once a minute for ~20 minutes,
        # then the next scheduled run takes over. State is saved to the repo between runs.
        if not NTFY_TOPIC:
            log("WARNING: NTFY_TOPIC secret is not set - you won't get phone alerts!")
        run_minutes = float(os.environ.get("CI_RUN_MINUTES", "20"))
        every = float(os.environ.get("CI_CHECK_SECONDS", "60"))
        deadline = time.time() + run_minutes * 60
        state = load_state()
        while True:
            try:
                run_once(state)
                state["failures"] = 0
            except Exception as e:
                state["failures"] = state.get("failures", 0) + 1
                log(f"check failed ({state['failures']} in a row): {e}")
                if state["failures"] == 15:  # about 15 minutes of failures
                    alert("Doomsday watcher can't reach the site",
                          "No successful check for about 15 minutes. Open the Actions tab to see why.")
            save_state(state)
            wait = max(10.0, every + random.uniform(-10, 10))
            if time.time() + wait > deadline:
                break
            time.sleep(wait)
        return
    if "--once" in args:
        for name, base in BRANCHES.items():
            print(name, json.dumps(check(base), indent=2))
        return

    log(f"Watching Scene ({', '.join(BRANCHES)}) for Avengers: Doomsday every ~{CHECK_EVERY_SECONDS // 60} min. "
        f"Phone topic: ntfy.sh/{NTFY_TOPIC}  (Ctrl+C to stop)")
    state = load_state()
    failures = 0
    while True:
        try:
            run_once(state)
            failures = 0
        except Exception as e:
            failures += 1
            log(f"check failed ({failures} in a row): {e}")
            if failures == 5:
                alert("Doomsday watcher can't reach the site",
                      "5 failed checks in a row. Check your internet or the script.")
        delay = CHECK_EVERY_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        if failures:
            delay = min(delay * (1 + failures), 900)  # back off while failing, max 15 min
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
