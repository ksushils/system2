#!/bin/sh
# Safety-critical startup wrapper: PMF V1 is retired for new broker entries.
# Keep research stamping, reporting, reconciliation and OCO protection active.
export PMF_AUTO_EXEC_ENABLED=false
export PMF_V1_RETIRED_NO_NEW_ENTRIES=true
exec node /root/fund-system/server/index.js
