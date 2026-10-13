"""AI evaluation set runner (PRD: Testing; Phase 3 done-when: 85%+ on trade, skills, location).

    python -m app.ai.evaluation                # uses LLM_PROVIDER and its API key
    python -m app.ai.evaluation --min 0.85 --file ../../seed/eval/profiles.jsonl

Scoring, per case:
- trade:    predicted primary_trade equals the expected trade.
- location: an expected alias appears in the predicted area/city/pincode.
- skills:   at least 60% of the expected skill groups are matched; a group matches
            when any of its aliases is a substring of a predicted skill name.
Field accuracy = share of cases scored correct. The run passes only if every
field reaches the threshold. Predicted skills that match no expected group are
listed for human review (possible inventions).
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ai.llm import LLMClient, get_llm
from app.schemas.profile import WorkerProfileData
from app.services.profile import extract_profile

DEFAULT_FILE = Path(__file__).resolve().parents[4] / "seed" / "eval" / "profiles.jsonl"
FIELDS = ("primary_trade", "skills", "location")
SKILL_RECALL = 0.6


@dataclass(frozen=True)
class Case:
    id: str
    language: str
    transcript: str
    trade: str
    skill_groups: list[list[str]]
    location_aliases: list[str]


@dataclass
class CaseResult:
    case: Case
    ok: dict[str, bool]
    unmatched_skills: list[str] = field(default_factory=list)
    error: str | None = None


def load_cases(path: Path = DEFAULT_FILE) -> list[Case]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        exp = row["expected"]
        cases.append(
            Case(
                row["id"],
                row["language"],
                row["transcript"],
                exp["primary_trade"],
                exp["skills"],
                exp["location"],
            )
        )
    return cases


def score(case: Case, profile: WorkerProfileData | None) -> CaseResult:
    if profile is None:
        return CaseResult(case, dict.fromkeys(FIELDS, False), error="no valid profile")
    names = [s.name.casefold() for s in profile.skills]
    matched_groups = sum(
        any(alias.casefold() in name for alias in group for name in names)
        for group in case.skill_groups
    )
    needed = math.ceil(len(case.skill_groups) * SKILL_RECALL)
    unmatched = [
        name
        for name in names
        if not any(alias.casefold() in name for group in case.skill_groups for alias in group)
    ]
    loc = profile.location
    place = " ".join(p for p in (loc.area, loc.city, loc.pincode) if p).casefold()
    return CaseResult(
        case,
        {
            "primary_trade": profile.primary_trade == case.trade,
            "skills": matched_groups >= needed,
            "location": any(alias.casefold() in place for alias in case.location_aliases),
        },
        unmatched,
    )


def run(llm: LLMClient, cases: list[Case]) -> list[CaseResult]:
    return [score(c, extract_profile(llm, [c.transcript])) for c in cases]


def accuracy(results: list[CaseResult]) -> dict[str, float]:
    return {f: sum(r.ok[f] for r in results) / len(results) for f in FIELDS} if results else {}


def report(results: list[CaseResult], threshold: float) -> tuple[str, bool]:
    acc = accuracy(results)
    lines = [f"{'case':8} {'lang':4} trade skills location  notes"]
    for r in results:
        marks = "  ".join("ok  " if r.ok[f] else "FAIL" for f in FIELDS)
        notes = r.error or (
            f"extra skills: {', '.join(r.unmatched_skills)}" if r.unmatched_skills else ""
        )
        lines.append(f"{r.case.id:8} {r.case.language:4} {marks}  {notes}")
    by_lang: dict[str, list[CaseResult]] = {}
    for r in results:
        by_lang.setdefault(r.case.language, []).append(r)
    lines.append("")
    for lang, rs in sorted(by_lang.items()):
        a = accuracy(rs)
        lines.append(f"{lang}: " + "  ".join(f"{f}={a[f]:.0%}" for f in FIELDS))
    passed = bool(acc) and all(v >= threshold for v in acc.values())
    lines.append("")
    lines.append(
        "overall: " + "  ".join(f"{f}={acc[f]:.0%}" for f in FIELDS)
        + f"  (threshold {threshold:.0%}) -> {'PASS' if passed else 'FAIL'}"
    )  # fmt: skip
    return "\n".join(lines), passed


def main(argv: list[str] | None = None, llm: LLMClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--min", type=float, default=0.85, dest="threshold")
    parser.add_argument("--json", type=Path, help="also write per-case results here")
    args = parser.parse_args(argv)
    results = run(llm or get_llm(), load_cases(args.file))
    text, passed = report(results, args.threshold)
    print(text)
    if args.json:
        payload: dict[str, Any] = {
            "accuracy": accuracy(results),
            "passed": passed,
            "cases": [
                {
                    "id": r.case.id,
                    "ok": r.ok,
                    "unmatched_skills": r.unmatched_skills,
                    "error": r.error,
                }
                for r in results
            ],
        }
        args.json.write_text(json.dumps(payload, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
