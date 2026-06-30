#!/usr/bin/env python3
"""
Sync World Cup card counts from ESPN's public scoreboard endpoint straight
into cards-manual.json. ESPN is treated as the source of truth: the file is
fully regenerated each run, so this is idempotent and safe to run repeatedly
(and safe to collide with the live-scores loop on the same branch).

Per team, cards are aggregated into the four FIFA conduct buckets:
    yellow             - caution, player not sent off
    indirect_red       - second yellow -> sent off
    direct_red         - straight red, no prior yellow
    yellow_direct_red  - earlier yellow, then a separate straight red

Usage:
    python3 scripts/sync_cards.py             # writes cards-manual.json
    python3 scripts/sync_cards.py audit TEAM  # prints one team's card events
    CARDS_FILE=other.json python3 scripts/sync_cards.py   # override target

Manual additions: cards ESPN can't see (e.g. a coach's booking) go in
cards-overrides.json as additive deltas — { "Germany": { "yellow": 1 } } — which
are ADDED on top of ESPN's counts every run. ESPN stays authoritative for player
cards (new bookings still appear); the manual delta is never removed and never
absorbed when the team picks up more cards.

Exit codes: 0 on success, 1 on fetch/parse failure (so the workflow can tell).
No API key required. Standard library only.

ESPN event-type ids in the feed: 94 = Yellow Card, 93 = Red Card.
"""

import json
import os
import sys
import tempfile
import urllib.request
from collections import defaultdict

SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
    "scoreboard?limit=300&dates=20260611-20260719"
)
CARDS_FILE = os.environ.get("CARDS_FILE", "cards-manual.json")
# Hand-entered card deltas ADDED on top of ESPN's auto-synced counts, so a card
# ESPN never records (e.g. a coach's caution) survives every regeneration and is
# never absorbed when the team later picks up more player cards.
OVERRIDES_FILE = os.environ.get("CARDS_OVERRIDES_FILE", "cards-overrides.json")

# ESPN display names -> your canonical names.
NAME_MAP = {
    "Cape Verde": "Cabo Verde",
    "Ivory Coast": "Côte d'Ivoire",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "Turkey": "Türkiye",
    "USA": "United States",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Congo DR": "DR Congo",
}

# Canonical team set. Output is zero-filled across all of these so a team with
# no cards (or one that hasn't played yet) still appears with zeros.
CANONICAL = {
    "Algeria", "Argentina", "Australia", "Austria", "Belgium",
    "Bosnia and Herzegovina", "Brazil", "Cabo Verde", "Canada", "Colombia",
    "Croatia", "Curaçao", "Czechia", "Côte d'Ivoire", "DR Congo", "Ecuador",
    "Egypt", "England", "France", "Germany", "Ghana", "Haiti", "Iran", "Iraq",
    "Japan", "Jordan", "Mexico", "Morocco", "Netherlands", "New Zealand",
    "Norway", "Panama", "Paraguay", "Portugal", "Qatar", "Saudi Arabia",
    "Scotland", "Senegal", "South Africa", "South Korea", "Spain", "Sweden",
    "Switzerland", "Tunisia", "Türkiye", "United States", "Uruguay",
    "Uzbekistan",
}

BUCKETS = ("yellow", "indirect_red", "direct_red", "yellow_direct_red")


def fetch():
    req = urllib.request.Request(SCOREBOARD, headers={"User-Agent": "card-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def canonical(espn_name, unmapped):
    name = NAME_MAP.get(espn_name, espn_name)
    if espn_name not in NAME_MAP and espn_name not in CANONICAL:
        unmapped.add(espn_name)
    return name


def finished_events(data):
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        if comp["status"]["type"].get("completed"):
            yield ev, comp


def card_events(comp):
    """Yield (team_id, athlete_id, athlete_name, minute_str, is_yellow, is_red, clock)."""
    for d in comp.get("details", []):
        if not (d.get("yellowCard") or d.get("redCard")):
            continue
        clock = d.get("clock") or {}
        for a in d.get("athletesInvolved") or []:
            yield (
                d["team"]["id"], a["id"], a.get("displayName", "?"),
                clock.get("displayValue", "?"),
                bool(d.get("yellowCard")), bool(d.get("redCard")),
                clock.get("value"),
            )


def classify_player(events):
    """events: list of (is_yellow, is_red, clock). Returns (bucket, n) pairs."""
    yellows = [e for e in events if e[0]]
    reds = [e for e in events if e[1]]
    if not reds:
        return [("yellow", len(yellows))]
    if not yellows:
        return [("direct_red", 1)]
    # both a yellow and a red for the same player:
    #   red shares a yellow's clock (or there are 2 yellows) -> second yellow
    #   otherwise -> earlier yellow + a separate straight red
    yellow_clocks = {y[2] for y in yellows}
    if reds[0][2] in yellow_clocks or len(yellows) >= 2:
        return [("indirect_red", 1)]
    return [("yellow_direct_red", 1)]


def build(data):
    tally = defaultdict(lambda: {b: 0 for b in BUCKETS})
    unmapped = set()
    for _ev, comp in finished_events(data):
        id_to_name = {c["team"]["id"]: c["team"]["displayName"]
                      for c in comp["competitors"]}
        per_player = defaultdict(list)
        for team_id, ath_id, _n, _m, y, r, clk in card_events(comp):
            per_player[(team_id, ath_id)].append((y, r, clk))
        for (team_id, _ath), evs in per_player.items():
            name = canonical(id_to_name.get(team_id, team_id), unmapped)
            for bucket, n in classify_player(evs):
                if n:
                    tally[name][bucket] += n
    out = {t: tally.get(t, {b: 0 for b in BUCKETS}) for t in sorted(CANONICAL)}
    for t in tally:  # surface any unexpected mapped-through team
        out.setdefault(t, tally[t])
    return out, unmapped


def apply_overrides(out, path):
    """Add hand-entered card deltas from `path` ON TOP of ESPN's counts; return
    how many deltas were applied. ESPN never records cards shown to coaches or
    other officials (they aren't players in the feed), so those have to be added
    here. Because the deltas are ADDED to whatever ESPN reports — not a floor and
    not a max — ESPN stays authoritative for every player card (new bookings keep
    appearing), while a manual addition is never wiped by the regeneration and
    never gets absorbed when a team later picks up more cards.

    Schema mirrors cards-manual.json (canonical team name -> bucket -> count):
        { "Germany": { "yellow": 1 } }
    Values are integers added to the ESPN tally; negatives are allowed (to undo a
    miscount) and each bucket is clamped at 0. Best-effort: a missing file is
    silently fine, a malformed one is warned about and skipped (never fatal), and
    any entry whose value isn't a bucket dict (e.g. a "_comment" string) is
    ignored."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            overrides = json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: couldn't read {path} ({e}); manual card overrides "
              f"skipped.", file=sys.stderr)
        return 0
    applied = 0
    for team, buckets in (overrides or {}).items():
        if not isinstance(buckets, dict):
            continue  # e.g. a "_comment" string — safely ignored
        name = NAME_MAP.get(team, team)
        row = out.setdefault(name, {b: 0 for b in BUCKETS})
        for bucket, n in buckets.items():
            if bucket not in BUCKETS:
                continue
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if n:
                row[bucket] = max(0, row[bucket] + n)
                applied += 1
    return applied


def write_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def audit(data, team_query):
    unmapped = set()
    print(f"Card events for: {team_query}\n")
    found = False
    for ev, comp in finished_events(data):
        id_to_name = {c["team"]["id"]: c["team"]["displayName"]
                      for c in comp["competitors"]}
        for team_id, _ath, player, minute, y, r, _clk in card_events(comp):
            name = canonical(id_to_name.get(team_id, team_id), unmapped)
            if name.lower() == team_query.lower():
                found = True
                kind = "YELLOW" if y and not r else "RED" if r and not y else "Y+R"
                print(f"  {minute:>7}  {kind:<7} {player}   ({ev['shortName']})")
    if not found:
        print("  (no card events found — check spelling / team not yet booked)")


def main():
    try:
        data = fetch()
    except Exception as e:  # network/HTTP/JSON failure
        print(f"ERROR: could not fetch ESPN scoreboard: {e}", file=sys.stderr)
        return 1

    if len(sys.argv) >= 3 and sys.argv[1] == "audit":
        audit(data, " ".join(sys.argv[2:]))
        return 0

    out, unmapped = build(data)
    if unmapped:
        # An unmapped team means cards could be silently misfiled. Fail loudly
        # (exit 2, distinct from a transient fetch failure) and write nothing.
        for name in sorted(unmapped):
            print(f"ERROR: ESPN name '{name}' not in NAME_MAP/CANONICAL — "
                  f"add it before writing.", file=sys.stderr)
        return 2

    applied = apply_overrides(out, OVERRIDES_FILE)

    write_atomic(CARDS_FILE, out)
    total = sum(sum(v.values()) for v in out.values())
    msg = f"Wrote {CARDS_FILE}: {len(out)} teams, {total} cards total."
    if applied:
        msg += f" (+{applied} manual override(s) from {OVERRIDES_FILE})"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
