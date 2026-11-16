#!/usr/bin/env python3
"""Bake OpenDota hero aggregates into a static data/dota.json.

Run this once a day (cron / GitHub Action). The app never calls OpenDota live,
so the free 2000 req/day limit is never a user-facing problem.

    python bake.py
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = "https://api.opendota.com/api"
OUT = pathlib.Path(__file__).parent / "data" / "dota.json"
# OpenDota free tier: 60 req/min. One call per hero, so pace just under it.
SLEEP = 1.05


def get(path):
    req = urllib.request.Request(
        API + path, headers={"User-Agent": "dota-coach-bake/0.1"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_retry(path, tries=3):
    for attempt in range(tries):
        try:
            return get(path)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise
            # 429 or a blip: back off and try again.
            print(f"  retry {path} after {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def main():
    print("fetching constants...")
    item_ids = get_retry("/constants/item_ids")  # {"1": "blink", ...}
    items = get_retry("/constants/items")  # {"blink": {"cost": 2250, "dname": ...}}
    heroes = get_retry("/heroes")

    costs = {}
    names = {}
    for key, meta in items.items():
        if not isinstance(meta, dict):
            continue
        costs[key] = meta.get("cost") or 0
        names[key] = meta.get("dname") or key.replace("_", " ").title()

    out = {
        "baked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "item_cost": costs,
        "item_name": names,
        "builds_into": build_ancestors(items),
        "heroes": {},
    }

    total = len(heroes)
    for i, h in enumerate(heroes, 1):
        hid = str(h["id"])
        # flush: this takes minutes, and a redirected log should show progress.
        print(f"[{i}/{total}] {h['localized_name']}", flush=True)
        try:
            pop = get_retry(f"/heroes/{hid}/itemPopularity")
        except Exception as e:  # one dead hero must not kill the whole bake
            print(f"  skipped: {e}", file=sys.stderr)
            pop = {}
        out["heroes"][hid] = {
            "name": h["localized_name"],
            # GSI reports hero as "npc_dota_hero_antimage"; keep the key to match on.
            "npc": h["name"],
            "roles": h.get("roles", []),
            "phases": {
                phase: resolve(pop.get(f"{phase}_items", {}), item_ids)
                for phase in ("start_game", "early_game", "mid_game", "late_game")
            },
        }
        time.sleep(SLEEP)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"\nwrote {OUT} ({kb:.0f} KB), {len(out['heroes'])} heroes")


def build_ancestors(items):
    """{component_key: [every item it eventually builds into]}.

    OpenDota gives each item its direct `components` only, so Blade of Alacrity
    knows about Yasha but not Manta Style. The coach wants to name the item the
    hero is actually building, which may be several tiers up, so close the
    relation here once rather than walking it on every GSI tick.
    """
    parents = {}
    for key, meta in items.items():
        if not isinstance(meta, dict):
            continue
        for child in meta.get("components") or []:
            parents.setdefault(child, set()).add(key)

    out = {}
    for child in parents:
        seen, stack = set(), list(parents[child])
        while stack:
            p = stack.pop()
            if p in seen:  # also guards against a malformed cyclic recipe
                continue
            seen.add(p)
            stack.extend(parents.get(p, ()))
        out[child] = sorted(seen)
    return out


def resolve(pop, item_ids):
    """{item_id: count} -> [[item_key, count], ...] sorted most-bought first."""
    rows = []
    for iid, count in pop.items():
        key = item_ids.get(str(iid))
        if key:
            rows.append([key, count])
    rows.sort(key=lambda r: -r[1])
    return rows


if __name__ == "__main__":
    main()
