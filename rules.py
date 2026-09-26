"""Deterministic triage: plain regex rules, no model, no cost, no latency.

Design choices worth explaining:
  * Emergency rules are checked FIRST and are deliberately over-sensitive:
    a false alarm costs a phone call, a missed flood costs a building.
  * Only messages that match NO rule ever reach the LLM.
  * Known limitation: no negation handling ("there is no smoke" still matches).
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RuleResult:
    category: str   # plumbing | electrical | hvac | locksmith | general | fire | gas | office
    urgency: str    # emergency | urgent | routine
    rule: str       # name of the rule that fired (goes into the audit log)


def _norm(text: str) -> str:
    return text.lower().replace("\u2019", "'").replace("`", "'")


# (rule name, category, regex)
_EMERGENCY = [
    ("gas_leak", "gas",
     r"\bgas (smell|leak|odou?r)\b|\bsmell(s|ing|ed)? (of |like )?gas\b|\bgas is leaking\b|\bleaking gas\b"),
    ("carbon_monoxide", "gas", r"\bcarbon monoxide\b"),
    ("fire_smoke", "fire",
     r"\b(on fire|a fire|fire (in|is|has|started)|flames?|smoke|burning smell|smells? like (something )?burning)\b"),
    ("electrical_hazard", "electrical",
     r"\b(sparks?|sparking|electrical (fire|shock)|got shocked|burning (outlet|wire)s?|outlet (is )?smoking)\b"),
    ("water_emergency", "plumbing",
     r"\b(burst pipe|pipe (has )?burst|flood(ed|ing)?|water (is )?(pouring|gushing|streaming)"
     r"|water (is )?coming (through|down)"
     r"|(ceiling|roof).{0,40}(leak\w*|water|dripping|collaps\w*|sagging)"
     r"|(leak\w*|water|dripping).{0,40}(ceiling|roof))\b"),
]

_URGENT = [
    ("no_heat", "hvac",
     r"\bno (heat|heating)\b|\b(heater|furnace|boiler|heating) (is |has )?(not working|broken|stopped|out|dead)\b|\bwithout heat\b"),
    ("no_ac", "hvac",
     r"\bno (ac|a/c|air ?conditioning|cooling)\b|\b(ac|a/c|air ?conditioner|air conditioning) (is |has )?(not working|broken|stopped|out|dead)\b"),
    ("no_hot_water", "plumbing", r"\bno hot water\b|\bwithout hot water\b"),
    ("no_water", "plumbing", r"\bno (running )?water\b|\bwater (is |has )?(off|stopped|not working)\b"),
    ("no_power", "electrical",
     r"\bno (power|electricity)\b|\bpower (is |has )?(out|off|gone)\b|\bpower outage\b"),
    ("lockout", "locksmith",
     r"\blocked out\b|\blocked myself out\b|\blost (my )?keys?\b|\bkey (is )?(broken|stuck)\b"),
    ("sewage_toilet", "plumbing",
     r"\bsewage\b|\bsewer\b|\boverflowing toilet\b"
     r"|\btoilet\b.{0,30}\b(overflow\w*|backing up|backed up|clogged|blocked|won't flush)\b"),
]

_ROUTINE = [
    ("dripping_faucet", "plumbing",
     r"\b(faucet|tap|sink|shower ?head)\b.{0,30}\bdrip\w*|\bdrip\w*\b.{0,20}\b(faucet|tap|sink|shower ?head)\b"),
    ("light_bulb", "electrical",
     r"\b(light ?bulbs?|bulbs?|light fixture)\b.{0,30}\b(out|burn\w*|blown|broken|flicker\w*|dead)\b"),
    ("cosmetic_general", "general",
     r"\b(cabinet|drawer|door ?knob|door handle|closet door|blinds?|curtain rod|window screen|paint|scuff\w*)\b"),
]

_OFFICE = [
    ("office_query", "office", r"\b(rent|lease|payment|receipt|renewal|renew|deposit|parking|invoice)\b"),
]

# A chirping smoke detector is NOT a fire. We cut that phrase out and re-check the rest,
# so "alarm beeping AND I see smoke" is still an emergency.
_DETECTOR = re.compile(
    r"\b(smoke|fire|carbon monoxide|co)\s*(detector|alarm)s?\b.{0,40}\b(chirp\w*|beep\w*|low battery|battery)\b"
    r"|\b(chirp\w*|beep\w*|battery)\b.{0,40}\b(smoke|fire|carbon monoxide|co)\s*(detector|alarm)s?\b")


def _first_match(rules, text):
    for name, category, pattern in rules:
        if re.search(pattern, text):
            return name, category
    return None


def classify(message: str):
    """Return a RuleResult, or None if no rule is confident (=> the LLM may be asked)."""
    text = _norm(message)

    detector = _DETECTOR.search(text)
    if detector:
        text = text[:detector.start()] + " " + text[detector.end():]

    hit = _first_match(_EMERGENCY, text)
    if hit:
        return RuleResult(hit[1], "emergency", hit[0])

    hit = _first_match(_URGENT, text)
    if hit:
        return RuleResult(hit[1], "urgent", hit[0])

    if detector:
        return RuleResult("general", "routine", "detector_battery")

    hit = _first_match(_ROUTINE, text)
    if hit:
        return RuleResult(hit[1], "routine", hit[0])

    hit = _first_match(_OFFICE, text)
    if hit:
        return RuleResult(hit[1], "routine", hit[0])

    return None
