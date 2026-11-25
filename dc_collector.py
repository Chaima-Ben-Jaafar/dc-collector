"""DonorsChoose project-trajectory collector (with DonorsChoose's written permission).

Permission (DonorsChoose, 24-25 Nov 2026; full email thread saved as PDFs):
  * find newly posted projects through the public listing request the search page uses;
  * from public project pages record ONLY: amount still needed (with and without an active
    match, as displayed), donor count, expiration date, whether an active match offer is visible;
  * at enrolment, from the public listing: approval date, total project cost, school poverty
    band, match offer type and multiplier (no sponsor details);
  * at most 3 observations per project per day; collection 25 Nov - 22 Dec 2026;
  * one final check per project after its expiration date, through 31 Mar 2027
    (funded vs not funded only);
  * no teacher, school or donor names, no messages, no other personal data.

v2 (25 Nov 2026) changes, for unattended runs on GitHub Actions:
  * secrets and paths come from environment variables (nothing private in this file):
      DC_API_KEY   the website key used by the public listing request (required for discovery)
      DC_DATA_DIR  where registry/observation files live (default: ./data next to this file)
      DC_CONTACT   contact text added to the User-Agent
  * compliance guard: a project is skipped if it already has 3 observations on the current
    calendar day (counted both in UTC and in US Eastern time, so the limit holds whichever
    "day" DonorsChoose means), or if its last observation is less than MIN_HOURS_BETWEEN_OBS
    old. Extra, manual or delayed runs are therefore safe;
  * a run is allowed at any time inside the approved window (the guard enforces the limits),
    so delayed scheduled runs are not lost;
  * the four extra enrolment fields are stored in registry.csv (raw approval time in ms, so
    the time-zone conversion can be fixed at analysis time);
  * `final-check` mode;
  * request spacing 1.05 s (promised: at most 1 request per second);
  * logs print counts only (safe for a public repository).

Files (in DC_DATA_DIR):
  registry.csv            enrolled projects, enrolment fields and status
  obs_YYYYMMDDTHHMMZ.csv  one row per project observed in that run
  final_check.csv         one row per project checked after expiry

Usage:  python dc_collector.py run            (inside 25 Nov - 22 Dec 2026)
        python dc_collector.py final-check    (after expiry dates, until 31 Mar 2027)
        python dc_collector.py parse FILE.html
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----------------------------------------------------------------- settings
BASE = "https://www.donorschoose.org"
LISTING_PATH = "/common/json_feed.html?showFacetCounts=true&APIKey={key}&max=10&sortBy=7&index={index}"  # sortBy=7 = newest
LISTING_PAGES = 10              # 10 x 10 = 100 newest projects inspected per run
NEW_WITHIN_HOURS = 48           # enrol only projects approved within this many hours
SAMPLE_CAP = 1000               # agreed: ~1,000 (maximum allowed 2,500)
ENROL_PER_RUN = 27              # ~1,000 by 8 Dec at 3 runs/day
START = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)
ENROL_UNTIL = datetime(2026, 12, 8, 0, 0, tzinfo=timezone.utc)    # >= 1 week of follow-up before the 15 Dec analysis cut
END = datetime(2026, 12, 22, 23, 59, tzinfo=timezone.utc)         # four weeks after START
FINAL_CHECK_UNTIL = datetime(2027, 3, 31, 23, 59, tzinfo=timezone.utc)
MAX_OBS_PER_DAY = 3
MIN_HOURS_BETWEEN_OBS = 4.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.05
REG_FIELDS = ["project_id", "url", "cohort", "enrolled_utc", "status", "last_obs_utc", "n_obs", "recent_obs",
              "expiration_date", "approved_time_ms", "total_cost", "poverty_band", "match_type",
              "match_multiplier", "final_status", "final_checked_utc"]
OBS_FIELDS = ["run_utc", "fetched_utc", "project_id", "still_needed", "still_needed_with_match", "donor_count",
              "expiration_date", "match_visible", "http_status", "parse_ok"]
FINAL_FIELDS = ["checked_utc", "project_id", "final_status", "http_status", "source"]


def data_dir() -> Path:
    return Path(os.environ.get("DC_DATA_DIR") or Path(__file__).resolve().parent / "data")


def registry_path() -> Path:
    return data_dir() / "registry.csv"


_last_request = 0.0


# ----------------------------------------------------------------- http
def fetch(url: str) -> tuple[int, str]:
    """Polite GET: requests start >= MIN_SECONDS_BETWEEN_REQUESTS apart, retries with back-off, no cookies."""
    global _last_request
    contact = os.environ.get("DC_CONTACT", "PhD research, DonorsChoose-approved study")
    for attempt in range(1, 4):
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": f"Mozilla/5.0 ({contact})"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, ""
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(30 * attempt)
                continue
            return e.code, ""
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt < 3:
                time.sleep(15 * attempt)
                continue
            return 0, ""
    return 0, ""


# ----------------------------------------------------------------- parsing
_num = lambda s: float(s.replace(",", "").replace("$", "").strip())  # noqa: E731


def _label(v) -> str:
    """Readable value of a listing field that may be a string, number or small dict. Never returns names."""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("label", "name", "value", "type", "level"):
            if v.get(k) not in (None, ""):
                return str(v[k])
        return ""
    return str(v)


def _match_fields(mf) -> tuple[str, str]:
    """(type, multiplier) of a match offer from the listing; the sponsor is never read."""
    if isinstance(mf, list):
        mf = mf[0] if mf else None
    if not isinstance(mf, dict):
        return "", ""
    mtype = next((str(mf[k]) for k in ("type", "matchType", "matchingType", "matchingKey") if mf.get(k)), "")
    mult = next((str(mf[k]) for k in ("matchImpactMultiple", "multiplier", "matchMultiplier") if mf.get(k)), "")
    return mtype, mult


def enrolment_fields(p: dict) -> dict:
    """The four approved enrolment fields from one listing entry (plus nothing else)."""
    mtype, mult = _match_fields(p.get("matchingFund"))
    t = p.get("approvedTime")
    return {"approved_time_ms": "" if t is None else str(t),
            "total_cost": "" if p.get("totalPrice") in (None, "") else str(p.get("totalPrice")),
            "poverty_band": _label(p.get("povertyType")),
            "match_type": mtype, "match_multiplier": mult}


def parse_listing_json(text: str, now: datetime, within_hours: float = NEW_WITHIN_HOURS) -> list[tuple[str, str, dict]]:
    """(project_id, url, enrolment fields) for projects approved within `within_hours`."""
    try:
        props = json.loads(text).get("proposals", [])
    except (ValueError, AttributeError):
        return []
    out = []
    for p in props:
        pid, url, t = p.get("id"), p.get("proposalURL", ""), p.get("approvedTime")
        if not pid or "/project/" not in url:
            continue
        if t is not None:
            try:
                age_h = (now.timestamp() - float(t) / 1000) / 3600
            except (TypeError, ValueError):
                age_h = 0
            if age_h > within_hours:
                continue
        out.append((str(pid), url, enrolment_fields(p)))
    return out


def parse_project(html: str, fetched: datetime) -> dict:
    """Extract ONLY the four agreed fields. Returns None values when not found. (Unchanged from v1.)"""
    rec = {"still_needed": None, "still_needed_with_match": None, "donor_count": None,
           "expiration_date": None, "match_visible": None}
    m = re.search(r"var amountToGo\s*=\s*([\d.]+)", html)
    if m:
        rec["still_needed"] = float(m.group(1))
    else:
        m = re.search(r'class="togo"[^>]*data-togo="([\d,\.]+)"', html)
        if m:
            rec["still_needed"] = _num(m.group(1))
    if rec["still_needed"] is None and re.search(r"[Ff]ully funded|project-funded", html):
        rec["still_needed"] = 0.0
    stats = re.search(r'<ul class="donation-stats">(.*?)</ul>', html, re.S)
    block = stats.group(1) if stats else ""
    if "Be the first" in block and not re.search(r"\d[\d,]*\s*</strong>\s*donors?", block):
        rec["donor_count"] = 0
    else:
        m = (re.search(r"<strong>\s*([\d,]+)\s*</strong>\s*donors?", block)
             or re.search(r"([\d,]+)\s+donors?\b", block))
        if m:
            rec["donor_count"] = int(m.group(1).replace(",", ""))
    m = re.search(r"var daysToExpiration\s*=\s*([\d.]+)", html)
    if m:
        rec["expiration_date"] = (fetched + timedelta(days=float(m.group(1)))).date().isoformat()
    else:
        m = re.search(r'class="expiration-date">([^<]+)<', html)
        if m:
            rec["expiration_date"] = m.group(1).strip()
    m = re.search(r"var amountToGoWithMatch\s*=\s*([\d.]+)", html)
    eligible = re.search(r"var isEligibleForFunding\s*=\s*(true|false)", html)
    if eligible and eligible.group(1) == "false":
        rec["match_visible"] = False
    elif m:
        rec["match_visible"] = float(m.group(1)) > 0
    elif re.search(r'class="[^"]*(match-offer-section|with-match)[^"]*"', html):
        rec["match_visible"] = True
    if m and float(m.group(1)) > 0:
        rec["still_needed_with_match"] = float(m.group(1))
    return rec


# ----------------------------------------------------------------- registry
def load_registry() -> dict[str, dict]:
    p = registry_path()
    if not p.exists():
        return {}
    with p.open(newline="") as f:
        reg = {r["project_id"]: r for r in csv.DictReader(f)}
    for r in reg.values():                       # files written by v1 lack the new columns
        for k in REG_FIELDS:
            if r.get(k) is None:
                r[k] = "new" if k == "cohort" else ""
    return reg


def save_registry(reg: dict[str, dict]) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    tmp = registry_path().with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(reg.values())
    tmp.replace(registry_path())


def _times(r: dict) -> list[datetime]:
    out = []
    for s in (r.get("recent_obs") or "").split(";") + [r.get("last_obs_utc") or ""]:
        s = s.strip()
        if s:
            try:
                t = datetime.fromisoformat(s)
                out.append(t if t.tzinfo else t.replace(tzinfo=timezone.utc))
            except ValueError:
                pass
    return sorted(set(out))


DAY_OFFSETS_HOURS = (0, -5)     # calendar day in UTC and in US Eastern (standard time)


def can_observe(r: dict, now: datetime) -> bool:
    """Compliance guard: fewer than MAX_OBS_PER_DAY observations on the current calendar day, counted
    both in UTC and in US Eastern time, and >= MIN_HOURS_BETWEEN_OBS since the last observation."""
    ts = _times(r)
    if ts and now - ts[-1] < timedelta(hours=MIN_HOURS_BETWEEN_OBS):
        return False
    for off in DAY_OFFSETS_HOURS:
        shift = timedelta(hours=off)
        today = (now + shift).date()
        if sum((t + shift).date() == today for t in ts) >= MAX_OBS_PER_DAY:
            return False
    return True


def _record_obs(r: dict, t: datetime) -> None:
    ts = [x for x in _times(r) if t - x < timedelta(hours=48)] + [t]
    r["recent_obs"] = ";".join(x.isoformat() for x in sorted(set(ts)))
    r["last_obs_utc"] = t.isoformat()
    r["n_obs"] = str(int(r.get("n_obs") or 0) + 1)


# ----------------------------------------------------------------- one run
def is_run_time(now: datetime) -> bool:
    """Kept for the Colab notebook: True inside the approved window (the guard enforces the limits)."""
    return START <= now <= END


def run(now: datetime | None = None, fetcher=fetch, rng: random.Random | None = None,
        clock=lambda: datetime.now(timezone.utc)) -> Path | None:
    now = now or clock()
    rng = rng or random.Random(int(now.strftime("%Y%m%d%H")))
    reg = load_registry()
    stamp = now.replace(second=0, microsecond=0)
    n_requests = 0

    # 1. discovery
    key = os.environ.get("DC_API_KEY", "")
    if now < ENROL_UNTIL and len(reg) < SAMPLE_CAP:
        if not key:
            print("DC_API_KEY not set; discovery skipped", flush=True)
        else:
            fresh: list[tuple[str, str, dict]] = []
            for page in range(LISTING_PAGES):
                status, text = fetcher(BASE + LISTING_PATH.format(key=key, index=page * 10))
                n_requests += 1
                if status != 200:
                    print(f"listing request returned status {status}; stopping discovery", flush=True)
                    break
                seen = {f[0] for f in fresh}
                found = parse_listing_json(text, now)
                fresh += [x for x in found if x[0] not in reg and x[0] not in seen]
                if not found:               # past the recently approved projects: stop paging
                    break
            rng.shuffle(fresh)
            take = min(ENROL_PER_RUN, SAMPLE_CAP - len(reg), len(fresh))
            for pid, url, extra in fresh[:take]:
                reg[pid] = {k: "" for k in REG_FIELDS} | {
                    "project_id": pid, "url": url, "cohort": "new", "enrolled_utc": stamp.isoformat(),
                    "status": "open", "n_obs": "0", **extra}
            print(f"discovery: {len(fresh)} unseen projects on {LISTING_PAGES} pages, enrolled {take}; "
                  f"sample size now {len(reg)}", flush=True)

    # 2. observation (each open project at most once per run, and only if the guard allows it)
    rows, skipped = [], 0
    for pid, r in reg.items():
        if r["status"] != "open":
            continue
        if not can_observe(r, clock()):
            skipped += 1
            continue
        status, html = fetcher(r["url"])
        n_requests += 1
        fetched = clock().replace(microsecond=0)
        rec = parse_project(html, fetched) if status == 200 else {
            "still_needed": None, "still_needed_with_match": None, "donor_count": None,
            "expiration_date": None, "match_visible": None}
        ok = status == 200 and rec["still_needed"] is not None
        rows.append({"run_utc": stamp.isoformat(), "fetched_utc": fetched.isoformat(), "project_id": pid,
                     **rec, "http_status": status, "parse_ok": ok})
        _record_obs(r, fetched)
        if rec["expiration_date"] and len(rec["expiration_date"]) == 10:
            r["expiration_date"] = rec["expiration_date"]
        if status in (404, 410):
            r["status"] = "gone"
        elif ok and rec["still_needed"] == 0:
            r["status"] = "funded"
        elif ok and r["expiration_date"] and r["expiration_date"] < fetched.date().isoformat():
            r["status"] = "expired"
    save_registry(reg)
    if skipped:
        print(f"guard: skipped {skipped} projects (observation limit)", flush=True)
    if not rows:
        print(f"no projects observed in this run; {n_requests} requests", flush=True)
        return None
    out = data_dir() / f"obs_{stamp.strftime('%Y%m%dT%H%MZ')}.csv"
    exists = out.exists()
    with out.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(rows)
    bad = sum(not x["parse_ok"] for x in rows)
    print(f"observed {len(rows)} projects ({bad} not parsed), {n_requests} requests -> {out.name}", flush=True)
    if bad / len(rows) > 0.2:
        print("WARNING: more than 20% of pages not parsed; the page layout may have changed.", flush=True)
    return out


# ----------------------------------------------------------------- final check
def final_check(now: datetime | None = None, fetcher=fetch,
                clock=lambda: datetime.now(timezone.utc)) -> int:
    """One check per project after its expiration date: funded vs not funded. Returns number of requests."""
    now = now or clock()
    reg = load_registry()
    rows, n_requests, waiting = [], 0, 0
    cutoff = (now - timedelta(days=1)).date().isoformat()     # one-day margin for time zones
    for pid, r in reg.items():
        if r.get("final_status"):
            continue
        if r["status"] == "funded":                            # already known; no request
            r["final_status"], r["final_checked_utc"] = "funded", now.replace(microsecond=0).isoformat()
            rows.append({"checked_utc": r["final_checked_utc"], "project_id": pid, "final_status": "funded",
                         "http_status": "", "source": "panel"})
            continue
        exp = r.get("expiration_date") or ""
        if len(exp) != 10 or exp >= cutoff:
            waiting += 1
            continue
        status, html = fetcher(r["url"])
        n_requests += 1
        t = clock().replace(microsecond=0).isoformat()
        if status in (404, 410):
            final = "gone"
        elif status == 200:
            rec = parse_project(html, clock())
            if rec["still_needed"] is None:
                final = ""
            else:
                final = "funded" if rec["still_needed"] == 0 else "not_funded"
        else:
            final = ""                                          # request failed: try again next time
        if final:
            r["final_status"], r["final_checked_utc"] = final, t
            rows.append({"checked_utc": t, "project_id": pid, "final_status": final,
                         "http_status": status, "source": "final_check"})
    save_registry(reg)
    if rows:
        p = data_dir() / "final_check.csv"
        exists = p.exists()
        with p.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FINAL_FIELDS)
            if not exists:
                w.writeheader()
            w.writerows(rows)
    print(f"final check: {len(rows)} outcomes recorded, {waiting} not yet expired, {n_requests} requests", flush=True)
    return n_requests


# ----------------------------------------------------------------- entry point
def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "parse":
        print(parse_project(Path(argv[2]).read_text(encoding="utf-8", errors="replace"), datetime.now(timezone.utc)))
        return 0
    now = datetime.now(timezone.utc)
    try:
        if len(argv) >= 2 and argv[1] == "run":
            if not (START <= now <= END):
                print(f"{now.isoformat()}  outside the approved period (25 Nov - 22 Dec 2026); nothing to do")
                return 0
            run(now)
            return 0
        if len(argv) >= 2 and argv[1] == "final-check":
            if not (START <= now <= FINAL_CHECK_UNTIL):
                print(f"{now.isoformat()}  outside the final-check period (until 31 Mar 2027); nothing to do")
                return 0
            final_check(now)
            return 0
    except Exception as e:   # never print URLs or page content in public logs
        print(f"ERROR: {type(e).__name__}", flush=True)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
