"""Verify your tracker.gg key works and see what the bot parses.

    python check_tracker.py "Name#TAG"

Prints the parsed profile and latest match, and writes the raw JSON to
debug_*.json so you can adjust tracker.py if tracker.gg changes field names.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from tracker import TrackerClient, TrackerError, _q, parse_riot_id


async def main(riot_id: str):
    client = TrackerClient(os.environ["TRACKER_API_KEY"])
    try:
        raw = await client.raw(f"/profile/riot/{_q(riot_id)}")
        with open("debug_profile.json", "w") as f:
            json.dump(raw, f, indent=2)
        print("Profile:", await client.get_profile(riot_id))

        matches = await client.get_recent_matches(riot_id, "any")
        print(f"\n{len(matches)} recent matches; newest:", matches[0] if matches else None)
        if matches:
            raw = await client.raw(f"/matches/{_q(matches[0].id)}")
            with open("debug_match.json", "w") as f:
                json.dump(raw, f, indent=2)
            detail = await client.get_match(matches[0].id)
            print(f"\n{detail.map} · {detail.mode} · total rounds {detail.total_rounds}")
            for tid, t in detail.teams.items():
                print(f"  team {tid}: won={t.won} rounds={t.rounds_won}")
            for p in detail.players:
                print(f"  [{p.team}] {p.riot_id:<22} {p.agent:<10} {p.kills}/{p.deaths}/{p.assists} "
                      f"ACS {p.acs or 0:.0f} {p.rank}")
        print("\nRaw JSON written to debug_profile.json / debug_match.json")
    finally:
        await client.close()


if __name__ == "__main__":
    load_dotenv()
    rid = parse_riot_id(" ".join(sys.argv[1:]))
    if not rid:
        sys.exit('Usage: python check_tracker.py "Name#TAG"')
    try:
        asyncio.run(main(rid))
    except TrackerError as e:
        sys.exit(f"tracker.gg error: {e}")
