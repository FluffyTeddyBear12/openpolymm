import re

FILL_RISK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "draft_prop",
        re.compile(
            r"\b(?:first pick in the [a-z0-9 ]+ draft|be the (?:first|#1) (?:overall )?pick in the [a-z0-9 ]+ draft)\b"
        ),
    ),
    (
        "virality_views",
        re.compile(
            r"\b(?:daily youtube view count|youtube view count|view count hit|\d+ (?:million|m) views on (?:day|week) \d|views on (?:day|week) \d)\b"
        ),
    ),
    (
        "virality_social_posts",
        re.compile(
            r'\b(?:post again on (?:x|twitter)|post "[^"]+" on (?:truth social|x|twitter)|post \d+[-\+]?\d* (?:tweets|truth social posts|posts)|be said (?:during the next episode|\d+\+ times during the (?:first |next )?(?:[a-z0-9 ]+ )?(?:experience|podcast|episode|show)))\b'
        ),
    ),
    (
        "celebrity_attendance",
        re.compile(
            r"\b(?:attend .*?ballon d'or ceremony|attend an f1 race|appear on the [a-z0-9 ]+ podcast|perform at the [a-z0-9 ]+ halftime show|halftime performance|meet with (?:mrbeast|ishowspeed|adin ross|kai cenat|xqc|hasan piker|nick fuentes|clavicular|tarik|threadguy|arrowcs|alix earle|jake paul|logan paul|sneako|jack doherty)|(?:clavicular|mrbeast|ishowspeed|adin ross|kai cenat|xqc|hasan piker|alix earle|jake paul|logan paul|jack doherty) visit [a-z ]+)\b"
        ),
    ),
    (
        "celebrity_behavior",
        re.compile(
            r'\b(?:wear a yarmulke|eat a bug on stream|boxing fight with|(?:clavicular|jack doherty|hasan piker) (?:sentenced to prison|sentenced to no prison time|arrested)|baby: (?:boy or girl|gender)|confirmed pregnant|pregnant (?:in|before|by)|divorce[d]? (?:before|by|in)|engaged (?:in|by|before|to)|have a baby (?:in|before|by)|give birth (?:in|before|by)|(?:jake paul|mrbeast) announce a (?:presidential |public office )?run|(?:release|drop) (?:an?|their|his|her) (?:new |debut |next )?album (?:in|by|before)|say "[^"]+" (?:or "[^"]+" )?during his midterm rally)\b|(?:[a-z0-9 ]+(?:\band\b|&)\s+[a-z0-9 ]+\s+(?:split|break up|divorce[d]?)\s+in\s+\d{4})'
        ),
    ),
    (
        "chart_rank",
        re.compile(
            r"(?:be the (?:#\d+|\b\d+) (?:us|global) netflix (?:movie|show)|billboard hot 100 (?:#\d+|\b\d+) song|billboard 200 (?:#\d+|\b\d+) album|billboard (?:#\d+|\b\d+) top artist|(?:#\d+|\b\d+) song on the billboard|\bbe the top artist (?:in the us )?for \d{4}\b|\bbe the (?:#\d+|top) song (?:in the us|for \d{4}|globally)\b|\bmost streamed artist for \d{4}\b|\bsecond most streamed artist\b|\bnumber of monthly spotify listeners\b|(?:#\d+|\b\d+) (?:paid|free) app in the (?:us )?apple app store|\brank in google(?:'s|’s)? top \d+ most searched\b)"
        ),
    ),
    (
        "award_nomination",
        re.compile(
            r"\b(?:nominated for best|nomination for best|get the most oscar nominations|most oscar nominations|oscar nomination|grammy nomination|emmy nomination)\b"
        ),
    ),
    (
        "award_win",
        re.compile(
            r"\b(?:win best [a-z0-9 ]+ at the \d+.*?\b(?:academy|grammy|emmy) awards?|win best (?:actor|actress|supporting|director|picture|original song|documentary|animated|live action|casting|international feature|production design|screenplay|sound|film editing|makeup|visual effects|new artist|rap album)|win album of the year|win record of the year|win song of the year|win (?:the|a) nobel (?:peace )?prize|be time person of the year|time person of the year|ballon d'or)\b"
        ),
    ),
    (
        "player_award",
        re.compile(
            r"\b(?:win (?:the |an? )?(?:\d{4}(?:-\d{2})? )?(?:[a-z0-9]+ )*(?:mvp|most valuable player)|(?:\d{4}(?:-\d{2})? )?(?:nba|nfl|nhl|mlb|mls|al|nl|wnba|finals|world series|super bowl|regular season|all-star|hltv) (?:mvp|most valuable player)|mvp awards?|golden boy(?: award)?|heisman (?:trophy)?|rookie of the year|calder trophy|hart (?:memorial )?trophy|vezina trophy|(?:james )?norris (?:memorial )?trophy|(?:frank j\. )?selke trophy|(?:gold|platinum) glove award|(?:defensive|offensive) (?:player|rookie) of the year|(?:dpoy|opoy)|cy young(?: award)?|golden boot)\b"
        ),
    ),
    (
        "player_stat_prop",
        re.compile(
            r"\b(?:most receptions|\d+\+ receptions|anytime touchdown|(?:rushing|receiving) yards o/u|lead the (?:nba|nfl|nhl|mlb) in (?:points|assists|rebounds|blocks|player efficiency rating|efficiency)|record \d+\+ goals|record the most (?:assists|passing yards|rushing yards|receiving yards)|most (?:passing|rushing|receiving) yards|(?:passing|rushing|receiving) yards leader|most (?:scrimmage |rushing |receiving )?touchdowns)\b"
        ),
    ),
)


def fill_risk_category(question: str) -> str | None:
    q = question.lower()
    for cat, rx in FILL_RISK_RULES:
        if rx.search(q):
            return cat
    return None
