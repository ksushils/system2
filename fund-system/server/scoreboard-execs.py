"""Per scanner per day: how often did it run, and did the TRADING PATH fire?

An unrestricted trigger reports ALIVE while the weekday trading path
never executes, so the two counts must be separate.

Detection is on RESPONSE-SIDE field names the scanners' own code emits
(signal_fired, rejection_stage, skip_reason, reservation_id), NOT on
request URLs. URLs are built from variables and never appear literally
in the flatted blob -- a detector keyed on them reports every scanner as
zero, which is how an earlier attempt produced a table that contradicted
known ground truth.

Reads {"days": N} on stdin. READ-ONLY (mode=ro).
"""
import sqlite3, json, os, re, sys, collections

DB = os.environ.get("N8N_SQLITE_DB", "/var/lib/docker/volumes/n8n_data/_data/database.sqlite")
TAG = r"""(?:SCANNER\s*=\s*|["']scanner["']\s*:\s*)['"]([a-z_]+)['"]"""

# Emitted by scanner code when the trading path actually runs.
TRADING = re.compile(r"signal_fired|rejection_stage|skip_reason|reservation_id|risk_gate|trade_type", re.I)
BEAT = re.compile(r"heartbeat|/ping|\bping\b", re.I)


def main():
    req = json.load(sys.stdin)
    days = int(req.get("days", 7))
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # scanner -> workflow id, self-declared by the workflow
    active = {}
    for r in con.execute("SELECT id,nodes,active FROM workflow_entity"):
        found = {}
        for m in re.finditer(TAG, r["nodes"] or ""):
            found[m.group(1)] = found.get(m.group(1), 0) + 1
        if found:
            s = max(found, key=found.get)
            if s not in active or r["active"]:
                active[s] = {"workflow_id": r["id"], "active": bool(r["active"])}

    out = collections.defaultdict(dict)
    for scanner, meta in active.items():
        rows = con.execute(
            "SELECT id, date(startedAt) d FROM execution_entity "
            "WHERE workflowId=? AND startedAt >= date('now', ?)",
            (meta["workflow_id"], f"-{days} day")).fetchall()
        per = collections.defaultdict(lambda: {"executions": 0, "trading_path": 0, "heartbeat_only": 0, "no_data": 0})
        for r in rows:
            bucket = per[r["d"]]
            bucket["executions"] += 1
            d = con.execute("SELECT data FROM execution_data WHERE executionId=?", (r["id"],)).fetchone()
            if not d or not d["data"]:
                bucket["no_data"] += 1
                continue
            b = d["data"]
            if TRADING.search(b):
                bucket["trading_path"] += 1
            elif BEAT.search(b):
                bucket["heartbeat_only"] += 1
        out[scanner] = {"workflow_id": meta["workflow_id"], "active": meta["active"], "days": dict(per)}

    json.dump({"scanners": out, "window_days": days}, sys.stdout)


if __name__ == "__main__":
    main()
