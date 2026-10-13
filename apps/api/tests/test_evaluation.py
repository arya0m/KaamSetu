"""Phase 3 done-when: the AI evaluation set and its scoring (real LLM runs separately)."""

import json
from pathlib import Path
from typing import Any

import pytest

from app import i18n
from app.ai import evaluation
from app.ai.llm import LLMResponse
from app.schemas.profile import TRADES

pytestmark = pytest.mark.skipif(
    not evaluation.DEFAULT_FILE.exists(), reason="repo seed/ directory not available"
)


def test_p3_eval_set_has_30_cases_split_10_10_10() -> None:
    cases = evaluation.load_cases()
    assert len(cases) == 30
    langs = [c.language for c in cases]
    assert langs.count("hi") == 10 and langs.count("en") == 10
    regional = [lang for lang in langs if lang not in ("hi", "en")]
    assert len(regional) == 10
    assert {regional.count(code) for code in ("bn", "mr", "ta", "te", "gu")} == {2}
    assert len({c.id for c in cases}) == 30


def test_p3_eval_cases_are_well_formed() -> None:
    for c in evaluation.load_cases():
        assert c.language in i18n.LANGUAGE_CODES
        assert c.trade in TRADES, c.id
        assert c.skill_groups and all(c.skill_groups), c.id
        assert c.location_aliases, c.id
        assert len(c.transcript) > 30, c.id


def test_p3_eval_covers_every_named_trade() -> None:
    trades = {c.trade for c in evaluation.load_cases()}
    assert trades == set(TRADES)


class OracleLLM:
    """Answers each eval case perfectly, quoting the transcript as evidence."""

    provider, model = "oracle", "oracle-1"

    def __init__(self, cases: list[evaluation.Case], wrong: bool = False) -> None:
        self.by_text = {c.transcript: c for c in cases}
        self.wrong = wrong

    def complete_json(self, messages: list[dict[str, str]], schema: Any, name: str) -> LLMResponse:
        transcript = json.loads(messages[1]["content"])["transcript"].removeprefix("Message 1: ")
        case = self.by_text[transcript]
        evidence = transcript[:10]
        data = {
            "name": None,
            "primary_trade": "other" if self.wrong else case.trade,
            "skills": [] if self.wrong else [
                {"name": group[0], "level": "skilled", "evidence": evidence}
                for group in case.skill_groups
            ],
            "years_experience": 5,
            "location": {"city": "Nowhere" if self.wrong else case.location_aliases[0],
                         "area": None, "pincode": None},
            "languages_spoken": [],
            "availability": None,
            "expected_daily_pay_inr": None,
            "missing_fields": [],
            "confidence": 0.9,
        }  # fmt: skip
        return LLMResponse(json.dumps(data, ensure_ascii=False))


def test_p3_perfect_answers_score_100_percent() -> None:
    cases = evaluation.load_cases()
    results = evaluation.run(OracleLLM(cases), cases)
    assert evaluation.accuracy(results) == {"primary_trade": 1.0, "skills": 1.0, "location": 1.0}
    text, passed = evaluation.report(results, 0.85)
    assert passed and "PASS" in text


def test_p3_wrong_answers_fail_and_exit_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    cases = evaluation.load_cases()
    code = evaluation.main(["--min", "0.85"], llm=OracleLLM(cases, wrong=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "skills=0%" in out


def test_p3_eval_writes_json_report(tmp_path: Path) -> None:
    cases = evaluation.load_cases()
    out = tmp_path / "report.json"
    assert evaluation.main(["--json", str(out)], llm=OracleLLM(cases)) == 0
    report = json.loads(out.read_text())
    assert report["passed"] is True and len(report["cases"]) == 30


def test_p3_scoring_rules() -> None:
    from app.schemas.profile import WorkerProfileData

    case = evaluation.Case(
        "x",
        "en",
        "t",
        "electrician",
        [["wiring"], ["inverter"], ["fan"]],
        ["bangalore", "bengaluru"],
    )

    def prof(skills: list[str], city: str | None) -> WorkerProfileData:
        return WorkerProfileData.model_validate(
            {
                "primary_trade": "electrician",
                "skills": [{"name": s, "level": "basic"} for s in skills],
                "location": {"city": city},
                "confidence": 1,
            }
        )

    two_of_three = evaluation.score(
        case, prof(["house wiring", "inverter installation"], "Bengaluru")
    )
    assert two_of_three.ok == {"primary_trade": True, "skills": True, "location": True}
    one_of_three = evaluation.score(case, prof(["house wiring", "solar"], "Mysore"))
    assert one_of_three.ok["skills"] is False and one_of_three.ok["location"] is False
    assert one_of_three.unmatched_skills == ["solar"]
    assert evaluation.score(case, None).ok == dict.fromkeys(evaluation.FIELDS, False)
