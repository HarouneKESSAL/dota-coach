#!/usr/bin/env python3
"""Bake per-bracket, per-position hero averages from STRATZ into data/brackets.json.

OpenDota cannot do this: its only rank-aware table covers replay-parsed matches,
and those skew so hard to high MMR that Herald has no rows at all. STRATZ
ingests every match, so its brackets are usable at the bottom of the ladder,
which is the whole point of this tool.

Needs a free STRATZ token (https://stratz.com/api, sign in with Steam):

    export STRATZ_TOKEN=...        # bash
    $env:STRATZ_TOKEN = "..."      # PowerShell
    python bake_stratz.py --probe  # one call: proves the query, settles units
    python bake_stratz.py          # the real bake, ~7 requests

The token is read here and only here. The output is a static file, so the key
never reaches a browser. Re-run it on the same schedule as bake.py.

    python bake_stratz.py --selftest    # folding logic, no token, no network
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.stratz.com/graphql"
OUT = pathlib.Path(__file__).parent / "data" / "brackets.json"

# STRATZ pairs adjacent medals, so there are four real buckets, not eight.
BRACKETS = ["HERALD_GUARDIAN", "CRUSADER_ARCHON", "LEGEND_ANCIENT", "DIVINE_IMMORTAL"]
POSITIONS = ["POSITION_1", "POSITION_2", "POSITION_3", "POSITION_4", "POSITION_5"]

# What we read back per (hero, bracket, position). These are AVERAGES, not
# percentiles: STRATZ does not expose a distribution here, so the report says
# "+18% vs average", never "top 20%".
#
# goldPerMinute is deliberately absent. The field exists on this type but comes
# back null for every row, so `networth` is the gold-ish comparison instead.
# Everything here is a per-match average, verified against a live response.
FIELDS = ["kills", "deaths", "assists", "networth", "xp", "cs",
          "heroDamage", "towerDamage"]

# A cell built from a handful of games is noise. Below this it is dropped, so a
# rare hero/position/bracket combination has no comparison rather than a
# confident wrong one.
MIN_MATCHES = 50


def build_query(hero_ids):
    """Literals are inlined rather than passed as GraphQL variables.

    Variables would need their declared types to be exactly right
    ($heroIds: [Short!], and the two enum type names), and a wrong guess fails
    the whole request. Inline integers and bare enum members need no
    declarations. Everything interpolated here is generated above, never user
    input.
    """
    return """
{
  heroStats {
    stats(
      heroIds: [%s]
      bracketBasicIds: [%s]
      positionIds: [%s]
      groupByBracket: true
      groupByPosition: true
    ) {
      heroId
      bracketBasicIds
      position
      matchCount
      winCount
      %s
    }
  }
}
""" % (", ".join(str(int(h)) for h in hero_ids),
       ", ".join(BRACKETS),
       ", ".join(POSITIONS),
       "\n      ".join(FIELDS))


def post_raw(token, hero_ids):
    """The whole GraphQL payload, errors included. Used by --probe."""
    body = json.dumps({"query": build_query(hero_ids)}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # STRATZ rejects some clients without this exact User-Agent.
            "User-Agent": "STRATZ_API",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def post(token, hero_ids):
    payload = post_raw(token, hero_ids)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"])[:400])
    stats = ((payload.get("data") or {}).get("heroStats") or {}).get("stats")
    return stats or []


def fold(rows, out, min_matches=MIN_MATCHES):
    """Fold API rows into {hero: {bracket: {position: cell}}}. Returns drops."""
    dropped = 0
    for row in rows:
        n = row.get("matchCount") or 0
        hero = row.get("heroId")
        bracket = row.get("bracketBasicIds")
        pos = row.get("position")
        if hero is None or not bracket or not pos:
            dropped += 1
            continue
        if n < min_matches:
            dropped += 1
            continue
        cell = {"n": n, "win": row.get("winCount") or 0}
        for f in FIELDS:
            v = row.get(f)
            if v is not None:
                cell[f] = round(float(v), 1)
        out.setdefault(str(hero), {}).setdefault(bracket, {})[pos] = cell
    return dropped


def main():
    token = os.environ.get("STRATZ_TOKEN")
    if not token:
        sys.exit("Set STRATZ_TOKEN first. Get a free one at https://stratz.com/api")

    dota = pathlib.Path(__file__).parent / "data" / "dota.json"
    if not dota.exists():
        sys.exit(f"Run bake.py first: {dota} is missing.")
    hero_ids = sorted(int(h) for h in json.loads(dota.read_text(encoding="utf-8"))["heroes"])

    out, dropped = {}, 0
    # Heroes go up in chunks: all of them at once tends to time out, one per
    # request wastes the quota.
    chunk = 20
    for i in range(0, len(hero_ids), chunk):
        batch = hero_ids[i:i + chunk]
        print(f"[{i + len(batch)}/{len(hero_ids)}] heroes {batch[0]}-{batch[-1]}", flush=True)
        try:
            rows = post(token, batch)
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                detail = " " + e.read()[:300].decode("utf-8", "replace")
            sys.exit(f"STRATZ request failed: {e}{detail}")
        dropped += fold(rows, out)
        time.sleep(1.0)

    if not out:
        sys.exit("STRATZ returned no usable rows. Nothing written.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "baked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "stratz",
        "min_matches": MIN_MATCHES,
        "brackets": BRACKETS,
        "positions": POSITIONS,
        "fields": FIELDS,
        "heroes": out,
    }), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"\nwrote {OUT} ({kb:.0f} KB), {len(out)} heroes, "
          f"{dropped} cells dropped under {MIN_MATCHES} matches")


# A support's plausible range decides whether a field is per-minute or a
# per-match total. The two are an order of magnitude apart, so the call is safe.
UNIT_SPLIT = {"xp": 3000, "cs": 20, "heroDamage": 3000, "networth": 3000,
              "towerDamage": 100}

# The OpenDota field each one would be compared against, and whether THAT field
# is per-minute. If the two disagree, the comparison is meaningless.
# A live response settled all of these: STRATZ returns per-match averages, which
# line up with OpenDota's per-match totals everywhere except xp, where OpenDota
# only publishes xp_per_min.
MATCH_SIDE = {
    "xp": ("xp_per_min", True),
    "cs": ("last_hits", False),
    "heroDamage": ("hero_damage", False),
    "networth": ("net_worth", False),
    "towerDamage": ("tower_damage", False),
}


def probe(token):
    """One request for one hero: proves the query and settles the ambiguous units.

    Run this before the full bake. It costs a single call, so a wrong field name
    or a bad token shows up immediately instead of after 127 heroes.
    """
    print(f"POST {ENDPOINT}  (1 hero, 4 brackets, 5 positions)\n")
    try:
        payload = post_raw(token, [5])  # Crystal Maiden: common at every bracket
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode("utf-8", "replace")
        print(f"HTTP {e.code}\n{body}")
        if e.code in (401, 403):
            print("\nThat is the auth gate. Check STRATZ_TOKEN is the bearer token\n"
                  "from https://stratz.com/api, not a session cookie.")
        return 1
    except urllib.error.URLError as e:
        print(f"Could not reach STRATZ: {e}")
        return 1

    if payload.get("errors"):
        print("GraphQL rejected the query:\n")
        print(json.dumps(payload["errors"], indent=2)[:1200])
        print("\nThe field or argument names in build_query() need correcting.")
        return 1

    rows = ((payload.get("data") or {}).get("heroStats") or {}).get("stats") or []
    print(f"OK: {len(rows)} rows returned.\n")
    if not rows:
        print("No rows. groupByBracket/groupByPosition may need different arguments.")
        return 1

    print("First row verbatim:")
    print(json.dumps(rows[0], indent=2)[:900])

    missing = [f for f in ["heroId", "bracketBasicIds", "position", "matchCount"]
               if f not in rows[0]]
    print(f"\nGrouping keys: {'all present' if not missing else 'MISSING ' + str(missing)}")
    if missing:
        print("fold() cannot build cells without these. Fix the query first.")
        return 1

    # Settle what the README flags as unknown.
    best = max(rows, key=lambda r: r.get("matchCount") or 0)
    print(f"\nUnits, judged from the largest cell "
          f"({best.get('bracketBasicIds')} {best.get('position')}, "
          f"n={best.get('matchCount')}):")
    print(f"  {'goldPerMinute':<14} {best.get('goldPerMinute')}  (per minute by name)")
    verdict = {}
    for field, split in UNIT_SPLIT.items():
        v = best.get(field)
        if v is None:
            print(f"  {field:<14} absent from the response")
            continue
        per_min = float(v) < split
        verdict[field] = per_min
        print(f"  {field:<14} {v}  -> looks like "
              f"{'PER MINUTE' if per_min else 'a PER-MATCH TOTAL'}")

    if verdict:
        print("\nTo display these, add to VS_BRACKET in coach.py and index.html:")
        mismatched = False
        for field, per_min in verdict.items():
            key, want_per_min = MATCH_SIDE[field]
            ok = per_min == want_per_min
            mismatched = mismatched or not ok
            mark = "" if ok else f"   # MISMATCH: {key} is " + (
                "a match total" if not want_per_min else "per minute")
            print(f'  ("{field}", "{key}", "{field}", False),{mark}')
        if mismatched:
            print("\nA mismatch means STRATZ and OpenDota use different units for the\n"
                  "same stat. Convert one side before comparing, or leave it out.")
    return 0


def selftest():
    q = build_query([1, 14])
    assert "heroIds: [1, 14]" in q
    # Enum members must be bare, never quoted, or the server rejects them.
    assert "bracketBasicIds: [HERALD_GUARDIAN, CRUSADER_ARCHON," in q
    assert '"HERALD_GUARDIAN"' not in q
    assert "positionIds: [POSITION_1," in q
    assert "networth" in q and "matchCount" in q
    # Always null in a real response; asking for it would just be noise.
    assert "goldPerMinute" not in q
    assert q.count("{") == q.count("}"), "unbalanced braces in query"

    rows = [
        {"heroId": 5, "bracketBasicIds": "HERALD_GUARDIAN", "position": "POSITION_5",
         "matchCount": 900, "winCount": 450, "networth": 10561.456, "deaths": 8.2},
        # Under the floor: must be dropped, not averaged in.
        {"heroId": 5, "bracketBasicIds": "DIVINE_IMMORTAL", "position": "POSITION_5",
         "matchCount": 3, "winCount": 2, "networth": 99999.0},
        # Ungrouped junk rows must not become a cell.
        {"heroId": 5, "bracketBasicIds": None, "position": "POSITION_5", "matchCount": 900},
        {"heroId": None, "bracketBasicIds": "LEGEND_ANCIENT", "position": "POSITION_1",
         "matchCount": 900},
    ]
    out = {}
    dropped = fold(rows, out)
    assert dropped == 3, dropped
    assert list(out) == ["5"]
    cell = out["5"]["HERALD_GUARDIAN"]["POSITION_5"]
    assert cell["n"] == 900 and cell["win"] == 450
    assert cell["networth"] == 10561.5, cell
    assert "DIVINE_IMMORTAL" not in out["5"]
    # A field the API omitted must be absent, not zero.
    assert "cs" not in cell

    print("bake_stratz self-check OK (folding and query shape)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--probe" in sys.argv:
        tok = os.environ.get("STRATZ_TOKEN")
        if not tok:
            sys.exit("Set STRATZ_TOKEN first. Get a free one at https://stratz.com/api")
        sys.exit(probe(tok))
    else:
        main()
