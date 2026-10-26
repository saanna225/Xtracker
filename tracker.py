#!/usr/bin/env python3
"""Track repost/quote/like/reply/view counts and lists for an X post.

Usage:
    python tracker.py [post_url] [--counts-only]

Logs in once via a persistent Chromium profile at ./x-profile, then reads
the engagement numbers off the post and (unless --counts-only) scrolls the
/retweets and /quotes pages to collect who reposted/quoted it.
"""

import argparse
import csv
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "x-profile"
TRACKER_CSV = BASE_DIR / "tracker.csv"
QUOTES_CSV = BASE_DIR / "quotes.csv"
REPOSTS_CSV = BASE_DIR / "reposts.csv"

DEFAULT_URL = "https://x.com/shivanipod/status/2114764275533435194"

TRACKER_FIELDS = [
    "timestamp", "url", "replies", "reposts", "quotes", "likes", "views",
    "reposts_collected", "quotes_collected",
]

IDLE_ROUNDS_TO_STOP = 3
MAX_SCROLL_ROUNDS = 400


def warn(msg):
    print(f"[warn] {msg}", file=sys.stderr)


def parse_count(raw):
    """Turn '12,345' / '1.2K' / '3.4M' into an int. Returns None if unparseable."""
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMB]?)$", s, re.IGNORECASE)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return None
    num = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(num * mult)


def normalize_url(url):
    url = url.split("?")[0].split("#")[0].rstrip("/")
    return url


def ensure_logged_in(page):
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(
            '[data-testid="SideNav_AccountSwitcher_Button"]', timeout=5000
        )
        return
    except PWTimeoutError:
        pass

    print("Not logged in to X — opening login page. Please log in in the browser window...")
    page.goto("https://x.com/login", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(
            '[data-testid="SideNav_AccountSwitcher_Button"]', timeout=300_000
        )
    except PWTimeoutError:
        warn("Timed out waiting for login (5 min). Continuing anyway, but the next step may fail.")
        return
    print("Logged in, continuing...")


def fallback_button_count(article, testid):
    try:
        btn = article.locator(f'[data-testid="{testid}"]').first
        if btn.count() == 0:
            return None
        label = btn.get_attribute("aria-label") or ""
        m = re.search(r"([\d,.]+[KMB]?)", label, re.IGNORECASE)
        if not m:
            inner = btn.inner_text()
            m = re.search(r"([\d,.]+[KMB]?)", inner, re.IGNORECASE)
        return parse_count(m.group(1)) if m else None
    except Exception:
        return None


def scrape_counts(page):
    counts = {"replies": None, "reposts": None, "quotes": None, "likes": None, "views": None}
    try:
        article = page.locator('article[data-testid="tweet"]').first
        article.wait_for(timeout=20_000)
    except PWTimeoutError:
        warn("Could not find the main tweet (article[data-testid='tweet']) — X's layout may have changed.")
        return counts

    text = article.inner_text()
    pattern = re.compile(
        r"([\d,.]+[KMB]?)\s*\n?\s*(Repl(?:y|ies)|Reposts?|Quotes?|Likes?|Bookmarks?|Views?)",
        re.IGNORECASE,
    )
    for num, label in pattern.findall(text):
        label_l = label.lower()
        value = parse_count(num)
        if label_l.startswith("repl"):
            counts["replies"] = value
        elif label_l.startswith("repost"):
            counts["reposts"] = value
        elif label_l.startswith("quote"):
            counts["quotes"] = value
        elif label_l.startswith("like"):
            counts["likes"] = value
        elif label_l.startswith("view"):
            counts["views"] = value

    if counts["replies"] is None:
        counts["replies"] = fallback_button_count(article, "reply")
    if counts["reposts"] is None:
        counts["reposts"] = fallback_button_count(article, "retweet")
    if counts["likes"] is None:
        counts["likes"] = fallback_button_count(article, "like")

    missing = [k for k, v in counts.items() if v is None]
    if missing:
        warn(f"Could not read these counts: {', '.join(missing)} (selector may need updating)")
    return counts


def random_scroll(page):
    page.mouse.wheel(0, random.randint(600, 1300))
    time.sleep(random.uniform(1, 3))


def scroll_collect(page, url, extract_fn, wait_selector, empty_message):
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        warn(f"Failed to open {url}: {e}")
        return []

    try:
        page.wait_for_selector(wait_selector, timeout=15_000)
    except PWTimeoutError:
        warn(f"{empty_message} (no items found, or selector '{wait_selector}' may have broken)")
        return []

    seen = {}
    idle_rounds = 0
    rounds = 0
    while idle_rounds < IDLE_ROUNDS_TO_STOP and rounds < MAX_SCROLL_ROUNDS:
        rounds += 1
        new_count = 0
        try:
            for item in extract_fn(page):
                key = item["_key"]
                if key not in seen:
                    seen[key] = item
                    new_count += 1
        except Exception as e:
            warn(f"Error while extracting items from {url}: {e}")
            break

        if new_count == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0
        random_scroll(page)

    for item in seen.values():
        item.pop("_key", None)
    return list(seen.values())


def extract_repost_cells(page):
    results = []
    cells = page.locator('[data-testid="UserCell"]')
    count = cells.count()
    for i in range(count):
        cell = cells.nth(i)
        try:
            text = cell.inner_text()
        except Exception:
            continue
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        handle = next((l for l in lines if l.startswith("@")), "")
        name = lines[0] if lines else ""
        if not handle:
            continue
        results.append({"handle": handle, "name": name, "_key": handle})
    return results


def extract_quote_articles(page):
    results = []
    articles = page.locator('article[data-testid="tweet"]')
    count = articles.count()
    for i in range(count):
        art = articles.nth(i)
        try:
            name_block = art.locator('[data-testid="User-Name"]').first
            name_text = name_block.inner_text() if name_block.count() else ""
        except Exception:
            name_text = ""
        lines = [l.strip() for l in name_text.split("\n") if l.strip()]
        handle = next((l for l in lines if l.startswith("@")), "")
        name = lines[0] if lines else ""
        try:
            text_block = art.locator('[data-testid="tweetText"]').first
            quote_text = text_block.inner_text() if text_block.count() else ""
        except Exception:
            quote_text = ""
        if not handle:
            continue
        key = handle + "|" + quote_text[:120]
        results.append({"handle": handle, "name": name, "text": quote_text, "_key": key})
    return results


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_last_row(path):
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


def append_tracker_row(path, row):
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def print_summary(prev, row):
    print("\n--- Current counts ---")
    for field in ["replies", "reposts", "quotes", "likes", "views"]:
        print(f"  {field}: {row[field]}")
    if row["reposts_collected"] != "":
        print(f"  reposts_collected: {row['reposts_collected']}")
    if row["quotes_collected"] != "":
        print(f"  quotes_collected: {row['quotes_collected']}")

    if prev is None:
        print("\n(no previous run to compare against)")
        return

    print("\n--- Change since last run ---")
    for field in ["replies", "reposts", "quotes", "likes", "views", "reposts_collected", "quotes_collected"]:
        old_raw, new_raw = prev.get(field, ""), row.get(field, "")
        if old_raw in (None, "") or new_raw in (None, ""):
            continue
        try:
            diff = int(new_raw) - int(old_raw)
        except ValueError:
            continue
        sign = "+" if diff >= 0 else ""
        print(f"  {field}: {sign}{diff}")


def parse_args():
    parser = argparse.ArgumentParser(description="Track reposts/quotes for an X post.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="X post URL")
    parser.add_argument(
        "--counts-only", action="store_true",
        help="Skip scrolling /retweets and /quotes; just read the summary counts.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    url = normalize_url(args.url)
    parts = urlsplit(url)
    if "/status/" not in parts.path:
        print(f"'{url}' doesn't look like a post URL (expected .../status/<id>)", file=sys.stderr)
        sys.exit(1)

    retweets_url = url + "/retweets"
    quotes_url = url + "/quotes"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()

        ensure_logged_in(page)

        print(f"Opening post: {url}")
        page.goto(url, wait_until="domcontentloaded")
        counts = scrape_counts(page)

        reposts, quotes = [], []
        if not args.counts_only:
            print("Collecting reposts (scrolling /retweets)...")
            reposts = scroll_collect(
                page, retweets_url, extract_repost_cells,
                '[data-testid="UserCell"]', "No reposts found",
            )
            print(f"  collected {len(reposts)} reposts")

            print("Collecting quotes (scrolling /quotes)...")
            quotes = scroll_collect(
                page, quotes_url, extract_quote_articles,
                'article[data-testid="tweet"]', "No quotes found",
            )
            print(f"  collected {len(quotes)} quotes")

            write_csv(REPOSTS_CSV, reposts, ["handle", "name"])
            write_csv(QUOTES_CSV, quotes, ["handle", "name", "text"])

        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "url": url,
            "replies": counts["replies"] if counts["replies"] is not None else "",
            "reposts": counts["reposts"] if counts["reposts"] is not None else "",
            "quotes": counts["quotes"] if counts["quotes"] is not None else "",
            "likes": counts["likes"] if counts["likes"] is not None else "",
            "views": counts["views"] if counts["views"] is not None else "",
            "reposts_collected": len(reposts) if not args.counts_only else "",
            "quotes_collected": len(quotes) if not args.counts_only else "",
        }

        prev = read_last_row(TRACKER_CSV)
        append_tracker_row(TRACKER_CSV, row)
        print_summary(prev, row)

        context.close()


if __name__ == "__main__":
    main()
