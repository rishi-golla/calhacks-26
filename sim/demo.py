"""Demo trigger, simulator side (W4 task 3A): one call starts a fresh case on the PRD hero counterparties and
fast-forwards it to the moment that opens the case, so the routable event reaches Redis `events` within seconds.

    result = await start_demo(DemoRequest(preset="phone", demo_target="+1510..."), r=redis_client)

The chosen teammate (`demo_target`) rides on the load: sim -> `raw` (tender payload) -> ingest, which stamps it on the
case-opening event's payload (P0, source "demo"), which the router hands to agents as the task subject and the Voice
Desk dials through common.outreach.route_target(). Fresh load IDs per call, so presets can be repeated in rehearsal
without touching the seeded LOAD-4821 history.

    python -m sim.demo phone --target team-1
    python -m sim.demo classify "the carrier wants us to wire to a new bank account"
"""
import argparse
import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Literal

import redis.asyncio as redis

from common.outreach import OutreachBlocked, route_target
from sim import generator, hero
from sim.world import ensure


@dataclass(frozen=True)
class Preset:
    scenario: str
    trigger_step: str  # lifecycle step whose events open the case
    trigger_event: str  # the routable event ingest derives from it
    case_suffix: str  # appended to CASE-<load> by ingest for this event
    pins: dict


PRESETS = {
    # PRD hero script: $1,850 rate con, invoice adds 2 h detention ($300), GPS shows 47 min dwell of 120 free.
    "phone": Preset("detention_unsupported", "carrier_invoice", "carrier_invoice.received", "", {"dwell": 47}),
    # Rapid Ridge asks to redirect a $9,400.00 payment from a lookalike domain.
    "bank_change": Preset("bank_change", "bank_change", "bank_change.requested", "-BANK", {}),
    # Bay Produce pays $1,975.00 against the $2,200.00 shipper invoice: $225 short, "rate dispute".
    "short_pay": Preset("shipper_short_pay", "paid", "shipper_payment.received", "-AR", {"short_amount": 225}),
}
ALIASES = {"email": "bank_change"}
TRIGGER = {p.scenario: p.trigger_step for p in PRESETS.values()}
# Outbound email (W4 task 8) does not exist yet; every demo reaches its teammate by phone.
OUTREACH_CHANNEL = "phone"
LABEL = re.compile(r"team-(\d+)")


@dataclass(frozen=True)
class DemoRequest:
    preset: Literal["phone", "email", "bank_change", "short_pay"]
    channel: Literal["phone", "email"] = "phone"
    demo_target: str | None = None  # None -> route_target's first allowlist entry


@dataclass(frozen=True)
class DemoStarted:
    injection_id: str
    preset: str
    scenario: str
    load_id: str
    case_id: str  # the case ID of the routable trigger event (CASE-..., CASE-...-BANK, CASE-...-AR)
    trigger_event: str  # carrier_invoice.received | bank_change.requested | shipper_payment.received
    demo_target: str
    channel: str
    outreach_channel: str  # "phone" until outbound email (task 8) exists


def preset_name(preset: str) -> str:
    name = ALIASES.get(preset, preset)
    if name not in PRESETS:
        raise ValueError(f"unknown demo preset {preset!r}; expected one of {sorted([*PRESETS, *ALIASES])}")
    return name


def demo_load(world: dict, preset: str) -> tuple[dict, str]:
    """A fresh load on the preset's hero lane, shaped like generator.new_load() so lifecycle() runs unchanged."""
    p = PRESETS[preset_name(preset)]
    lane, consignee = (hero.bank_change_lane() if p.scenario == "bank_change"
                       else (hero.hero_lane(), dict(hero.HERO_CONSIGNEE)))
    known = {c["id"] for c in world["carriers"] + world["shippers"]}
    if missing := {lane["carrier_id"], lane["shipper_id"]} - known:
        raise ValueError(f"world has no hero counterparties {sorted(missing)}")
    load_id = generator._id("LD")
    # Hero lanes carry no fuel surcharge: the rate con is the flat linehaul the PRD script quotes.
    return generator.build_load(world, load_id, lane, 0.0, generator._load_rng({"load_id": load_id}),
                                consignee), p.scenario


def check_target(demo_target: str | None, channel: str = OUTREACH_CHANNEL) -> str:
    """The target the demo will reach, refused unless it is on the allowlist. A label (`team-1` = the first
    allowlist entry) is checked here but passed through as given; the Voice Desk re-checks on dial (P-011)."""
    label = LABEL.fullmatch(demo_target or "")
    if label:
        allow = [x.strip() for x in os.getenv("DEMO_ALLOWLIST_PHONES" if channel == "phone"
                                              else "DEMO_ALLOWLIST_EMAILS", "").split(",") if x.strip()]
        index = int(label.group(1)) - 1
        if not 0 <= index < len(allow):
            raise OutreachBlocked(f"{demo_target} is not on the demo allowlist")
        return demo_target
    return route_target(channel, "", demo_target).routed_to


async def start_demo(req: DemoRequest, r: redis.Redis | None = None) -> DemoStarted:
    """Validate, announce the case on `raw` and return; the load streams in the background, fast-forwarded so its
    trigger step is published straight away. Nothing is published if the target is refused."""
    name = preset_name(req.preset)
    if req.channel not in ("phone", "email"):
        raise ValueError(f"unknown demo channel {req.channel!r}")
    target = check_target(req.demo_target)
    p = PRESETS[name]
    load, scenario = demo_load(ensure(), name)
    injection_id = f"INJ-{uuid.uuid4().hex}"
    load["demo"] = {"injection_id": injection_id, "preset": name, "channel": req.channel, "demo_target": target}
    await generator.launch(load, scenario, r, {"injection_id": injection_id, "demo": load["demo"]}, priority="P0",
                           pins=p.pins, fast_forward_to=p.trigger_step)
    return DemoStarted(injection_id=injection_id, preset=name, scenario=scenario, load_id=load["load_id"],
                       case_id=f"{load['case_id']}{p.case_suffix}", trigger_event=p.trigger_event,
                       demo_target=target, channel=req.channel, outreach_channel=OUTREACH_CHANNEL)


@dataclass(frozen=True)
class ScenarioChoice:
    scenario: str | None  # None: no match, nothing is started
    preset: str | None  # a hero preset for start_demo, or None for a plain generator.inject() template
    matched: list[str]  # which of OUR keywords matched; never text copied from the input
    label: str  # what a console shows back: "understood as: bank change"


# Keyword rules per template. The text only picks one of these; amounts, IDs, phone numbers, recipients or
# approvals in it are never read (P-010, H9).
RULES = (
    ("bank_change", "bank_change", "bank change", ("bank", "banks", "routing", "account number", "new account",
                                                   "remit to", "remittance details", "wire", "payment details",
                                                   "ach details", "payee")),
    ("detention_unsupported", "phone", "detention dispute", ("detention", "dwell", "free time", "wait time",
                                                            "waiting time", "accessorial", "detained")),
    ("shipper_short_pay", "short_pay", "shipper short pay", ("short pay", "short-pay", "short paid", "shortpay",
                                                             "underpaid", "paid less", "deduction", "deducted",
                                                             "collections", "partial payment")),
    ("duplicate_invoice", None, "duplicate invoice", ("duplicate", "billed twice", "sent twice", "same invoice",
                                                      "double billed", "double-billed", "resent")),
    ("rate_mismatch", None, "rate above contract", ("above contract", "over contract", "rate mismatch",
                                                    "overbilled", "over-billed", "higher than the rate",
                                                    "more than the rate con")),
)
TEXT_BOUNDS = (10, 4000)


def classify_scenario(text: str) -> ScenarioChoice:
    if not isinstance(text, str) or not TEXT_BOUNDS[0] <= len(text) <= TEXT_BOUNDS[1]:
        raise ValueError(f"scenario text must be {TEXT_BOUNDS[0]}-{TEXT_BOUNDS[1]} characters")
    lowered = text.casefold()
    scored = []
    for scenario, preset, label, keywords in RULES:
        matched = [k for k in keywords if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", lowered)]
        if matched:
            scored.append((len(matched), scenario, preset, label, matched))
    scored.sort(key=lambda item: -item[0])
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        # Nothing, or two templates equally likely: say so instead of guessing.
        tied = [k for item in scored if item[0] == scored[0][0] for k in item[4]] if scored else []
        return ScenarioChoice(None, None, tied, "no match" if not scored else "ambiguous: " + " or ".join(
            item[3] for item in scored if item[0] == scored[0][0]))
    _count, scenario, preset, label, matched = scored[0]
    return ScenarioChoice(scenario, preset, matched, f"understood as: {label}")


async def _cli(args: argparse.Namespace) -> None:
    if args.preset == "classify":
        print(json.dumps(asdict(classify_scenario(" ".join(args.text))), indent=2))
        return
    client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    try:
        started_at = time.monotonic()
        result = await start_demo(DemoRequest(args.preset, args.channel, args.target), client)
        print(json.dumps(asdict(result), indent=2))
        print(f"started in {time.monotonic() - started_at:.3f}s; streaming the rest of the load (Ctrl-C to stop)")
        await asyncio.gather(*generator._BACKGROUND)
    finally:
        await client.aclose()


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Start a demo case on the hero counterparties.")
    parser.add_argument("preset", choices=[*PRESETS, *ALIASES, "classify"])
    parser.add_argument("text", nargs="*", help="classify: the scenario description")
    parser.add_argument("--channel", choices=["phone", "email"], default="phone")
    parser.add_argument("--target", help="allowlisted phone number or label (team-1); default: first entry")
    asyncio.run(_cli(parser.parse_args()))
