"""Read ACTUAL n8n fleet state. READ-ONLY (mode=ro URI, never writes).

Invoked by server/fleet-state.js via execFile. Emits one JSON object on
stdout. Actual state is read from n8n itself, never inferred from our
own records.
"""
import sqlite3, json, os, re, sys

DB = os.environ.get("N8N_SQLITE_DB", "/var/lib/docker/volumes/n8n_data/_data/database.sqlite")

# Trading-path marker nodes. A heartbeat is NOT one of these: comm's
# heartbeat authenticates with an n8n credential that was always valid
# and landed while its Code nodes were 401ing, so heartbeat liveness
# says nothing about whether the scanner can trade.
TRADING_MARKERS = ("Risk Gate", "Risk Open", "Risk Close", "Place Order",
                   "Place Margin Order", "Confirm & Register Deal")

TAG_RE = r"""(?:SCANNER\s*=\s*|["']scanner["']\s*:\s*)['"]([a-z_]+)['"]"""
TAGS = ["crypto", "indices", "comm", "volume", "mean_reversion",
        "forex", "pa", "fmp", "fmp_alpaca", "failed_breakout"]

SCAN_WINDOW = int(os.environ.get("FLEET_TRADING_SCAN_WINDOW", "250"))


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT id,name,active,versionId,activeVersionId,updatedAt,nodes FROM workflow_entity"
    ).fetchall()

    # self-declared tag, as the workflow itself reports it
    tagged = {t: [] for t in TAGS}
    for r in rows:
        found = {}
        for m in re.finditer(TAG_RE, r["nodes"] or ""):
            found[m.group(1)] = found.get(m.group(1), 0) + 1
        if not found:
            continue
        tag = max(found, key=found.get)
        if tag in tagged:
            tagged[tag].append(r)

    out = {"scanners": {}, "ghosts": [], "active_total": 0}
    out["active_total"] = sum(1 for r in rows if r["active"])

    # a ghost is any row that is inactive yet still holds a live activeVersionId
    for r in rows:
        if not r["active"] and r["activeVersionId"]:
            out["ghosts"].append({"id": r["id"], "name": r["name"],
                                  "activeVersionId": r["activeVersionId"]})

    for tag in TAGS:
        group = tagged[tag]
        act = [r for r in group if r["active"]]
        inact = [r for r in group if not r["active"]]
        entry = {
            "actual_active_count": len(act),
            "versions_total": len(group),
            "actual_workflow_id": None,
            "actual_active_version_id": None,
            "actual_name": None,
            "actual_updated_at": None,
            "execution_count": 0,
            "last_execution_at": None,
            "last_trading_path_at": None,
            "dormant_newer": None,
        }
        if act:
            a = act[0]
            entry.update(actual_workflow_id=a["id"],
                         actual_active_version_id=a["activeVersionId"],
                         actual_name=a["name"],
                         actual_updated_at=a["updatedAt"])

            c = con.execute(
                "SELECT count(*), max(startedAt) FROM execution_entity WHERE workflowId=?",
                (a["id"],)).fetchone()
            entry["execution_count"] = c[0] or 0
            entry["last_execution_at"] = c[1]
            entry["execution_count_7d"] = con.execute(
                "SELECT count(*) FROM execution_entity WHERE workflowId=? "
                "AND startedAt > datetime('now','-7 days')", (a["id"],)).fetchone()[0]

            # Trading-path execution: scanned inside sqlite so the blobs are
            # never transferred out, and bounded to the most recent
            # SCAN_WINDOW executions so a 1.6 GB database cannot make this a
            # slow request. A null here means "not seen within the window",
            # which is why the window is reported alongside it.
            cond = " OR ".join(["instr(d.data, ?)>0"] * len(TRADING_MARKERS))
            q = (f"SELECT max(e.startedAt) FROM execution_entity e "
                 f"JOIN execution_data d ON d.executionId=e.id "
                 f"WHERE e.id IN (SELECT id FROM execution_entity WHERE workflowId=? "
                 f"                ORDER BY startedAt DESC LIMIT {SCAN_WINDOW}) "
                 f"AND ({cond})")
            try:
                entry["last_trading_path_at"] = con.execute(
                    q, (a["id"], *TRADING_MARKERS)).fetchone()[0]
            except Exception as e:
                entry["last_trading_path_at"] = None
                entry["trading_path_error"] = str(e)[:120]
            entry["trading_path_scan_window"] = SCAN_WINDOW

            # a NEWER inactive version sitting dormant beside the active one.
            # volume's and comm's v7 rows sat like this from 2026-08-07
            # while the broken v6 versions ran, and nothing surfaced it.
            newer = [r for r in inact
                     if r["updatedAt"] and a["updatedAt"] and r["updatedAt"] > a["updatedAt"]]
            if newer:
                n = sorted(newer, key=lambda x: x["updatedAt"])[-1]
                entry["dormant_newer"] = {"id": n["id"], "name": n["name"],
                                          "imported_at": n["updatedAt"]}
        out["scanners"][tag] = entry

    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
