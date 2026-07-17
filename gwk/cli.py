"""gwk — manage Garmin Connect workouts from YAML files.

Commands:
    gwk login                       authenticate and store tokens
    gwk validate FILE...            parse YAML and show the built workout
    gwk push FILE...                create/replace workouts on Garmin Connect
    gwk list                        list workouts on the account
    gwk delete NAME|ID              delete a workout
    gwk schedule NAME DATE...       put a workout on the calendar (YYYY-MM-DD)
    gwk exercises TERM              search Garmin's exercise keys
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .build import BuildError, build_workout, parse_duration
from .client import AuthError, get_client, login_interactive
from .exercises import ExerciseError, search


def _load_specs(paths: list[str]) -> list[tuple[Path, dict]]:
    specs = []
    for pattern in paths:
        p = Path(pattern)
        files = sorted(p.parent.glob(p.name)) if any(c in pattern for c in "*?[") else [p]
        if not files:
            sys.exit(f"No files match: {pattern}")
        for f in files:
            with open(f) as fh:
                docs = [d for d in yaml.safe_load_all(fh) if d]
            for doc in docs:
                specs.append((f, doc))
    return specs


def _describe(payload: dict, indent: int = 0, steps: list | None = None) -> None:
    if steps is None:
        mins = payload["estimatedDurationInSecs"] // 60
        print(f"  {payload['workoutName']}  [{payload['sportType']['sportTypeKey']}, ~{mins} min]")
        steps = payload["workoutSegments"][0]["workoutSteps"]
    for s in steps:
        pad = "    " + "  " * indent
        if s["type"] == "RepeatGroupDTO":
            print(f"{pad}{s['numberOfIterations']}x:")
            _describe(payload, indent + 1, s["workoutSteps"])
            continue
        kind = s["stepType"]["stepTypeKey"]
        cond = s["endCondition"]["conditionTypeKey"]
        val = s["endConditionValue"]
        if cond == "reps":
            amount = f"{val} reps"
        elif cond in ("time", "fixed.rest"):
            amount = f"{int(val) // 60}:{int(val) % 60:02d}"
        elif cond == "distance":
            amount = f"{val / 1000:g} km" if val >= 1000 else f"{val:g} m"
        else:
            amount = "lap button"
        name = s.get("exerciseName") or kind
        target = ""
        if s.get("zoneNumber"):
            target = f"  @ HR zone {s['zoneNumber']}"
        elif s.get("targetType", {}).get("workoutTargetTypeKey") == "pace.zone":
            def pace(mps: float) -> str:
                spk = 1000 / mps
                return f"{int(spk // 60)}:{int(spk % 60):02d}"
            target = f"  @ {pace(s['targetValueTwo'])}-{pace(s['targetValueOne'])} min/km"
        note = f"  ({s['description']})" if s.get("description") else ""
        print(f"{pad}{name}: {amount}{target}{note}")


def cmd_validate(args) -> None:
    for path, spec in _load_specs(args.files):
        payload = build_workout(spec)
        print(f"{path}:")
        _describe(payload)
        if args.json:
            print(json.dumps(payload, indent=2))
        print()


def _existing_by_name(garmin) -> dict[str, int]:
    return {
        w["workoutName"]: w["workoutId"]
        for w in garmin.get_workouts(0, 200)
    }


def cmd_push(args) -> None:
    specs = _load_specs(args.files)
    payloads = [(p, build_workout(s)) for p, s in specs]  # fail fast before touching the API
    garmin = get_client()
    existing = _existing_by_name(garmin)
    for path, payload in payloads:
        name = payload["workoutName"]
        if name in existing:
            garmin.delete_workout(existing[name])
            verb = "Replaced"
        else:
            verb = "Created"
        result = garmin.upload_workout(payload)
        print(f"{verb} {name!r} (id {result.get('workoutId')}) from {path}")


def cmd_list(args) -> None:
    garmin = get_client()
    for w in garmin.get_workouts(0, 200):
        sport = w.get("sportType", {}).get("sportTypeKey", "?")
        print(f"{w['workoutId']}  [{sport}]  {w['workoutName']}")


def cmd_delete(args) -> None:
    garmin = get_client()
    target = args.workout
    if target.isdigit():
        garmin.delete_workout(target)
        print(f"Deleted workout {target}")
        return
    existing = _existing_by_name(garmin)
    if target not in existing:
        sys.exit(f"No workout named {target!r}. Use 'gwk list' to see workouts.")
    garmin.delete_workout(existing[target])
    print(f"Deleted {target!r} (id {existing[target]})")


def cmd_schedule(args) -> None:
    garmin = get_client()
    existing = _existing_by_name(garmin)
    if args.workout not in existing:
        sys.exit(f"No workout named {args.workout!r}. Push it first with 'gwk push'.")
    workout_id = existing[args.workout]
    for date in args.dates:
        garmin.schedule_workout(workout_id, date)
        print(f"Scheduled {args.workout!r} on {date}")


def cmd_exercises(args) -> None:
    matches = search(args.term)
    if not matches:
        sys.exit(f"No exercises match {args.term!r}")
    for cat, ex in matches:
        print(f"{cat}/{ex}")


def cmd_login(args) -> None:
    login_interactive()


def main() -> None:
    parser = argparse.ArgumentParser(prog="gwk", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="authenticate with Garmin Connect")

    p = sub.add_parser("validate", help="parse and preview workout YAML")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true", help="also print the raw payload")

    p = sub.add_parser("push", help="create/replace workouts on Garmin Connect")
    p.add_argument("files", nargs="+")

    sub.add_parser("list", help="list workouts on the account")

    p = sub.add_parser("delete", help="delete a workout by name or id")
    p.add_argument("workout")

    p = sub.add_parser("schedule", help="schedule a workout on calendar dates")
    p.add_argument("workout")
    p.add_argument("dates", nargs="+", metavar="YYYY-MM-DD")

    p = sub.add_parser("exercises", help="search Garmin exercise keys")
    p.add_argument("term")

    args = parser.parse_args()
    try:
        {
            "login": cmd_login,
            "validate": cmd_validate,
            "push": cmd_push,
            "list": cmd_list,
            "delete": cmd_delete,
            "schedule": cmd_schedule,
            "exercises": cmd_exercises,
        }[args.command](args)
    except (BuildError, ExerciseError, AuthError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
