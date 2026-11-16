#!/usr/bin/env python3
"""Free Dota coach. Two modes, no account, no subscription, no ML.

    python coach.py review 9092563405 --player 1   # post-game, works today
    python coach.py live                           # GSI listener on :3000
    python coach.py demo                           # self-check, no network

`review` is the one to ship first: it needs nothing installed on the player's
machine, just a match id. `live` needs the .cfg dropped into the Dota folder.
"""
import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

DATA = pathlib.Path(__file__).parent / "data" / "dota.json"
# Optional: written by bake_stratz.py. Without it the bracket section is skipped.
BRACKETS_FILE = pathlib.Path(__file__).parent / "data" / "brackets.json"
API = "https://api.opendota.com/api"
TURBO = 23  # game_mode id
# Seconds. Outside this band, per-match totals stop being comparable with an
# average taken over games of every length.
NORMAL_LENGTH = (1800, 2700)

# OpenDota's benchmark pct is only unambiguous where more is plainly better.
# deaths_per_min and hero_healing_per_min tie at 0 for most players, which
# lands a 0 at the ~95th percentile and reads as praise. They are excluded.
HIGHER_IS_BETTER = {
    "gold_per_min": "GPM",
    "xp_per_min": "XPM",
    "last_hits_per_min": "last hits/min",
    "hero_damage_per_min": "hero damage/min",
}

# Ranking a player against the nine others in their own game needs no extra
# data, works on matches nobody replay-parsed, and is immune to the two things
# that make global percentiles lie: Turbo inflating everyone's numbers, and a
# Herald being measured against Immortals. Same bracket, patch and game length
# by construction.
IN_GAME = [
    ("gold_per_min", "GPM", False),
    ("xp_per_min", "XPM", False),
    ("last_hits", "Last hits", False),
    ("hero_damage", "Hero damage", False),
    ("deaths", "Deaths", True),
]

# STRATZ field -> OpenDota field. Units were settled against a live response:
# STRATZ returns per-match averages, which match OpenDota's per-match totals.
# Two fields are deliberately absent: goldPerMinute comes back null for every
# row, and xp has no OpenDota counterpart (only xp_per_min), so networth and cs
# carry the economy comparison instead.
VS_BRACKET = [
    ("networth", "net_worth", "Net worth", False),
    ("cs", "last_hits", "Last hits", False),
    ("heroDamage", "hero_damage", "Hero damage", False),
    ("kills", "kills", "Kills", False),
    ("deaths", "deaths", "Deaths", True),
    ("assists", "assists", "Assists", False),
]

# Items worth nagging about regardless of what the aggregate says. Wards are
# deliberately absent: nagging a carry to buy observers is bad advice.
STAPLES = {"tpscroll": "No TP scroll. Buy one."}


# ---------------------------------------------------------------- shared

def load_data():
    if not DATA.exists():
        sys.exit(f"Missing {DATA}. Run: python bake.py")
    return json.loads(DATA.read_text(encoding="utf-8"))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dota-coach/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def phase_of(clock_time):
    """Dota clock seconds -> which itemPopularity bucket applies."""
    if clock_time < 0:
        return "start_game"
    if clock_time < 600:
        return "early_game"
    if clock_time < 1500:
        return "mid_game"
    return "late_game"


# ---------------------------------------------------------------- live

def owned_items(items):
    """GSI `items` block -> set of bare item keys, inventory + backpack + stash."""
    out = set()
    for slot, meta in (items or {}).items():
        if not isinstance(meta, dict):
            continue
        name = meta.get("name", "empty")
        if name and name != "empty":
            out.add(name[5:] if name.startswith("item_") else name)
    return out


def goal_for(key, pool_rank, data):
    """Which item in this hero's pool is `key` a component of?

    Returns a pool item key, or None if `key` is a goal in its own right.
    When several ancestors are in the pool (Blade of Alacrity builds toward
    both Yasha and Manta Style) the most-bought one wins, since that is the
    one the hero is usually heading for.
    """
    best = None
    for parent in data.get("builds_into", {}).get(key, ()):
        rank = pool_rank.get(parent)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, parent)
    return best[1] if best else None


def suggest(payload, data, top=3):
    """GSI payload -> list of advice lines. Pure function, hence testable."""
    player = payload.get("player") or {}
    hero = payload.get("hero") or {}
    gamemap = payload.get("map") or {}

    # Spectating nests everything under team2/team3; only own-match is handled.
    if player and all(k.startswith("team") for k in player):
        return ["Spectator feed not supported yet."]
    if not hero.get("id"):
        return []

    entry = data["heroes"].get(str(hero["id"]))
    if not entry:
        return []

    gold = player.get("gold", 0)
    clock = gamemap.get("clock_time", 0)
    have = owned_items(payload.get("items"))
    pool = entry["phases"].get(phase_of(clock), [])

    lines = []
    for key, msg in STAPLES.items():
        if key not in have and clock > 60:
            lines.append(msg)

    pool_rank = {key: i for i, (key, _count) in enumerate(pool)}
    affordable, upcoming = [], []
    for key, _count in pool:
        if key in have:
            continue
        cost = data["item_cost"].get(key, 0)
        if cost <= 0:  # recipes and non-purchasables
            continue
        goal = goal_for(key, pool_rank, data)
        if goal and goal in have:
            # Already holding the finished item; its parts were consumed into
            # it, so the aggregate still listing them is noise.
            continue
        label = f"{data['item_name'].get(key, key)} ({cost}g)"
        if goal:
            label += f" -> {data['item_name'].get(goal, goal)}"
        (affordable if cost <= gold else upcoming).append(label)

    if affordable:
        lines.append(f"Can buy now ({gold}g): " + ", ".join(affordable[:top]))
    if upcoming:
        lines.append("Next goal: " + ", ".join(upcoming[:top]))
    return lines


def serve(port, data):
    last = [None]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            # Answer before thinking: Dota drops the feed if we are slow.
            self.send_response(200)
            self.end_headers()
            try:
                payload = json.loads(body)
            except ValueError:
                return
            lines = suggest(payload, data)
            if lines and lines != last[0]:
                last[0] = lines
                clock = (payload.get("map") or {}).get("clock_time", 0)
                print(f"\n[{clock // 60:02d}:{abs(clock) % 60:02d}]")
                for line in lines:
                    print("  " + line)

        def log_message(self, *_):  # GSI posts every second; stay quiet
            pass

    print(f"Listening on http://localhost:{port}/gsi  (Ctrl+C to stop)")
    print("Dota needs the .cfg in place and -gamestateintegration set.")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ---------------------------------------------------------------- review

def pick_player(match, who):
    """`who` may be an account id, a slot index 0-9, or part of a hero name."""
    players = match["players"]
    if who is None:
        return players[0]
    if who.isdigit():
        n = int(who)
        if n < len(players) and len(who) <= 2:
            return players[n]
        for p in players:
            if p.get("account_id") == n:
                return p
        sys.exit(f"No player {who} in this match.")
    data = load_data()
    for p in players:
        name = data["heroes"].get(str(p["hero_id"]), {}).get("name", "")
        if who.lower() in name.lower():
            return p
    sys.exit(f"No hero matching {who!r} in this match.")


def load_brackets():
    """STRATZ averages, if someone has run bake_stratz.py. Optional by design."""
    if not BRACKETS_FILE.exists():
        return None
    try:
        data = json.loads(BRACKETS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if data.get("heroes") else None


def infer_position(p, mates):
    """Net worth order inside a team is the usual proxy for position 1 to 5."""
    def nw(x):
        return x.get("net_worth") or x.get("total_gold") or 0
    for i, x in enumerate(sorted(mates, key=nw, reverse=True), 1):
        if x.get("player_slot") == p.get("player_slot"):
            return f"POSITION_{i}"
    return None


def rank_among(players, me, key, lower=False):
    """Competition rank: 1 + however many players beat `me` on this metric."""
    mine = me.get(key)
    if mine is None:
        return None
    vals = [p.get(key) for p in players if p.get(key) is not None]
    if len(vals) < 2:
        return None
    better = sum(1 for v in vals if (v < mine if lower else v > mine))
    return better + 1, len(vals)


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def weakest_window(gold_t, minutes=5):
    """Lowest-earning N-minute stretch after the lane phase. (start_min, gold)."""
    per_min = [b - a for a, b in zip(gold_t, gold_t[1:])]
    if len(per_min) < minutes + 5:
        return None
    best = None
    for i in range(5, len(per_min) - minutes + 1):
        total = sum(per_min[i:i + minutes])
        if best is None or total < best[1]:
            best = (i, total)
    return best


def review(match_id, who, bracket=None):
    data = load_data()
    try:
        match = get_json(f"{API}/matches/{match_id}")
    except urllib.error.HTTPError as e:
        sys.exit(f"OpenDota said {e.code} for match {match_id}.")

    # A match that ended moments ago exists as a row before its players are
    # ingested: ten entries with hero_id 0 and no stats.
    if not any(pl.get("hero_id") for pl in match.get("players") or []):
        sys.exit(f"OpenDota knows match {match_id} but has not processed its "
                 f"players yet. Try again in a few minutes.")

    p = pick_player(match, who)
    hero = data["heroes"].get(str(p["hero_id"]), {}).get("name", f"hero {p['hero_id']}")
    won = p.get("win") == 1
    dur = match.get("duration", 0)

    # ASCII only: the Windows console is cp1252 and mangles em-dashes.
    print(f"\n{hero} - {'WIN' if won else 'LOSS'} in {dur // 60}:{dur % 60:02d}")
    print(f"  {p.get('kills')}/{p.get('deaths')}/{p.get('assists')}  "
          f"{p.get('gold_per_min')} GPM  {p.get('last_hits')} LH")

    mates = [x for x in match["players"]
             if (x.get("player_slot", 0) < 128) == (p.get("player_slot", 0) < 128)]
    rows = []
    for key, label, lower in IN_GAME:
        overall = rank_among(match["players"], p, key, lower)
        if not overall:
            continue
        line = f"    {label:<14}{p.get(key):>8}   {ordinal(overall[0])} of {overall[1]}"
        team = rank_among(mates, p, key, lower)
        if team:
            line += f", {ordinal(team[0])} on team"
        rows.append(line)
    if rows:
        print("\n  In this game:")
        print("\n".join(rows))

    marks = p.get("benchmarks") or {}
    scored = [(m, marks[m]["pct"]) for m in HIGHER_IS_BETTER
              if marks.get(m, {}).get("pct") is not None]

    brackets = load_brackets()
    cell = None
    position = infer_position(p, mates)
    if brackets and bracket and position:
        cell = ((brackets["heroes"].get(str(p["hero_id"])) or {})
                .get(bracket, {}).get(position))

    # Turbo doubles gold and xp, which skews every comparison against an outside
    # population: the rank averages as much as the all-ranks percentiles. The
    # in-game ranking is the only part that survives it.
    if match.get("game_mode") == TURBO and (cell or scored):
        print("\n  Turbo match. Everything below compares you with players who were "
              "not\n  in a Turbo game, so it flatters you. The in-game ranking is "
              "unaffected.")

    if cell:
        print(f"\n  Versus {bracket.replace('_', '-').title()} "
              f"{position.replace('POSITION_', 'pos ')} {hero} "
              f"({cell['n']:,} matches):")
        for api, key, label, lower in VS_BRACKET:
            avg, mine = cell.get(api), p.get(key)
            if not avg or mine is None:
                continue
            delta = (mine - avg) / avg * 100
            print(f"    {label:<10}{mine:>8}  vs {avg:>7} avg   {delta:+.0f}%")
        print("    (position guessed from net worth order)")
        # Net worth, last hits and hero damage are per-match totals, and STRATZ
        # publishes no average duration to divide by, so an unusually long or
        # short game skews them. Say so rather than let the number mislead.
        if dur and not (NORMAL_LENGTH[0] <= dur <= NORMAL_LENGTH[1]):
            longer = "long" if dur > NORMAL_LENGTH[1] else "short"
            print(f"    Note: at {dur // 60} min this game was {longer}. Net worth, "
                  f"last hits\n    and hero damage are match totals, so they skew "
                  f"{'high' if longer == 'long' else 'low'} here.")
    elif brackets and not bracket:
        print(f"\n  Pass --bracket to compare against your rank: "
              f"{', '.join(brackets['brackets'])}")

    if scored:
        print(f"\n  Versus other {hero} games, all ranks pooled:")
        for metric, pct in sorted(scored, key=lambda r: r[1]):
            band = "bottom" if pct < 0.5 else "top"
            shown = pct if pct < 0.5 else 1 - pct
            print(f"    {HIGHER_IS_BETTER[metric]:<18} {band} {shown * 100:.0f}%")

        worst = min(scored, key=lambda r: r[1])
        if worst[1] < 0.35:
            print(f"\n  Weakest: {HIGHER_IS_BETTER[worst[0]]}. Fix that one first.")
    elif rows:
        print("\n  No all-games benchmarks for this match yet.")
    else:
        print("\n  Nothing to compare yet. OpenDota has the match but not its stats.")
        return

    window = weakest_window(p.get("gold_t") or [])
    if window:
        start, total = window
        print(f"  Quietest stretch: {start}-{start + 5} min, {total} gold earned.")

    deaths = p.get("deaths") or 0
    if dur and deaths > dur / 240:  # more than one death per 4 minutes
        print(f"  {deaths} deaths in {dur // 60} min is high. Deaths cost map "
              f"control, not just gold.")

    log = p.get("purchase_log") or []
    big = [(e["time"], e["key"]) for e in log
           if data["item_cost"].get(e["key"], 0) >= 2000]
    if big:
        t, key = big[0]
        label = data["item_name"].get(key, key)
        print(f"  First big item: {label} at {t // 60}:{t % 60:02d}.")


# ---------------------------------------------------------------- self-check

def demo():
    """Runs without network or a baked file. Fails loudly if logic breaks."""
    data = {
        "heroes": {"1": {"name": "Anti-Mage", "npc": "npc_dota_hero_antimage",
                         "roles": [], "phases": {
                             "start_game": [["tango", 9], ["quelling_blade", 5]],
                             "early_game": [["ring_of_health", 9], ["boots", 8]],
                             "mid_game": [["bfury", 9], ["broadsword", 7],
                                          ["manta", 4], ["yasha", 3],
                                          ["blade_of_alacrity", 2]],
                             "late_game": [["abyssal_blade", 3]]}}},
        "item_cost": {"tango": 90, "quelling_blade": 100, "ring_of_health": 850,
                      "boots": 500, "broadsword": 1000, "blade_of_alacrity": 1000,
                      "yasha": 2100, "bfury": 3900, "manta": 4650,
                      "abyssal_blade": 6375, "tpscroll": 50, "recipe_x": 0},
        "item_name": {"bfury": "Battle Fury", "boots": "Boots of Speed",
                      "ring_of_health": "Ring of Health", "broadsword": "Broadsword",
                      "yasha": "Yasha", "manta": "Manta Style",
                      "blade_of_alacrity": "Blade of Alacrity"},
        "builds_into": {"broadsword": ["bfury"], "yasha": ["manta"],
                        "blade_of_alacrity": ["manta", "yasha"]},
    }

    assert phase_of(-30) == "start_game"
    assert phase_of(0) == "early_game"
    assert phase_of(599) == "early_game"
    assert phase_of(600) == "mid_game"
    assert phase_of(1500) == "late_game"

    assert owned_items({"slot0": {"name": "item_tpscroll"},
                        "slot1": {"name": "empty"},
                        "stash0": {"name": "item_bfury"}}) == {"tpscroll", "bfury"}

    rank = {"bfury": 0, "broadsword": 1, "manta": 2, "yasha": 3}
    assert goal_for("broadsword", rank, data) == "bfury"
    # Two ancestors in the pool: the more-bought one (Manta, rank 2) wins.
    assert goal_for("blade_of_alacrity", rank, data) == "manta"
    assert goal_for("blade_of_alacrity", {"yasha": 3}, data) == "yasha"
    assert goal_for("bfury", rank, data) is None, "a goal item is not a component"
    assert goal_for("nonexistent", rank, data) is None

    payload = {
        "map": {"clock_time": 700},
        "player": {"gold": 1200},
        "hero": {"id": 1},
        "items": {"slot0": {"name": "item_tpscroll"}},
    }
    lines = suggest(payload, data)
    joined = " ".join(lines)
    buy = next(l for l in lines if l.startswith("Can buy now"))
    goal = next(l for l in lines if l.startswith("Next goal"))

    # Components must name what they are building toward.
    assert "Broadsword (1000g) -> Battle Fury" in buy, buy
    assert "Blade of Alacrity (1000g) -> Manta Style" in buy, buy
    assert "Yasha (2100g) -> Manta Style" in goal, goal
    # Finished items stand alone, with no arrow.
    assert "Battle Fury (3900g)" in goal and "Battle Fury (3900g) ->" not in goal
    assert "Battle Fury" not in buy.split("->")[0], "3900g offered at 1200 gold"
    assert "No TP scroll" not in joined, "TP scroll is held but still nagged"

    # Holding the finished item silences its parts, but not an unrelated branch.
    payload["items"]["slot1"] = {"name": "item_bfury"}
    buy2 = next(l for l in suggest(payload, data) if l.startswith("Can buy now"))
    assert "Broadsword" not in buy2, buy2
    assert "Blade of Alacrity" in buy2, buy2

    # Same state without the TP scroll must produce the nag.
    payload["items"] = {}
    assert any("No TP scroll" in l for l in suggest(payload, data))

    # Recipes (cost 0) must never be suggested.
    payload["map"]["clock_time"] = 1600
    data["heroes"]["1"]["phases"]["late_game"] = [["recipe_x", 9]]
    assert not any("recipe" in l for l in suggest(payload, data))

    # Unknown hero -> silence, not a crash.
    assert suggest({"hero": {"id": 999}, "map": {}, "player": {}}, data) == []
    assert suggest({"hero": {}, "map": {}, "player": {}}, data) == []

    # gold_t: minutes 10-14 are flat, so that is the window to find.
    gold_t = [0, 300, 600, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000,
              3010, 3020, 3030, 3040, 3050, 3400, 3800]
    assert weakest_window(gold_t) == (10, 50), weakest_window(gold_t)
    assert weakest_window([0, 100]) is None

    # Ranking within a single match.
    squad = [{"gold_per_min": 500, "deaths": 2},
             {"gold_per_min": 700, "deaths": 9},
             {"gold_per_min": 300, "deaths": 2}]
    assert rank_among(squad, squad[0], "gold_per_min") == (2, 3)
    # Tied players share the better rank, as in any competition ranking.
    assert rank_among(squad, squad[0], "deaths", lower=True) == (1, 3)
    assert rank_among(squad, squad[2], "deaths", lower=True) == (1, 3)
    assert rank_among(squad, squad[1], "deaths", lower=True) == (3, 3)
    assert rank_among(squad, squad[0], "last_hits") is None, "absent metric cannot rank"
    assert rank_among([squad[0]], squad[0], "gold_per_min") is None, "one player is no ranking"
    # A real zero must still rank, where a missing value must not.
    assert rank_among(squad, {"deaths": 0}, "deaths", lower=True) == (1, 3)

    assert [ordinal(n) for n in (1, 2, 3, 4, 10)] == ["1st", "2nd", "3rd", "4th", "10th"]
    assert [ordinal(n) for n in (11, 12, 13, 21)] == ["11th", "12th", "13th", "21st"]

    print("self-check OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    live = sub.add_parser("live", help="listen to Dota's GSI feed")
    live.add_argument("--port", type=int, default=3000)

    rev = sub.add_parser("review", help="analyse a finished match")
    rev.add_argument("match_id")
    rev.add_argument("--player", help="account id, slot 0-9, or hero name")
    rev.add_argument("--bracket", help="your rank, e.g. HERALD_GUARDIAN "
                                       "(needs bake_stratz.py to have run)")

    sub.add_parser("demo", help="run the self-check")

    args = ap.parse_args()
    # `live` runs for a whole match and is often piped to a log or a UI.
    # Block buffering would hold every suggestion until the process exits.
    sys.stdout.reconfigure(line_buffering=True)

    if args.cmd == "live":
        serve(args.port, load_data())
    elif args.cmd == "review":
        review(args.match_id, args.player, args.bracket)
    else:
        demo()


if __name__ == "__main__":
    main()
