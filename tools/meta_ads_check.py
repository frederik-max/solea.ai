#!/usr/bin/env python3
"""Daily Meta Ads performance check.

Pulls every ad in the ad account, compares it against its ad-set siblings and
the account as a whole, and emits a JSON report with a recommendation per ad:

  TURN_OFF   - enough evidence that the ad is underperforming and should be paused
  WATCH      - one warning sign; re-check tomorrow
  KEEP       - performing in line with or better than its peers
  WAIT       - not enough evidence yet (still learning, too new, too few impressions)

Every flag carries a plain-English reason so the report can explain itself.

Environment variables:
  META_ACCESS_TOKEN     System-user or long-lived user token with ads_read
  META_AD_ACCOUNT_ID    Ad account id, with or without the "act_" prefix
  META_API_VERSION      Optional, defaults to v21.0
  META_RESULT_ACTION    Optional, force the action type that counts as a "result"
                        (e.g. lead, purchase, onsite_conversion.lead_grouped)

Usage:
  python3 tools/meta_ads_check.py [--days 7] [--out report.json] [--markdown]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Action types that count as a "result", most valuable first. The first type
# that any ad in an ad set produced becomes that ad set's result metric.
RESULT_PRIORITY = [
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "lead",
    "onsite_conversion.lead_grouped",
    "offsite_conversion.fb_pixel_lead",
    "complete_registration",
    "schedule",
    "submit_application",
    "contact",
    "onsite_conversion.messaging_conversation_started_7d",
    "landing_page_view",
    "link_click",
]

# Thresholds. Tune here, not in the routine prompt.
MIN_AGE_DAYS = 3            # younger ads are WAIT, not judged
MIN_IMPRESSIONS = 1000      # below this (and below MIN_SPEND) the sample is too small
MIN_SPEND = 20.0            # account currency
CPR_WORSE_THAN_PEERS = 1.5  # cost per result vs ad-set median
CPR_WORSE_THAN_ACCOUNT = 1.3
CTR_WEAK_VS_PEERS = 0.6     # link CTR vs ad-set median
FATIGUE_FREQUENCY = 3.0
FATIGUE_CTR_DROP = 0.25     # CTR fell by this share vs prior window
CPR_DETERIORATION = 0.40    # CPR rose by this share vs prior window
ZERO_RESULT_SPEND_MULT = 2.0  # spend >= N x ad-set median CPR with no result
STARVED_SPEND_SHARE = 0.05


class MetaAPIError(RuntimeError):
    pass


def api_get(path: str, params: dict, token: str, version: str) -> dict:
    params = dict(params)
    params["access_token"] = token
    url = f"https://graph.facebook.com/{version}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(body).get("error", {})
            except json.JSONDecodeError:
                err = {"message": body}
            code = err.get("code")
            msg = err.get("message", "")
            if code in (4, 17, 32, 613) and attempt < 3:  # rate limits
                time.sleep(30 * (attempt + 1))
                continue
            if code == 190:
                raise MetaAPIError(f"Access token invalid or expired: {msg}") from None
            raise MetaAPIError(f"Graph API error {code} on {path}: {msg}") from None
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise MetaAPIError(f"Network error reaching graph.facebook.com: {e.reason}") from None
    raise MetaAPIError("exhausted retries")


def paginate(path: str, params: dict, token: str, version: str) -> list:
    out = []
    page = api_get(path, params, token, version)
    while True:
        out.extend(page.get("data", []))
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            return out
        with urllib.request.urlopen(nxt, timeout=60) as resp:
            page = json.load(resp)


def action_value(actions: list | None, action_type: str) -> float:
    for a in actions or []:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value", 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def median(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def fetch_insights(account: str, since: dt.date, until: dt.date, token: str, version: str) -> dict:
    fields = ",".join([
        "ad_id", "spend", "impressions", "reach", "clicks", "inline_link_clicks",
        "inline_link_click_ctr", "ctr", "cpc", "cpm", "frequency", "actions",
        "cost_per_action_type", "quality_ranking", "engagement_rate_ranking",
        "conversion_rate_ranking",
    ])
    rows = paginate(f"{account}/insights", {
        "level": "ad",
        "fields": fields,
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "limit": 500,
    }, token, version)
    return {r["ad_id"]: r for r in rows}


def fetch_ads(account: str, token: str, version: str) -> list:
    fields = ",".join([
        "id", "name", "status", "effective_status", "created_time", "updated_time",
        "issues_info",
        "adset{id,name,status,effective_status,optimization_goal,daily_budget,"
        "lifetime_budget,start_time,learning_stage_info,bid_strategy}",
        "campaign{id,name,status,objective}",
    ])
    return paginate(f"{account}/ads", {"fields": fields, "limit": 200}, token, version)


def pick_result_action(adset_ads: list[dict], forced: str | None) -> str:
    if forced:
        return forced
    present = set()
    for ad in adset_ads:
        for a in ad["_cur"].get("actions", []) or []:
            if f(a.get("value")) > 0:
                present.add(a.get("action_type"))
    for t in RESULT_PRIORITY:
        if t in present:
            return t
    return "link_click"


def analyze(ads: list, cur: dict, prev: dict, days: int, forced_action: str | None) -> dict:
    today = dt.datetime.now(dt.timezone.utc)
    live = []
    for ad in ads:
        if ad.get("status") != "ACTIVE":
            continue  # already paused/archived by a human
        if ad.get("effective_status") in ("ADSET_PAUSED", "CAMPAIGN_PAUSED", "PAUSED", "ARCHIVED", "DELETED"):
            continue
        ad["_cur"] = cur.get(ad["id"], {})
        ad["_prev"] = prev.get(ad["id"], {})
        live.append(ad)

    # Group by ad set
    by_adset: dict[str, list] = {}
    for ad in live:
        by_adset.setdefault(ad["adset"]["id"], []).append(ad)

    records = []
    for adset_id, group in by_adset.items():
        result_action = pick_result_action(group, forced_action)
        # Per-ad metrics
        for ad in group:
            c, p = ad["_cur"], ad["_prev"]
            spend = f(c.get("spend"))
            impressions = f(c.get("impressions"))
            link_clicks = f(c.get("inline_link_clicks"))
            link_ctr = (link_clicks / impressions * 100) if impressions else None
            results = action_value(c.get("actions"), result_action)
            cpr = (spend / results) if results else None
            p_spend = f(p.get("spend"))
            p_imp = f(p.get("impressions"))
            p_clicks = f(p.get("inline_link_clicks"))
            p_ctr = (p_clicks / p_imp * 100) if p_imp else None
            p_results = action_value(p.get("actions"), result_action)
            p_cpr = (p_spend / p_results) if p_results else None
            ad["_m"] = dict(
                spend=spend, impressions=impressions, reach=f(c.get("reach")),
                link_clicks=link_clicks, link_ctr=link_ctr, cpc=f(c.get("cpc")) or None,
                cpm=f(c.get("cpm")) or None, frequency=f(c.get("frequency")) or None,
                results=results, cpr=cpr, result_action=result_action,
                prev_spend=p_spend, prev_results=p_results, prev_cpr=p_cpr, prev_link_ctr=p_ctr,
                quality_ranking=c.get("quality_ranking"),
                engagement_rate_ranking=c.get("engagement_rate_ranking"),
                conversion_rate_ranking=c.get("conversion_rate_ranking"),
            )
        adset_spend = sum(a["_m"]["spend"] for a in group)
        adset_median_cpr = median([a["_m"]["cpr"] for a in group])
        adset_median_ctr = median([a["_m"]["link_ctr"] for a in group])
        adset_has_results = any(a["_m"]["results"] > 0 for a in group)
        for ad in group:
            ad["_ctx"] = dict(adset_spend=adset_spend, adset_median_cpr=adset_median_cpr,
                              adset_median_ctr=adset_median_ctr, adset_has_results=adset_has_results,
                              siblings=len(group))

    account_median_cpr = median([a["_m"]["cpr"] for a in live])
    account_median_ctr = median([a["_m"]["link_ctr"] for a in live])

    for ad in live:
        m, ctx = ad["_m"], ad["_ctx"]
        adset = ad["adset"]
        learning = (adset.get("learning_stage_info") or {}).get("status")
        created = dt.datetime.fromisoformat(ad["created_time"].replace("+0000", "+00:00"))
        age_days = (today - created).days
        hard, soft, wait, notes = [], [], [], []

        # ---- Not enough evidence: these are reasons to WAIT, not to turn off
        if learning == "LEARNING":
            wait.append("Ad set is still in Meta's learning phase; performance is not yet stable and pausing now would reset learning.")
        if age_days < MIN_AGE_DAYS:
            wait.append(f"Ad is only {age_days} day(s) old; needs at least {MIN_AGE_DAYS} days before judging.")
        if m["impressions"] < MIN_IMPRESSIONS and m["spend"] < MIN_SPEND:
            wait.append(f"Only {int(m['impressions'])} impressions and {m['spend']:.2f} spend in the last {days} days; sample too small to judge.")

        # ---- Delivery / policy problems (independent of performance)
        if ad.get("effective_status") in ("DISAPPROVED", "WITH_ISSUES"):
            hard.append(f"Meta status is {ad['effective_status']}: the ad is not delivering normally and needs fixing or removing.")
        for issue in ad.get("issues_info") or []:
            hard.append(f"Meta reports an issue: {issue.get('error_summary') or issue.get('error_message')}")
        if learning == "FAIL":
            notes.append("Ad set is 'learning limited': it is not reaching ~50 optimisation events per week. Consolidating ads/ad sets or broadening targeting fixes this, not pausing a single ad.")

        # ---- Performance vs peers (only meaningful once there is a sample)
        enough_sample = m["impressions"] >= MIN_IMPRESSIONS or m["spend"] >= MIN_SPEND
        if enough_sample:
            if m["results"] == 0 and ctx["adset_has_results"]:
                threshold = max(MIN_SPEND, ZERO_RESULT_SPEND_MULT * (ctx["adset_median_cpr"] or 0))
                if m["spend"] >= threshold:
                    hard.append(f"No {m['result_action']} results after {m['spend']:.2f} spend, while sibling ads in the same ad set convert at a median cost of {ctx['adset_median_cpr']:.2f}.")
            if m["cpr"] and ctx["adset_median_cpr"] and ctx["siblings"] > 1 and m["cpr"] > CPR_WORSE_THAN_PEERS * ctx["adset_median_cpr"]:
                pct = (m["cpr"] / ctx["adset_median_cpr"] - 1) * 100
                hard.append(f"Cost per {m['result_action']} is {m['cpr']:.2f}, {pct:.0f}% above the ad-set median of {ctx['adset_median_cpr']:.2f}.")
            elif m["cpr"] and account_median_cpr and m["cpr"] > CPR_WORSE_THAN_ACCOUNT * account_median_cpr:
                pct = (m["cpr"] / account_median_cpr - 1) * 100
                soft.append(f"Cost per {m['result_action']} is {m['cpr']:.2f}, {pct:.0f}% above the account median of {account_median_cpr:.2f}.")
            if m["link_ctr"] is not None and ctx["adset_median_ctr"] and ctx["siblings"] > 1 and m["link_ctr"] < CTR_WEAK_VS_PEERS * ctx["adset_median_ctr"]:
                soft.append(f"Link CTR {m['link_ctr']:.2f}% is well below the ad-set median of {ctx['adset_median_ctr']:.2f}%: the creative or hook is not earning clicks.")
            if m["frequency"] and m["frequency"] >= FATIGUE_FREQUENCY and m["prev_link_ctr"] and m["link_ctr"] is not None \
                    and m["link_ctr"] < (1 - FATIGUE_CTR_DROP) * m["prev_link_ctr"]:
                hard.append(f"Creative fatigue: frequency is {m['frequency']:.1f} and link CTR fell from {m['prev_link_ctr']:.2f}% to {m['link_ctr']:.2f}% versus the prior {days} days.")
            elif m["frequency"] and m["frequency"] >= FATIGUE_FREQUENCY:
                soft.append(f"Frequency is {m['frequency']:.1f}; the audience has seen this ad many times and fatigue is likely soon.")
            if m["cpr"] and m["prev_cpr"] and m["prev_results"] >= 3 and m["cpr"] > (1 + CPR_DETERIORATION) * m["prev_cpr"]:
                soft.append(f"Cost per {m['result_action']} rose from {m['prev_cpr']:.2f} to {m['cpr']:.2f} versus the prior {days} days.")
            for key, label in (("quality_ranking", "quality"), ("engagement_rate_ranking", "engagement rate"), ("conversion_rate_ranking", "conversion rate")):
                r = m.get(key)
                if r and r.startswith("BELOW_AVERAGE"):
                    soft.append(f"Meta ranks this ad's {label} as {r.replace('_', ' ').lower()} against ads competing for the same audience.")
            share = (m["spend"] / ctx["adset_spend"]) if ctx["adset_spend"] else None
            if share is not None and ctx["siblings"] > 1 and share < STARVED_SPEND_SHARE:
                notes.append(f"Meta is only giving this ad {share*100:.1f}% of the ad set's spend; the auction has already deprioritised it. Pausing it changes little; the question is whether to replace it.")

        # ---- Decision
        disapproved = any("status is" in h or "Meta reports an issue" in h for h in hard)
        if disapproved:
            rec = "TURN_OFF"
        elif wait:
            rec = "WAIT"
        elif len(hard) >= 1 and (len(hard) + len(soft)) >= 2 or any("No " in h for h in hard):
            rec = "TURN_OFF"
        elif hard or soft:
            rec = "WATCH"
        else:
            rec = "KEEP"

        records.append({
            "ad_id": ad["id"], "ad_name": ad["name"],
            "adset_id": adset["id"], "adset_name": adset.get("name"),
            "campaign_id": ad["campaign"]["id"], "campaign_name": ad["campaign"].get("name"),
            "objective": ad["campaign"].get("objective"),
            "optimization_goal": adset.get("optimization_goal"),
            "effective_status": ad.get("effective_status"),
            "learning_stage": learning, "age_days": age_days,
            "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
            "adset_context": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ctx.items()},
            "recommendation": rec,
            "reasons_turn_off": hard,
            "warning_signs": soft,
            "reasons_to_wait": wait,
            "notes": notes,
        })

    order = {"TURN_OFF": 0, "WATCH": 1, "WAIT": 2, "KEEP": 3}
    records.sort(key=lambda r: (order[r["recommendation"]], -r["metrics"]["spend"]))
    summary = {k: sum(1 for r in records if r["recommendation"] == k) for k in order}
    return {
        "generated_at": today.isoformat(),
        "window_days": days,
        "account_median_cpr": account_median_cpr,
        "account_median_link_ctr": account_median_ctr,
        "live_ads": len(records),
        "summary": summary,
        "ads": records,
    }


def to_markdown(report: dict) -> str:
    lines = [f"# Meta Ads daily check ({report['window_days']}-day window)", ""]
    s = report["summary"]
    lines.append(f"Live ads: {report['live_ads']}  |  Turn off: {s['TURN_OFF']}  |  Watch: {s['WATCH']}  |  Wait: {s['WAIT']}  |  Keep: {s['KEEP']}")
    lines.append("")
    for rec in ("TURN_OFF", "WATCH", "WAIT"):
        rows = [r for r in report["ads"] if r["recommendation"] == rec]
        if not rows:
            continue
        lines.append(f"## {rec.replace('_', ' ').title()}")
        for r in rows:
            m = r["metrics"]
            cpr = f"{m['cpr']:.2f}" if m["cpr"] else "n/a"
            ctr = f"{m['link_ctr']:.2f}%" if m["link_ctr"] is not None else "n/a"
            lines.append(f"- **{r['ad_name']}** (ad set: {r['adset_name']}, campaign: {r['campaign_name']}) "
                         f"spend {m['spend']:.2f}, {int(m['results'])} {m['result_action']}, CPR {cpr}, link CTR {ctr}, freq {m['frequency'] or 0:.1f}")
            for why in r["reasons_turn_off"] + r["warning_signs"] + r["reasons_to_wait"] + r["notes"]:
                lines.append(f"  - {why}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None, help="write JSON report here")
    ap.add_argument("--markdown", action="store_true", help="print a markdown summary instead of JSON")
    args = ap.parse_args()

    token = os.environ.get("META_ACCESS_TOKEN")
    account = os.environ.get("META_AD_ACCOUNT_ID")
    version = os.environ.get("META_API_VERSION", "v21.0")
    forced_action = os.environ.get("META_RESULT_ACTION") or None
    missing = [n for n, v in (("META_ACCESS_TOKEN", token), ("META_AD_ACCOUNT_ID", account)) if not v]
    if missing:
        print(f"MISSING_CONFIG: set {', '.join(missing)} in the environment", file=sys.stderr)
        return 2
    if not account.startswith("act_"):
        account = f"act_{account}"

    today = dt.date.today()
    cur_until = today - dt.timedelta(days=1)  # yesterday: today's data is partial
    cur_since = cur_until - dt.timedelta(days=args.days - 1)
    prev_until = cur_since - dt.timedelta(days=1)
    prev_since = prev_until - dt.timedelta(days=args.days - 1)

    try:
        ads = fetch_ads(account, token, version)
        cur = fetch_insights(account, cur_since, cur_until, token, version)
        prev = fetch_insights(account, prev_since, prev_until, token, version)
    except MetaAPIError as e:
        print(f"API_ERROR: {e}", file=sys.stderr)
        return 3

    report = analyze(ads, cur, prev, args.days, forced_action)
    report["window"] = {"since": cur_since.isoformat(), "until": cur_until.isoformat(),
                        "prior_since": prev_since.isoformat(), "prior_until": prev_until.isoformat()}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
    if args.markdown:
        print(to_markdown(report))
    elif not args.out:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
