#!/usr/bin/env python3
"""Paper-only outcome meta-model for System 2."""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "model.pkl"
STATS_PATH = ROOT / "meta_model_stats.json"
MIN_TRAINING_SAMPLES = 30

CATEGORICAL = [
    "setup_score_bucket", "sector", "options_verdict", "rvol_bucket",
    "day_of_week", "market_cap_bucket", "above_20d_trend", "regime",
    "combined_forecast_dir", "confluence_bucket",
]
NUMERIC = ["distance_from_52w_high_pct", "sector_rank"]
FEATURES = CATEGORICAL + NUMERIC


def primary_r(row):
    for key in ("actual_r", "planned_r", "r_3d", "r_10d", "r_1d"):
        if row.get(key) is not None:
            return float(row[key])
    return None


def bucket_confluence(value):
    if value is None:
        return "missing"
    value = float(value)
    return "95+" if value >= 95 else "85-94" if value >= 85 else "75-84" if value >= 75 else "60-74"


def feature_row(row):
    out = {key: row.get(key) for key in FEATURES}
    out["above_20d_trend"] = str(bool(row.get("above_20d_trend"))) if row.get("above_20d_trend") is not None else "missing"
    out["regime"] = row.get("market_regime") or row.get("regime") or "untagged"
    out["confluence_bucket"] = bucket_confluence(row.get("confluence_score"))
    for key in CATEGORICAL:
        if out.get(key) in (None, ""):
            out[key] = "missing"
        else:
            out[key] = str(out[key])
    return out


def resolved_rows(ideas):
    return [
        row for row in ideas
        if primary_r(row) is not None
        and (
            row.get("paper_status") == "CLOSED"
            or row.get("paper_outcome") is not None
            or row.get("hit") in {"STOP", "TARGET", "TIME"}
            or int(row.get("scored_stage") or 0) >= 10
        )
    ]


def status_for_count(count):
    if count < 30:
        return "Accumulating data"
    if count < 50:
        return "Training"
    if count < 300:
        return "Active - use with caution"
    return "Production ready"


def train_model(ideas):
    rows = resolved_rows(ideas)
    count = len(rows)
    base = {
        "resolved_count": count,
        "trained": False,
        "min_required": MIN_TRAINING_SAMPLES,
        "accuracy_cv": None,
        "top_features": [],
        "regime_breakdown": {},
        "samples_to_next_milestone": next((m - count for m in (30, 50, 150, 300) if count < m), 0),
        "status": status_for_count(count),
    }
    for regime in sorted({str(r.get("market_regime") or r.get("regime") or "untagged") for r in rows}):
        cohort = [r for r in rows if str(r.get("market_regime") or r.get("regime") or "untagged") == regime]
        base["regime_breakdown"][regime] = {
            "count": len(cohort),
            "win_rate": round(sum(primary_r(r) > 0 for r in cohort) / len(cohort) * 100, 1) if cohort else None,
        }
    if count < MIN_TRAINING_SAMPLES or len({primary_r(r) > 0 for r in rows}) < 2:
        MODEL_PATH.unlink(missing_ok=True)
        STATS_PATH.write_text(json.dumps(base, indent=2), encoding="utf-8")
        return base

    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import accuracy_score
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    frame = pd.DataFrame([feature_row(r) for r in rows])
    labels = [1 if primary_r(r) > 0 else 0 for r in rows]
    prep = ColumnTransformer([
        ("cat", Pipeline([("impute", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), CATEGORICAL),
        ("num", Pipeline([("impute", SimpleImputer(strategy="median"))]), NUMERIC),
    ])
    pipe = Pipeline([("prep", prep), ("model", GradientBoostingClassifier(random_state=42))])
    minority = min(labels.count(0), labels.count(1))
    folds = min(5, minority)
    accuracy = None
    if folds >= 2:
        cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
        accuracy = float(cross_val_score(pipe, frame, labels, cv=cv, scoring="accuracy").mean())
    pipe.fit(frame, labels)
    names = pipe.named_steps["prep"].get_feature_names_out()
    importance = pipe.named_steps["model"].feature_importances_
    top = sorted(zip(names, importance), key=lambda x: x[1], reverse=True)[:12]
    base.update({
        "trained": True,
        "accuracy_cv": round(accuracy * 100, 1) if accuracy is not None else None,
        "top_features": [{"feature": str(name).replace("cat__", "").replace("num__", ""), "importance": round(float(value) * 100, 2)} for name, value in top],
    })
    with MODEL_PATH.open("wb") as handle:
        pickle.dump({"pipeline": pipe, "sample_size": count, "accuracy_cv": base["accuracy_cv"]}, handle)
    STATS_PATH.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return base


def predict_probability(row):
    if not MODEL_PATH.exists():
        return None, False, 0, None
    import pandas as pd
    with MODEL_PATH.open("rb") as handle:
        saved = pickle.load(handle)
    probability = float(saved["pipeline"].predict_proba(pd.DataFrame([feature_row(row)]))[0][1])
    return probability, True, int(saved["sample_size"]), saved.get("accuracy_cv")


def predict_file(input_path, output_path):
    rows = json.loads(Path(input_path).read_text(encoding="utf-8"))
    out = []
    for row in rows:
        probability, trained, sample_size, accuracy = predict_probability(row)
        out.append({
            **row,
            "meta_probability": round(probability * 100, 1) if probability is not None else None,
            "meta_model_trained": trained,
            "meta_model_sample_size": sample_size,
            "meta_model_accuracy_cv": accuracy,
        })
    Path(output_path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return {"count": len(out), "trained": bool(out and out[0]["meta_model_trained"]), "sample_size": out[0]["meta_model_sample_size"] if out else 0}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train")
    train.add_argument("--ideas", required=True)
    pred = sub.add_parser("predict-file")
    pred.add_argument("--input", required=True)
    pred.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "train":
        print(json.dumps(train_model(json.loads(Path(args.ideas).read_text(encoding="utf-8"))), indent=2))
    else:
        print(json.dumps(predict_file(args.input, args.output), indent=2))


if __name__ == "__main__":
    main()
