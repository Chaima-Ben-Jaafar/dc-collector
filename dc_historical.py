"""DonorsChoose historical (closed, funded) project sample, with DonorsChoose's written permission (25 Nov 2026).

Uses the public listing request of the search page with historical=true (the list holds FUNDED
projects only, ~3.6M), reads RANDOM pages (seeded, recorded), and stores per project ONLY the
approved fields:
  approval date (raw approvedTime, ms), fully-funded date and time (raw text + UTC), status,
  number of donors, total cost, school poverty band, match type and multiplier.
No teacher, school, donor or sponsor names; no messages. Results are reported in aggregate only.

Known properties of the list (data-audit §14): funded projects only -> time to funding GIVEN
success; order is not by date and pages can overlap -> duplicates removed by project ID and the
sample is described as "approximately random"; `page_index` is stored to check coverage.

Usage (environment: DC_API_KEY, optional DC_HIST_DIR, DC_CONTACT):
    python dc_historical.py collect 50 5        # test: up to 50 projects, at most 5 requests
    python dc_historical.py collect 20000       # full run (resumable: run again to continue)
    python dc_historical.py summary
"""
from __future__ import annotations

import csv
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dc_collector as dc

HIST_PATH = "/common/json_feed.html?showFacetCounts=true&APIKey={key}&max=10&sortBy=7&historical=true&index={index}"
SEED = 20261125
HIST_UNTIL = datetime(2026, 12, 22, 23, 59, tzinfo=timezone.utc)   # inside the approved collection period
FIELDS = ["project_id", "approved_time_ms", "fully_funded_raw", "fully_funded_utc", "funding_status",
          "num_donors", "total_cost", "poverty_band", "match_type", "match_multiplier", "page_index", "fetched_utc"]
TZ_OFFSETS = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8, "UTC": 0, "GMT": 0}


def hist_dir() -> Path:
    return Path(os.environ.get("DC_HIST_DIR") or Path(__file__).resolve().parent / "historical")


def parse_funded(text) -> str:
    """'Friday, August 21, 2026 9:08:37 AM EDT' -> '2026-08-21T13:08:37+00:00' ('' if not parseable)."""
    if not text or not isinstance(text, str):
        return ""
    parts = text.strip().rsplit(" ", 1)
    if len(parts) != 2 or parts[1] not in TZ_OFFSETS:
        return ""
    try:
        local = datetime.strptime(parts[0], "%A, %B %d, %Y %I:%M:%S %p")
    except ValueError:
        return ""
    return (local - timedelta(hours=TZ_OFFSETS[parts[1]])).replace(tzinfo=timezone.utc).isoformat()


def record(p: dict, page: int, fetched: str) -> dict | None:
    """The approved fields of one closed project (nothing else)."""
    pid = p.get("id")
    if not pid:
        return None
    mtype, mult = dc._match_fields(p.get("completedMatching") or p.get("matchingFund"))
    t = p.get("approvedTime")
    raw = p.get("fullyFundedDate") or ""
    return {"project_id": str(pid), "approved_time_ms": "" if t is None else str(t),
            "fully_funded_raw": raw if isinstance(raw, str) else "", "fully_funded_utc": parse_funded(raw),
            "funding_status": str(p.get("fundingStatus") or ""), "num_donors": str(p.get("numDonors") or ""),
            "total_cost": "" if p.get("totalPrice") in (None, "") else str(p.get("totalPrice")),
            "poverty_band": dc._label(p.get("povertyType")), "match_type": mtype, "match_multiplier": mult,
            "page_index": str(page), "fetched_utc": fetched}


class Historical:
    def __init__(self, fetcher=dc.fetch, clock=lambda: datetime.now(timezone.utc)):
        self.fetch, self.clock = fetcher, clock
        self.dir = hist_dir()
        self.state_path, self.out_path = self.dir / "state.json", self.dir / "projects.csv"

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st))

    def _seen(self) -> set[str]:
        if not self.out_path.exists():
            return set()
        with self.out_path.open(newline="") as f:
            return {r["project_id"] for r in csv.DictReader(f)}

    def _page(self, index_page: int, key: str):
        status, text = self.fetch(dc.BASE + HIST_PATH.format(key=key, index=index_page * 10))
        if status != 200:
            return status, None, []
        try:
            data = json.loads(text)
        except ValueError:
            return status, None, []
        total = data.get("totalProposals")
        try:
            total = int(str(total).replace(",", "")) if total not in (None, "") else None
        except ValueError:
            total = None
        return status, total, data.get("proposals", []) or []

    # ---------------------------------------------------------------- collection
    def collect(self, target: int, max_requests: int | None = None) -> dict:
        key = os.environ.get("DC_API_KEY", "")
        if not key:
            print("DC_API_KEY not set; nothing done", flush=True)
            return {}
        if self.clock() > HIST_UNTIL:
            print("outside the approved collection period; nothing done", flush=True)
            return {}
        st, seen = self._load(), self._seen()
        n_req = 0
        if "pages" not in st:                          # plan a random page order once (seeded, recorded)
            status, total, props = self._page(0, key)
            n_req += 1
            if status != 200 or not total:
                print(f"first request returned status {status}; nothing planned", flush=True)
                return {}
            n_pages = (total + 9) // 10
            order = list(range(n_pages))
            random.Random(SEED).shuffle(order)
            st = {"seed": SEED, "total_proposals": total, "n_pages": n_pages, "pages": order, "pos": 0,
                  "planned_utc": self.clock().replace(microsecond=0).isoformat(),
                  "requests": 0, "empty_pages": 0, "failed_pages": 0, "duplicates": 0}
            self._save(st)
        new_rows, stats = [], Counter()
        exists = self.out_path.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.out_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if not exists:
                w.writeheader()
            while len(seen) < target and st["pos"] < len(st["pages"]):
                if max_requests is not None and n_req >= max_requests:
                    break
                page = st["pages"][st["pos"]]
                status, _total, props = self._page(page, key)
                n_req += 1
                st["requests"] += 1
                if status != 200:
                    st["failed_pages"] += 1
                    stats["failed"] += 1
                    if stats["failed"] >= 5 and stats["failed"] > stats["ok"]:
                        print(f"stopping: repeated failures (last status {status})", flush=True)
                        break
                    st["pos"] += 1
                    continue
                stats["ok"] += 1
                if not props:
                    st["empty_pages"] += 1
                fetched = self.clock().replace(microsecond=0).isoformat()
                for p in props:
                    r = record(p, page, fetched)
                    if r is None:
                        continue
                    if r["project_id"] in seen:
                        st["duplicates"] += 1
                        continue
                    seen.add(r["project_id"])
                    w.writerow(r)
                    new_rows.append(r)
                st["pos"] += 1
                if st["pos"] % 50 == 0:
                    f.flush()
                    self._save(st)
        self._save(st)
        print(f"historical: {len(new_rows)} new projects, {len(seen)} in total, {n_req} requests this run; "
              f"pages used {st['pos']} of {st['n_pages']}; empty pages {st['empty_pages']}, "
              f"failed {st['failed_pages']}, duplicates {st['duplicates']}", flush=True)
        return {"new": len(new_rows), "total": len(seen), "requests": n_req}

    # ---------------------------------------------------------------- checks
    def summary(self) -> dict:
        if not self.out_path.exists():
            print("no data yet", flush=True)
            return {}
        with self.out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        years = Counter()
        for r in rows:
            try:
                years[datetime.fromtimestamp(int(r["approved_time_ms"]) / 1000, timezone.utc).year] += 1
            except (ValueError, OSError):
                years["?"] += 1
        status = Counter(r["funding_status"] for r in rows)
        pages = [int(r["page_index"]) for r in rows]
        unparsed = sum(1 for r in rows if r["fully_funded_raw"] and not r["fully_funded_utc"])
        stamp = Counter(r["fully_funded_utc"] for r in rows if r["fully_funded_utc"])
        shared = sum(c for c in stamp.values() if c > 1)
        st = self._load()
        print(f"projects: {len(rows)}; approval years: {dict(sorted(years.items(), key=str))}", flush=True)
        print(f"status: {dict(status)}; funded-time unparsed: {unparsed}; "
              f"projects sharing a funded second (bulk funding?): {shared}", flush=True)
        if pages:
            print(f"page_index range: {min(pages)}-{max(pages)} of {st.get('n_pages', '?')}", flush=True)
        return {"n": len(rows), "years": dict(years), "status": dict(status), "shared_second": shared}


def main(argv: list[str]) -> int:
    try:
        if len(argv) >= 3 and argv[1] == "collect":
            Historical().collect(int(argv[2]), int(argv[3]) if len(argv) >= 4 and argv[3] else None)
            return 0
        if len(argv) >= 2 and argv[1] == "summary":
            Historical().summary()
            return 0
    except Exception as e:   # never print URLs or page content in public logs
        print(f"ERROR: {type(e).__name__}", flush=True)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
