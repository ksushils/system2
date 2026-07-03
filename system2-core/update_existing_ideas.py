import json
from pathlib import Path

ROOT = Path('/root/system2-core')
FUND_JSON = Path('/root/fund-system/data/fund.json')

# Load danelfin
danel_scores = {}
try:
    danelfin_data = json.loads((ROOT / 'data' / 'danelfin_scores.json').read_text())
    scores = danelfin_data.get('scores', danelfin_data)
    for ticker, data in scores.items():
        if isinstance(data, dict):
            danel_scores[str(ticker).upper()] = data
except Exception:
    pass

# Load fund.json
db = json.loads(FUND_JSON.read_text())
ideas = db.get('ideas', [])

updated = 0
for idea in ideas:
    changed = False
    ticker = str(idea.get('ticker', '')).upper()

    # Fix era
    if not idea.get('era'):
        logged = idea.get('logged_at', '') or idea.get('date', '')
        if logged >= '2026-06-09':
            idea['era'] = 'system2_v2'
        else:
            idea['era'] = 'legacy'
        changed = True

    # Add danelfin if missing
    if not idea.get('danelfin_ai_score') and ticker in danel_scores:
        d = danel_scores[ticker]
        idea['danelfin_ai_score'] = d.get('ai_score')
        idea['danelfin_data_available'] = True
        idea['danelfin_technical'] = d.get('technical')
        idea['danelfin_fundamental'] = d.get('fundamental')
        idea['danelfin_sentiment'] = d.get('sentiment')
        changed = True

    if changed:
        updated += 1

db['ideas'] = ideas
FUND_JSON.write_text(json.dumps(db, indent=2))
print(f'Updated {updated} ideas')
