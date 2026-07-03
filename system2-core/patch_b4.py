#!/usr/bin/env python3
path = '/root/system2-core/b4_correlation_cluster_engine.py'
content = open(path).read()

# 1. Add SECTOR_CAPS after MAX_NAMES_PER_CLUSTER
old_max = 'MAX_NAMES_PER_CLUSTER = int(STAGE7_CONFIG.get("max_names_per_cluster", 2))'
new_max = '''MAX_NAMES_PER_CLUSTER = int(STAGE7_CONFIG.get("max_names_per_cluster", 2))

# DYNAMIC SECTOR CAPS — based on intelligence engine shadow portfolio analysis
# Raised to 3 for sectors where cluster cap was cutting winners
SECTOR_CAPS = {
    "Energy": 2,                  # working — keep tight
    "Technology": 2,              # correlated sector
    "Financial Services": 2,      # correlated sector
    "Communication Services": 2,  # working — keep
    "Consumer Defensive": 3,      # FLAGGED — raise to 3
    "Industrials": 3,             # FLAGGED — raise to 3
    "Consumer Cyclical": 3,       # FLAGGED — raise to 3
    "Healthcare": 2,              # binary event risk
    "Basic Materials": 2,         # gold miner correlation
    "Utilities": 2,               # defensive / rate-sensitive
    "Real Estate": 2,             # rate-sensitive
    "default": 2,                 # all others
}


def get_sector_cap(sector: str) -> int:
    return SECTOR_CAPS.get(sector, SECTOR_CAPS["default"])'''

content = content.replace(old_max, new_max)

# 2. Replace MAX_NAMES_PER_CLUSTER usage with dynamic cap in main loop
old_cluster = '''    for key, names in sorted(clusters.items(), key=lambda item: max(rank_key(s) for s in item[1]), reverse=True):
        sector, etf = key.split("|", 1)
        ranked = sorted(names, key=rank_key, reverse=True)
        kept = ranked[:MAX_NAMES_PER_CLUSTER]
        rejected = ranked[MAX_NAMES_PER_CLUSTER:]'''

new_cluster = '''    for key, names in sorted(clusters.items(), key=lambda item: max(rank_key(s) for s in item[1]), reverse=True):
        sector, etf = key.split("|", 1)
        sector_cap = get_sector_cap(sector)
        ranked = sorted(names, key=rank_key, reverse=True)
        kept = ranked[:sector_cap]
        rejected = ranked[sector_cap:]'''

content = content.replace(old_cluster, new_cluster)

# 3. Update rejection reason to show actual cap
old_rej = '''        for setup in rejected:
            rejections.append({
                **setup,
                "clusterRejectReason": f"cluster_cap_{MAX_NAMES_PER_CLUSTER}_per_{sector}",
                "cluster": {
                    "sector": sector,
                    "etf": etf,
                    "clusterSizeBefore": len(ranked),
                    "clusterRiskBudget": cluster_risk_budget,
                },
            })'''

new_rej = '''        for setup in rejected:
            rejections.append({
                **setup,
                "clusterRejectReason": f"cluster_cap_{sector_cap}_per_{sector}",
                "sector_cap_used": sector_cap,
                "cluster": {
                    "sector": sector,
                    "etf": etf,
                    "clusterSizeBefore": len(ranked),
                    "clusterRiskBudget": cluster_risk_budget,
                },
            })'''

content = content.replace(old_rej, new_rej)

# 4. Update cluster report to show dynamic cap
old_report = '''        cluster_reports.append({
            "sector": sector,
            "etf": etf,
            "rawCount": len(ranked),
            "keptCount": len(kept),
            "rejectedCount": len(rejected),'''

new_report = '''        cluster_reports.append({
            "sector": sector,
            "etf": etf,
            "sectorCapUsed": sector_cap,
            "rawCount": len(ranked),
            "keptCount": len(kept),
            "rejectedCount": len(rejected),'''

content = content.replace(old_report, new_report)

# 5. Update report clusterRule to show dynamic
old_rule = '''        "clusterRule": {
            "groupBy": "sector + shared sector ETF",
            "maxNamesPerCluster": MAX_NAMES_PER_CLUSTER,
            "clusterRiskBudget": "one position risk per sector/ETF cluster",
            "allocation": "equal split among kept names in the cluster",
        },'''

new_rule = '''        "clusterRule": {
            "groupBy": "sector + shared sector ETF",
            "maxNamesPerCluster": MAX_NAMES_PER_CLUSTER,
            "dynamicSectorCaps": SECTOR_CAPS,
            "clusterRiskBudget": "one position risk per sector/ETF cluster",
            "allocation": "equal split among kept names in the cluster",
        },'''

content = content.replace(old_rule, new_rule)

open(path, 'w').write(content)
print('B4 engine updated successfully')
