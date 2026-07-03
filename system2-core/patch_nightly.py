#!/usr/bin/env python3
import re

path = '/root/system2-core/nightly_learning.py'
content = open(path).read()

# 1. Add import after 'import requests'
old_import = 'import requests\n'
new_import = '''import requests

from intelligence_engine import (
    track_shadow_performance,
    analyse_stage_funnel,
    analyse_source_value,
    analyse_what_if,
    generate_monthly_report,
    INTELLIGENCE_PATH,
)
'''
content = content.replace(old_import, new_import)

# 2. Add --intelligence-only flag
old_parser = '''    parser.add_argument("--daily-scorer", action="store_true", help="Run daily scorer + log resolved ideas")
    parser.add_argument("--date", default=None, help="Date for post-mortem (YYYY-MM-DD)")'''
new_parser = '''    parser.add_argument("--daily-scorer", action="store_true", help="Run daily scorer + log resolved ideas")
    parser.add_argument("--intelligence-only", action="store_true", help="Run intelligence engine only")
    parser.add_argument("--date", default=None, help="Date for post-mortem (YYYY-MM-DD)")'''
content = content.replace(old_parser, new_parser)

# 3. Add intelligence-only handling before weekly check
old_weekly = '''    if args.weekly:
        result = generate_weekly_digest()
        print(json.dumps(result, indent=2))
        return'''
new_weekly = '''    if args.intelligence_only:
        shadow = track_shadow_performance()
        funnel = analyse_stage_funnel()
        sources = analyse_source_value()
        what_if = analyse_what_if()
        intelligence = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "shadow_portfolio": shadow,
            "stage_funnel": funnel,
            "source_value": sources,
            "what_if": what_if,
        }
        INTELLIGENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTELLIGENCE_PATH.write_text(json.dumps(intelligence, indent=2, default=str), encoding="utf-8")
        print(json.dumps(intelligence, indent=2, default=str))
        return

    if args.weekly:
        result = generate_weekly_digest()
        print(json.dumps(result, indent=2))
        return'''
content = content.replace(old_weekly, new_weekly)

# 4. Update default nightly run
old_default = '''    # Default nightly run: post-mortem + attribution + council calibration
    attr = compute_attribution()
    cal = compute_council_calibration()
    sug = generate_council_suggestions(cal)
    pm = generate_postmortem(args.date)
    print(json.dumps({"attribution": attr, "council_calibration": cal, "council_suggestions": sug, "postmortem": pm}, indent=2, default=str))'''

new_default = '''    # Default nightly run: post-mortem + attribution + council calibration + intelligence
    attr = compute_attribution()
    cal = compute_council_calibration()
    sug = generate_council_suggestions(cal)
    pm = generate_postmortem(args.date)

    # Intelligence capture
    shadow = track_shadow_performance()
    funnel = analyse_stage_funnel()
    sources = analyse_source_value()
    what_if = analyse_what_if()
    intelligence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_portfolio": shadow,
        "stage_funnel": funnel,
        "source_value": sources,
        "what_if": what_if,
    }
    INTELLIGENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTELLIGENCE_PATH.write_text(json.dumps(intelligence, indent=2, default=str), encoding="utf-8")

    # Monthly report on 1st of month
    today_day = datetime.now(timezone.utc).day
    if today_day == 1:
        monthly_text = generate_monthly_report()
        send_telegram(f"📊 Monthly Intelligence Report generated\\n{monthly_text[:400]}")

    print(json.dumps({"attribution": attr, "council_calibration": cal, "council_suggestions": sug, "postmortem": pm, "intelligence": {"shadow_gates": shadow.get("gates_with_data", 0), "funnel_runs": funnel.get("total_runs", 0), "best_source": sources.get("best_source"), "best_strategy": what_if.get("best_strategy")}}, indent=2, default=str))'''

content = content.replace(old_default, new_default)

open(path, 'w').write(content)
print('Updated nightly_learning.py')
