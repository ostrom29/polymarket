"""
Market scraper — discovers all eligible token pairs across multiple arbitrage strategies.

Strategies extracted per match:
  btts_vs_o15  : NO BTTS  + YES O1.5
  o15_vs_o25   : YES O1.5 + NO O2.5
  o25_vs_o35   : YES O2.5 + NO O3.5
  1x2_surebet  : YES Home + YES Draw + YES Away  (question-text heuristic)

Output: wc_pairs.json
"""
import requests
import json
import re
import time
from datetime import datetime, timezone

GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
SOCCER_TAG_ID = 100350


def extract_base_slug(slug: str) -> str:
    if not slug:
        return slug
    m = re.match(r"^(.*?\d{4}-\d{2}-\d{2})", slug)
    return m.group(1) if m else slug


def _parse_clob_ids(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def _team_names_from_title(title: str) -> tuple[str, str]:
    """Best-effort extraction of team names from 'Team A vs. Team B' style titles."""
    parts = re.split(r"\s+vs\.?\s+", title, flags=re.IGNORECASE)
    if len(parts) < 2:
        return "", ""
    home = parts[0].strip()
    away = re.split(r"\s+-\s+", parts[1])[0].strip()
    return home, away


def _scan_markets(markets: list, title: str) -> dict:
    """
    Extract all relevant tokens from a list of sub-markets.
    Returns a dict with token IDs for each role, or None for missing.
    """
    home, away = _team_names_from_title(title)

    slots: dict[str, str | None] = {
        "btts_no": None,
        "btts_yes": None,
        "o15_yes": None,
        "o15_no": None,
        "o25_yes": None,
        "o25_no": None,
        "o35_yes": None,
        "o35_no": None,
        "home_yes": None,
        "draw_yes": None,
        "away_yes": None,
    }

    for sm in markets:
        sm_type = sm.get("sportsMarketType") or ""
        question = (sm.get("question") or "").lower()
        clob = _parse_clob_ids(sm.get("clobTokenIds"))

        if not clob or len(clob) < 2:
            continue

        yes_tok, no_tok = clob[0], clob[1]

        if sm_type == "both_teams_to_score":
            slots["btts_no"] = no_tok
            slots["btts_yes"] = yes_tok

        elif sm_type == "totals":
            raw_line = sm.get("line")
            try:
                line = float(str(raw_line).replace(",", ".")) if raw_line is not None else 0.0
            except ValueError:
                continue

            if abs(line - 1.5) < 0.01:
                slots["o15_yes"] = yes_tok
                slots["o15_no"] = no_tok
            elif abs(line - 2.5) < 0.01:
                slots["o25_yes"] = yes_tok
                slots["o25_no"] = no_tok
            elif abs(line - 3.5) < 0.01:
                slots["o35_yes"] = yes_tok
                slots["o35_no"] = no_tok

        else:
            # 1X2: identify by question text — moneyline or any question with team name + win
            q = question
            if "draw" in q or "tie" in q:
                if slots["draw_yes"] is None:
                    slots["draw_yes"] = yes_tok
            elif home and home.lower() in q and ("win" in q or "beat" in q):
                if slots["home_yes"] is None:
                    slots["home_yes"] = yes_tok
            elif away and away.lower() in q and ("win" in q or "beat" in q):
                if slots["away_yes"] is None:
                    slots["away_yes"] = yes_tok

    return slots


def _build_pairs(match_id: str, title: str, slots: dict) -> list[dict]:
    """Assemble valid strategy pairs from extracted token slots."""
    pairs: list[dict] = []

    def _add(strategy: str, tokens: list[str | None], label: str) -> None:
        if all(t is not None for t in tokens):
            pairs.append({
                "pair_id": f"{match_id}::{strategy}",
                "match_id": match_id,
                "title": title,
                "strategy": strategy,
                "tokens": tokens,
                "label": label,
            })

    _add("btts_vs_o15", [slots["btts_no"], slots["o15_yes"]], "NO BTTS + YES O1.5")
    _add("o15_vs_o25",  [slots["o15_yes"], slots["o25_no"]], "YES O1.5 + NO O2.5")
    _add("o25_vs_o35",  [slots["o25_yes"], slots["o35_no"]], "YES O2.5 + NO O3.5")
    _add("1x2_surebet", [slots["home_yes"], slots["draw_yes"], slots["away_yes"]],
         "YES Home + YES Draw + YES Away")

    return pairs


def fetch_all_pairs(output_file: str = "wc_pairs.json") -> None:
    print("🚀 Scanning all active football matches for arbitrage token pairs...")

    limit = 50
    offset = 0
    # Accumulate ALL markets per base_slug across paginated results and "-more-markets" events
    markets_by_slug: dict[str, dict] = {}  # base_slug → {"title": str, "markets": list}

    while True:
        params = {
            "active": "true",
            "closed": "false",
            "tag_id": SOCCER_TAG_ID,
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = requests.get(GAMMA_API_URL, params=params, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            print(f"❌ API error at offset {offset}: {e}")
            break

        if not events:
            print(f"🏁 End of catalogue at offset {offset}.")
            break

        for event in events:
            slug = event.get("slug") or ""
            base_slug = extract_base_slug(slug)
            title = event.get("title") or ""
            markets = event.get("markets") or []

            if base_slug not in markets_by_slug:
                markets_by_slug[base_slug] = {"title": title, "markets": []}
            # Merge markets from all events sharing the same base_slug (incl. -more-markets)
            markets_by_slug[base_slug]["markets"].extend(markets)

        offset += limit
        time.sleep(0.05)

    # Now build pairs from the merged market lists
    all_pairs: list[dict] = []
    all_tokens: set[str] = set()

    for base_slug, data in markets_by_slug.items():
        title = data["title"]
        slots = _scan_markets(data["markets"], title)
        pairs = _build_pairs(base_slug, title, slots)

        if pairs:
            all_pairs.extend(pairs)
            for p in pairs:
                all_tokens.update(p["tokens"])
            strategies = {p["strategy"] for p in pairs}
            print(f"  ✅ {title[:42]:<42} | {', '.join(sorted(strategies))}")

    # Strategy breakdown
    by_strategy: dict[str, int] = {}
    for p in all_pairs:
        by_strategy[p["strategy"]] = by_strategy.get(p["strategy"], 0) + 1

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_pairs": len(all_pairs),
        "total_tokens": len(all_tokens),
        "strategy_counts": by_strategy,
        "pairs": all_pairs,
        "all_tokens": list(all_tokens),
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 {len(all_pairs)} pairs across {len(markets_by_slug)} matches → {output_file}")
    for strat, count in sorted(by_strategy.items()):
        print(f"   {strat:20} : {count} pairs")


if __name__ == "__main__":
    fetch_all_pairs()
