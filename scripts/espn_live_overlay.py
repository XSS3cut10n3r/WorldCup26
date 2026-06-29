#!/usr/bin/env python3
"""
espn_live_overlay.py — lightly patch LIVE games into data.json using ESPN.

football-data.org remains the single source of truth: update_scores.py builds the
whole data.json (leaderboard, groups, bracket, conduct, scoring, the sim block).
This script runs AFTER it and does one narrow job — make sure games that are
actually being played show up as live, even when the football-data feed is slow to
flip a fixture to IN_PLAY (which is why live games were vanishing from the
leaderboard page).

It is deliberately minimal and non-authoritative:
  * It only touches the `live` / `recent` / `upcoming` match-card arrays.
  * It NEVER changes leaderboard points, groups, bracket, conduct, or the sim
    block. Points still come from football-data whenever it next updates, so a
    score can briefly lead the points during a feed lag — same as any lag today.
  * Owner / FIFA rank / canonical name for each team are read out of data.json
    itself (sim.teams), so no extra join files or second source are needed.
  * The watcher counts live / finished / upcoming games of EVERY stage, so the
    GitHub Actions loop stays awake through the knockout rounds too (the bug
    that silently killed runs the moment the group stage ended).
  * Group cards are injected as before — a group live card REPLACES a
    differently-spelled football-data copy (the Côte d'Ivoire dedup).
  * Knockout cards (Round of 32 … Final, with round labels and penalty
    shootouts) are now bridged too, but only as a GAP-FILLER: update_scores.py
    owns the richer knockout card (matchNo, bracket slots), so the overlay only
    injects a knockout card when football-data hasn't published that pairing
    yet, and never removes or overwrites the producer's copy.

Idempotent: every card this script adds is tagged "_src", and each run first
removes its own previous tags before re-deriving from ESPN. If nothing is live
(and nothing it previously added needs clearing), data.json is left byte-for-byte
unchanged, so the pipeline's no-change check still fires and no empty commit is
made. If ESPN can't be reached, it warns and leaves data.json untouched (exit 0),
so it can never break the scores pipeline.

Usage:
    python3 scripts/espn_live_overlay.py            # patch ./data.json in place
    DATA_FILE=other.json python3 scripts/espn_live_overlay.py
    ESPN_FIXTURE=sample.json python3 scripts/espn_live_overlay.py   # read a saved
                                                    # scoreboard instead of fetching

Standard library only. No API key.
"""

import copy
import json
import os
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
ESPN_FIXTURE = os.environ.get("ESPN_FIXTURE")  # optional local file, for testing
# How recently a game must have kicked off for the "just finished, football-data
# hasn't posted it yet" filler to apply. This stops the filler from back-filling
# the whole tournament: football-data keeps only a short rolling `recent` window,
# so every older finished game would otherwise look "missing". A group game runs
# well under 4h, so anything older than this is history, not a live-window gap.
FINAL_GAP_HOURS = float(os.environ.get("FINAL_GAP_HOURS", "4"))
SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
    "scoreboard?limit=300&dates=20260611-20260719"
)

# ESPN display names -> your canonical names (same direction as sync_cards.py).
NAME_MAP = {
    "Cape Verde": "Cabo Verde",
    "Cape Verde Islands": "Cabo Verde",   # football-data's long-form name
    "Ivory Coast": "Côte d'Ivoire",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "Turkey": "Türkiye",
    "USA": "United States",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Congo DR": "DR Congo",
}

# Tag we stamp on cards we inject, so each run can cleanly remove its own previous
# additions and re-derive. football-data's own cards never carry this key.
TAG = "_src"
TAG_LIVE = "espn-live"
TAG_FINAL = "espn-final"

# ESPN season.slug -> (stageCode, label) used by update_scores.py. The
# round-of-32 slug is confirmed from a live sample; the rest follow FIFA's
# standard scoreboard slugs, with a note-text fallback in classify_stage() in
# case a slug differs. stageCode/label here MUST match update_scores.py's
# STAGE_LABELS so an overlay-bridged card is indistinguishable from a producer
# card on the page.
ESPN_STAGE_SLUG = {
    "group-stage":   ("GROUP_STAGE",    "Group stage"),
    "round-of-32":   ("LAST_32",        "Round of 32"),
    "round-of-16":   ("LAST_16",        "Round of 16"),
    "quarterfinals": ("QUARTER_FINALS", "Quarter-final"),
    "semifinals":    ("SEMI_FINALS",    "Semi-final"),
    "third-place":   ("THIRD_PLACE",    "Third-place match"),
    "final":         ("FINAL",          "Final"),
}


def _norm(s):
    """Fold accents, unify apostrophes, lowercase, collapse spaces — for matching
    ESPN spellings to canonical names robustly."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    for ch in "\u2018\u2019\u02bc\u0060\u00b4":
        s = s.replace(ch, "'")
    return " ".join(s.lower().split())


def fetch_espn():
    if ESPN_FIXTURE:
        with open(ESPN_FIXTURE, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(SCOREBOARD, headers={"User-Agent": "live-overlay/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pair_key(home_name, away_name):
    """Order-independent identity for a fixture, robust to spelling/accents."""
    return tuple(sorted((_norm(home_name), _norm(away_name))))


def build_team_lookup(data):
    """canonical name -> {owner, fifa}, plus a normalized-name index, both read
    straight out of data.json so no external join data is needed."""
    meta, by_norm = {}, {}
    for nm, t in ((data.get("sim") or {}).get("teams") or {}).items():
        meta[nm] = {"owner": t.get("owner"), "fifa": t.get("fifa")}
        by_norm[_norm(nm)] = nm
    # groups table as a secondary source (covers any team missing from sim.teams)
    for g in (data.get("groups") or []):
        for r in (g.get("table") or []):
            nm = r.get("team")
            if nm and nm not in meta:
                meta[nm] = {"owner": r.get("owner"), "fifa": r.get("fifaRank")}
                by_norm.setdefault(_norm(nm), nm)
    return meta, by_norm


def canonicalize(espn_name, meta, by_norm):
    """ESPN display name -> canonical name present in data.json, or None."""
    cand = NAME_MAP.get(espn_name, espn_name)
    if cand in meta:
        return cand
    hit = by_norm.get(_norm(cand)) or by_norm.get(_norm(espn_name))
    return hit  # None if we can't confidently place the team


def classify_stage(ev, comp):
    """Map an ESPN event to (stage_code, stage_label, is_group).

    stage_code is None when we genuinely cannot place a knockout round — in that
    case the watcher still counts the game, but we won't inject a card for it
    (better a brief gap than a card with a wrong/blank round label)."""
    slug = ((ev.get("season") or {}).get("slug")
            or (comp.get("season") or {}).get("slug") or "").lower()
    if slug in ESPN_STAGE_SLUG:
        code, label = ESPN_STAGE_SLUG[slug]
        return code, label, code == "GROUP_STAGE"
    # Fallback: read the human note / event name. Order matters — "third",
    # "semi" and "quarter" are checked before the bare "final" substring, since
    # "semifinal" etc. all contain "final".
    low = (comp.get("altGameNote") or ev.get("name") or "").lower()
    if "group" in low:
        return "GROUP_STAGE", "Group stage", True
    if "round of 32" in low:                   return "LAST_32", "Round of 32", False
    if "round of 16" in low:                   return "LAST_16", "Round of 16", False
    if "quarter" in low:                       return "QUARTER_FINALS", "Quarter-final", False
    if "semi" in low:                          return "SEMI_FINALS", "Semi-final", False
    if "third" in low or "3rd" in low:         return "THIRD_PLACE", "Third-place match", False
    if "final" in low:                         return "FINAL", "Final", False
    return None, None, False


def espn_events(espn):
    """Yield (competition, state, stage_code, stage_label) for every event.

    Unlike the old group-only generator, this yields ALL stages so the watcher's
    live / finished / next-kickoff counts stay correct through the knockout
    rounds. stage_code distinguishes what we may inject (anything we can place)
    from what we can't (stage_code is None)."""
    for ev in espn.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        state = (((comp.get("status") or {}).get("type") or {}).get("state")) or ""
        stage_code, stage_label, _is_group = classify_stage(ev, comp)
        yield comp, state, stage_code, stage_label


def sides(comp):
    """Return (home_competitor, away_competitor) or (None, None)."""
    h = a = None
    for c in comp.get("competitors") or []:
        if c.get("homeAway") == "home":
            h = c
        elif c.get("homeAway") == "away":
            a = c
    return h, a


def _kickoff_dt(comp):
    """ESPN kickoff time -> aware UTC datetime, or None. Handles the trailing 'Z'
    and the seconds-optional formats ESPN uses (e.g. '2026-06-25T20:00Z')."""
    s = comp.get("date") or comp.get("startDate")
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def goals_of(competitor):
    try:
        return int(competitor.get("score"))
    except (TypeError, ValueError):
        return 0


def _shootout(competitor, comp):
    """Penalty-shootout tally for a competitor, or None if the game wasn't decided
    on penalties. Regulation/ET goals stay in `score`; the shootout is separate.

    Primary source is ESPN's per-competitor `shootoutScore` (seen on decided KO
    games). We have no finished-shootout sample to verify against, so as a
    fallback we count made shootout kicks from the play-by-play `details` (each
    carries a `shootout` flag, plus `scoringPlay` + `team` for a converted kick)."""
    v = competitor.get("shootoutScore")
    if v not in (None, ""):
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    tid = (competitor.get("team") or {}).get("id")
    saw, made = False, 0
    for d in comp.get("details") or []:
        if not d.get("shootout"):
            continue
        saw = True
        if d.get("scoringPlay") and (d.get("team") or {}).get("id") == tid:
            made += 1
    return made if saw else None


def _ko_winner(h, a, home, away, sh, sa):
    """Knockout result -> 'HOME_TEAM' / 'AWAY_TEAM' / None. Trust ESPN's per-side
    `winner` boolean first; fall back to ET goals, then the shootout tally."""
    if h.get("winner") is True:
        return "HOME_TEAM"
    if a.get("winner") is True:
        return "AWAY_TEAM"
    hg, ag = home.get("goals") or 0, away.get("goals") or 0
    if hg != ag:
        return "HOME_TEAM" if hg > ag else "AWAY_TEAM"
    if sh is not None and sa is not None and sh != sa:
        return "HOME_TEAM" if sh > sa else "AWAY_TEAM"
    return None


def team_block(competitor, meta, by_norm, with_goals=True):
    espn_name = (competitor.get("team") or {}).get("displayName") \
        or (competitor.get("team") or {}).get("name") or ""
    canon = canonicalize(espn_name, meta, by_norm)
    if canon is None:
        return None, espn_name
    m = meta.get(canon, {})
    block = {
        "name": canon,
        "owner": m.get("owner"),
        "goals": goals_of(competitor) if with_goals else None,
        "penGoals": None,
        "fifaRank": m.get("fifa"),
    }
    return block, espn_name


def make_card(comp, meta, by_norm, status, stage_code, stage_label):
    h, a = sides(comp)
    if not (h and a):
        return None, None
    home, hn = team_block(h, meta, by_norm)
    away, an = team_block(a, meta, by_norm)
    if home is None or away is None:
        # A team we can't place to an owner/rank — skip rather than inject a
        # broken card. (Returns the unmatched ESPN name for a warning.)
        return None, (hn if home is None else an)

    is_group = (stage_code == "GROUP_STAGE")

    # Penalty shootout (knockout only): expose the tally as penGoals and flip the
    # `penalties` flag, mirroring how update_scores.py marks a shootout result.
    sh, sa = _shootout(h, comp), _shootout(a, comp)
    penalties = (sh is not None) or (sa is not None)
    if penalties:
        home["penGoals"], away["penGoals"] = sh, sa

    winner = None
    if status == "FINISHED":
        if is_group:
            hg, ag = home["goals"], away["goals"]
            winner = ("DRAW" if hg == ag else
                      "HOME_TEAM" if hg > ag else "AWAY_TEAM")
        else:
            winner = _ko_winner(h, a, home, away, sh, sa)

    if not is_group:
        # Match the producer's knockout card shape so the page treats a bridged
        # card the same as a real one. Both sides are real teams (ESPN named
        # them), so they're resolved and not projected. We omit `slot` (only read
        # for unresolved sides) and top-level `matchNo` (a bracket index we can't
        # get from ESPN); a bridged card is replaced by the producer's richer one
        # on the very next football-data poll, so this is at most a brief gap.
        home["projected"], home["resolved"] = False, True
        away["projected"], away["resolved"] = False, True

    card = {
        "stage": stage_label,
        "stageCode": stage_code,
        "status": status,
        "utcDate": comp.get("date") or comp.get("startDate"),
        "home": home,
        "away": away,
        "penalties": penalties,
        "winner": winner,
        TAG: None,  # set by caller
    }
    return card, pair_key(home["name"], away["name"])


def main():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except Exception as e:
        print(f"ERROR: could not read {DATA_FILE}: {e}", file=sys.stderr)
        return 1

    original = copy.deepcopy(data)

    try:
        espn = fetch_espn()
    except Exception as e:
        print(f"NOTE: ESPN unreachable ({e}); leaving {DATA_FILE} untouched.",
              file=sys.stderr)
        print("LIVE_COUNT=-1")  # unknown; the loop treats this as "no change in state"
        print("FINISHED_COUNT=-1")
        print("NEXT_KICKOFF_MINS=-1")
        return 0  # never break the scores pipeline over the live overlay

    meta, by_norm = build_team_lookup(data)

    for key in ("live", "recent", "upcoming"):
        data.setdefault(key, [])

    # 1) Strip our own previous injections everywhere, so this run fully re-derives.
    for key in ("live", "recent", "upcoming"):
        data[key] = [m for m in data[key] if m.get(TAG) not in (TAG_LIVE, TAG_FINAL)]

    # 2) Walk every ESPN game. The watcher needs live / finished / next-kickoff
    #    counts across ALL stages (otherwise the loop terminates the moment the
    #    group stage ends — the knockout-blindness bug that killed the run).
    #    We collect group and knockout cards into separate lists because they're
    #    injected with different rules (see step 5): group cards REPLACE a
    #    differently-spelled football-data copy, while knockout cards only BRIDGE
    #    a gap and defer to update_scores.py's richer card whenever it exists.
    live_group, live_ko = [], []
    final_group, final_ko = [], []
    ko_final_results = {}   # pairing -> ESPN finished KO card, ANY age, for correcting
                            # the producer's copy (separate from the recency-gated
                            # final_ko gap-fill, so a correction never expires)
    unplaceable = set()
    now = datetime.now(timezone.utc)
    live_total = 0              # live games of ANY stage (drives the watcher loop)
    finished_count = 0          # finished games of ANY stage (drives marker/dispatch)
    next_kick_mins = None       # minutes until the soonest not-yet-started game, ANY stage
    for comp, state, stage_code, stage_label in espn_events(espn):
        if state == "in":
            live_total += 1
            if stage_code is None:
                continue        # counted (loop stays awake); can't place -> no card
            card, key = make_card(comp, meta, by_norm, "IN_PLAY", stage_code, stage_label)
            if card:
                card[TAG] = TAG_LIVE
                (live_group if stage_code == "GROUP_STAGE" else live_ko).append(card)
            elif key:
                unplaceable.add(key)
        elif state == "post":
            finished_count += 1
            if stage_code is None:
                continue
            card, key = make_card(comp, meta, by_norm, "FINISHED", stage_code, stage_label)
            if not card:
                if key:
                    unplaceable.add(key)
                continue
            card[TAG] = TAG_FINAL
            # Record EVERY finished knockout result (any age) so we can correct the
            # producer's stored copy whenever football-data has it wrong (notably
            # mis-parsed penalty shootouts). This is independent of the recency
            # window below, so a correction doesn't lapse after FINAL_GAP_HOURS.
            if stage_code != "GROUP_STAGE":
                ko_final_results[pair_key(card["home"]["name"], card["away"]["name"])] = card
            # Only GAP-FILL (inject into recent) a game that JUST finished — older
            # finished games are history football-data has rotated out, not a gap;
            # back-filling them all is what flooded `recent`.
            dt = _kickoff_dt(comp)
            if dt is not None and (now - dt).total_seconds() <= FINAL_GAP_HOURS * 3600:
                (final_group if stage_code == "GROUP_STAGE" else final_ko).append(card)
        else:  # "pre" (scheduled): track the soonest kickoff for the watcher
            dt = _kickoff_dt(comp)
            if dt is not None:
                mins = (dt - now).total_seconds() / 60.0
                mins = mins if mins > 0 else 0.0
                next_kick_mins = mins if next_kick_mins is None else min(next_kick_mins, mins)

    # Group live games are the only ones we REPLACE in the file (this is what
    # fixes the "Ivory Coast" vs "Côte d'Ivoire" duplicate-live bug). Knockout
    # live games are owned by update_scores.py — we never key off them for
    # removal, so we can't strip the producer's richer knockout card.
    group_live_keys = {pair_key(c["home"]["name"], c["away"]["name"]) for c in live_group}

    # Identity key for a card ALREADY in data.json. Crucial: football-data's live
    # feed may spell a team differently from the canonical name the overlay injects
    # (e.g. "Ivory Coast" vs "Côte d'Ivoire"), so we canonicalize each side through
    # the same name map before keying — otherwise the football-data copy wouldn't
    # match the ESPN copy and both would show (the duplicate-live-game bug).
    def existing_key(m):
        h = (m.get("home") or {}).get("name")
        a = (m.get("away") or {}).get("name")
        return pair_key(canonicalize(h, meta, by_norm) or h,
                        canonicalize(a, meta, by_norm) or a)

    # 3) A GROUP live game must not also sit in recent/upcoming (or as a stale
    #    football-data live entry) — remove any matching pair so it shows once.
    for key in ("live", "recent", "upcoming"):
        data[key] = [m for m in data[key] if existing_key(m) not in group_live_keys]

    # 4) What's present across the file now (after group-live removal). A knockout
    #    card — live or final — is only injected when its pairing is absent here,
    #    i.e. football-data genuinely hasn't published the game yet.
    present = set()
    for key in ("live", "recent", "upcoming"):
        for m in data[key]:
            present.add(existing_key(m))

    def _pk(c):
        return pair_key(c["home"]["name"], c["away"]["name"])

    # 5) Inject / refresh.
    #    Group live cards prepend unconditionally (their duplicates were removed).
    #    Knockout live games are handled by what football-data already has:
    #      * football-data FINISHED it  -> defer entirely (never revert a final to
    #        live just because ESPN lags behind).
    #      * football-data has it LIVE   -> the watch loop never re-runs the
    #        producer, so its score is frozen at the baseline. Refresh the live
    #        score / status FROM ESPN in place, keeping the producer's richer
    #        fields (matchNo, bracket slots). This is what makes a knockout score
    #        actually tick over during the watch instead of sitting still.
    #      * football-data has it only as a not-yet-started `upcoming` fixture, or
    #        not at all -> bridge the ESPN live card in and drop the stale upcoming
    #        copy so it shows once, as live. (football-data is slow to flip a
    #        knockout TIMED -> IN_PLAY, which is the whole reason this overlay
    #        exists; a TIMED fixture must NOT count as the producer "owning" it.)
    DONE_STATUSES = {"FINISHED", "AWARDED"}
    LIVE_STATUSES = {"IN_PLAY", "PAUSED"}
    producer_done = set()
    producer_live_card = {}
    for _k in ("live", "recent", "upcoming"):
        for _m in data[_k]:
            st = _m.get("status")
            if st in DONE_STATUSES:
                producer_done.add(existing_key(_m))
            elif st in LIVE_STATUSES and _k == "live":
                producer_live_card.setdefault(existing_key(_m), _m)

    add_live_ko = []
    refreshed_live = 0   # producer live cards whose score we updated in place
    for c in live_ko:
        pk = _pk(c)
        if pk in group_live_keys or pk in producer_done:
            continue
        pm = producer_live_card.get(pk)
        if pm is not None:
            # Refresh football-data's own live card with ESPN's current score,
            # leaving its structural fields (matchNo, slot, resolved/projected)
            # untouched. Mutating in place means the card survives next cycle's
            # tag-strip, so matchNo isn't lost while the score keeps updating.
            pm["status"] = c["status"]
            pm["penalties"] = c["penalties"]
            pm["winner"] = c["winner"]
            for side in ("home", "away"):
                pm[side]["goals"] = c[side]["goals"]
                pm[side]["penGoals"] = c[side]["penGoals"]
            refreshed_live += 1
        else:
            add_live_ko.append(c)
    bridged_ko = {_pk(c) for c in add_live_ko}
    if bridged_ko:   # remove the stale not-yet-started copy so the game isn't shown twice
        data["upcoming"] = [m for m in data["upcoming"]
                            if existing_key(m) not in bridged_ko]
    live_group.sort(key=lambda c: c.get("utcDate") or "")
    add_live_ko.sort(key=lambda c: c.get("utcDate") or "")
    data["live"] = live_group + add_live_ko + data["live"]
    for c in live_group + add_live_ko:        # so a final-gap card can't re-add a live one
        present.add(_pk(c))

    # Finished knockout games: ESPN is authoritative for the actual result.
    # football-data's free-tier feed mis-reports penalty shootouts (a real
    # 1-1 / 4-3-on-penalties can arrive as 0-1 with a 5-5 shootout and a null
    # winner), and we otherwise defer to it once it flags the game finished, so its
    # bad result would stick. When ESPN has a finished knockout result, overwrite
    # the producer card's scoreline, shootout tally, penalties flag and winner from
    # ESPN (keeping matchNo, bracket slots and owners), and make sure the game ends
    # up in `recent`, not stranded in live/upcoming. Mutated in place so it survives
    # the next tag-strip and is re-applied if a later producer run rewrites the bad
    # values.
    def _apply_final(m, c):
        m["status"] = "FINISHED"
        m["penalties"] = c["penalties"]
        m["winner"] = c["winner"]
        for side in ("home", "away"):
            m[side]["goals"] = c[side]["goals"]
            m[side]["penGoals"] = c[side]["penGoals"]

    corrected_final = 0
    if ko_final_results:
        relocated = []
        for k in ("live", "upcoming"):       # a finished game shouldn't sit here
            kept = []
            for m in data[k]:
                c = ko_final_results.get(existing_key(m))
                if c is None:
                    kept.append(m)
                else:
                    _apply_final(m, c); corrected_final += 1; relocated.append(m)
            data[k] = kept
        for m in data["recent"]:             # correct any copy already in recent
            c = ko_final_results.get(existing_key(m))
            if c is not None:
                _apply_final(m, c); corrected_final += 1
        # The bracket page reads data["bracket"] (rounds -> matches) separately, so
        # correct the played match there too. Only touch matches already marked
        # finished, never a future projection that happens to share a pairing.
        for rnd in (data.get("bracket") or []):
            for m in (rnd.get("matches") or []):
                if m.get("status") not in ("FINISHED", "AWARDED"):
                    continue
                c = ko_final_results.get(existing_key(m))
                if c is not None:
                    _apply_final(m, c); corrected_final += 1
        if relocated:                        # re-home the pulled ones (present already
            relocated.sort(key=lambda m: m.get("utcDate") or "", reverse=True)
            data["recent"] = relocated + data["recent"]   # lists their keys, so no dup)

    # final-gap: group + knockout, only when the game is absent everywhere.
    add_finals = [c for c in (final_group + final_ko) if _pk(c) not in present]
    add_finals.sort(key=lambda c: c.get("utcDate") or "", reverse=True)

    # football-data decides the recent window (normally 5). When we prepend a
    # just-finished game it would otherwise become 6 (or 7 for two), so trim the
    # oldest back to that window — the bridged games are newer and stay, the
    # oldest football-data entries drop off. Once football-data posts the result
    # itself, the game is "present", nothing is bridged, and recent is its own 5.
    base_recent_len = len(data["recent"])
    data["recent"] = add_finals + data["recent"]
    if add_finals and base_recent_len and len(data["recent"]) > base_recent_len:
        data["recent"] = data["recent"][:base_recent_len]

    if unplaceable:
        for nm in sorted(unplaceable):
            print(f"WARNING: ESPN team '{nm}' not matched to a canonical name; "
                  f"its game was skipped.", file=sys.stderr)

    # 6) Write only if something actually changed (keeps the no-change check happy).
    print(f"LIVE_COUNT={live_total}")
    print(f"FINISHED_COUNT={finished_count}")
    print(f"NEXT_KICKOFF_MINS={int(next_kick_mins) if next_kick_mins is not None else -1}")
    if data == original:
        print("No live changes; data.json left untouched.")
        return 0

    text = json.dumps(data, indent=2, ensure_ascii=False)
    if raw.endswith("\n"):
        text += "\n"
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    n_live = len(live_group) + len(add_live_ko)
    print(f"Patched {DATA_FILE}: {n_live} live injected "
          f"({len(add_live_ko)} knockout-bridged), "
          f"{refreshed_live} live score(s) refreshed, "
          f"{corrected_final} finished KO result(s) corrected, "
          f"{len(add_finals)} final-gap game(s) injected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
