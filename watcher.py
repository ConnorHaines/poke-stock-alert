"""Pokémon TCG 30th Celebration stock watcher.

Checks UK shops for sealed 30th Celebration products and sends a phone push
(via ntfy) when something comes into stock at a sensible price. It never adds
to basket or buys anything: it just tells you, and you buy it yourself.

Only uses the Python standard library, so it runs anywhere Python 3.9+ does.

    python watcher.py --once --dry-run      # one check, print instead of push
    python watcher.py --loop-minutes 55     # keep checking for 55 minutes
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# Pages that mean "a bot wall answered instead of the shop". We don't try to
# get past these: the shop is just reported as blocked and skipped.
BLOCK_MARKERS = ("Just a moment...", "Pardon Our Interruption", "Access Denied", "cf-challenge")
IN_STOCK = {"instock", "preorder", "presale", "limitedavailability", "onlineonly", "backorder"}


class Blocked(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"[{now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url, accept="text/html,application/json;q=0.9,*/*;q=0.8", timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-GB,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise Blocked(f"HTTP {e.code}") from None
        raise
    if len(body) < 20000 and any(m in body for m in BLOCK_MARKERS):
        raise Blocked("bot check page")
    return body


# ---------------------------------------------------------------- shop readers
# Each reader returns a list of dicts: title, url, price (float or None),
# available (bool), preorder (bool), extra (text used only for filtering).

def read_shopify_collection(shop):
    """Shopify's public /collections/<handle>/products.json feed."""
    host = shop["host"]
    out, seen = [], set()
    for handle in shop["collections"]:
        for page in range(1, shop.get("max_pages", 4) + 1):
            url = f"https://{host}/collections/{handle}/products.json?limit=250&page={page}"
            products = json.loads(fetch(url, accept="application/json"))["products"]
            for p in products:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                variants = p.get("variants") or []
                live = [v for v in variants if v.get("available")]
                prices = [float(v["price"]) for v in (live or variants) if v.get("price")]
                tags = p.get("tags") or []
                tags = tags if isinstance(tags, list) else str(tags).split(",")
                out.append({
                    "title": p["title"],
                    "url": f"https://{host}/products/{p['handle']}",
                    "price": min(prices) if prices else None,
                    "available": bool(live),
                    "preorder": bool(re.search(r"pre-?order", p["title"] + " " + " ".join(tags), re.I)),
                    "extra": f"{p.get('product_type', '')} {p['handle']}",
                })
            if len(products) < 30:  # some shops cap pages at 30, so only stop on a short page
                break
            time.sleep(random.uniform(1.0, 2.0))
    return out


def read_shopify_search(shop):
    """Shopify's predictive search, for shops without a 30th collection."""
    host = shop["host"]
    out, seen = [], set()
    for q in shop["queries"]:
        url = (f"https://{host}/search/suggest.json?q={urllib.parse.quote(q)}"
               "&resources[type]=product&resources[limit]=10")
        data = json.loads(fetch(url, accept="application/json"))
        for p in data["resources"]["results"].get("products", []):
            link = f"https://{host}/products/{p['handle']}"
            if link in seen:
                continue
            seen.add(link)
            price = p.get("price")
            out.append({
                "title": html.unescape(p["title"]),
                "url": link,
                "price": float(price) if price not in (None, "") else None,
                "available": bool(p.get("available")),
                "preorder": bool(re.search(r"pre-?order", p["title"] + " " + " ".join(p.get("tags") or []), re.I)),
                "extra": f"{p.get('type', '')} {p['handle']}",
            })
        time.sleep(random.uniform(1.0, 2.0))
    return out


def read_jsonld_pages(shop):
    """Product pages that publish schema.org Product/Offer data (most shops do)."""
    out = []
    for i, url in enumerate(shop["urls"]):
        if i:
            time.sleep(random.uniform(1.5, 3.0))
        body = fetch(url)
        product = None
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body, re.S):
            try:
                data = json.loads(m.group(1).strip(), strict=False)
            except ValueError:
                continue
            for node in data if isinstance(data, list) else data.get("@graph", [data]):
                if isinstance(node, dict) and node.get("@type") == "Product":
                    product = node
                    break
            if product:
                break
        if not product:
            log(f"  {shop['name']}: no product data on {url}")
            continue
        offers = product.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        states = [str(o.get("availability", "")).rsplit("/", 1)[-1].lower() for o in offers]
        prices = [float(o["price"]) for o in offers if str(o.get("price", "")).replace(".", "", 1).isdigit()]
        out.append({
            "title": html.unescape(product.get("name", url)),
            "url": url,
            "price": min(prices) if prices else None,
            "available": any(s in IN_STOCK for s in states),
            "preorder": any(s in ("preorder", "presale") for s in states),
            "extra": url,
        })
    return out


READERS = {
    "shopify_collection": read_shopify_collection,
    "shopify_search": read_shopify_search,
    "jsonld_pages": read_jsonld_pages,
}


# ---------------------------------------------------------------- matching

def classify(item, cfg):
    """Return the product rule this listing belongs to, or None to ignore it."""
    f = cfg["filters"]
    title = item["title"]
    haystack = f"{title} {item['extra']}"
    if not re.search(f["must_match"], title, re.I):
        return None
    if not re.search(f["must_be_pokemon"], haystack, re.I):
        return None
    if re.search(f["exclude"], haystack, re.I):
        return None
    for rule in cfg["products"]:
        if re.search(rule["match"], title, re.I):
            # Far below RRP means it's an insert or empty box, not the product.
            floor = (rule.get("max_price") or 0) * cfg.get("min_price_fraction", 0)
            if item["price"] and item["price"] < floor:
                return None
            return rule
    return None


def price_ok(item, rule):
    cap = rule.get("max_price")
    return cap is None or item["price"] is None or item["price"] <= 0 or item["price"] <= cap


# ---------------------------------------------------------------- alerts

def notify(cfg, title, message, url=None, priority=4, tags=None, dry_run=False):
    topic = os.environ.get("NTFY_TOPIC") or cfg["ntfy"].get("topic")
    if dry_run or not topic:
        log(f"  PUSH{' (dry run)' if dry_run else ' (no NTFY_TOPIC set)'}: {title} | {message} | {url or ''}")
        return
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open shop page", "url": url, "clear": True}]
    req = urllib.request.Request(
        cfg["ntfy"].get("server", "https://ntfy.sh"),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return
        except Exception as e:
            log(f"  ntfy send failed ({e}), retrying")
            time.sleep(3 * (attempt + 1))


def fmt_price(p):
    return f"£{p:.2f}" if p else "price not shown"


# ---------------------------------------------------------------- state + git

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def git_sync(message):
    """Commit state.json so the next GitHub Actions run remembers what it saw."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    git("add", "state.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", message)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git("pull", "--rebase", "--autostash", "-X", "theirs", "origin", branch)
    res = git("push", "origin", f"HEAD:{branch}")
    log("  state saved to repo" if res.returncode == 0 else f"  git push failed: {res.stderr.strip()[:200]}")


# ---------------------------------------------------------------- main loop

def check_shop(shop, cfg):
    items = READERS[shop["type"]](shop)
    tracked = []
    for item in items:
        rule = classify(item, cfg)
        if rule:
            item["rule"] = rule
            tracked.append(item)
    return tracked


def apply_result(cfg, state, shop, tracked, error, dry_run):
    """Fold one shop's check into state. Returns (changed, new alerts)."""
    changed = False
    alerts = []
    health = state["shops"].setdefault(shop["name"], {"fails": 0, "flagged": False})
    if error:
        health["fails"] += 1
        health["last_error"] = f"{type(error).__name__}: {error}"[:200]
        log(f"{shop['name']}: FAILED ({health['last_error']}) x{health['fails']}")
        if health["fails"] >= cfg["warn_after_failures"] and not health["flagged"]:
            health["flagged"] = True
            changed = True
            notify(cfg, f"Can't check {shop['name']}",
                   f"{health['last_error']}. Skipping it until it answers again. "
                   "Check this shop yourself for now.", priority=2, tags=["warning"], dry_run=dry_run)
        return changed, alerts

    if health["flagged"]:
        notify(cfg, f"{shop['name']} is back", "Stock checks for this shop are working again.",
               priority=2, tags=["white_check_mark"], dry_run=dry_run)
    if health["fails"] or health["flagged"]:
        changed = True
    health.update(fails=0, flagged=False)
    health.pop("last_error", None)

    log(f"{shop['name']}: {len(tracked)} tracked, {sum(i['available'] for i in tracked)} in stock")
    for item in tracked:
        prev = state["items"].get(item["url"], {})
        alertable = item["available"] and price_ok(item, item["rule"])
        record = {
            "shop": shop["name"], "title": item["title"], "product": item["rule"]["name"],
            "price": item["price"], "available": item["available"], "alertable": alertable,
            "last_change": prev.get("last_change"),
        }
        if prev.get("available") != item["available"] or prev.get("price") != item["price"]:
            record["last_change"] = now().isoformat(timespec="seconds")
        if alertable and not prev.get("alertable"):
            alerts.append((shop, item))
        if record != prev:
            state["items"][item["url"]] = record
            changed = True
    return changed, alerts


def send_alert(cfg, shop, item, dry_run):
    rule = item["rule"]
    kind = "PRE-ORDER" if item["preorder"] else "IN STOCK"
    cap = f" (your max {fmt_price(rule['max_price'])})" if rule.get("max_price") else ""
    notify(cfg, f"{kind}: {rule['name']} at {shop['name']}",
           f"{fmt_price(item['price'])}{cap}\n{item['title']}\nTap to open. Be quick!",
           url=item["url"], priority=rule.get("priority", cfg["ntfy"].get("priority", 4)),
           tags=["rotating_light"], dry_run=dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one check then exit")
    ap.add_argument("--loop-minutes", type=float, default=0, help="keep checking for this many minutes")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--git-commit", action="store_true", help="commit state.json after changes (GitHub Actions)")
    ap.add_argument("--only", help="only check shops whose name contains this text")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    first_run = not state.get("items")
    state.setdefault("items", {})
    state.setdefault("shops", {})

    shops = [s for s in cfg["shops"] if s.get("enabled", True)
             and (not args.only or args.only.lower() in s["name"].lower())]
    deadline = time.time() + args.loop_minutes * 60
    lock = threading.Lock()
    last_commit = [0.0]

    def check(shop):
        try:
            tracked, error = check_shop(shop, cfg), None
        except Exception as e:
            tracked, error = None, e
        with lock:
            changed, alerts = apply_result(cfg, state, shop, tracked, error, args.dry_run)
            if changed:
                state["updated"] = now().isoformat(timespec="seconds")
                save_state(state)
                if args.git_commit and time.time() - last_commit[0] > 300:
                    git_sync("Update stock state")
                    last_commit[0] = time.time()
        return alerts

    # First pass checks every shop together, so a brand-new setup can send one
    # "here's what's in stock now" summary instead of a burst of alerts.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(shops)) as pool:
        found = [a for alerts in pool.map(check, shops) for a in alerts]
    if first_run:
        lines = [f"{i['rule']['name']} at {s['name']} ({fmt_price(i['price'])})" for s, i in found]
        notify(cfg, "Pokémon 30th stock watcher is running",
               f"Watching {len(state['items'])} products across {len(shops)} shops. "
               + ("In stock right now: " + "; ".join(lines[:10]) if lines
                  else "Nothing in stock at a fair price right now."),
               priority=3, tags=["eyes"], dry_run=args.dry_run)
    else:
        for shop, item in found:
            send_alert(cfg, shop, item, args.dry_run)

    # After that each shop runs on its own timer, so a slow shop never holds up the others.
    def shop_loop(shop):
        while True:
            wait = shop.get("interval_seconds", cfg["interval_seconds"]) * random.uniform(0.8, 1.2)
            if time.time() + wait > deadline:
                return
            time.sleep(wait)
            for s, item in check(shop):
                send_alert(cfg, s, item, args.dry_run)

    if not args.once:
        threads = [threading.Thread(target=shop_loop, args=(s,), daemon=True) for s in shops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if args.git_commit:
        git_sync("Update stock state")


if __name__ == "__main__":
    sys.exit(main())
