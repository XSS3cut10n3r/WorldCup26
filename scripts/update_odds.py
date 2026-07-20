#!/usr/bin/env python3
"""
World Cup 2026 family ODDS updater.

Fetches the championship ("who wins the World Cup") market from Kalshi's
public API, normalizes every team's implied probability to sum to 100%
(stripping the market's built-in margin / overround), then rolls those up
to each family member as the combined chance that one of their teams wins.
Writes odds.json for the website's Odds tab to display.

Companion to update_scores.py and uses the SAME team-name matching
(normalize() + ALIASES), so Kalshi's names line up with assignments.json.

Kalshi market data is public — NO API key or auth is required.

No third-party dependencies — standard library only.
"""

import json
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Kalshi public REST API. These market-data endpoints need NO API key or
# auth headers — this is the documented public host (serves ALL markets,
# not just elections, despite Kalshi's other "elections" subdomain).
KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
# The men's World Cup 2026 winner event. Each contender is a yes/no market
# under this event (e.g. KXMENWORLDCUP-26-ES = Spain).
EVENT_TICKER = "KXMENWORLDCUP-26"

# Alternate spellings: normalized alias -> normalized canonical assignment
# name. Mirrors update_scores.py so both scripts canonicalize identically.
# normalize() strips accents/punctuation first, so "Türkiye" -> "turkiye",
# "Côte d'Ivoire" -> "cote divoire", "Curaçao" -> "curacao" already match;
# these handle the remaining word-level differences Kalshi might use.
ALIASES = {
    "usa": "united states",
    "united states of america": "united states",
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "korea dpr": "north korea",
    "turkey": "turkiye",
    "czech republic": "czechia",
    "congo dr": "dr congo",
    "dr congo": "dr congo",
    "democratic republic of the congo": "dr congo",
    "dr of congo": "dr congo",
    "cape verde": "cabo verde",
    "cape verde islands": "cabo verde",
    "ivory coast": "cote divoire",
    "ir iran": "iran",
    "iran ir": "iran",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "holland": "netherlands",
}


def normalize(name: str) -> str:
    """Lowercase, strip accents, drop punctuation, collapse spaces."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().replace("'", "").replace("\u2019", "")
    name = "".join(c if c.isalnum() or c.isspace() else " " for c in name)
    name = " ".join(name.split())
    return ALIASES.get(name, name)


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_markets(event_ticker: str) -> list:
    """All markets under the event, following Kalshi's cursor pagination."""
    markets = []
    cursor = None
    for _ in range(20):  # safety cap; one event has well under 20 pages
        qs = f"?event_ticker={event_ticker}&limit=1000&status=open"
        if cursor:
            qs += f"&cursor={cursor}"
        url = f"{KALSHI_BASE}/markets{qs}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "family-world-cup-odds/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            sys.exit(f"Kalshi request failed: HTTP {e.code}. Response: {body}")
        except urllib.error.URLError as e:
            sys.exit(f"Kalshi request failed: {e.reason}")
        batch = payload.get("markets", [])
        markets.extend(batch)
        cursor = payload.get("cursor")
        if not cursor or not batch:
            break
    return markets


def champion_from_local_results():
    """Fallback for once the tournament is over and Kalshi's market has
    closed: read the known champion straight from our own data.json
    (bracket -> FINAL stage -> match winner), rather than depending on an
    external market that's no longer live.

    Returns the normalized team name, or None if data.json / the final
    result isn't available yet (tournament still in progress, etc).
    """
    path = ROOT / "data.json"
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError):
        return None

    final_stage = next(
        (s for s in data.get("bracket", []) if s.get("stageCode") == "FINAL"),
        None,
    )
    if not final_stage or not final_stage.get("matches"):
        return None

    match = final_stage["matches"][0]
    winner_side = match.get("winner")  # "HOME_TEAM" or "AWAY_TEAM"
    side = {"HOME_TEAM": "home", "AWAY_TEAM": "away"}.get(winner_side)
    if not side:
        return None

    team = (match.get(side) or {}).get("name")
    return normalize(team) if team else None


def market_price(m: dict):
    """Implied YES price for a market, in cents (0..100), or None.

    Prefers the bid/ask midpoint; falls back to last trade, then either
    side alone. Kalshi exposes both integer-cent fields (yes_bid, yes_ask,
    last_price) and dollar-string fields (yes_bid_dollars, ...); we read
    whichever is present and work in cents so the units cancel on
    normalization anyway."""
    def cents(base):
        v = m.get(base)
        if isinstance(v, (int, float)):
            return float(v)
        d = m.get(base + "_dollars")
        if d not in (None, ""):
            try:
                return float(d) * 100.0
            except (TypeError, ValueError):
                return None
        return None

    bid, ask = cents("yes_bid"), cents("yes_ask")
    if bid is not None and ask is not None and (bid > 0 or ask > 0):
        return (bid + ask) / 2.0
    last = cents("last_price")
    if last is not None and last > 0:
        return last
    for side in (ask, bid):
        if side is not None and side > 0:
            return side
    return None


def team_name_of(m: dict) -> str:
    """The contender name for a winner market — yes_sub_title, else title."""
    return (m.get("yes_sub_title") or m.get("yes_subtitle")
            or m.get("title") or "")


def shared_ranks(items, key):
    """Assign 1-based ranks, sharing a rank across equal keys (like update_scores)."""
    rank, prev = 0, None
    for i, it in enumerate(items, start=1):
        k = key(it)
        if k != prev:
            rank, prev = i, k
        it["rank"] = rank


def _core(payload: dict) -> dict:
    """Change-detection view of a payload: only the meaningful odds data.

    Ignores 'generated' (always changes) and each person's 'delta' (derived
    from the previous file), so a run that produces identical odds is still a
    no-op and the on-disk arrows keep pointing at the last real movement."""
    return {
        "standings": [{"name": s.get("name"), "odds": s.get("odds"),
                       "teams": s.get("teams")} for s in payload.get("standings", [])],
        "overround": payload.get("overround"),
        "source": payload.get("source"),
        "event": payload.get("event"),
    }


def main():
    assignments = load_json(ROOT / "assignments.json")
    participants = assignments["participants"]

    source = "Kalshi"
    markets = fetch_markets(EVENT_TICKER)

    if markets:
        # ---- Normal path: live Kalshi market -----------------------------
        # normalized team name -> implied price (cents). If Kalshi ever lists
        # a team twice, keep the richer (higher) quote.
        price_by_team = {}
        raw_total = 0.0
        for m in markets:
            nm = normalize(team_name_of(m))
            price = market_price(m)
            if not nm or price is None:
                continue
            raw_total += price
            if nm not in price_by_team or price > price_by_team[nm]:
                price_by_team[nm] = price

        total = sum(price_by_team.values())
        if total <= 0:
            sys.exit("All Kalshi prices were zero/unavailable; nothing to normalize.")

        # prob(team) = price(team) / sum(all prices). Dividing by the total
        # removes the overround so the field sums to exactly 1.0.
        prob_by_team = {nm: p / total for nm, p in price_by_team.items()}
        overround = round(raw_total / 100.0, 4)  # e.g. 1.05 = 105%
    else:
        # ---- Fallback: Kalshi market has closed (tournament is over) -----
        # There's nothing left to price on Kalshi once the event resolves,
        # so fall back to the champion we already know from our own results.
        print(f"No open Kalshi markets for {EVENT_TICKER} "
              "(tournament likely decided) — falling back to data.json.")
        champion = champion_from_local_results()
        if not champion:
            sys.exit(
                f"No markets returned for event {EVENT_TICKER}, and no "
                "champion could be read from data.json's FINAL bracket "
                "match either. Check the event ticker is live on Kalshi, "
                "or that data.json has a finished FINAL match."
            )
        print(f"Using known champion from data.json: {champion}")
        prob_by_team = {champion: 1.0}
        price_by_team = {champion: 100.0}  # for the "N priced markets" log line
        overround = 1.0  # fully settled book, no market margin to report
        source = "Results (Kalshi market closed)"

    # ---- Roll up to family members --------------------------------------
    standings = []
    unmatched_teams = []
    used_norms = set()
    for owner, teams in participants.items():
        tlist = []
        for team in teams:
            nm = normalize(team)
            odds = prob_by_team.get(nm, 0.0)
            if nm in prob_by_team:
                used_norms.add(nm)
            else:
                unmatched_teams.append(team)
            tlist.append({"name": team, "odds": round(odds, 6)})
        standings.append({
            "name": owner,
            "odds": round(sum(t["odds"] for t in tlist), 6),
            "teams": tlist,
        })

    # Highest combined odds first; ties broken alphabetically, shared ranks.
    standings.sort(key=lambda r: (-r["odds"], r["name"].lower()))
    shared_ranks(standings, key=lambda r: r["odds"])

    # ---- Diagnostics (mirrors update_scores.py's helpful notes) ---------
    if unmatched_teams:
        print("Warning: no Kalshi market matched these assigned teams "
              f"(shown at 0%): {', '.join(sorted(set(unmatched_teams)))}")
    owned_norms = {normalize(t) for ts in participants.values() for t in ts}
    leftover = [nm for nm in prob_by_team if nm not in owned_norms]
    if leftover:
        print(f"Note: {len(leftover)} Kalshi market(s) had no owner in "
              f"assignments.json: {', '.join(sorted(leftover))}")

    payload = {
        "standings": standings,
        "overround": overround,
        "source": source,
        "event": EVENT_TICKER,
    }

    out_path = ROOT / "odds.json"
    # Persistent trend deltas. Each value carries a `base` = what it was as of the
    # last time it actually changed; delta = odds - base. A no-op re-run leaves
    # `base` (and so the arrow) untouched — the baseline only moves when the value
    # moves — so the trend survives quiet markets and double-runs instead of
    # collapsing to "no change, no arrow". Per-TEAM deltas are written too: the
    # page sums a person's team deltas (excluding eliminated teams) for their
    # headline arrow, so without per-team deltas no arrow ever shows.
    existing = None
    prev_p, prev_t = {}, {}
    if out_path.exists():
        try:
            existing = load_json(out_path)
            for s in existing.get("standings", []):
                prev_p[s["name"]] = s
                for t in s.get("teams", []):
                    prev_t[t["name"]] = t
        except (json.JSONDecodeError, OSError):
            existing = None

    EPS = 5e-7

    def trend(new, prev):
        """Return (base, delta) with a baseline that only moves when the value
        does, so the arrow reflects the last real change and persists."""
        if not prev or prev.get("odds") is None:
            return new, None                      # first ever: nothing to show yet
        o = prev["odds"]
        b = prev.get("base")
        if b is None:                             # migrate an old file lacking base
            d = prev.get("delta")
            b = (o - d) if d is not None else o
        base = o if abs(new - o) > EPS else b     # moved -> new baseline is prev value
        return base, round(new - base, 6) or None

    for s in standings:
        for t in s["teams"]:
            b, d = trend(t["odds"], prev_t.get(t["name"]))
            t["base"], t["delta"] = round(b, 6), d
        b, d = trend(s["odds"], prev_p.get(s["name"]))
        s["base"], s["delta"] = round(b, 6), d

    # Skip rewriting only when nothing moved at all (same odds AND same trend), so
    # a no-op re-run neither churns the file nor resets the arrows.
    if existing is not None:
        same = (existing.get("standings") == payload["standings"]
                and existing.get("overround") == payload.get("overround"))
        if same:
            print("No odds changes since last run; odds.json left untouched.")
            return

    payload["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    leader = standings[0] if standings else None
    print(f"Wrote odds.json — {len(price_by_team)} priced markets, "
          f"overround {payload['overround']:.0%}"
          + (f", leader {leader['name']} at {leader['odds']:.1%}." if leader else "."))


if __name__ == "__main__":
    main()
