import json
from pathlib import Path

import pytest

from pmus.strategy.fill_risk import fill_risk_category

FIXTURES = Path(__file__).parent / "fixtures" / "fill_risk"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["celebrity_virality.json", "awards_players.json"])
def test_risk_corpus_recall(name):
    risk = [r["question"] for r in _load(name)["risk"]]
    missed = [q for q in risk if fill_risk_category(q) is None]
    assert 1 - len(missed) / len(risk) >= 0.97, missed


def test_must_keep_negatives_are_never_filtered():
    keep = [k["question"] for k in _load("keep_negatives.json")["keep"]]
    assert [q for q in keep if fill_risk_category(q) is not None] == []


def test_award_corpus_keeps_only_known_stat_prop_conflict():
    keep = [k["question"] for k in _load("awards_players.json")["keep"]]
    flagged = [q for q in keep if fill_risk_category(q) is not None]
    assert all("efficiency" in q for q in flagged), flagged


@pytest.mark.parametrize(
    "question,category",
    [
        ("Will Kaka attend the 2026 Ballon d'Or ceremony?", "celebrity_attendance"),
        ("Will Joe Burrow win the 2026 NFL MVP?", "player_award"),
        ('Will "Club Kid" be nominated for Best Original Screenplay at the 99th Academy Awards?', "award_nomination"),
        ("Will Elon Musk post 220-239 tweets from October 16 to October 23, 2026?", "virality_social_posts"),
        ("Will Taylor Swift be confirmed pregnant before 2027?", "celebrity_behavior"),
        ("Will J.T. Miller record 40+ goals in the 2026-27 regular season?", "player_stat_prop"),
        ('Will "Dude" be said 20+ times during the first Joe Rogan Experience of the week of October 19?', "virality_social_posts"),
    ],
)
def test_known_risk_phrasings(question, category):
    assert fill_risk_category(question) == category


@pytest.mark.parametrize(
    "question",
    [
        "Will Trump meet with Xi Jinping in 2026?",
        "Will Zelenskyy visit Washington in 2026?",
        "Will Rubio attend a US x Iran diplomatic meeting?",
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Will Notre Dame win the 2027 CFP National Championship?",
        "Will the St. Louis Blues win the 2026-27 NHL Presidents' Trophy?",
        'Will "AI" be said 20+ times during the Nvidia Q3 earnings call?',
        "Will the Democratic Party win the IN-03 House seat?",
    ],
)
def test_protected_markets_pass_through(question):
    assert fill_risk_category(question) is None


@pytest.mark.parametrize(
    "question,category",
    [
        ("Will Lionel Messi win 2026 MLS Most Valuable Player?", "player_award"),
        ("Will Yan Diomande win the 2026 Golden Boy award?", "player_award"),
        ("Will Jaxon Smith-Njigba finish the 2026-27 NFL regular season with the most receiving yards?", "player_stat_prop"),
        ("Will Drake release a new album by December 31?", "celebrity_behavior"),
        ("Will Man I Need - Olivia Dean be the #2 song in the US for 2026?", "chart_rank"),
    ],
)
def test_review_found_gaps_are_closed(question, category):
    assert fill_risk_category(question) == category


def test_dictionary_word_speed_is_not_a_streamer():
    assert fill_risk_category("Will envoys meet with speed and urgency?") is None


@pytest.mark.parametrize(
    "question,expected_categories",
    [
        ("Will lukxo have the most first bloods at VALORANT Champions 2026?", ("player_stat_prop", "esports")),
        ("Will Champi peak as a Very strong typhoon?", ("severe_weather",)),
        ("Walmart (WMT) Q3 US comp sales (ex-fuel) 2.5% to 4%?", ("corporate_metrics",)),
        ("Procter & Gamble (PG) Q1 organic sales growth 2.5% to 3.5%?", ("corporate_metrics",)),
    ],
)
def test_esports_weather_and_corporate_metrics_filtered(question, expected_categories):
    assert fill_risk_category(question) in expected_categories

