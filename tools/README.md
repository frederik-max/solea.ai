# tools/meta_ads_check.py

Daily Meta Ads performance check. Run by the "Meta Ads Daily Performance Check" routine.

Requires in the cloud environment:
- `META_ACCESS_TOKEN` – system-user token with `ads_read`
- `META_AD_ACCOUNT_ID` – e.g. `act_1234567890`
- Network access to `graph.facebook.com`

```
python3 tools/meta_ads_check.py --days 7 --out report.json --markdown
```

Recommendations per ad: TURN_OFF, WATCH, WAIT (still learning / too new / too little data), KEEP.
Thresholds live at the top of the script.
