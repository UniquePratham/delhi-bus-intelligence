"""Pretty-print a journey-plan API response."""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
print("journeys:", len(d["journeys"]))
for j in d["journeys"]:
    cs = j["crowding_summary"]
    print(
        f"  #{j['rank']} {j['duration_minutes']}min {j['transfers']}ch "
        f"walk {j['walk_minutes']} crowd={cs['category']} "
        f"known={cs.get('legs_known')}/{cs.get('legs_total')}"
    )
    for leg in j["legs"]:
        cat = (leg["crowding"] or {}).get("category")
        print(
            f"     {leg['kind']:4s} {leg['from_name'][:24]:24s}->{leg['to_name'][:24]:24s} "
            f"{leg['duration_minutes']:5.1f}m {leg['route'] or '':10s} "
            f"{leg['confidence']:8s} crowd={cat}"
        )
    for w in j["warnings"]:
        print("     !", w)
