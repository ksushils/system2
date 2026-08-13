"""Which (scanner, param) pairs does the fleet ACTUALLY read at runtime?

Reads n8n execution_data and decodes the flatted format. Deliberately
does NOT read workflow source: a param present in source but absent from
executed data is exactly the case this exists to catch.

Reads {"pairs":[{"scanner":..,"param":..,"value":..}]} on stdin, emits
evidence per pair on stdout. READ-ONLY (mode=ro URI).
"""
import sqlite3, json, os, re, sys

DB = os.environ.get("N8N_SQLITE_DB", "/var/lib/docker/volumes/n8n_data/_data/database.sqlite")
LIMIT = int(os.environ.get("PARAM_CONN_SCAN", "60"))
TAG_RE = r"""(?:SCANNER\s*=\s*|["']scanner["']\s*:\s*)['"]([a-z_]+)['"]"""

# A parameter can only be observed if the execution actually reached the
# code that reads it. Detected on RESPONSE-side field names the scanners
# emit, never on request URLs -- URLs are built from variables and never
# appear literally in the flatted blob.
TRADING_RE = re.compile(
    r"signal_fired|rejection_stage|skip_reason|reservation_id|risk_gate|trade_type", re.I)
# A scanner stopped by its own breaker still executes; it just returns
# before the config node emits anything. That is not evidence about a
# parameter.
HALT_RE = re.compile(
    r"SYSTEM_BREAKER|CONSECUTIVE_LOSS_HALT|MAX_ORDERS_PER_DAY|MAX_DAILY_LOSS_R|kill_switch|BREAKER", re.I)


def main():
    req = json.load(sys.stdin)
    pairs = req.get("pairs", [])
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # scanner -> active workflow id, self-declared by the workflow itself
    active = {}
    for r in con.execute("SELECT id,nodes FROM workflow_entity WHERE active=1"):
        found = {}
        for m in re.finditer(TAG_RE, r["nodes"] or ""):
            found[m.group(1)] = found.get(m.group(1), 0) + 1
        if found:
            active.setdefault(max(found, key=found.get), r["id"])

    # one pass per scanner: pull the blobs once, test every param against them
    by_scanner = {}
    for p in pairs:
        by_scanner.setdefault(p["scanner"], []).append(p)

    out = []
    for scanner, plist in by_scanner.items():
        wid = active.get(scanner)
        if not wid:
            for p in plist:
                out.append({**p, "status": "NOT_WIRED", "executions_examined": 0,
                            "executions_with_param": 0, "trading_path_executions": 0,
                            "halted_executions": 0, "execution_state": "NOT_WIRED",
                            "execution_note": "no active workflow declares this scanner",
                            "observed_value": None, "last_observed_at": None,
                            "reason": "no active workflow declares this scanner"})
            continue

        rows = con.execute(
            "SELECT id, startedAt FROM execution_entity WHERE workflowId=? ORDER BY startedAt DESC LIMIT ?",
            (wid, LIMIT)).fetchall()
        blobs = []
        for r in rows:
            d = con.execute("SELECT data FROM execution_data WHERE executionId=?", (r["id"],)).fetchone()
            if d and d["data"]:
                blobs.append((r["startedAt"], d["data"]))

        # What was this scanner DOING over the window? A NO_RUNTIME_READ on
        # a scanner that never reached its trading path says nothing about
        # the parameter -- it says the scanner was halted or out of hours.
        trading = sum(1 for _, b in blobs if TRADING_RE.search(b))
        halted = sum(1 for _, b in blobs if HALT_RE.search(b))
        if not blobs:
            exec_state, exec_note = "NO_EXECUTIONS", "no retained executions in the window"
        elif trading == 0 and halted > 0:
            exec_state, exec_note = "HALTED", f"{halted} of {len(blobs)} executions hit a breaker or halt; none reached the trading path"
        elif trading == 0:
            exec_state, exec_note = "NO_TRADING_PATH", f"all {len(blobs)} executions were heartbeat-only; the trading path never ran (out of window, or gated upstream)"
        elif halted > trading:
            exec_state, exec_note = "MOSTLY_HALTED", f"{halted} halted vs {trading} trading-path executions"
        else:
            exec_state, exec_note = "RUNNING", f"{trading} of {len(blobs)} executions reached the trading path"

        for p in plist:
            name = p["param"]
            # A bare substring test manufactures hits: MAX_HEAT is a prefix
            # of MAX_HEAT_PCT, MAX_POSITIONS of MAX_POSITIONS_PER_X. Require
            # the name to end at a non-word character or a false CONNECTED
            # is only a naming coincidence away.
            edge = r'(?![A-Za-z0-9_])'
            present = re.compile(re.escape(name) + edge)
            val_a = re.compile(re.escape(name) + edge + r'"\s*,\s*"?([0-9.eE+-]{1,24})"?')
            val_b = re.compile(re.escape(name) + edge + r'["\s:,]+([0-9.eE+-]{1,24})')
            hits, seen, last_at = 0, set(), None
            for started, b in blobs:
                if not present.search(b):
                    continue
                hits += 1
                if last_at is None:
                    last_at = started
                # flatted stores values in a string table; capture what sits
                # near the key in either form
                for m in val_a.finditer(b):
                    seen.add(m.group(1))
                for m in val_b.finditer(b):
                    seen.add(m.group(1))

            stored = str(p.get("value", ""))
            matched = any(v == stored or (
                _num(v) is not None and _num(stored) is not None and _num(v) == _num(stored)
            ) for v in seen)

            if hits > 0 and matched:
                status, reason = "CONNECTED", f"value {stored} observed in {hits} of {len(blobs)} executions"
            elif hits > 0:
                # the name appears but never with the stored value
                status = "UNPROVEN"
                reason = (f"param name seen in {hits} executions but stored value {stored} "
                          f"not observed (values seen: {sorted(seen)[:4]})")
            elif len(blobs) >= 30 and trading > 0:
                status = "NO_RUNTIME_READ"
                reason = (f"zero appearances across {len(blobs)} executions, of which {trading} "
                          f"reached the trading path — the scanner ran and did not read this value")
            elif len(blobs) >= 30:
                # Enough executions, but NONE reached the code that could
                # read a parameter. Absence here is not evidence of
                # absence -- it is evidence about the scanner's state.
                status = "INDETERMINATE"
                reason = (f"not observed, but no execution reached the trading path in this window "
                          f"({exec_note}) — this says nothing about the parameter")
            else:
                status = "UNPROVEN"
                reason = f"only {len(blobs)} executions retained — too thin to conclude"

            out.append({**p, "status": status, "executions_examined": len(blobs),
                        "executions_with_param": hits,
                        "trading_path_executions": trading,
                        "halted_executions": halted,
                        "execution_state": exec_state, "execution_note": exec_note,
                        "observed_value": sorted(seen)[0] if seen else None,
                        "last_observed_at": last_at, "reason": reason})

    json.dump({"pairs": out, "scan_window": LIMIT}, sys.stdout)


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


if __name__ == "__main__":
    main()
