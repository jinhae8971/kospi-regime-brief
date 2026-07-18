"""Network-free contracts for the EWY correction monitor."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import requests

import monitor


ROOT = Path(__file__).resolve().parents[1]


def read_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a unit/contract test accidentally reaches the network."""

    def denied(*_args, **_kwargs):
        raise AssertionError("tests must not make network requests")

    monkeypatch.setattr(requests.sessions.Session, "request", denied)


@pytest.mark.parametrize("score", [-1_000, -100, -9, 0, 100, 1_000])
def test_scenario_probabilities_are_bounded_and_sum_to_100(score: int) -> None:
    scenarios = monitor.scenario_probabilities(score)

    assert [item["key"] for item in scenarios] == ["direct", "retest", "deep"]
    assert sum(item["probability"] for item in scenarios) == 100
    assert all(isinstance(item["probability"], int) for item in scenarios)
    assert all(0 <= item["probability"] <= 100 for item in scenarios)


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (95, "막바지 가능성 높음"),
        (68, "막바지 가능성 높음"),
        (67, "막바지 우세"),
        (56, "막바지 우세"),
        (55, "재시험·방향 확인 구간"),
        (45, "재시험·방향 확인 구간"),
        (44, "조정 진행 중 우세"),
        (33, "조정 진행 중 우세"),
        (32, "추가 조정 가능성 높음"),
        (5, "추가 조정 가능성 높음"),
    ],
)
def test_verdict_boundaries(probability: int, expected: str) -> None:
    assert monitor.verdict_label(probability) == expected


def test_history_upsert_replaces_same_market_key_and_appends_new_key() -> None:
    original = read_json("data/latest.json")
    first = copy.deepcopy(original)
    first["market_key"] = "US-2026-07-17_KR-2026-07-16"
    first["generated_at"] = "2026-07-18T07:17:00+09:00"

    history = monitor.upsert_history({}, first)
    assert len(history["observations"]) == 1
    assert history["observations"][0]["market_key"] == first["market_key"]

    revised = copy.deepcopy(first)
    revised["generated_at"] = "2026-07-18T08:00:00+09:00"
    revised["verdict"]["near_end_probability"] = 49
    revised["verdict"]["ongoing_probability"] = 51
    revised["change_reasons"] = ["외국인 수급 개선"]
    history = monitor.upsert_history(history, revised)

    assert len(history["observations"]) == 1
    assert history["observations"][0]["generated_at"] == revised["generated_at"]
    assert history["observations"][0]["verdict"]["near_end_probability"] == 49
    assert history["observations"][0]["change_reasons"] == ["외국인 수급 개선"]

    next_day = copy.deepcopy(revised)
    next_day["market_key"] = "US-2026-07-20_KR-2026-07-20"
    next_day["generated_at"] = "2026-07-21T07:17:00+09:00"
    history = monitor.upsert_history(history, next_day)

    assert len(history["observations"]) == 2
    assert [item["market_key"] for item in history["observations"]] == [
        first["market_key"],
        next_day["market_key"],
    ]


def test_telegram_message_is_concise_and_contains_decision_inputs() -> None:
    snapshot = read_json("data/latest.json")
    message = monitor.build_telegram_message(snapshot)

    assert len(message) <= 4096
    assert "EWY·코스피 조정 모니터" in message
    assert "막바지" in message and "진행" in message
    assert "직접반등" in message and "재시험" in message and "심화" in message
    assert "외국인" in message
    assert "주봉 RSI" in message and "MACD Hist" in message
    assert "153↓ 위험" in message and "177.5↑ 개선" in message and "184.2↑ 강한 확인" in message
    assert monitor.PAGE_URL in message
    assert snapshot["market_dates"]["us"] in message
    assert snapshot["market_dates"]["kr"] in message
    assert "TELEGRAM_TOKEN" not in message


def test_successful_notification_state_is_separate_from_market_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "notification.json"
    monkeypatch.setattr(monitor, "NOTIFICATION_PATH", state_path)
    snapshot = read_json("data/latest.json")

    monitor.record_successful_notification(snapshot)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["schema_version"] == snapshot["schema_version"]
    assert state["market_key"] == snapshot["market_key"]
    assert state["notified_at"].endswith("+09:00")
    assert set(state) == {"schema_version", "market_key", "notified_at"}


def test_repository_data_and_dashboard_share_one_versioned_contract() -> None:
    latest = read_json("data/latest.json")
    history = read_json("data/history.json")
    methodology = read_json("data/methodology.json")

    assert latest["schema_version"] == history["schema_version"] == methodology["schema_version"] == 1
    assert latest["methodology_version"] == history["methodology_version"] == methodology["version"]
    assert latest["market_key"].startswith("US-") and "_KR-" in latest["market_key"]
    assert set(latest["market_dates"]) >= {"us", "kr"}

    verdict = latest["verdict"]
    assert verdict["near_end_probability"] + verdict["ongoing_probability"] == 100
    expected_probability = round(monitor.clamp(5, 95, 50 + 0.45 * verdict["score"]))
    assert verdict["near_end_probability"] == expected_probability
    assert sum(item["probability"] for item in latest["scenarios"]) == 100

    factors = latest["factors"]
    assert {item["key"] for item in factors} == {
        "price_structure",
        "daily_reversal",
        "weekly_momentum",
        "foreign_flow",
        "fx",
        "semiconductor",
        "macro_risk",
        "wave",
    }
    assert sum(item["max_points"] for item in factors) == 100
    assert all(-item["max_points"] <= item["points"] <= item["max_points"] for item in factors)
    assert {"EWY", "EWY_WEEKLY", "KOSPI", "USDKRW", "SOXX"} <= set(latest["metrics"])
    assert latest["sources"] and all({"name", "url", "as_of", "status"} <= set(item) for item in latest["sources"])

    observations = history["observations"]
    assert observations
    assert any(item["market_key"] == latest["market_key"] for item in observations)
    assert len(observations) <= 730

    dashboard = (ROOT / "index.html").read_text(encoding="utf-8")
    for public_file in ("data/latest.json", "data/history.json", "data/methodology.json"):
        assert public_file in dashboard
    for contract_key in ("market_key", "verdict", "scenarios", "factors", "sources"):
        assert contract_key in dashboard
    assert "규칙 기반" in dashboard
    assert "투자자문" in dashboard

    serialized_public_data = json.dumps(
        {"latest": latest, "history": history, "methodology": methodology},
        ensure_ascii=False,
    )
    assert "TELEGRAM_TOKEN" not in serialized_public_data
    assert "TELEGRAM_CHAT_ID" not in serialized_public_data


def test_actions_contract_prevents_generated_data_trigger_loop() -> None:
    workflow = (ROOT / ".github/workflows/daily-brief.yml").read_text(encoding="utf-8")
    push_paths = workflow.split("  push:", 1)[1].split("\npermissions:", 1)[0]

    assert 'cron: "17 22 * * 0-4"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "notify:" in workflow and "force_send:" in workflow
    assert "concurrency:" in workflow
    assert "contents: write" in workflow
    assert "pages: write" in workflow
    assert 'python monitor.py --dry-run --github-output "$GITHUB_OUTPUT"' in workflow
    assert "git add -- data/*.json" in workflow
    assert "git push origin HEAD:main" in workflow
    assert "/pages/builds" in workflow
    assert "python monitor.py" in workflow and "--wait-pages" in workflow
    assert "--generated-at" in workflow
    assert "python monitor.py --send-existing" in workflow
    assert "git add -- data/notification.json" in workflow
    assert "contains(github.event.head_commit.message, '[notify]')" in workflow
    assert "deploy_pages" not in workflow
    assert '"data/' not in push_paths
