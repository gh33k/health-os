"""Convert workout YAML definitions to Garmin Connect workout JSON payloads.

YAML format (see workouts/ for examples):

    name: Utegym A
    sport: strength            # strength | running | cycling | cardio
    steps:
      - warmup: 5:00           # timed warmup (mm:ss or '90s' or '10min')
      - exercise: pull-up      # Garmin key, friendly form ok ('pull-up' -> PULL_UP)
        sets: 3
        reps: 8                # or time: 40s for holds like planks
        rest: 90s              # rest after each set
        note: leave 1-2 reps in reserve
      - run: 25:00             # running interval; also accepts distance ('5km')
        hr_zone: 2             # or pace: 5:30-6:00 (min/km)
      - repeat: 4              # explicit repeat group
        steps:
          - run: 0:30
          - recover: 1:30
      - cooldown: 5:00
"""

from __future__ import annotations

import re
from typing import Any

from .exercises import resolve

SPORT_TYPES = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 9},
    "cardio": {"sportTypeId": 6, "sportTypeKey": "cardio_training", "displayOrder": 8},
}

STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}

END_CONDITIONS = {
    "lap.button": {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True},
    "time": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
    "distance": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
    "iterations": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
    "fixed.rest": {"conditionTypeId": 8, "conditionTypeKey": "fixed.rest", "displayOrder": 8, "displayable": True},
    "reps": {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True},
}

TARGET_TYPES = {
    "no.target": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
    "heart.rate.zone": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
    "pace.zone": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
}


class BuildError(Exception):
    pass


def parse_duration(value: Any) -> int:
    """Parse '5:00', '90s', '10min', or bare seconds into seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    if m := re.fullmatch(r"(\d+):(\d{1,2})", s):
        return int(m.group(1)) * 60 + int(m.group(2))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|secs)", s):
        return int(float(m.group(1)))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins)", s):
        return int(float(m.group(1)) * 60)
    raise BuildError(f"Cannot parse duration: {value!r}")


def parse_distance(value: Any) -> float | None:
    """Parse '5km' or '400m' into meters; return None if it's not a distance."""
    s = str(value).strip().lower()
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*km", s):
        return float(m.group(1)) * 1000
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*m", s):
        return float(m.group(1))
    return None


def _pace_to_mps(pace: str) -> float:
    """'5:30' (min/km) -> meters per second."""
    secs = parse_duration(pace)
    if secs <= 0:
        raise BuildError(f"Invalid pace: {pace!r}")
    return 1000.0 / secs


def _apply_target(out: dict, step: dict) -> None:
    if zone := step.get("hr_zone"):
        out["targetType"] = TARGET_TYPES["heart.rate.zone"]
        out["zoneNumber"] = int(zone)
        out["targetValueOne"] = None
        out["targetValueTwo"] = None
    elif pace := step.get("pace"):
        lo, _, hi = str(pace).partition("-")
        hi = hi or lo
        # Slower pace = lower speed; Garmin wants speeds in m/s, low bound first.
        speeds = sorted([_pace_to_mps(lo.strip()), _pace_to_mps(hi.strip())])
        out["targetType"] = TARGET_TYPES["pace.zone"]
        out["targetValueOne"] = speeds[0]
        out["targetValueTwo"] = speeds[1]
        out["targetValueUnit"] = None
    else:
        out["targetType"] = TARGET_TYPES["no.target"]


class _StepBuilder:
    def __init__(self, sport: str):
        self.sport = sport
        self.order = 1

    def executable(self, step_type: str, step: dict, *,
                   duration: Any = None, distance_or_time: Any = None) -> dict:
        out: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepId": self.order,
            "stepOrder": self.order,
            "stepType": STEP_TYPES[step_type],
            "description": str(step.get("note", "")) or None,
            "stepAudioNote": None,
        }
        self.order += 1

        if distance_or_time is not None:
            meters = parse_distance(distance_or_time)
            if meters is not None:
                out["endCondition"] = END_CONDITIONS["distance"]
                out["endConditionValue"] = meters
            else:
                out["endCondition"] = END_CONDITIONS["time"]
                out["endConditionValue"] = parse_duration(distance_or_time)
        elif duration is not None:
            out["endCondition"] = END_CONDITIONS["time"]
            out["endConditionValue"] = parse_duration(duration)
        else:
            out["endCondition"] = END_CONDITIONS["lap.button"]
            out["endConditionValue"] = None

        _apply_target(out, step)
        return out

    def exercise_step(self, step: dict) -> dict:
        category, exercise_name = resolve(str(step["exercise"]))
        out: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepId": self.order,
            "stepOrder": self.order,
            "stepType": STEP_TYPES["interval"],
            "description": str(step.get("note", "")) or None,
            "stepAudioNote": None,
            "category": category,
            "exerciseName": exercise_name,
            "targetType": TARGET_TYPES["no.target"],
        }
        self.order += 1

        if "reps" in step:
            out["endCondition"] = END_CONDITIONS["reps"]
            out["endConditionValue"] = int(step["reps"])
        elif "time" in step:
            out["endCondition"] = END_CONDITIONS["time"]
            out["endConditionValue"] = parse_duration(step["time"])
        else:
            out["endCondition"] = END_CONDITIONS["lap.button"]
            out["endConditionValue"] = None
        return out

    def rest_step(self, duration: Any) -> dict:
        # Always time-based: Garmin's API accepts 'fixed.rest' but zeroes its
        # duration on save (observed 2026-07-13), producing 0-second rests.
        cond = "time"
        out = {
            "type": "ExecutableStepDTO",
            "stepId": self.order,
            "stepOrder": self.order,
            "stepType": STEP_TYPES["rest"],
            "endCondition": END_CONDITIONS[cond],
            "endConditionValue": parse_duration(duration),
            "targetType": TARGET_TYPES["no.target"],
            "description": None,
            "stepAudioNote": None,
        }
        self.order += 1
        return out

    def repeat_group(self, iterations: int, children: list[dict]) -> dict:
        out = {
            "type": "RepeatGroupDTO",
            "stepId": self.order,
            "stepOrder": self.order,
            "stepType": STEP_TYPES["repeat"],
            "numberOfIterations": iterations,
            "smartRepeat": False,
            "endCondition": END_CONDITIONS["iterations"],
        }
        self.order += 1
        out["workoutSteps"] = [self.build(c) for c in children]
        return out

    def build(self, step: dict) -> dict:
        """Build one YAML step entry into a Garmin step DTO."""
        if not isinstance(step, dict):
            raise BuildError(f"Each step must be a mapping, got: {step!r}")

        if "exercise" in step:
            sets = int(step.get("sets", 1))
            if sets <= 1 and "rest" not in step:
                return self.exercise_step(step)
            # sets x (exercise + rest) as a repeat group
            group = {
                "type": "RepeatGroupDTO",
                "stepId": self.order,
                "stepOrder": self.order,
                "stepType": STEP_TYPES["repeat"],
                "numberOfIterations": max(sets, 1),
                "smartRepeat": False,
                "endCondition": END_CONDITIONS["iterations"],
            }
            self.order += 1
            children = [self.exercise_step(step)]
            if "rest" in step:
                children.append(self.rest_step(step["rest"]))
            group["workoutSteps"] = children
            return group

        if "repeat" in step:
            return self.repeat_group(int(step["repeat"]), step.get("steps", []))

        for key, step_type in (
            ("warmup", "warmup"),
            ("cooldown", "cooldown"),
            ("run", "interval"),
            ("interval", "interval"),
            ("recover", "recovery"),
            ("recovery", "recovery"),
        ):
            if key in step:
                return self.executable(step_type, step, distance_or_time=step[key])

        if "rest" in step:
            return self.rest_step(step["rest"])

        raise BuildError(f"Cannot understand step: {step!r}")


def _estimate_seconds(steps: list[dict]) -> int:
    total = 0.0
    for s in steps:
        if s["type"] == "RepeatGroupDTO":
            total += s["numberOfIterations"] * _estimate_seconds(s["workoutSteps"])
        else:
            key = s["endCondition"]["conditionTypeKey"]
            if key in ("time", "fixed.rest"):
                total += s["endConditionValue"]
            elif key == "distance":
                total += s["endConditionValue"] * 0.36  # ~6:00 min/km
            elif key == "reps":
                total += s["endConditionValue"] * 4  # ~4s per rep
    return int(total)


def build_workout(spec: dict) -> dict:
    """Build a full Garmin Connect workout payload from a parsed YAML dict."""
    for field in ("name", "sport", "steps"):
        if field not in spec:
            raise BuildError(f"Workout is missing required field: {field!r}")
    sport_key = str(spec["sport"]).lower()
    if sport_key not in SPORT_TYPES:
        raise BuildError(
            f"Unsupported sport: {sport_key!r} (expected one of {', '.join(SPORT_TYPES)})"
        )
    sport = SPORT_TYPES[sport_key]

    builder = _StepBuilder(sport_key)
    steps = [builder.build(s) for s in spec["steps"]]

    return {
        "sportType": sport,
        "subSportType": None,
        "workoutName": str(spec["name"]),
        "description": spec.get("description"),
        "estimatedDistanceUnit": {"unitKey": None},
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
        ],
        "avgTrainingSpeed": None,
        "estimatedDurationInSecs": _estimate_seconds(steps),
        "estimatedDistanceInMeters": 0,
        "estimateType": None,
    }
