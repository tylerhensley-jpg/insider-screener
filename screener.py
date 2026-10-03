#!/usr/bin/env python3
"""Insider-trade screener.

Reads every SEC Form 4 filed since the last run, keeps open-market buys (code P)
and sales (code S) that clear the thresholds in config.json, looks up each
insider's past open-market trades to score their timing, and writes a static
digest page to site/.

Stdlib only. SEC asks for a User-Agent with a contact email: set SEC_USER_AGENT
(env) or "sec_user_agent" in config.json, e.g. "Jane Doe jane@example.com".
"""
import gzip
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OWNERS = os.path.join(DATA, "owners")
SITE = os.path.join(ROOT, "site")

with open(os.path.join(ROOT, "config.json")) as f:
    CFG = json.load(f)

UA = os.environ.get("SEC_USER_AGENT") or CFG.get("sec_user_agent")
if not UA or "@" not in UA:
    sys.exit("Set SEC_USER_AGENT (or sec_user_agent in config.json) to 'Name email@domain'.")

TODAY = datetime.now(timezone.utc).date()


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- http

class Limiter:
    """Spaces requests evenly. SEC's fair-access cap is 10/s; stay under it."""

    def __init__(self, per_sec):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        time.sleep(max(0.0, slot - now))


SEC_LIMIT = Limiter(8)


def http_get(url, sec=True, retries=4):
    headers = {"Accept-Encoding": "gzip"}
    headers["User-Agent"] = UA if sec else "Mozilla/5.0 (insider-screener)"
    for attempt in range(retries):
        if sec:
            SEC_LIMIT.wait()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 403 and sec and attempt == 0:
                log("SEC returned 403; check SEC_USER_AGENT", url)
            if e.code in (403, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            return None
        except Exception:
            time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------- form 4 parsing

def _text(el, path):
    node = el.find(path)
    if node is None:
        return None
    val = node.find("value")
    txt = (val.text if val is not None else node.text) or ""
    txt = txt.strip()
    return txt or None


def _num(el, path):
    t = _text(el, path)
    try:
        return float(t.replace(",", "")) if t else None
    except ValueError:
        return None


def _true(t):
    return (t or "").strip().lower() in ("1", "true")


PLAN_RE = re.compile(r"10b5-?1", re.I)
NOT_PLAN_RE = re.compile(r"not\s+(?:made\s+|effected\s+|executed\s+)?pursuant\s+to\s+(?:a\s+)?(?:rule\s+)?10b5", re.I)


def parse_form4(raw):
    """Full-submission .txt -> dict, or None if it isn't a parseable Form 4."""
    if not raw:
        return None
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", raw, re.S)
    if not m:
        return None
    try:
        doc = ET.fromstring(m.group(1).strip().encode("utf-8"))
    except ET.ParseError:
        return None
    if (_text(doc, "documentType") or "") != "4":
        return None

    owner = doc.find("reportingOwner")
    if owner is None:
        return None
    rel = owner.find("reportingOwnerRelationship")
    rel = rel if rel is not None else ET.Element("x")

    footnotes = " ".join((fn.text or "") for fn in doc.iter("footnote"))
    plan = _true(_text(doc, "aff10b5One"))
    if not plan and PLAN_RE.search(footnotes) and not NOT_PLAN_RE.search(footnotes):
        plan = True

    rows = []
    buckets = {}  # (direct/indirect, nature) -> shares held after the last row in that bucket
    for el in doc.iter():
        if el.tag not in ("nonDerivativeTransaction", "nonDerivativeHolding"):
            continue
        nature = (_text(el, "ownershipNature/directOrIndirectOwnership") or "D",
                  _text(el, "ownershipNature/natureOfOwnership") or "")
        after = _num(el, "postTransactionAmounts/sharesOwnedFollowingTransaction")
        if after is not None:
            buckets[nature] = after
        if el.tag == "nonDerivativeTransaction":
            rows.append({
                "date": (_text(el, "transactionDate") or "")[:10],
                "code": _text(el, "transactionCoding/transactionCode"),
                "shares": _num(el, "transactionAmounts/transactionShares") or 0.0,
                "price": _num(el, "transactionAmounts/transactionPricePerShare"),
                "ad": _text(el, "transactionAmounts/transactionAcquiredDisposedCode"),
            })

    return {
        "issuer_cik": (_text(doc, "issuer/issuerCik") or "").lstrip("0"),
        "issuer": _text(doc, "issuer/issuerName") or "",
        "ticker": (_text(doc, "issuer/issuerTradingSymbol") or "").upper().strip(),
        "owner_cik": (_text(owner, "reportingOwnerId/rptOwnerCik") or "").lstrip("0"),
        "owner": _text(owner, "reportingOwnerId/rptOwnerName") or "",
        "is_director": _true(_text(rel, "isDirector")),
        "is_officer": _true(_text(rel, "isOfficer")),
        "is_ten_pct": _true(_text(rel, "isTenPercentOwner")),
        "title": _text(rel, "officerTitle") or "",
        "plan": plan,
        "rows": rows,
        "held_after": sum(buckets.values()),
    }


def summarize(f4, acc):
    """Collapse a filing's P and S rows into at most one buy and one sell move."""
    moves = []
    for code, side in (("P", "buy"), ("S", "sell")):
        rows = [r for r in f4["rows"] if r["code"] == code and r["shares"] > 0 and r["price"]]
        if not rows:
            continue
        shares = sum(r["shares"] for r in rows)
        value = sum(r["shares"] * r["price"] for r in rows)
        held = f4["held_after"]
        if side == "sell":
            pct = shares / (shares + held) if shares + held > 0 else None
        else:
            pct = shares / (held - shares) if held - shares > 0 else None
        dates = sorted(r["date"] for r in rows if r["date"])
        moves.append({
            "acc": acc,
            "side": side,
            "shares": round(shares),
            "value": round(value),
            "price": round(value / shares, 4),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "pct": round(pct, 4) if pct is not None else None,
            "held_after": round(held),
            "plan": f4["plan"] if side == "sell" else False,
            "issuer_cik": f4["issuer_cik"],
            "issuer": f4["issuer"],
            "ticker": f4["ticker"],
        })
    return moves


def filing_url(cik, acc):
    return "https://www.sec.gov/Archives/edgar/data/%s/%s/%s-index.htm" % (cik, acc.replace("-", ""), acc)


def role_of(f4):
    t = f4["title"]
    if f4["is_officer"] and t:
        return t
    if f4["is_officer"]:
        return "Officer"
    if f4["is_director"]:
        return "Director"
    if f4["is_ten_pct"]:
        return "10% owner"
    return "Insider"


def role_weight(f4):
    t = f4["title"].lower()
    if re.search(r"\bceo\b|chief executive|\bpresident\b|chair", t):
        return 2.0
    if re.search(r"\bcfo\b|chief financial|\bcoo\b|chief operating", t):
        return 1.6
    if f4["is_officer"]:
        return 1.0
    if f4["is_director"]:
        return 0.6
    return 0.0


# ---------------------------------------------------------------- daily index

IDX_RE = re.compile(r"^(\S+)\s+.+?\s+(\d+)\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/data/\S+\.txt)\s*$")


def form4_paths_for(day):
    q = (day.month - 1) // 3 + 1
    url = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx" % (day.year, q, day.strftime("%Y%m%d"))
    body = http_get(url)
    if body is None:
        return None
    paths = []
    seen = set()
    for line in body.splitlines():
        m = IDX_RE.match(line)
        if m and m.group(1) == "4" and m.group(4) not in seen:
            seen.add(m.group(4))
            paths.append(m.group(4))
    return paths


def fetch_filing(path):
    acc = path.rsplit("/", 1)[1][:-4]
    f4 = parse_form4(http_get("https://www.sec.gov/Archives/" + path))
    return acc, f4


# ---------------------------------------------------------------- prices (Yahoo chart API)

_PRICE_CACHE = {}
_PRICE_LOCK = threading.Lock()


def prices(ticker):
    """Daily series for ~10y: list of (iso_date, close, adjclose). Empty list if unavailable."""
    ticker = (ticker or "").upper().replace(".", "-")
    if not ticker or ticker in ("NONE", "N/A", "NA"):
        return []
    with _PRICE_LOCK:
        if ticker in _PRICE_CACHE:
            return _PRICE_CACHE[ticker]
    series = []
    for host in ("query1", "query2"):
        body = http_get("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=10y&interval=1d" % (host, ticker), sec=False, retries=2)
        if not body:
            continue
        try:
            res = json.loads(body)["chart"]["result"][0]
            ts = res["timestamp"]
            close = res["indicators"]["quote"][0]["close"]
            adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or close
            for t, c, a in zip(ts, close, adj):
                if c is not None and a is not None:
                    series.append((datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), c, a))
            break
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    with _PRICE_LOCK:
        _PRICE_CACHE[ticker] = series
    return series


def _index_on_or_after(series, iso):
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < iso:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(series) else None


def price_context(series, iso):
    """Where the stock sat in its trailing 52-week range on a given date."""
    i = _index_on_or_after(series, iso)
    if i is None or i < 20:
        return None
    window = [c for _, c, _ in series[max(0, i - 251):i + 1]]
    lo, hi, c = min(window), max(window), series[i][1]
    return {
        "range_pos": round((c - lo) / (hi - lo), 3) if hi > lo else None,
        "off_high": round(c / hi - 1, 4),
        "off_low": round(c / lo - 1, 4),
        "close": round(c, 2),
    }


def forward_returns(series, spy, iso):
    i = _index_on_or_after(series, iso)
    j = _index_on_or_after(spy, iso) if spy else None
    out = {}
    if i is None:
        return out
    for label, n in (("m3", 63), ("m6", 126), ("m12", 252)):
        if i + n < len(series):
            r = series[i + n][2] / series[i][2] - 1
            out[label] = round(r, 4)
            if j is not None and j + n < len(spy):
                out[label + "_x"] = round(r - (spy[j + n][2] / spy[j][2] - 1), 4)
    return out


# ---------------------------------------------------------------- insider history + timing record

def owner_history(owner_cik):
    """All open-market moves this insider has filed (any issuer), cached in data/owners/."""
    path = os.path.join(OWNERS, "%s.json" % owner_cik)
    cache = {"seen": [], "moves": []}
    if os.path.exists(path):
        with open(path) as f:
            cache = json.load(f)
    seen = set(cache["seen"])

    body = http_get("https://data.sec.gov/submissions/CIK%010d.json" % int(owner_cik))
    if not body:
        return cache["moves"]
    recent = json.loads(body).get("filings", {}).get("recent", {})
    cutoff = (TODAY - timedelta(days=365 * CFG["track_record"]["history_years"])).isoformat()
    todo = []
    for form, acc, filed in zip(recent.get("form", []), recent.get("accessionNumber", []), recent.get("filingDate", [])):
        if form == "4" and filed >= cutoff and acc not in seen:
            todo.append(acc)
    todo = todo[:CFG["track_record"]["max_filings_per_owner"]]

    def grab(acc):
        raw = http_get("https://www.sec.gov/Archives/edgar/data/%s/%s.txt" % (owner_cik, acc))
        return acc, parse_form4(raw), raw is not None

    with ThreadPoolExecutor(6) as ex:
        for acc, f4, ok in ex.map(grab, todo):
            if not ok:
                continue  # transient failure; retry next run
            seen.add(acc)
            if f4 and f4["owner_cik"] == str(owner_cik).lstrip("0"):
                cache["moves"].extend(summarize(f4, acc))

    cache["seen"] = sorted(seen)
    cache["moves"].sort(key=lambda m: m["first"] or "")
    with open(path, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache["moves"]


def episodes(moves):
    """Merge same-side moves at one issuer that are < N days apart (a selling program = one decision)."""
    gap = CFG["track_record"]["episode_gap_days"]
    out = []
    last = {}
    for m in sorted(moves, key=lambda m: m["first"] or ""):
        if not m["first"]:
            continue
        key = (m["issuer_cik"], m["side"])
        ep = last.get(key)
        if ep and (date.fromisoformat(m["first"]) - date.fromisoformat(ep["last"])).days <= gap:
            ep["shares"] += m["shares"]
            ep["value"] += m["value"]
            ep["last"] = max(ep["last"], m["last"])
            ep["plan"] = ep["plan"] and m["plan"]
            ep["held_after"] = m["held_after"]
            ep["accs"].append(m["acc"])
        else:
            ep = {k: m[k] for k in ("side", "shares", "value", "first", "last", "plan", "held_after", "issuer_cik", "issuer", "ticker")}
            ep["accs"] = [m["acc"]]
            out.append(ep)
            last[key] = ep
    for ep in out:
        ep["price"] = round(ep["value"] / ep["shares"], 2) if ep["shares"] else None
        base = ep["shares"] + ep["held_after"] if ep["side"] == "sell" else ep["held_after"] - ep["shares"]
        ep["pct"] = round(ep["shares"] / base, 4) if base > 0 else None
    return out


def timing_record(eps, spy, ticker_for):
    """Add price context + forward returns to each episode; summarize discretionary ones."""
    for ep in eps:
        series = prices(ticker_for.get(ep["issuer_cik"]) or ep["ticker"])
        ep["ctx"] = price_context(series, ep["first"]) if series else None
        ep["fwd"] = forward_returns(series, spy, ep["last"]) if series else {}

    def side_stats(side):
        pool = [e for e in eps if e["side"] == side and not e["plan"] and "m6_x" in e["fwd"]]
        if not pool:
            return None
        if side == "buy":
            hits = sum(1 for e in pool if e["fwd"]["m6_x"] > 0)
        else:
            hits = sum(1 for e in pool if e["fwd"]["m6_x"] < 0)
        ctx = [e["ctx"]["range_pos"] for e in pool if e.get("ctx") and e["ctx"].get("range_pos") is not None]
        m12 = [e["fwd"]["m12"] for e in pool if "m12" in e["fwd"]]
        return {
            "n": len(pool),
            "hits": hits,
            "avg_m6": round(sum(e["fwd"]["m6"] for e in pool) / len(pool), 4),
            "avg_m6_x": round(sum(e["fwd"]["m6_x"] for e in pool) / len(pool), 4),
            "avg_m12": round(sum(m12) / len(m12), 4) if m12 else None,
            "avg_range_pos": round(sum(ctx) / len(ctx), 3) if ctx else None,
        }

    return {
        "buys": side_stats("buy"),
        "sells": side_stats("sell"),
        "plan_sells": sum(1 for e in eps if e["side"] == "sell" and e["plan"]),
        "pending": sum(1 for e in eps if not e["plan"] and "m6_x" not in e["fwd"]),
    }


# ---------------------------------------------------------------- screening

def load_watchlist():
    out = set()
    with open(os.path.join(ROOT, "watchlist.txt")) as f:
        for line in f:
            line = line.split("#", 1)[0].strip().upper()
            if line:
                out.add(line)
    return out


ENTITY_RE = re.compile(r"\b(inc|llc|l\.?p|ltd|limited|corp|corporation|co|company|fund|funds|trust|holdings?|capital|partners|management|advisors|group|bank|insurance|reinsurance|life|plc|pte|gmbh|s\.?a|n\.?v|ag)\b\.?", re.I)
FUND_TICKER_RE = re.compile(r"^[A-Z]{4}X$")  # mutual-fund share classes


def qualifies(move, f4, watch):
    if move["price"] < CFG["min_share_price"]:
        return False
    rules = CFG["watchlist"] if watch else CFG["market"]
    ten_pct_only = f4["is_ten_pct"] and not (f4["is_officer"] or f4["is_director"])
    if ten_pct_only and not watch:
        if ENTITY_RE.search(f4["owner"]) and not CFG["market"].get("include_institutional_owners"):
            return False
        if move["value"] < CFG["market"]["ten_pct_owner_min_value"]:
            return False
    pct = move["pct"] or 0
    if move["side"] == "buy":
        return move["value"] >= rules["buy_min_value"]
    if move["plan"]:
        return pct >= rules["plan_sell_min_pct"] and move["value"] >= rules["plan_sell_min_value"]
    if move["value"] >= rules["discretionary_sell_min_value"]:
        return True
    if not watch:
        m = CFG["market"]
        return pct >= m["discretionary_sell_min_pct"] and move["value"] >= m["discretionary_sell_pct_min_value"]
    return False


def sanity_check_price(mv):
    """Filers sometimes type a total or a typo into the per-share price. If the filed price
    is >50x above that day's close, re-price and mark it (typos add digits, so take the lower). Smaller gaps are usually real:
    ADRs (one ADR = several ordinary shares), share classes, or a foreign currency."""
    series = prices(mv["ticker"])
    i = _index_on_or_after(series, mv["first"]) if series and mv["first"] else None
    if i is None:
        return True
    close = series[i][1]
    if close <= 0:
        return True
    ratio = mv["price"] / close
    if 1 / 50 <= ratio <= 50 or ratio < 1:
        return True  # a far-too-low filed price is usually a share-class mismatch (BRK.A vs B); keep it
    mv["filed_price"] = mv["price"]
    per_share = mv["price"] / mv["shares"] if mv["shares"] else 0
    if 1 / 3 <= per_share / close <= 3:
        price = per_share  # filer typed the total dollar amount into the price box
    else:
        price = close
    mv["price"] = round(price, 4)
    mv["value"] = round(mv["shares"] * price)
    mv["price_est"] = True
    return True


def score(flag):
    s = math.log10(max(flag["value"], 1)) - 4
    s += flag["role_weight"]
    s += 1.5 if flag["side"] == "buy" else 0
    s += min(flag["pct"] or 0, 1) * 3
    s += 1.5 if flag["watch"] else 0
    s += 1.0 if flag.get("cluster") else 0
    s -= 3.0 if flag.get("program") else 0
    ctx = flag.get("ctx") or {}
    if flag["side"] == "buy" and (ctx.get("off_high") or 0) <= -0.30:
        s += 1.0
    if flag["side"] == "sell" and (ctx.get("range_pos") or 0) >= 0.9:
        s += 0.5
    return round(s, 2)


def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main():
    os.makedirs(OWNERS, exist_ok=True)
    state_path = os.path.join(DATA, "state.json")
    flags_path = os.path.join(DATA, "flags.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    flags = json.load(open(flags_path)) if os.path.exists(flags_path) else []
    known = {(f["acc"], f["side"]) for f in flags}
    watch = load_watchlist()

    last_done = state.get("last_index_day")
    start = date.fromisoformat(last_done) + timedelta(days=1) if last_done else TODAY - timedelta(days=CFG["first_run_lookback_days"])
    if len(sys.argv) > 1:
        start = date.fromisoformat(sys.argv[1])  # manual backfill: python screener.py 2026-09-01
    end = TODAY - timedelta(days=1)

    new = []
    for day in business_days(start, end):
        paths = form4_paths_for(day)
        if paths is None:
            log(day, "no index (holiday or not published yet)")
            if day >= TODAY - timedelta(days=2):
                break  # not published yet; try again next run
            state["last_index_day"] = day.isoformat()
            continue
        log(day, len(paths), "Form 4s")
        with ThreadPoolExecutor(6) as ex:
            for acc, f4 in ex.map(fetch_filing, paths):
                if not f4:
                    continue
                for mv in summarize(f4, acc):
                    is_watch = f4["ticker"] in watch
                    if (acc, mv["side"]) in known or not qualifies(mv, f4, is_watch):
                        continue
                    if not is_watch and (FUND_TICKER_RE.match(f4["ticker"]) or not prices(f4["ticker"])):
                        continue  # non-traded fund/REIT or mutual-fund class: no market to read
                    if not sanity_check_price(mv) or not qualifies(mv, f4, is_watch):
                        continue
                    mv.update({
                        "filed": day.isoformat(),
                        "url": filing_url(f4["issuer_cik"], acc),
                        "owner_cik": f4["owner_cik"],
                        "owner": f4["owner"],
                        "role": role_of(f4),
                        "role_weight": role_weight(f4),
                        "watch": is_watch,
                    })
                    new.append(mv)
                    known.add((acc, mv["side"]))
        state["last_index_day"] = day.isoformat()
    log(len(new), "new flags")

    # Price context + clusters for the new flags.
    for fl in new:
        series = prices(fl["ticker"])
        fl["ctx"] = price_context(series, fl["first"]) if series and fl["first"] else None

    cutoff = (TODAY - timedelta(days=CFG["flag_retention_days"])).isoformat()
    flags = [f for f in flags if f["filed"] >= cutoff] + new
    by_issuer = {}
    for f in flags:
        if f["side"] == "buy":
            by_issuer.setdefault(f["issuer_cik"], []).append(f)
    program = {}
    for f in flags:
        if f["side"] == "buy":
            program.setdefault((f["issuer_cik"], f["first"], round(f["price"], 2)), set()).add(f["owner_cik"])
    for f in flags:
        peers = by_issuer.get(f["issuer_cik"], []) if f["side"] == "buy" else []
        near = {p["owner_cik"] for p in peers if abs((date.fromisoformat(p["filed"]) - date.fromisoformat(f["filed"])).days) <= 14}
        same_day = len(program.get((f["issuer_cik"], f["first"], round(f["price"], 2)), ())) if f["side"] == "buy" else 0
        f["program"] = same_day if same_day >= 5 else 0  # many insiders, same day, same price: a company plan
        f["cluster"] = len(near) if len(near) >= 2 and not f["program"] else 0
        f["score"] = score(f)

    # Timing record for every insider still on the page.
    spy = prices("SPY")
    ticker_for = {f["issuer_cik"]: f["ticker"] for f in flags}
    owners = {}
    owner_ciks = sorted({f["owner_cik"] for f in flags})
    log("track records for", len(owner_ciks), "insiders")
    for n, cik in enumerate(owner_ciks, 1):
        try:
            hist = owner_history(cik)
        except Exception as e:  # one bad owner shouldn't sink the run
            log("history failed", cik, e)
            continue
        eps = episodes(hist)
        for ep in eps:
            ticker_for.setdefault(ep["issuer_cik"], ep["ticker"])
        rec = timing_record(eps, spy, ticker_for)
        name = next((f["owner"] for f in flags if f["owner_cik"] == cik), cik)
        owners[cik] = {"name": name, "record": rec, "episodes": eps}
        if n % 25 == 0:
            log(" ", n, "/", len(owner_ciks))

    # Weekly price files for the page's charts.
    os.makedirs(os.path.join(SITE, "prices"), exist_ok=True)
    tickers = {f["ticker"] for f in flags} | {ticker_for.get(ep["issuer_cik"]) or ep["ticker"] for o in owners.values() for ep in o["episodes"]}
    for t in sorted(x for x in tickers if x):
        series = prices(t)
        if not series:
            continue
        weekly = {}
        for d, c, _ in series:
            weekly[datetime.strptime(d, "%Y-%m-%d").strftime("%G-%V")] = (d, round(c, 2))
        with open(os.path.join(SITE, "prices", "%s.json" % t.replace("/", "-")), "w") as f:
            json.dump(sorted(weekly.values()), f, separators=(",", ":"))

    flags.sort(key=lambda f: (f["filed"], f["score"]), reverse=True)
    with open(flags_path, "w") as f:
        json.dump(flags, f, separators=(",", ":"))
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    payload = {
        "generated": state["last_run"],
        "through": state.get("last_index_day"),
        "watchlist": sorted(watch),
        "flags": flags,
        "owners": owners,
        "ticker_for": ticker_for,
    }
    with open(os.path.join(ROOT, "template.html")) as f:
        html = f.read()
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(SITE, "index.html"), "w") as f:
        f.write(html.replace("/*__DATA__*/null", blob))
    log("wrote site/index.html:", len(flags), "flags,", len(owners), "insiders")

    send_digest(new, owners)


# ---------------------------------------------------------------- email digest

def _pct(v, signed=False):
    if v is None:
        return "n/a"
    return ("+" if signed and v > 0 else "") + "%d%%" % round(v * 100)


def _usd(v):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return "$%.1f%s" % (v / div, suf) if suf != "K" else "$%d%s" % (round(v / div), suf)
    return "$%d" % v


def record_text(rec):
    if not rec:
        return "No timing record."
    parts = []
    b, s = rec.get("buys"), rec.get("sells")
    if b:
        parts.append("Earlier buys: stock beat the S&P over 6 mo after %d of %d (avg %s vs S&P), bought at %s of 52-wk range." % (
            b["hits"], b["n"], _pct(b["avg_m6_x"], True), _pct(b["avg_range_pos"])))
    if s:
        parts.append("Earlier sales by choice: stock trailed the S&P over 6 mo after %d of %d (avg %s vs S&P), sold at %s of 52-wk range." % (
            s["hits"], s["n"], _pct(s["avg_m6_x"], True), _pct(s["avg_range_pos"])))
    if not parts:
        parts.append("No earlier open-market trades old enough to score.")
    return " ".join(parts)


def send_digest(new, owners):
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        log("email: GMAIL_USER / GMAIL_APP_PASSWORD not set, skipping")
        return
    if not new:
        log("email: nothing new, skipping")
        return
    import html as h
    import smtplib
    from email.mime.text import MIMEText

    page = os.environ.get("PAGE_URL", "")
    top = sorted(new, key=lambda f: (not f["watch"], -f["score"]))[:int(os.environ.get("DIGEST_MAX", "20"))]
    rows = []
    for f in top:
        ctx = f.get("ctx") or {}
        if f["side"] == "buy":
            what = "&#9650; BUY"
            pctline = "added %s to stake" % _pct(f["pct"]) if f["pct"] is not None else ""
        else:
            what = "&#9660; SELL" + (" (planned 10b5-1)" if f["plan"] else "")
            pctline = "sold %s of holdings" % _pct(f["pct"]) if f["pct"] is not None else ""
        where = ("stock at %s of 52-wk range" % _pct(ctx["range_pos"])) if ctx.get("range_pos") is not None else ""
        if f["side"] == "buy" and (ctx.get("off_high") or 0) <= -0.15:
            where = "%s below 52-wk high" % _pct(-ctx["off_high"])
        tags = " &middot; ".join(x for x in ["Watchlist" if f["watch"] else "", "%d insiders buying" % f["cluster"] if f.get("cluster") else ""] if x)
        rec = record_text((owners.get(f["owner_cik"]) or {}).get("record"))
        rows.append(
            '<tr><td style="padding:12px 0;border-bottom:1px solid #e6e5e0">'
            '<div style="font-size:13px;color:#52514e">%s%s</div>'
            '<div style="font-size:15px"><b>%s %s</b> %s</div>'
            '<div style="font-size:13px;color:#52514e">%s &middot; %s</div>'
            '<div style="font-size:15px;margin-top:4px"><b>%s</b> @ $%s &middot; %s &middot; %s</div>'
            '<div style="font-size:13px;color:#52514e;margin-top:4px">%s</div>'
            '<div style="font-size:13px;margin-top:4px"><a href="%s">SEC filing</a></div>'
            '</td></tr>' % (
                what, (" &middot; " + tags) if tags else "",
                h.escape(f["ticker"]), "", h.escape(f["issuer"]),
                h.escape(f["owner"]), h.escape(f["role"]),
                _usd(f["value"]), ("%.2f" % f["price"]), pctline, where,
                h.escape(rec), f["url"]))
    more = len(new) - len(top)
    body = (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;color:#0b0b0b">'
        '<p style="font-size:15px">%d new insider trade%s flagged.%s</p>'
        '<table style="width:100%%;border-collapse:collapse">%s</table>'
        '<p style="font-size:13px;color:#52514e">%s</p></div>' % (
            len(new), "" if len(new) == 1 else "s",
            (' <a href="%s">Open the full page</a> for charts and trade history.' % page) if page else "",
            "".join(rows),
            ("Plus %d more on the page." % more) if more > 0 else ""))
    watch_n = sum(1 for f in new if f["watch"])
    subject = "Insider moves: %d new%s" % (len(new), (", %d on watchlist" % watch_n) if watch_n else "")
    msg = MIMEText(body, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.environ.get("DIGEST_TO", user)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    log("email: sent", subject)


if __name__ == "__main__":
    main()
