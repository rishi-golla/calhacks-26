"""Replace the offline carrier fixtures in data/world.json with real, Apify-confirmed FMCSA carriers.

The offline fixtures pair invented names with USDOT numbers that belong to other real companies and invented MC
dockets, so every live `lookup_carrier_authority` came back `no_matching_source_records`. This script:

  --fetch  picks candidates from the FMCSA census (Socrata) in each fixture carrier's state, keeps those with a
           company email domain whose website answers, confirms them in ONE pinned Apify FMCSA actor run (live
           SAFER authority, docket and status; ~$0.003 per carrier) and writes sim/fixtures/fmcsa_carriers.json.
  default  applies that snapshot: each offline_fixture carrier becomes a real one in the same state under a new
           `C-<usdot>` id, and lanes are remapped. Hero fixtures are untouched. New ids keep already-ingested
           cases from seeing a different USDOT under their old carrier id.

    make seed-carriers ARGS=--fetch    # spends Apify credit once (APIFY_TOKEN from ENV_FILE)
    make seed-carriers                 # offline: re-apply the committed snapshot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from integrations import apify, directory, fmcsa
from integrations.normalize import e164, email
from sim import geo

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "sim/fixtures/fmcsa_carriers.json"
FIXTURE_SOURCE = "offline_fixture"
# Mailbox providers say nothing about the carrier's own website.
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com",
            "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "ymail.com", "me.com", "protonmail.com",
            "mail.com", "gmx.com", "cox.net", "charter.net", "earthlink.net", "bellsouth.net", "rocketmail.com"}
NAME_STOPWORDS = {"inc", "llc", "corp", "co", "company", "the", "and", "ltd", "dba", "of", "enterprises", "group",
                  "services", "usa", "america", "american", "trucking", "transport", "transportation", "logistics",
                  "freight", "express", "lines"}
CARRIER_WORDS = re.compile(r"\b(TRUCK|TRUCKING|TRANSPORT|TRANSPORTATION|FREIGHT|LOGISTICS|EXPRESS|CARRIERS?|HAUL"
                           r"|HAULING|LINES|TRANSIT|CARTAGE|TRANSFER|DRAYAGE)\b", re.IGNORECASE)
# The census cargo filter still admits bus lines and charters; a freight brokerage tenders to none of them.
PASSENGER_WORDS = re.compile(r"\b(BUS|BUSES|COACH|COACHES|CHARTERS?|LIMOUSINE|TOURS?)\b", re.IGNORECASE)
CANDIDATES_PER_SLOT = 2  # Apify-confirmed spares per needed carrier; numbers that match no carrier are free
MAX_CHARGE_USD = 0.50
RUN_TIMEOUT_S = 600


def website_for(mail: str | None) -> str | None:
    domain = (mail or "").rpartition("@")[2].strip().lower()
    return f"https://{domain}/" if domain and "." in domain and domain not in FREEMAIL else None


def census_candidates(state: str, limit: int = 200) -> list[dict]:
    where = (f"{fmcsa.BASE_WHERE} AND power_units::number BETWEEN 10 AND 300 AND mcs150_date > '20250101' "
             f"AND phy_state='{state}'")
    rows = fmcsa._get({"$select": fmcsa.FIELDS, "$where": where, "$limit": limit, "$order": "dot_number"})
    return [fmcsa.normalize(r) | {"website": site} for r in rows if (site := website_for(r.get("email_address")))
            and not PASSENGER_WORDS.search(f"{r.get('legal_name')} {r.get('dba_name')}")]


def name_tokens(c: dict) -> set[str]:
    words = re.findall(r"[a-z0-9]+", f"{c.get('legal_name') or ''} {c.get('name') or ''}".lower())
    return {w for w in words if len(w) >= 3 and w not in NAME_STOPWORDS}


def owns_site(c: dict, html: str) -> bool:
    """An email domain is the carrier's site only if the domain or the homepage names the carrier
    (rules out ISP mailboxes such as frontier.com)."""
    tokens = name_tokens(c)
    stem = re.sub(r"[^a-z0-9]", "", c["website"].removeprefix("https://").split("/")[0].rsplit(".", 1)[0])
    return any(t in stem for t in tokens) or any(t in html.lower() for t in tokens if len(t) >= 5)


async def live_websites(candidates: list[dict]) -> list[dict]:
    """Keep candidates whose public website answers and belongs to them; trucking names first, one per site."""
    async def check(c: dict, client: httpx.AsyncClient) -> bool:
        try:
            await apify.public_website(c["website"])
            response = await client.get(c["website"])
            return response.status_code < 400 and owns_site(c, response.text[:300_000])
        except (ValueError, httpx.HTTPError):
            return False

    async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        ok = await asyncio.gather(*(check(c, client) for c in candidates))
    seen, live = set(), []
    for c in sorted((c for c, good in zip(candidates, ok, strict=True) if good),
                    key=lambda c: not CARRIER_WORDS.search(f"{c.get('legal_name')} {c.get('name')}")):
        if c["website"] not in seen and c["usdot"] not in seen:
            seen.update((c["website"], c["usdot"]))
            live.append(c)
    return live


def apify_confirm(dots: list[str]) -> tuple[list[dict], dict]:
    """One pinned FMCSA actor run for every candidate. Returns dataset items and the run's provenance."""
    config = apify.apify_configuration_status("carrier")
    if not config["available"]:
        raise SystemExit(f"Apify carrier actor not configured: {config}")
    headers = {"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"}
    body = {"dotNumbers": dots, "docketNumbers": [], "liveCheck": True, "onlyChanges": False, "maxResults": len(dots)}
    with httpx.Client(base_url=apify.API, headers=headers, timeout=90) as client:
        run = client.post(f"/acts/{config['actor_id']}/runs", json=body, params={
            "build": config["build"], "timeout": RUN_TIMEOUT_S, "memory": 512,
            "maxTotalChargeUsd": MAX_CHARGE_USD}).raise_for_status().json()["data"]
        deadline = time.monotonic() + RUN_TIMEOUT_S + 60
        while run["status"] in {"READY", "RUNNING"} and time.monotonic() < deadline:
            run = client.get(f"/actor-runs/{run['id']}", params={"waitForFinish": 60}).raise_for_status().json()["data"]
        if run["status"] != "SUCCEEDED":
            raise SystemExit(f"Apify run {run['id']} ended {run['status']}")
        items = client.get(f"/datasets/{run['defaultDatasetId']}/items",
                           params={"clean": 1, "format": "json"}).raise_for_status().json()
    return items, {"provider": "apify", "actor": config["actor_id"], "build": config["build"], "run_id": run["id"],
                   "dataset_id": run["defaultDatasetId"], "charged_items": len(items),
                   "checked_at": run.get("finishedAt")}


def confirmed(candidate: dict, items: list[dict], run: dict) -> dict | None:
    """The census row, re-stated from the live Apify record when USDOT *and* MC both match and authority is active."""
    record = next(iter(apify.normalize_carriers([i for i in items if str(i.get("dotNumber")) == candidate["usdot"]],
                                                usdot=candidate["usdot"], mc=candidate["mc"].replace("-", ""))), None)
    if not record or record.get("authority_confirmed_active") is not True or record.get("out_of_service") is True \
            or (record.get("usdot_status") or "").lower() != "active":
        return None
    legal = record.get("legal_name") or candidate["legal_name"]
    return {**candidate, "name": record.get("dba_name") or candidate["name"] or legal, "legal_name": legal,
            "phone": e164(record.get("phone")) or candidate["phone"],
            "email": email(record.get("email")) or candidate["email"], "authority_status": "ACTIVE",
            "insurance": record.get("insurance") or {}, "safety_rating": record.get("safety_rating"),
            "website_source": "fmcsa_registered_email_domain",
            "verified_by": {**run, "evidence_ref": f"apify://runs/{run['run_id']}"}}


def fetch(world: dict) -> dict:
    need = Counter(c.get("state") for c in world["carriers"] if c.get("source") == FIXTURE_SOURCE)
    if not need:
        raise SystemExit("world has no offline_fixture carriers to replace")
    picks = []
    for state, n in sorted(need.items()):
        sites = {c["website"] for c in picks}
        live = [c for c in asyncio.run(live_websites(census_candidates(state))) if c["website"] not in sites]
        picks += live[:n * CANDIDATES_PER_SLOT]
        print(f"{state}: need {n}, {len(live)} candidates with a live website")
    items, run = apify_confirm([c["usdot"] for c in picks])
    carriers = [c for c in (confirmed(c, items, run) for c in picks) if c]
    with httpx.Client(timeout=20) as client:
        for c in carriers:
            c.update(geo.geocode(c["address"], c["state"], client))
    print(f"Apify run {run['run_id']}: {len(items)} records, {len(carriers)}/{len(picks)} confirmed")
    snapshot = {"built_at": datetime.now(UTC).isoformat(), "census": fmcsa.URL, "run": run, "carriers": carriers}
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1) + "\n")
    return snapshot


def apply(world: dict, snapshot: dict) -> tuple[dict, dict]:
    """Slot each fixture carrier to an unused real carrier, same state first. Returns (world, old id -> new id)."""
    pool = defaultdict(list)
    for c in snapshot["carriers"]:
        pool[c["state"]].append(c)
    taken = {c.get("usdot") for c in world["carriers"] if c.get("source") != FIXTURE_SOURCE}
    remap, carriers = {}, []
    for old in world["carriers"]:
        if old.get("source") != FIXTURE_SOURCE:
            carriers.append(old)
            continue
        choices = [c for c in pool[old.get("state")] if c["usdot"] not in taken] \
            or [c for cs in pool.values() for c in cs if c["usdot"] not in taken]
        if not choices:
            raise SystemExit(f"snapshot has too few carriers to replace {old['id']}; rerun with --fetch")
        new = {**choices[0], "terms": old.get("terms", "Net 30"), "factor": old.get("factor")}
        taken.add(new["usdot"])
        remap[old["id"]] = new["id"]
        carriers.append(new)
    lanes = [{**lane, "carrier_id": remap.get(lane["carrier_id"], lane["carrier_id"])} for lane in world["lanes"]]
    sources = {**world.get("sources", {}), "carriers": f"{fmcsa.URL} confirmed by Apify run "
               f"{snapshot['run']['run_id']} ({snapshot['run']['actor']}@{snapshot['run']['build']})"}
    return {**world, "carriers": carriers, "lanes": lanes, "sources": sources}, remap


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="re-select and re-confirm carriers (spends Apify credit)")
    ap.add_argument("--dry-run", action="store_true", help="print the remap without writing data/world.json")
    a = ap.parse_args()
    world = directory.load()
    snapshot = fetch(world) if a.fetch else json.loads(SNAPSHOT.read_text())
    seeded, remap = apply(world, snapshot)
    for old, new in remap.items():
        c = next(x for x in seeded["carriers"] if x["id"] == new)
        print(f"{old} -> {new}  {c['name']} ({c['city']}, {c['state']}) {c['mc']} {c['website']}")
    if a.dry_run:
        return
    backup = directory.path().with_suffix(f".{int(time.time())}.bak.json")
    shutil.copy2(directory.path(), backup)
    directory.save(seeded)
    print(f"{len(remap)} carriers replaced -> {directory.path()} (backup {backup.name})")


if __name__ == "__main__":
    main()
