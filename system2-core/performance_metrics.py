#!/usr/bin/env python3
"""Institutional-style performance metrics for System 2 paper trades."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev


ROOT = Path(__file__).resolve().parent
FUND_PATH = Path("/root/fund-system/data/fund.json")


def load_resolved_trades():
    """
    Load all v2-era resolved trades from fund.json.
    A resolved trade has actual_r or paper_exit_r set in v2 era.
    Paper trades do not require a manually-recorded entry price.
    """
    db = json.loads(FUND_PATH.read_text())
    ideas = db.get("ideas", [])

    resolved = []
    for idea in ideas:
        if idea.get("date", "") < "2026-06-09":
            continue
        # Prefer actual_r for live trades; fall back to paper_exit_r for paper trades.
        r_value = idea.get("actual_r")
        if r_value is None:
            r_value = idea.get("paper_exit_r")
        if r_value is None:
            continue
        entry_date = idea.get("actual_entry_date") or idea.get("paper_entry_date") or idea.get("entryRecorded_at") or idea.get("date")
        exit_date = idea.get("actual_exit_date") or idea.get("paper_exit_at")
        hold_days = idea.get("hold_days_actual")
        if hold_days is None and entry_date and exit_date:
            try:
                hold_days = (
                    datetime.fromisoformat(str(exit_date).replace("Z", "+00:00")).date()
                    - datetime.fromisoformat(str(entry_date).replace("Z", "+00:00")).date()
                ).days
            except Exception:
                hold_days = 0
        if hold_days is None and idea.get("scored_stage") is not None and not (
            idea.get("actual_entry_price") or idea.get("paper_entry_price") or idea.get("entryRecorded")
        ):
            try:
                hold_days = int(idea.get("scored_stage") or 0)
            except Exception:
                hold_days = 0
        resolved.append({
            "ticker": idea.get("ticker"),
            "date": idea.get("date"),
            "entry_date": entry_date,
            "r": float(r_value),
            "regime": idea.get("market_regime") or idea.get("regime") or "UNKNOWN",
            "setup_type": idea.get("setup_type") or idea.get("setup") or "UNKNOWN",
            "hold_days": hold_days if hold_days is not None else 0,
            "exit_reason": idea.get("exit_reason") or idea.get("paper_exit_reason") or "",
        })
    return resolved


def compute_metrics(trades, risk_free_rate=0.04):
    """Compute the full institutional metric set."""
    if not trades:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "message": "No resolved trades yet",
            "trade_count": 0,
        }

    rs = [t["r"] for t in trades]
    n = len(rs)

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    win_rate = len(wins) / n * 100
    avg_r = mean(rs)
    total_r = sum(rs)

    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = (
        gross_win / gross_loss
        if gross_loss > 0
        else (gross_win if gross_win > 0 else 0)
    )

    avg_win = mean(wins) if wins else 0
    avg_loss = mean(losses) if losses else 0
    expectancy = avg_r

    if n >= 2 and stdev(rs) > 0:
        r_std = stdev(rs)
        sharpe_per_trade = avg_r / r_std
        sharpe_annual = sharpe_per_trade * math.sqrt(50)
    else:
        sharpe_per_trade = 0
        sharpe_annual = 0

    downside = [r for r in rs if r < 0]
    if len(downside) >= 2 and stdev(downside) > 0:
        sortino = avg_r / stdev(downside) * math.sqrt(50)
    elif avg_r > 0 and not downside:
        sortino = 999
    else:
        sortino = 0

    equity = []
    cum = 0
    peak = 0
    max_dd = 0
    ordered = sorted(trades, key=lambda x: x.get("entry_date") or x["date"] or "")
    for trade in ordered:
        cum += trade["r"]
        equity.append({
            "date": trade.get("entry_date") or trade["date"],
            "ticker": trade["ticker"],
            "cumulative_r": round(cum, 2),
        })
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    calmar = total_r / max_dd if max_dd > 0 else (total_r if total_r > 0 else 0)

    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for trade in ordered:
        if trade["r"] > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    by_regime = {}
    for trade in trades:
        by_regime.setdefault(trade["regime"], []).append(trade["r"])
    regime_stats = {}
    for regime, reg_rs in by_regime.items():
        regime_stats[regime] = {
            "trades": len(reg_rs),
            "win_rate": round(len([r for r in reg_rs if r > 0]) / len(reg_rs) * 100, 1),
            "avg_r": round(mean(reg_rs), 3),
            "total_r": round(sum(reg_rs), 2),
        }

    by_setup = {}
    for trade in trades:
        by_setup.setdefault(trade["setup_type"], []).append(trade["r"])
    setup_stats = {}
    for setup_type, setup_rs in by_setup.items():
        setup_stats[setup_type] = {
            "trades": len(setup_rs),
            "win_rate": round(len([r for r in setup_rs if r > 0]) / len(setup_rs) * 100, 1),
            "avg_r": round(mean(setup_rs), 3),
        }

    rating_flags = []
    if sharpe_annual >= 1.5:
        rating_flags.append(("Sharpe", "STRONG"))
    elif sharpe_annual >= 1.0:
        rating_flags.append(("Sharpe", "ACCEPTABLE"))
    else:
        rating_flags.append(("Sharpe", "BELOW_BAR"))

    if profit_factor >= 1.5:
        rating_flags.append(("ProfitFactor", "STRONG"))
    elif profit_factor >= 1.3:
        rating_flags.append(("ProfitFactor", "ACCEPTABLE"))
    else:
        rating_flags.append(("ProfitFactor", "BELOW_BAR"))

    avg_hold_values = [
        max(2.0, float(t.get("hold_days") or 0))
        for t in trades
        if t.get("hold_days") not in (None, "")
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trade_count": n,
        "win_rate": round(win_rate, 1),
        "avg_r": round(avg_r, 3),
        "total_r": round(total_r, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 3),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "avg_hold_days": round(mean(avg_hold_values), 1) if avg_hold_values else 0,
        "sharpe_per_trade": round(sharpe_per_trade, 3),
        "sharpe_annual": round(sharpe_annual, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_drawdown_r": round(max_dd, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "best_trade": round(max(rs), 2),
        "worst_trade": round(min(rs), 2),
        "equity_curve": equity,
        "by_regime": regime_stats,
        "by_setup_type": setup_stats,
        "rating_flags": rating_flags,
        "benchmarks": {
            "sharpe_target": 1.5,
            "profit_factor_target": 1.3,
            "win_rate_target": 50,
            "note": "Institutional bar: Sharpe>2.0",
            "risk_free_rate": risk_free_rate,
        },
    }


def run():
    trades = load_resolved_trades()
    metrics = compute_metrics(trades)
    out = ROOT / "data" / "performance_metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2))
    print(f"Resolved trades: {metrics.get('trade_count')}")
    if metrics.get("trade_count", 0) > 0:
        print(f"Win rate: {metrics['win_rate']}%")
        print(f"Sharpe: {metrics['sharpe_annual']}")
        print(f"Profit factor: {metrics['profit_factor']}")
        print(f"Max DD: {metrics['max_drawdown_r']}R")
    return metrics


if __name__ == "__main__":
    run()
