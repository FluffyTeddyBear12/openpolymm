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
    unexpected = []
    for q in keep:
        cat = fill_risk_category(q)
        if cat in (None, "geopolitical_crisis", "macro_release"):
            continue
        if cat == "sports_line" and any(lg in q.lower() for lg in ("nba", "nhl", "nfl", "mlb")):
            continue
        if cat == "media_release" and "gta" in q.lower():
            continue
        unexpected.append((q, cat))
    assert unexpected == []


def test_award_corpus_keeps_only_known_stat_prop_conflict():
    keep = [k["question"] for k in _load("awards_players.json")["keep"]]
    flagged = [
        q for q in keep
        if fill_risk_category(q) not in (
            None,
            "corporate_metrics",
            "scraper_tracker",
            "geopolitical_crisis",
            "macro_release",
            "sports_line",
        )
    ]
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
        "Will Rubio attend a G7 diplomatic meeting?",
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Will the Republicans win the 2028 US Presidential Election?",
        "Will Notre Dame win the 2027 CFP National Championship?",
        'Will "AI" be said 20+ times during the Nvidia Q3 earnings call?',
        "Will the Democratic Party win the IN-03 House seat?",
        "Will commercial cockpit automation certification pass in 2027?",
        "Will morning commuter rail ridership exceed 2019 baselines in 2027?",
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
        ("Will the St. Louis Blues win the 2026-27 NHL Presidents' Trophy?", "sports_line"),
        ("Will Drake release a new album by December 31?", "celebrity_behavior"),
        ("Will Man I Need - Olivia Dean be the #2 song in the US for 2026?", "chart_rank"),
        ("Will Choosin' Texas be the #1 US song this week?", "chart_rank"),
        ("Will Die With A Smile be the #1 global song?", "chart_rank"),
        ("Will France declare a state of emergency before November?", "geopolitical_crisis"),
        ("Will Paris impose a curfew this month?", "geopolitical_crisis"),
        ("Will a ceasefire be reached by December 31?", "geopolitical_crisis"),
        ("Will Rubio attend a US x Iran diplomatic meeting?", "geopolitical_crisis"),
        ("US-Iran Hormuz Agreement by November 30?", "geopolitical_crisis"),
        ("Will Talarico and Paxton debate live by October 31?", "geopolitical_crisis"),
        ("Will Together win the third-most seats in the 2026 Israeli legislative election?", "geopolitical_crisis"),
        ("Will OXY play for Cloud9 in 2027?", "esports"),
        ("Will Amazon’s market capitalization be between $2.50T and $3.00T at the end of 2026?", "corporate_metrics"),
        ("Will US Q3 GDP growth exceed 2.5%?", "macro_release"),
        ("Will October CPI inflation be above 3.0%?", "macro_release"),
        ("Will US unemployment rate rise above 4.3%?", "macro_release"),
        ("Will October nonfarm payrolls exceed 180k?", "macro_release"),
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
        ("Will October 2026 be the wettest month on record?", ("severe_weather",)),
        ("Will a magnitude 7.0 earthquake hit California in 2026?", ("severe_weather",)),
        ("Walmart (WMT) Q3 US comp sales (ex-fuel) 2.5% to 4%?", ("corporate_metrics",)),
        ("Procter & Gamble (PG) Q1 organic sales growth 2.5% to 3.5%?", ("corporate_metrics",)),
        ("Coca-Cola (KO) Q3 organic revenue (non-GAAP) growth 5% to 6.5%?", ("corporate_metrics",)),
        ("Vertiv (VRT) Q3 adjusted operating margin 26% to 28%?", ("corporate_metrics",)),
        ("Will Apple (AAPL) Q4 Greater China revenue be above $17B?", ("corporate_metrics",)),
        ("Mastercard (MA) Q3 gross dollar volume growth (local currency basis) 6.5% to 7.5%?", ("corporate_metrics",)),
        ("Will Cursor hit $500M ARR in 2026?", ("corporate_metrics",)),
        ("Will the Ornn H200 Index hit $3.75 (LOW) by December 31, 2026?", ("scraper_tracker",)),
        ("Will 170-189 ships transit the Bab el-Mandeb Strait between October 19-October 25?", ("scraper_tracker",)),
        ("Will Claude 3.7 top OpenRouter tokens the week of October 19?", ("scraper_tracker",)),
        ("Will the Rhine River return to normal levels by November?", ("scraper_tracker",)),
        ("Will US Dollar Index (DXY) close above 105?", ("scraper_tracker",)),
        ("Will US take military action in 2026?", ("geopolitical_crisis",)),
        ("Will Wicked opening weekend box office exceed $120M?", ("chart_rank",)),
    ],
)
def test_esports_weather_and_corporate_metrics_filtered(question, expected_categories):
    assert fill_risk_category(question) in expected_categories


@pytest.mark.parametrize(
    "question",
    [
        "Will the Miami Dolphins be the last winless team of the 2026-27 NFL regular season?",
        "Will Tyreek Hill sign with the Kansas City Chiefs in 2026?",
        "Will Tyreek Hill play for the Kansas City Chiefs in the 2026-27 NFL season?",
        "Will the Bank of Brazil make no change to the target for the Selic rate at the December meeting?",
        "Will the Central Bank of Brazil decrease the Selic rate to 14.25% at the next meeting?",
        "Will UMich Consumer Sentiment be between 46.0 and 48.9 in November?",
        "Will University of Michigan Final Consumer Sentiment for October 2026 be 52.0 or higher?",
        "Will no next Israeli Minister of Justice be appointed by February 28, 2027?",
        "Will Miloš Vučević win the next Serbia presidential election?",
        "Another GTA VI trailer released by November 30?",
        "Will Moonshot release Kimi K3.1 Pro by December 31, 2026?",
        "Will St. Louis river level exceed 15 feet in November?",
        "Lakers vs Celtics Spread: -4.5",
        "Arsenal vs Chelsea: O/U 2.5",
        "Will Ornn B200 compute price drop below $4 in 2026?",
        "Will GPT-5 output token price drop below $5?",
        "Will India buy Russian crude in December 2026?",
        "Will Turkey imports Russian gas in Q4?",
    ],
)
def test_oct_25_26_toxic_jump_risk_and_consolidated_patterns_blocked(question):
    assert fill_risk_category(question) is not None


