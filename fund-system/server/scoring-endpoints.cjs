// ════════════════════════════════════════════════════════════════════
// SCORING LOOP ENDPOINTS  —  paste into fund-system/server/index.js
// just BEFORE the `app.get('*', ...)` catch-all and `app.listen(...)`.
//
// Purpose: log every overnight trade IDEA, then score it against what the
// stock ACTUALLY did at +1d, +3d, +10d. This is the feedback loop that
// tells you whether Chronos and the Council actually help.
//
// Works with lowdb (db.data.*) — same store the rest of the app uses.
// If db.data.ideas doesn't exist it is created on first write.
// ════════════════════════════════════════════════════════════════════

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');
const pmfAutoExecutor = require('./pmf-auto-executor.cjs');
let startupProtectionRecoveryScheduled = false;

module.exports = function attachScoring(app, db, deps) {
  const { adminOnly, serviceOrAdmin, now, fmpKey, httpGet } = deps;
  const system2Root = process.env.SYSTEM2_CORE_DIR || '/root/system2-core';
  const configPath = path.join(system2Root, 'system2-config.json');
  const envPath = path.join(system2Root, '.env');
  const logDir = path.join(system2Root, 'logs');
  const defaultSystem2Config = {
    mode: 'paper',
    notes: 'Bare core baseline only. C1 is ride-along logging-only. C3/C4/C2/C5 are not wired.',
    universe: { target_size: 800, min_market_cap: 2000000000, min_avg_volume: 0, exclude_price_below: 5 },
    stage1: { min_price: 5, min_avg_volume: 1000000, min_dollar_volume: 20000000, earnings_blackout_days: 5, blocked_tickers: ['STRC'] },
    stage2: { top_n: 40, max_workers: 4 },
    stage7: {
      account_size: 25000,
      risk_pct: 0.01,
      max_trades_per_day: 3,
      max_portfolio_heat: 0.06,
      max_names_per_cluster: 2,
      normal_nonpmf_halfsize_enabled: true,
      normal_nonpmf_halfsize_deployed_at: null,
      normal_nonpmf_halfsize_reeval_threshold: 40,
    },
    presentation: { pmf_focus_mode_enabled: true },
    layers: { options_flow: 'ride_along_logging_only', chronos: 'ride_along_logging_only', news_safety: 'LIVE', council: 'off', options_discovery: 'off', social_sentiment: 'off' },
  };

  function readJson(file, fallback = null) {
    try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
    catch { return fallback; }
  }

  function newsDangerMap() {
    const danger = new Map();
    const catalyst = readJson(path.join(system2Root, 'data', 'news_catalyst.json'), {});
    for (const [ticker, row] of Object.entries(catalyst.results || {})) {
      if (row?.has_danger === true || String(row?.news_verdict || '').toUpperCase() === 'DANGER') {
        danger.set(String(ticker).toUpperCase(), {
          reason: row.best_headline || row.news_summary || 'News catalyst danger',
          source: 'news_catalyst',
        });
      }
    }
    const rejections = readJson(path.join(system2Root, 'stage3_news_rejections.json'), []);
    for (const row of Array.isArray(rejections) ? rejections : (rejections.rejections || [])) {
      const ticker = String(row?.ticker || row?.symbol || '').toUpperCase();
      if (!ticker) continue;
      const landmine = row.hard_landmine || row.stage3RejectDetail;
      if (landmine || row.news_safety_status === 'FORCE_SKIP' || row.news_hard_reject === true) {
        danger.set(ticker, {
          reason: landmine?.summary || row.stage3RejectReason || row.news_summary || 'News safety rejection',
          type: landmine?.type || row.stage3RejectReason || null,
          source: 'news_safety',
        });
      }
    }
    return danger;
  }

  function withTraderReadiness(row, danger = newsDangerMap(), livePrices = null) {
    const ticker = String(row.ticker || '').toUpperCase();
    const storedLandmine = row.hard_landmine && row.hard_landmine !== false ? row.hard_landmine : null;
    const dangerInfo = storedLandmine
      ? {
          reason: storedLandmine.summary || storedLandmine.type || 'Stored hard landmine',
          type: storedLandmine.type || null,
          source: 'fund',
        }
      : danger.get(ticker);
    const live = livePrices?.[ticker] || null;
    const currentPriceRaw = Number(live?.last_price ?? row.current_price);
    const currentPrice = Number.isFinite(currentPriceRaw) && currentPriceRaw > 0 ? currentPriceRaw : null;
    const entry = Number(row.actual_entry_price ?? row.paper_entry_price ?? row.entry);
    const risk = Number(row.risk_per_share ?? row.riskPerShare ?? (entry - Number(row.stop)));
    const liveRRaw = currentPrice != null && Number.isFinite(entry) && Number.isFinite(risk) && risk > 0
      ? Number(((currentPrice - entry) / risk).toFixed(3))
      : null;
    const liveR = riskAdjustedStoredR(row, liveRRaw);
    const changePct = Number(live?.change_pct);
    const previousClose = Number.isFinite(currentPrice) && Number.isFinite(changePct) && changePct > -100
      ? Number((currentPrice / (1 + changePct / 100)).toFixed(4))
      : null;
    return {
      ...row,
      news_danger: Boolean(dangerInfo),
      news_danger_reason: dangerInfo?.reason || null,
      news_danger_type: dangerInfo?.type || null,
      news_danger_source: dangerInfo?.source || null,
      current_price: currentPrice,
      previous_close: previousClose,
      live_change_pct: Number.isFinite(changePct) ? changePct : null,
      live_r: liveR,
      live_r_raw: liveRRaw,
      live_r_direction: Number.isFinite(changePct) ? (changePct > 0 ? 'UP' : changePct < 0 ? 'DOWN' : 'FLAT') : null,
      live_price_updated_at: live?.updated_at || row.monitor_updated_at || null,
    };
  }

  function readEnvValue(key) {
    try {
      const line = fs.readFileSync(envPath, 'utf8').split(/\r?\n/).find(l => l.trim().startsWith(`${key}=`));
      return line ? line.split('=').slice(1).join('=').trim().replace(/^['"]|['"]$/g, '') : null;
    } catch {
      return null;
    }
  }

  const effectiveFmpKey = fmpKey || process.env.FMP_API_KEY || process.env.FMP_KEY || readEnvValue('FMP_API_KEY') || readEnvValue('FMP_KEY');
  const lookupRate = new Map();

  function countArtifact(file) {
    const data = readJson(path.join(system2Root, file), null);
    if (Array.isArray(data)) return data.length;
    if (data && typeof data === 'object') return data;
    return null;
  }

  function deepMerge(base, patch) {
    const out = { ...base };
    for (const [key, value] of Object.entries(patch || {})) {
      if (value && typeof value === 'object' && !Array.isArray(value) && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])) {
        out[key] = deepMerge(base[key], value);
      } else {
        out[key] = value;
      }
    }
    return out;
  }

  function readSystem2Config() {
    return deepMerge(defaultSystem2Config, readJson(configPath, {}));
  }

  function phaseRunSummaries() {
    try {
      return fs.readdirSync(logDir)
        .filter(f => /^phase_b_core_.*\.json$/.test(f))
        .map(name => ({ file: name, ...readJson(path.join(logDir, name), {}) }))
        .sort((a, b) => String(a.runStartedAt || a.file).localeCompare(String(b.runStartedAt || b.file)));
    } catch {
      return [];
    }
  }

  function latestRunSummary() {
    const runs = phaseRunSummaries();
    return runs.length ? runs[runs.length - 1] : null;
  }

  function phaseRunStatus(run) {
    if (!run) return 'UNKNOWN';
    if (run.ok === true) return 'SUCCESS';
    if (run.ok === false) return String(run.pipeline_status || run.status || 'FAILED').toUpperCase();
    return String(run.pipeline_status || run.status || 'UNKNOWN').toUpperCase();
  }

  function isScheduledNightlyRun(run) {
    const raw = run?.runStartedAt || '';
    const d = new Date(raw);
    if (!Number.isFinite(d.getTime())) return false;
    const hour = d.getUTCHours();
    const minute = d.getUTCMinutes();
    return hour === 2 && minute >= 0 && minute <= 35;
  }

  function latestScheduledNightlyRun() {
    const runs = phaseRunSummaries().filter(isScheduledNightlyRun);
    return runs.length ? runs[runs.length - 1] : null;
  }

  function latestManualRun() {
    const runs = phaseRunSummaries().filter(r => !isScheduledNightlyRun(r));
    return runs.length ? runs[runs.length - 1] : null;
  }

  function lastLines(file, maxLines = 120) {
    try {
      return fs.readFileSync(file, 'utf8').split(/\r?\n/).slice(-maxLines).join('\n');
    } catch {
      return '';
    }
  }

  function ensure() {
    if (!db.data.ideas) db.data.ideas = [];
    if (!db.data.system2_run_metadata) db.data.system2_run_metadata = [];
    if (!db.data.system2_rejections) db.data.system2_rejections = [];
    if (!db.data.system2_monitor_snapshots) db.data.system2_monitor_snapshots = [];
    if (!db.data.system2_stage_details) db.data.system2_stage_details = [];
  }

  // Run the same broker/ledger reconciliation used by the scheduled sync once
  // after startup. This closes the restart window for a filled entry whose
  // protection IDs were not yet persisted, without submitting new entries.
  if (!startupProtectionRecoveryScheduled) {
    startupProtectionRecoveryScheduled = true;
    const startupProtectionTimer = setTimeout(async () => {
      try {
        ensure();
        const result = await pmfAutoExecutor.syncOpenAutoPositions({
          allIdeas: db.data.ideas,
          readEnvValue,
          now,
          dryRun: false,
        });
        const alertEvents = [];
        for (const row of Array.isArray(result.results) ? result.results : []) {
          const idea = db.data.ideas.find(i => String(i.ticker || i.symbol || '').toUpperCase() === String(row.ticker || '').toUpperCase());
          const msg = formatSyncAlertMessage(row, idea);
          if (!msg) continue;
          const closed = String(row.action || '').startsWith('updated_exit');
          const external = row.external_close && row.external_close_alert_needed;
          const event = external ? 'external_close_detected' : (closed ? 'position_closed' : 'auto_exec_failure');
          alertEvents.push(queuePmfTelegramAlert(msg, { event, ticker: row.ticker, action: row.action, account: row.account }, external ? 'failures' : (closed ? 'positionClosed' : 'failures')));
        }
        await db.write();
        console.log('[PMF startup protection sync]', JSON.stringify({ ok: result.ok, results: result.results?.length || 0, alerts: alertEvents.length }));
      } catch (error) {
        console.error('[PMF startup protection sync] failed:', error.message);
        queuePmfTelegramAlert(`🚨 PMF STARTUP PROTECTION SYNC FAILED\n${error.message}`, { event: 'startup_protection_sync_failed' }, 'failures');
      }
    }, 30000);
    startupProtectionTimer.unref();
  }

  function isLocalRequest(req) {
    const ip = req.ip || req.connection?.remoteAddress || '';
    return ip === '127.0.0.1' || ip === '::1' || ip.includes('127.0.0.1') || ip.includes('::ffff:127.0.0.1');
  }

  function localOnly(req, res, next) {
    if (!isLocalRequest(req)) return res.status(403).json({ error: 'Local only' });
    next();
  }

  function ideaAgeDays(row) {
    return Math.floor((new Date() - new Date(row.date + 'T00:00:00Z')) / 86400000);
  }

  function deriveIdeaStatus(row) {
    if (isInvalidTicker(row.ticker) || row.r_calculation_suspect) {
      return { status: 'INVALID', outcome: 'INVALID' };
    }
    if (numericOrNull(row.actual_entry_price) != null && numericOrNull(row.actual_exit_price) == null) {
      return { status: 'OPEN', outcome: null };
    }
    if (numericOrNull(row.actual_exit_price) != null) {
      const realR = numericOrNull(row.real_r);
      return { status: 'CLOSED', outcome: realR > 0 ? 'WIN' : realR < 0 ? 'LOSS' : row.paper_outcome || null };
    }
    if (row.paper_exit_reason === 'TARGET' || row.hit === 'TARGET') {
      return { status: 'CLOSED', outcome: 'WIN' };
    }
    if (row.paper_exit_reason === 'STOP' || row.hit === 'STOP') {
      return { status: 'CLOSED', outcome: 'LOSS' };
    }
    if (row.scored_stage >= 10 || row.hit === 'TIME') {
      if (row.r_10d != null && row.r_10d > 0) return { status: 'CLOSED', outcome: 'WIN' };
      if (row.r_10d != null && row.r_10d < 0) return { status: 'CLOSED', outcome: 'LOSS' };
      return { status: 'CLOSED', outcome: 'TIMEOUT' };
    }
    if (ideaAgeDays(row) >= 10 && row.scored_stage >= 10) return { status: 'CLOSED', outcome: 'TIMEOUT' };
    return { status: 'OPEN', outcome: null };
  }

  function withDerivedStatus(row) {
    freezeOriginalRisk(row);
    markSuspectIfNeeded(row);
    const s = deriveIdeaStatus(row);
    return { ...row, paper_status: s.status || row.paper_status, paper_outcome: s.outcome || row.paper_outcome || null };
  }

  const INVALID_TICKERS = new Set(['TEST', 'TESTPUB', 'SOCIALTEST', 'FWONK', 'ALL']);
  const SUSPECT_TICKERS = new Set(['STM', 'AXON', 'CRDO', 'COO', 'FDXF', 'FOX', 'FOXA', 'SEM', 'CNTA']);
  const MAX_SANE_ABS_R = 10;
  const SUSPECT_ABS_R = 6;
  const SUSPECT_LOSS_R = -3;

  function isInvalidTicker(ticker) {
    if (!ticker) return true;
    return INVALID_TICKERS.has(String(ticker).toUpperCase());
  }

  function isSuspectTicker(ticker) {
    return SUSPECT_TICKERS.has(String(ticker || '').toUpperCase());
  }

  function isDisplayableIdea(row) {
    return row && row.paper !== false && !isInvalidTicker(row.ticker) && !isSuspectTicker(row.ticker) && !row.r_calculation_suspect;
  }

  function isPreMarketFavourable(row) {
    const status = String(row?.pre_market_status || row?.gap_status || '').toUpperCase();
    return row?.pre_market_gap_favourable === true || row?.pre_market_favourable === true || status === 'FAVOURABLE';
  }

  function isPmfConfirmedAtStamp(row) {
    return row?.pmf_confirmed_at_stamp === true;
  }

  function pmfStampCohort(row) {
    return String(row?.pmf_stamp_cohort || row?.pmf_entry_cohort || row?.pmf_cohort || '').toUpperCase();
  }

  function isPmfLate(row) {
    if (isPmfConfirmedAtStamp(row)) return pmfStampCohort(row) === 'PMF_LATE';
    return row?.pre_market_gap_favourable_late === true && String(row?.pmf_cohort || '').toUpperCase() === 'PMF_LATE';
  }

  function isPrimaryPmf(row) {
    return isPmfConfirmedAtStamp(row) && !isPmfLate(row);
  }

  function easternSessionFields(timestamp) {
    const instant = new Date(timestamp);
    if (!Number.isFinite(instant.getTime())) return null;
    const formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
      fractionalSecondDigits: 3, timeZoneName: 'longOffset',
    });
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(part => [part.type, part.value]));
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    const minutesFromOpen = minutes - (9 * 60 + 30);
    const sessionState = minutes < 9 * 60 + 30 ? 'PREMARKET' : (minutes < 16 * 60 ? 'REGULAR' : 'AFTERHOURS');
    const offset = String(parts.timeZoneName || 'GMT').replace('GMT', '') || '+00:00';
    return {
      session_eastern_time: `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}.${parts.fractionalSecond || '000'}${offset}`,
      session_state: sessionState,
      minutes_from_open: minutesFromOpen,
      session_label: minutesFromOpen < 0 ? 'PREOPEN_PMF' : 'POSTOPEN_PMF',
    };
  }

  function writeOnceSessionFields(row, timestamp) {
    const fields = easternSessionFields(timestamp);
    if (!row || !fields) return false;
    let changed = false;
    for (const [key, value] of Object.entries(fields)) {
      if (row[key] == null) {
        row[key] = value;
        changed = true;
      }
    }
    return changed;
  }

  function writePmfConfirmationStamp(idea, stamp) {
    if (!idea || idea.pmf_confirmed_at_stamp != null || !stamp) return;
    idea.pmf_confirmed_at_stamp = true;
    idea.pmf_stamp_time = stamp.at;
    idea.pmf_atr_multiple_at_stamp = stamp.atrMultiple;
    idea.pmf_price_at_stamp = stamp.price;
    idea.pmf_entry_at_stamp = stamp.entry;
    idea.pmf_atr_at_stamp = stamp.atr;
    idea.pmf_stamp_cohort = stamp.cohort;
    idea.pmf_average_share_volume_at_stamp = stamp.averageShareVolume;
    idea.pmf_average_dollar_volume_at_stamp = stamp.averageDollarVolume;
    idea.pmf_adv_at_stamp = stamp.averageDollarVolume;
    idea.pmf_adv_source = stamp.averageDollarVolume != null ? 'fmp_batch_quote_avgVolume_x_stamp_price' : null;
    // Session telemetry belongs to the final immutable PMF stamp. Earlier
    // operational checks may already have populated these fields, so replace
    // them atomically from the stamp instead of retaining stale check timing.
    const sessionFields = easternSessionFields(stamp.at);
    if (sessionFields) Object.assign(idea, sessionFields);
  }

  function intakeSourceLayer(row) {
    const raw = row?.intake_source_layer || row?.source_layer || row?.universe_source || row?.sourceLayer || row?.intakeSourceLayer;
    return raw ? String(raw) : 'unknown';
  }

  function currentUniverseSourceMeta() {
    const meta = readJson(path.join(system2Root, 'universe.metadata.json'), {});
    const sourceBySymbol = meta?.sourceBySymbol && typeof meta.sourceBySymbol === 'object' ? meta.sourceBySymbol : {};
    const counts = {};
    for (const src of Object.values(sourceBySymbol)) {
      if (!src) continue;
      counts[src] = (counts[src] || 0) + 1;
    }
    return {
      sourceBySymbol,
      counts,
      sourceAddedCountsAfterDedup: meta?.sourceAddedCountsAfterDedup || {},
      sourceFetchCountsBeforeDedup: meta?.sourceFetchCountsBeforeDedup || {},
    };
  }

  function pmfSourceTallyPayload() {
    ensure();
    const allPmf = db.data.ideas.filter(isDisplayableIdea).filter(isPrimaryPmf);
    const nowMs = Date.now();
    const last30 = allPmf.filter(r => {
      const d = Date.parse(`${String(r.date || r.created_at || r.pre_market_checked_at || '').slice(0, 10)}T00:00:00Z`);
      return Number.isFinite(d) && nowMs - d <= 30 * 86400000;
    });
    const meta = currentUniverseSourceMeta();
    const rowsFor = rows => {
      const bySource = new Map();
      for (const row of rows) {
        const source = intakeSourceLayer(row);
        if (!bySource.has(source)) bySource.set(source, { source, pmf_count: 0, unique_tickers: new Set() });
        const bucket = bySource.get(source);
        bucket.pmf_count += 1;
        if (row.ticker) bucket.unique_tickers.add(String(row.ticker).toUpperCase());
      }
      return Array.from(bySource.values()).map(bucket => {
        const currentNames = meta.counts[bucket.source] || 0;
        const uniquePmf = bucket.unique_tickers.size;
        return {
          source: bucket.source,
          pmf_count: bucket.pmf_count,
          unique_pmf_tickers: uniquePmf,
          current_universe_names: currentNames,
          pmf_rate_current_universe: currentNames ? Number((uniquePmf / currentNames).toFixed(4)) : null,
          note: bucket.source === 'unknown' ? 'Historical row has no persisted intake source layer.' : (uniquePmf < 10 ? 'TOO THIN - directional only' : 'sample building'),
        };
      }).sort((a, b) => (b.unique_pmf_tickers - a.unique_pmf_tickers) || a.source.localeCompare(b.source));
    };
    return {
      ok: true,
      generated_at: new Date().toISOString(),
      source: 'fund.json persisted intake_source_layer; historical untagged PMFs remain unknown',
      current_universe_source_counts: meta.counts,
      source_added_counts_after_dedup: meta.sourceAddedCountsAfterDedup,
      last30: rowsFor(last30),
      all_time: rowsFor(allPmf),
    };
  }

  function ideaRegime(row) {
    return String(row?.market_regime || row?.regime || '').trim().toUpperCase();
  }

  function positionRMultiplier(row) {
    if (row?.size_rule !== 'NORMAL_nonPMF_halfsize') return 1;
    const explicit = numericOrNull(row.position_r ?? row.size_rule_multiplier);
    return explicit != null && explicit > 0 ? explicit : 0.5;
  }

  function riskAdjustedStoredR(row, value) {
    const v = numericOrNull(value);
    if (v == null) return null;
    if (row?.r_values_risk_adjusted === true) return v;
    const mult = positionRMultiplier(row);
    return mult === 1 ? v : Number((v * mult).toFixed(3));
  }

  function latestStageDetailRow(maxDate = null) {
    ensure();
    const rows = [...db.data.system2_stage_details]
      .filter(r => !maxDate || String(r.date || '') <= maxDate)
      .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
    return rows[0] || null;
  }

  function stageByKey(row, key) {
    return (row?.stages || []).find(s => normalizeStageKey(s.stage_key || s.stage) === key) || null;
  }

  function councilRowsByTicker() {
    const rows = rootArtifact('stage6_council_enriched.json', []) || [];
    return Object.fromEntries((Array.isArray(rows) ? rows : []).map(r => [String(r.ticker || r.symbol || '').toUpperCase(), r]));
  }

  function cleanResolvedRows() {
    return db.data.ideas
      .map(withDerivedStatus)
      .filter(isDisplayableIdea)
      .filter(i => String(i.date || '') >= '2026-06-09')
      .filter(i => canonicalResolvedR(i) != null);
  }

  function statsFromR(rows, rKey = 'r_3d') {
    const vals = rows.map(r => canonicalResolvedR(r)).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    return {
      count: vals.length,
      win_rate: vals.length ? Number((wins.length / vals.length * 100).toFixed(1)) : null,
      avg_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null,
      total_r: vals.length ? Number(vals.reduce((s, v) => s + v, 0).toFixed(3)) : 0,
      profit_factor: grossLoss ? Number((grossWin / grossLoss).toFixed(2)) : (grossWin ? 'infinity' : null),
    };
  }

  function statsFromNumericField(rows, key) {
    const vals = rows.map(r => numericOrNull(r?.[key])).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    return {
      count: vals.length,
      win_rate: vals.length ? Number((wins.length / vals.length * 100).toFixed(1)) : null,
      avg_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null,
      total_r: vals.length ? Number(vals.reduce((s, v) => s + v, 0).toFixed(3)) : 0,
      profit_factor: grossLoss ? Number((grossWin / grossLoss).toFixed(2)) : (grossWin ? 'infinity' : null),
    };
  }

  function canonicalStatus(row) {
    return String(row?.paper_status || row?.status || '').trim().toUpperCase();
  }

  function isOpenPlaceholder(row) {
    return ['OPEN', 'WATCHING', 'PENDING', 'ACTIVE'].includes(canonicalStatus(row));
  }

  function isResolvedForCanonical(row) {
    if (!row || isOpenPlaceholder(row)) return false;
    const exitPrice = numericOrNull(row.paper_exit_price);
    if (!(exitPrice != null && exitPrice !== 0)) return false;
    const status = canonicalStatus(row);
    return ['WON', 'LOST', 'TIMED_OUT', 'CLOSED', 'RESOLVED', 'EXITED', 'INVALID', 'ERROR'].includes(status);
  }

  function rawCanonicalResolvedR(row) {
    if (!isResolvedForCanonical(row)) return null;
    const entry = effectiveEntry(row);
    const stop = effectiveStop(row);
    const exitPrice = numericOrNull(row.paper_exit_price);
    if (!(entry != null && stop != null && exitPrice != null)) return null;
    const risk = entry - stop;
    if (!Number.isFinite(risk) || Math.abs(risk) < 1e-12) return null;
    return Number(((exitPrice - entry) / risk).toFixed(4));
  }

  function canonicalQuarantineReasons(row) {
    const reasons = [];
    if (!isResolvedForCanonical(row)) return reasons;
    const status = canonicalStatus(row);
    const exitReason = String(row.paper_exit_reason || row.exit_reason || row.hit || '').trim().toUpperCase();
    const entry = effectiveEntry(row);
    const stop = effectiveStop(row);
    const target = effectiveTarget(row);
    const exitPrice = numericOrNull(row.paper_exit_price);
    const risk = entry != null && stop != null ? Math.abs(entry - stop) : null;
    const canonical = rawCanonicalResolvedR(row);

    if (row.cohort_stats_excluded === true) reasons.push(row.cohort_exclusion_reason || 'cohort_stats_excluded');
    if (row.r_calculation_suspect === true || String(row.r_calculation_suspect).toLowerCase() === 'true') reasons.push('r_calculation_suspect');
    if (['INVALID', 'ERROR'].includes(status) || ['INVALID', 'ERROR'].includes(exitReason)) reasons.push('paper_status/exit_reason invalid_error');
    if (entry != null && risk != null && risk < 0.005 * Math.abs(entry)) reasons.push('|entry-stop| < 0.5% entry');
    if (canonical != null && Math.abs(canonical) > 10) reasons.push('|canonical_R| > 10');
    if (exitPrice != null && exitPrice !== 0 && entry != null && stop != null && target != null && risk != null && risk > 0) {
      const lower = Math.min(stop, target) - 5 * risk;
      const upper = Math.max(stop, target) + 5 * risk;
      if (exitPrice < lower || exitPrice > upper) reasons.push('paper_exit_price outside stop/target sane band');
    }
    return [...new Set(reasons)];
  }

  function isCanonicalRQuarantined(row) {
    return canonicalQuarantineReasons(row).length > 0;
  }

  function canonicalResolvedR(row, { includeQuarantined = false } = {}) {
    if (!row || isInvalidTicker(row.ticker)) return null;
    if (!includeQuarantined && isCanonicalRQuarantined(row)) return null;
    const stored = numericOrNull(row.canonical_r);
    if (stored != null && (includeQuarantined || row.canonical_r_quarantined !== true)) return stored;
    return rawCanonicalResolvedR(row);
  }

  const TRUE_R_SLIPPAGE_PCT = 0.001;
  const TRUE_R_PROXY_GAP_FRACTION = 0.5;

  function firstNumeric(...values) {
    for (const value of values) {
      const n = numericOrNull(value);
      if (n != null && n > 0) return n;
    }
    return null;
  }

  function trueRTriggerPrice(row) {
    return firstNumeric(
      row?.trigger_price,
      row?.price_at_alert,
      row?.entry_trigger_price,
      row?.live_price_at_entry
    );
  }

  function trueRObservedEntryProxy(row) {
    return firstNumeric(
      row?.entry_day_price,
      row?.price_at_signal,
      row?.pre_market_price,
      row?.price,
      row?.previous_close,
      row?.current_price
    );
  }

  function trueRBaseExit(row) {
    const explicit = firstNumeric(row?.realistic_exit, row?.actual_exit_price, row?.paper_exit_price, row?.exit_price);
    if (explicit != null) return explicit;
    const reason = String(row?.paper_exit_reason || row?.exit_reason || row?.hit || row?.paper_outcome || '').toUpperCase();
    if (reason.includes('TARGET') || reason === 'WIN') return effectiveTarget(row);
    if (reason.includes('STOP') || reason === 'LOSS') return effectiveStop(row);
    return firstNumeric(row?.current_or_exit_price, row?.current_price, row?.close_price, row?.price);
  }

  function trueRForRow(row) {
    const plannedEntry = effectiveEntry(row);
    const stop = effectiveStop(row);
    const risk = effectiveRiskPerShare(row);
    const direction = String(row?.original_direction || '').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG';
    if (!(plannedEntry > 0 && stop > 0 && risk > 0)) return null;

    if (row?.fill_source === 'alpaca_paper') {
      const alpacaEntry = firstNumeric(row?.actual_entry_price);
      const alpacaExit = firstNumeric(row?.actual_exit_price);
      if (alpacaEntry != null && alpacaExit != null) {
        const numerator = direction === 'SHORT' ? alpacaEntry - alpacaExit : alpacaExit - alpacaEntry;
        return {
          realistic_entry: Number(alpacaEntry.toFixed(4)),
          realistic_exit: Number(alpacaExit.toFixed(4)),
          slippage_applied_pct: 0,
          true_r: Number((numerator / risk).toFixed(3)),
          true_r_fill_source: 'alpaca_paper',
          true_r_denominator_risk: Number(risk.toFixed(4)),
          true_r_estimated_fill: false,
          true_r_rule: 'Alpaca paper actual entry and exit fills; separate from modeled canonical R.',
        };
      }
    }

    const trigger = trueRTriggerPrice(row);
    let baseEntry = null;
    let fillSource = null;
    if (trigger != null) {
      baseEntry = direction === 'SHORT' ? Math.min(plannedEntry, trigger) : Math.max(plannedEntry, trigger);
      fillSource = 'trigger_price';
    } else {
      const observed = trueRObservedEntryProxy(row);
      if (observed != null) {
        const adverseGap = direction === 'SHORT'
          ? Math.max(0, plannedEntry - observed)
          : Math.max(0, observed - plannedEntry);
        baseEntry = direction === 'SHORT'
          ? plannedEntry - TRUE_R_PROXY_GAP_FRACTION * adverseGap
          : plannedEntry + TRUE_R_PROXY_GAP_FRACTION * adverseGap;
        fillSource = 'estimated_fill_proxy';
      } else {
        const zoneHigh = firstNumeric(row?.entry_zone_high, Array.isArray(row?.entryZone) ? row.entryZone[1] : null);
        const zoneLow = firstNumeric(row?.entry_zone_low, Array.isArray(row?.entryZone) ? row.entryZone[0] : null);
        if (zoneLow != null && zoneHigh != null) {
          baseEntry = (zoneLow + zoneHigh) / 2;
        } else {
          baseEntry = plannedEntry;
        }
        fillSource = 'estimated_zone_midpoint';
      }
    }

    const baseExit = trueRBaseExit(row);
    if (!(baseEntry > 0 && baseExit > 0)) return null;
    const realisticEntry = direction === 'SHORT'
      ? baseEntry * (1 - TRUE_R_SLIPPAGE_PCT)
      : baseEntry * (1 + TRUE_R_SLIPPAGE_PCT);
    const realisticExit = direction === 'SHORT'
      ? baseExit * (1 + TRUE_R_SLIPPAGE_PCT)
      : baseExit * (1 - TRUE_R_SLIPPAGE_PCT);
    const numerator = direction === 'SHORT'
      ? realisticEntry - realisticExit
      : realisticExit - realisticEntry;
    return {
      realistic_entry: Number(realisticEntry.toFixed(4)),
      realistic_exit: Number(realisticExit.toFixed(4)),
      slippage_applied_pct: Number((TRUE_R_SLIPPAGE_PCT * 100).toFixed(3)),
      true_r: Number((numerator / risk).toFixed(3)),
      true_r_fill_source: fillSource,
      true_r_denominator_risk: Number(risk.toFixed(4)),
      true_r_estimated_fill: fillSource !== 'trigger_price',
      true_r_rule: `entry=max(planned, trigger) for longs when trigger exists; otherwise planned + ${TRUE_R_PROXY_GAP_FRACTION}x adverse observed gap or zone midpoint; ${TRUE_R_SLIPPAGE_PCT * 100}% adverse slippage on entry and exit`,
    };
  }

  function withTrueR(row) {
    const t = trueRForRow(row);
    return t ? { ...row, ...t } : { ...row, realistic_entry: null, realistic_exit: null, slippage_applied_pct: TRUE_R_SLIPPAGE_PCT * 100, true_r: null, true_r_fill_source: null, true_r_estimated_fill: null };
  }

  function statsFromTrueR(rows) {
    const enriched = rows.map(trueRForRow).filter(Boolean);
    const vals = enriched.map(r => r.true_r).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    return {
      count: vals.length,
      win_rate: vals.length ? Number((wins.length / vals.length * 100).toFixed(1)) : null,
      avg_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null,
      total_r: vals.length ? Number(vals.reduce((s, v) => s + v, 0).toFixed(3)) : 0,
      profit_factor: grossLoss ? Number((grossWin / grossLoss).toFixed(2)) : (grossWin ? 'infinity' : null),
      trigger_price_count: enriched.filter(r => r.true_r_fill_source === 'trigger_price').length,
      estimated_fill_count: enriched.filter(r => r.true_r_fill_source !== 'trigger_price').length,
    };
  }

  function storedVsTrueStats(rows) {
    return { stored: statsFromR(rows), true: statsFromTrueR(rows) };
  }

  function trueRComparison(rows = cleanResolvedRows()) {
    const pmf = rows.filter(isPrimaryPmf);
    const pmfLate = rows.filter(isPmfLate);
    const normalNonPmf = rows.filter(r => ideaRegime(r) === 'NORMAL' && !isPmfConfirmedAtStamp(r) && !isPmfLate(r) && r.size_rule === 'NORMAL_nonPMF_halfsize');
    const caution = rows.filter(r => ideaRegime(r) === 'CAUTION');
    const normal = rows.filter(r => ideaRegime(r) === 'NORMAL');
    return {
      fill_rule: {
        slippage_pct: Number((TRUE_R_SLIPPAGE_PCT * 100).toFixed(3)),
        proxy_gap_fraction: TRUE_R_PROXY_GAP_FRACTION,
        entry: 'Longs use max(planned entry, trigger/live entry price) when available; otherwise planned entry plus half the adverse observed gap, falling back to zone midpoint.',
        exit: 'Existing target/stop/exit price basis, made equal-or-worse by 0.10% adverse exit slippage.',
        denominator: 'Frozen original_risk_per_share.',
      },
      cohorts: {
        all_resolved: storedVsTrueStats(rows),
        pre_market_favourable: storedVsTrueStats(pmf),
        pmf_late: storedVsTrueStats(pmfLate),
        normal_non_pmf_halfsize: storedVsTrueStats(normalNonPmf),
        caution: storedVsTrueStats(caution),
        normal: storedVsTrueStats(normal),
      },
      coverage: {
        rows: rows.length,
        trigger_price_count: rows.filter(r => trueRTriggerPrice(r) != null).length,
        estimated_fill_count: rows.filter(r => trueRTriggerPrice(r) == null && trueRForRow(r) != null).length,
      },
    };
  }

  function allResolvedDisplayableRows() {
    return db.data.ideas
      .map(withDerivedStatus)
      .filter(isDisplayableIdea)
      .filter(i => canonicalResolvedR(i) != null);
  }

  function latestIdeaDate(rows) {
    const dates = rows.map(r => String(r.date || r.date_suggested || '').slice(0, 10)).filter(Boolean).sort();
    return dates.length ? dates[dates.length - 1] : null;
  }

  function canonicalStatsObject() {
    const v2Clean = cleanResolvedRows();
    const allLegacy = allResolvedDisplayableRows();
    const regimeTagged = v2Clean.filter(r => String(r.market_regime || r.regime || '').trim());
    const resolved_v2_clean = statsFromR(v2Clean);
    const resolved_all_including_legacy = statsFromR(allLegacy);
    const resolved_regime_tagged = statsFromR(regimeTagged);
    return {
      resolved_v2_clean: {
        ...resolved_v2_clean,
        label: 'v2 integrity-verified set',
        definition: 'displayable paper ideas dated 2026-06-09 or later with real non-zero paper_exit_price, canonical R available, and not quarantined',
        latest_trade_date: latestIdeaDate(v2Clean),
      },
      resolved_all_including_legacy: {
        ...resolved_all_including_legacy,
        label: 'all displayable ideas, includes pre-v2 legacy',
        definition: 'all displayable paper ideas with CLOSED status or any stored R outcome',
        latest_trade_date: latestIdeaDate(allLegacy),
      },
      resolved_regime_tagged: {
        ...resolved_regime_tagged,
        label: 'regime-tagged subset of v2 integrity-verified set',
        definition: 'v2 integrity-verified resolved ideas that carry market_regime or regime',
        latest_trade_date: latestIdeaDate(regimeTagged),
      },
      reconciliation: {
        regime_tagged_lte_v2_clean: resolved_regime_tagged.count <= resolved_v2_clean.count,
        v2_clean_lte_all_including_legacy: resolved_v2_clean.count <= resolved_all_including_legacy.count,
        ok: resolved_regime_tagged.count <= resolved_v2_clean.count && resolved_v2_clean.count <= resolved_all_including_legacy.count,
      },
    };
  }

  function normalNonPmfHalfsizeReadout(rows = cleanResolvedRows()) {
    const cfg = readSystem2Config();
    const rule = cfg.stage7 || {};
    const deployedAt = rule.normal_nonpmf_halfsize_deployed_at || null;
    const deployedDate = deployedAt ? String(deployedAt).slice(0, 10) : new Date().toISOString().slice(0, 10);
    const threshold = Number(rule.normal_nonpmf_halfsize_reeval_threshold || 40);
    const since = rows.filter(r => String(r.date || '').slice(0, 10) >= deployedDate);
    const normalNonPmf = since.filter(r => ideaRegime(r) === 'NORMAL' && !isPmfConfirmedAtStamp(r) && !isPmfLate(r) && r.size_rule === 'NORMAL_nonPMF_halfsize');
    const cautionNonPmf = since.filter(r => ideaRegime(r) === 'CAUTION' && !isPmfConfirmedAtStamp(r) && !isPmfLate(r));
    return {
      enabled: rule.normal_nonpmf_halfsize_enabled !== false,
      flag_path: 'stage7.normal_nonpmf_halfsize_enabled',
      deployed_at: deployedAt,
      deployed_date: deployedDate,
      threshold,
      rule_text: 'Half-sizing NORMAL non-PMF trades - risk-reduction rule, reversible. Based on consistent direction of evidence; exact bucket numbers still stabilizing.',
      reeval_text: `Re-evaluate cut decision once ${threshold}+ new NORMAL-non-PMF trades have resolved under this rule.`,
      normal_nonpmf_since_deploy: statsFromR(normalNonPmf),
      caution_nonpmf_since_deploy: statsFromR(cautionNonPmf),
    };
  }

  function normalNonPmfHalfsizeFields(row, update = {}) {
    const cfg = readSystem2Config();
    const rule = cfg.stage7 || {};
    const merged = { ...(row || {}), ...(update || {}) };
    const priorRule = row?.size_rule === 'NORMAL_nonPMF_halfsize';
    const base = priorRule ? {
      size_rule: null,
      size_rule_multiplier: null,
      size_rule_reason: null,
      position_r: 1.0,
      normal_nonpmf_halfsize_enabled: rule.normal_nonpmf_halfsize_enabled !== false,
      normal_nonpmf_halfsize_deployed_at: rule.normal_nonpmf_halfsize_deployed_at || null,
      normal_nonpmf_halfsize_reeval_threshold: Number(rule.normal_nonpmf_halfsize_reeval_threshold || 40),
    } : {};
    if (rule.normal_nonpmf_halfsize_enabled === false) return base;
    if (ideaRegime(merged) === 'NORMAL' && !isPreMarketFavourable(merged) && !isPmfLate(merged)) {
      return {
        size_rule: 'NORMAL_nonPMF_halfsize',
        size_rule_multiplier: 0.5,
        size_rule_reason: 'Half-sizing NORMAL non-PMF trades - risk-reduction rule, reversible. Based on consistent direction of evidence; exact bucket numbers still stabilizing.',
        position_r: 0.5,
        normal_nonpmf_halfsize_enabled: true,
        normal_nonpmf_halfsize_deployed_at: rule.normal_nonpmf_halfsize_deployed_at || null,
        normal_nonpmf_halfsize_reeval_threshold: Number(rule.normal_nonpmf_halfsize_reeval_threshold || 40),
      };
    }
    return base;
  }

  function regimeCoverageFromRows(rows) {
    const byRegime = new Map();
    for (const row of rows) {
      const regime = String(row.market_regime || row.regime || '').trim().toUpperCase();
      if (!regime) continue;
      if (!byRegime.has(regime)) byRegime.set(regime, []);
      byRegime.get(regime).push(row);
    }
    const regimeRows = [...byRegime.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([regime, group]) => {
      const stats = statsFromR(group);
      return {
        regime,
        count: stats.count,
        avg_r: stats.avg_r,
        win_rate: stats.win_rate,
        total_r: stats.total_r,
        last_trade_date: latestIdeaDate(group),
        universe_label: 'regime-tagged subset',
      };
    });
    return {
      regimes_traded: regimeRows.filter(r => r.count > 0).map(r => r.regime),
      regime_coverage: regimeRows,
    };
  }

  function plainReason(reason) {
    return String(reason || 'unknown')
      .replace(/^cluster_cap_(\d+)_per_/i, 'correlation gate: max $1 names in ')
      .replace(/^contradiction_gate:/i, 'contradiction gate:')
      .replace(/_/g, ' ');
  }

  function normalizeRejectionStage(stage) {
    const s = String(stage || '').toLowerCase();
    if (s.includes('news')) return 'news_safety';
    if (s.includes('correlation') || s.includes('cluster') || s.includes('stage7')) return 'correlation';
    if (s.includes('council') || s.includes('stage6')) return 'council';
    if (s.includes('option') || s.includes('stage3')) return 'options';
    if (s.includes('technical') || s.includes('stage2')) return 'technical';
    if (s.includes('cheap') || s.includes('stage1')) return 'cheap_filter';
    if (s.includes('final')) return 'finalists';
    return s || 'unknown';
  }

  function shadowRows() {
    const dbPath = path.join(system2Root, 'system2_shadow.db');
    if (!fs.existsSync(dbPath)) return [];
    const script = [
      'import sqlite3,json,sys',
      'db=sys.argv[1]',
      'con=sqlite3.connect(db); con.row_factory=sqlite3.Row',
      'rows=[dict(r) for r in con.execute("select * from shadow_portfolio order by rejection_date desc, symbol")]',
      'print(json.dumps(rows))',
    ].join('\n');
    try {
      return JSON.parse(childProcess.execFileSync('python3', ['-c', script, dbPath], { encoding: 'utf8', timeout: 15000 }));
    } catch {
      return [];
    }
  }

  function shadowKey(ticker, date, stage = '') {
    return [String(ticker || '').toUpperCase(), String(date || '').slice(0, 10), normalizeRejectionStage(stage)].join('|');
  }

  function shadowIndex() {
    const rows = shadowRows().map(r => ({
      ...r,
      ticker: String(r.symbol || '').toUpperCase(),
      stage_key: normalizeRejectionStage(r.rejection_stage),
      days_since: numericOrNull(r.trading_days_since_rejection) ?? 0,
      move_pct: numericOrNull(r.price_change_pct),
      price_then: numericOrNull(r.last_known_price),
      price_now: numericOrNull(r.current_price),
      reason_plain: plainReason(r.rejection_reason),
    }));
    const exact = new Map();
    const loose = new Map();
    for (const r of rows) {
      exact.set(shadowKey(r.ticker, r.rejection_date, r.rejection_stage), r);
      loose.set([r.ticker, r.rejection_date].join('|'), r);
    }
    return { rows, exact, loose };
  }

  function estimatedRejectedR(rejection, shadow) {
    const entry = numericOrNull(rejection?.entry ?? shadow?.price_then);
    const stop = numericOrNull(rejection?.stop);
    const target = numericOrNull(rejection?.target);
    const nowPrice = numericOrNull(shadow?.price_now);
    if (entry != null && stop != null && nowPrice != null && Math.abs(entry - stop) > 0) {
      return Number(((nowPrice - entry) / Math.abs(entry - stop)).toFixed(2));
    }
    const move = numericOrNull(shadow?.move_pct ?? shadow?.price_change_pct);
    return move == null ? null : Number((move / 5).toFixed(2));
  }

  function enrichRejectionWithShadow(row, idx = shadowIndex()) {
    const ticker = String(row.ticker || row.symbol || '').toUpperCase();
    const date = String(row.date || row.rejection_date || '').slice(0, 10);
    const stage = row.stage_rejected || row.rejection_stage || '';
    const tracked = idx.exact.get(shadowKey(ticker, date, stage)) || idx.loose.get([ticker, date].join('|')) || null;
    const days = tracked ? tracked.days_since : null;
    const trackingStatus = tracked
      ? (days < 3 ? 'tracking' : 'tracked')
      : (date && ((Date.now() - new Date(date + 'T00:00:00Z')) / 86400000) < 4 ? 'tracking' : 'missing');
    const wouldBeR = tracked && days >= 3 ? estimatedRejectedR(row, tracked) : numericOrNull(row.r_3d);
    return {
      ...row,
      shadow_tracked: Boolean(tracked),
      tracking_status: trackingStatus,
      shadow_source: tracked ? 'system2_shadow.db shadow_portfolio' : null,
      shadow_price_then: tracked?.price_then ?? null,
      shadow_price_now: tracked?.price_now ?? null,
      shadow_move_pct: tracked?.move_pct ?? null,
      shadow_days_since: days,
      shadow_reason: tracked?.reason_plain ?? plainReason(row.reason),
      would_be_r: wouldBeR,
      r_3d: numericOrNull(row.r_3d) ?? (tracked && days >= 3 ? wouldBeR : row.r_3d),
    };
  }

  function rejectedOutcomeRows() {
    ensure();
    const idx = shadowIndex();
    const keyedRejections = new Map();
    for (const row of db.data.system2_rejections || []) {
      const ticker = String(row.ticker || '').toUpperCase();
      const date = String(row.date || '').slice(0, 10);
      if (ticker && date) keyedRejections.set([ticker, date].join('|'), row);
    }
    const fromShadow = idx.rows.map(s => {
      const base = keyedRejections.get([s.ticker, s.rejection_date].join('|')) || {
        ticker: s.ticker,
        date: s.rejection_date,
        stage_rejected: s.stage_key,
        reason: s.rejection_reason,
      };
      return enrichRejectionWithShadow(base, idx);
    });
    const existingKeys = new Set(fromShadow.map(r => [String(r.ticker).toUpperCase(), String(r.date).slice(0, 10)].join('|')));
    const recentPending = (db.data.system2_rejections || [])
      .filter(r => !existingKeys.has([String(r.ticker || '').toUpperCase(), String(r.date || '').slice(0, 10)].join('|')))
      .map(r => enrichRejectionWithShadow(r, idx));
    return [...fromShadow, ...recentPending].sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
  }

  function rejectedOutcomeSummary(rows) {
    const groups = {};
    const nowMs = Date.now();
    const dayMs = 86400000;
    const rowAgeDays = (r) => {
      const d = String(r.date || r.rejection_date || '').slice(0, 10);
      const t = d ? new Date(d + 'T00:00:00Z').getTime() : NaN;
      return Number.isFinite(t) ? Math.max(0, Math.floor((nowMs - t) / dayMs)) : null;
    };
    const avgMove = (vals) => vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(2)) : null;
    for (const r of rows) {
      const key = plainReason(r.reason || r.shadow_reason || r.stage_rejected);
      if (!groups[key]) groups[key] = { reason: key, tracked: 0, tracking: 0, total_r: 0, total_saved_r: 0, total_cost_r: 0, rejected_7d: 0, rejected_30d: 0, _recent_moves: [], _prior_moves: [] };
      const g = groups[key];
      const age = rowAgeDays(r);
      if (age != null && age <= 7) g.rejected_7d += 1;
      if (age != null && age <= 30) g.rejected_30d += 1;
      const move = numericOrNull(r.shadow_move_pct);
      if (move != null && r.tracking_status !== 'tracking') {
        if (age != null && age <= 7) g._recent_moves.push(move);
        else if (age != null && age <= 30) g._prior_moves.push(move);
      }
      if (r.tracking_status === 'tracking') g.tracking += 1;
      if (numericOrNull(r.would_be_r) != null && r.tracking_status !== 'tracking') {
        const rv = Number(r.would_be_r);
        g.tracked += 1;
        g.total_r = Number((g.total_r + rv).toFixed(2));
        if (rv > 0) g.total_cost_r = Number((g.total_cost_r + rv).toFixed(2));
        if (rv <= 0) g.total_saved_r = Number((g.total_saved_r + Math.abs(rv)).toFixed(2));
      }
    }
    return Object.values(groups).map(g => {
      const avg_move_recent_7d = avgMove(g._recent_moves);
      const avg_move_prior_30d = avgMove(g._prior_moves);
      let trend = 'insufficient data';
      if (avg_move_recent_7d != null && avg_move_prior_30d != null) {
        const delta = Number((avg_move_recent_7d - avg_move_prior_30d).toFixed(2));
        trend = Math.abs(delta) < 0.25 ? 'flat' : delta > 0 ? 'rising' : 'falling';
      }
      const { _recent_moves, _prior_moves, ...clean } = g;
      return {
        ...clean,
        avg_move_recent_7d,
        avg_move_prior_30d,
        trend,
      };
    }).sort((a, b) => b.tracked - a.tracked);
  }

  function stagePerformanceFrom(rows) {
    const latest = latestStageDetailRow();
    const stages = [
      ['universe', 'Universe'],
      ['stage1', 'B2 Cheap Filter'],
      ['stage2', 'B3 Technical'],
      ['stage5', 'News Safety'],
      ['stage3', 'Options'],
      ['stage6', 'Council'],
      ['stage7', 'Correlation'],
      ['finalists', 'Finalists'],
    ];
    const ideasByTicker = new Map(db.data.ideas.map(withDerivedStatus).filter(isDisplayableIdea).map(i => [String(i.ticker || '').toUpperCase(), i]));
    return stages.map(([key, name]) => {
      const stage = stageByKey(latest, key) || {};
      const rejected = rows.filter(r => normalizeRejectionStage(r.stage_rejected || r.rejection_stage) === normalizeRejectionStage(key));
      const tracked = rejected.filter(r => numericOrNull(r.would_be_r) != null && r.tracking_status !== 'tracking');
      const positive = tracked.filter(r => Number(r.would_be_r) > 0);
      const passedTickers = (stage.tickers || []).filter(r => ['KEPT', 'ENRICHED', 'FINALIST', 'TIER1', 'TIER2', 'UPGRADE'].includes(String(r.status || '').toUpperCase())).map(r => String(r.ticker || '').toUpperCase());
      const survivorResolved = passedTickers.map(t => ideasByTicker.get(t)).filter(i => i && canonicalResolvedR(i) != null);
      const survivorWins = survivorResolved.filter(i => Number(canonicalResolvedR(i)) > 0);
      const rejectAvg = tracked.length ? tracked.reduce((s, r) => s + Number(r.would_be_r), 0) / tracked.length : null;
      const survivorAvg = survivorResolved.length ? survivorResolved.reduce((s, i) => s + Number(canonicalResolvedR(i)), 0) / survivorResolved.length : null;
      const rejectQuality = tracked.length ? positive.length / tracked.length : null;
      const survivorQuality = survivorResolved.length ? survivorWins.length / survivorResolved.length : null;
      let verdict = 'Partial data';
      if (rejectQuality != null && survivorQuality != null) {
        verdict = survivorQuality > rejectQuality + 0.1 ? 'Working' : rejectQuality > survivorQuality + 0.1 ? 'Review' : 'Marginal';
      }
      return {
        stage_key: key,
        stage: name,
        passed: numericOrNull(stage.kept) ?? passedTickers.length,
        rejected: numericOrNull(stage.rejected) ?? rejected.length,
        rejection_rate: stage.entered ? Number(((Number(stage.rejected || 0) / Number(stage.entered || 1)) * 100).toFixed(1)) : null,
        reject_quality: {
          tracked: tracked.length,
          pct_positive_5d: tracked.length ? Number((positive.length / tracked.length * 100).toFixed(1)) : null,
          avg_missed_r: rejectAvg == null ? null : Number(rejectAvg.toFixed(2)),
        },
        survivor_quality: {
          resolved: survivorResolved.length,
          win_rate: survivorResolved.length ? Number((survivorWins.length / survivorResolved.length * 100).toFixed(1)) : null,
          avg_r: survivorAvg == null ? null : Number(survivorAvg.toFixed(2)),
        },
        verdict,
      };
    });
  }

  function numericOrNull(value) {
    if (value == null || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function freezeOriginalRisk(row) {
    if (!row) return;
    if (row.original_entry == null && row.entry != null) row.original_entry = Number(row.entry);
    if (row.original_stop == null && row.stop != null) row.original_stop = Number(row.stop);
    if (row.original_target == null && row.target != null) row.original_target = Number(row.target);
    if (row.original_risk_per_share == null) {
      const e = numericOrNull(row.original_entry ?? row.entry);
      const s = numericOrNull(row.original_stop ?? row.stop);
      if (e != null && s != null) {
        row.original_risk_per_share = Number(Math.abs(e - s).toFixed(4));
      } else if (numericOrNull(row.risk_per_share) > 0) {
        row.original_risk_per_share = numericOrNull(row.risk_per_share);
      }
    }
    if (row.original_direction == null) {
      const target = numericOrNull(row.original_target ?? row.target);
      const entry = numericOrNull(row.original_entry ?? row.entry);
      if (target != null && entry != null) {
        row.original_direction = target < entry ? 'SHORT' : 'LONG';
      }
    }
  }

  function effectiveEntry(row) {
    return numericOrNull(row?.original_entry ?? row?.actual_entry_price ?? row?.paper_entry_price ?? row?.entry);
  }

  function effectiveStop(row) {
    return numericOrNull(row?.original_stop ?? row?.stop);
  }

  function effectiveTarget(row) {
    return numericOrNull(row?.original_target ?? row?.target);
  }

  function effectiveRiskPerShare(row) {
    const explicit = numericOrNull(row?.original_risk_per_share);
    if (explicit > 0) return explicit;
    const entry = effectiveEntry(row);
    const stop = effectiveStop(row);
    if (entry != null && stop != null) {
      const risk = Math.abs(entry - stop);
      if (risk > 0) return Number(risk.toFixed(4));
    }
    return numericOrNull(row?.risk_per_share);
  }

  function markSuspectIfNeeded(row) {
    if (row == null) return;
    if (row.r_calculation_suspect != null) return;
    if (isInvalidTicker(row.ticker)) {
      row.r_calculation_suspect = true;
      row.r_calculation_suspect_reason = 'invalid ticker';
      return;
    }
    const r = numericOrNull(row.paper_exit_r ?? row.actual_r ?? row.unrealized_r);
    if (r != null && (Math.abs(r) > SUSPECT_ABS_R || r < SUSPECT_LOSS_R)) {
      row.r_calculation_suspect = true;
      row.r_calculation_suspect_reason = `implausible R ${r}R`;
      return;
    }
    const originalEntry = effectiveEntry(row);
    const actualEntry = numericOrNull(row.actual_entry_price);
    const risk = effectiveRiskPerShare(row);
    if (originalEntry != null && actualEntry != null && risk > 0) {
      if (Math.abs(actualEntry - originalEntry) > risk * 3) {
        row.r_calculation_suspect = true;
        row.r_calculation_suspect_reason = `actual entry ${actualEntry} far from planned ${originalEntry} (risk ${risk})`;
      }
    }
  }

  function resolvedActualR(row) {
    return canonicalResolvedR(row);
  }

  function rValue(entry, riskPerShare, price) {
    return riskPerShare && riskPerShare > 0 && price != null
      ? Number(((Number(price) - Number(entry)) / Number(riskPerShare)).toFixed(3))
      : null;
  }

  function simulatedLongExit(row, window, fallbackClose, timeoutHit) {
    const entry = effectiveEntry(row);
    const stop = effectiveStop(row);
    const target = effectiveTarget(row);
    for (const bar of window) {
      const open = Number(bar.open);
      const high = Number(bar.high);
      const low = Number(bar.low);
      if (Number.isFinite(stop) && Number.isFinite(low) && low <= stop) {
        const exitPrice = Number.isFinite(open) && open < stop ? open : stop;
        return { hit: 'STOP', exitPrice, exitDate: bar.date };
      }
      if (Number.isFinite(target) && Number.isFinite(high) && high >= target) {
        const exitPrice = Number.isFinite(open) && open > target ? open : target;
        return { hit: 'TARGET', exitPrice, exitDate: bar.date };
      }
    }
    return { hit: timeoutHit, exitPrice: fallbackClose, exitDate: window[window.length - 1]?.date || null };
  }

  async function fetchDailyHistory(ticker) {
    const url = `https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=${ticker}&apikey=${effectiveFmpKey}`;
    const data = await httpGet(url);
    return (data && data.historical) ? data.historical : (Array.isArray(data) ? data : []);
  }

  function applyMarks(row, hist, marks, today) {
    let updated = 0;
    const ideaDate = new Date(row.date + 'T00:00:00Z');
    const ageDays = Math.floor((today - ideaDate) / 86400000);
    const sorted = [...hist].sort((a,b)=> new Date(a.date)-new Date(b.date));
    const afterEntry = sorted.filter(h => new Date(h.date) > ideaDate);
    for (const m of marks) {
      if ((row.scored_stage || 0) >= m.stage) continue;
      if (ageDays < m.days) continue;
      if (afterEntry.length < m.days) continue;
      const bar = afterEntry[m.days - 1];
      const px = Number(bar.close);
      row[m.field] = px;
      const window = afterEntry.slice(0, m.days);
      const highs = window.map(h => Number(h.high));
      const lows = window.map(h => Number(h.low));
      const entryForPct = effectiveEntry(row);
      if (highs.length && entryForPct) row.max_gain_pct = Number((((Math.max(...highs) - entryForPct)/entryForPct)*100).toFixed(2));
      if (lows.length && entryForPct) row.max_dd_pct = Number((((Math.min(...lows) - entryForPct)/entryForPct)*100).toFixed(2));
      const exit = simulatedLongExit(row, window, px, m.days >= 10 ? 'TIME' : 'OPEN');
      row.hit = exit.hit;
      const rawR = rValue(effectiveEntry(row), effectiveRiskPerShare(row), exit.exitPrice);
      row[`${m.rfield}_raw`] = rawR;
      row[m.rfield] = riskAdjustedStoredR(row, rawR);
      if (positionRMultiplier(row) !== 1) row.r_values_risk_adjusted = true;
      freezeOriginalRisk(row);
      markSuspectIfNeeded(row);
      row.paper_exit_reason = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.hit : row.paper_exit_reason;
      row.paper_exit_at = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.exitDate : row.paper_exit_at;
      row.paper_exit_price = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.exitPrice : row.paper_exit_price;
      row.paper_exit_r = exit.hit === 'STOP' || exit.hit === 'TARGET' ? row[m.rfield] : row.paper_exit_r;
      row.scored_stage = m.stage;
      row.scored_at = now();
      const status = deriveIdeaStatus(row);
      row.paper_status = status.status;
      row.paper_outcome = status.outcome;
      updated++;
    }
    return updated;
  }

  async function quoteMap(symbols) {
    const out = {};
    const unique = [...new Set(symbols.filter(Boolean).map(s => String(s).toUpperCase()))];
    for (let i = 0; i < unique.length; i += 50) {
      const chunk = unique.slice(i, i + 50);
      try {
        const data = await httpGet(`https://financialmodelingprep.com/stable/batch-quote?symbols=${chunk.join(',')}&apikey=${effectiveFmpKey}`);
        const arr = Array.isArray(data) ? data : (data ? [data] : []);
        for (const q of arr) {
          const sym = String(q.symbol || '').toUpperCase();
          const price = Number(q.price || q.close || q.previousClose);
          if (sym && Number.isFinite(price)) out[sym] = price;
        }
      } catch {
        for (const sym of chunk) {
          try {
            const one = await httpGet(`https://financialmodelingprep.com/stable/quote?symbol=${sym}&apikey=${effectiveFmpKey}`);
            const q = Array.isArray(one) ? one[0] : one;
            const price = Number(q?.price || q?.close || q?.previousClose);
            if (Number.isFinite(price)) out[sym] = price;
          } catch {}
        }
      }
    }
    return out;
  }

  function quotePrice(q) {
    const fields = [
      'preMarketPrice', 'premarketPrice', 'preMarket', 'pre_market_price',
      'price', 'close', 'previousClose',
    ];
    for (const field of fields) {
      const v = Number(q?.[field]);
      if (Number.isFinite(v) && v > 0) return { price: v, source: field };
    }
    return { price: null, source: null };
  }

  async function quoteRows(symbols) {
    const out = {};
    const unique = [...new Set(symbols.filter(Boolean).map(s => String(s).toUpperCase()))];
    for (let i = 0; i < unique.length; i += 50) {
      const chunk = unique.slice(i, i + 50);
      try {
        const data = await httpGet(`https://financialmodelingprep.com/stable/batch-quote?symbols=${chunk.join(',')}&apikey=${effectiveFmpKey}`);
        const arr = Array.isArray(data) ? data : (data ? [data] : []);
        for (const q of arr) {
          const sym = String(q.symbol || '').toUpperCase();
          if (sym) out[sym] = q;
        }
      } catch {
        for (const sym of chunk) {
          try {
            const one = await httpGet(`https://financialmodelingprep.com/stable/quote?symbol=${sym}&apikey=${effectiveFmpKey}`);
            const q = Array.isArray(one) ? one[0] : one;
            if (q) out[sym] = q;
          } catch {}
        }
      }
    }
    return out;
  }

  function envFlag(name, fallback = false) {
    const raw = process.env[name] ?? readEnvValue(name);
    if (raw == null || raw === '') return fallback;
    return ['1', 'true', 'yes', 'on'].includes(String(raw).toLowerCase());
  }

  function pmfAlertConfig() {
    return {
      enabled: envFlag('PMF_ALERTS_ENABLED', false),
      pmfConfirmed: envFlag('PMF_ALERT_PMF_CONFIRMED', true),
      autoFilled: envFlag('PMF_ALERT_AUTO_FILLED', true),
      positionClosed: envFlag('PMF_ALERT_POSITION_CLOSED', true),
      failures: envFlag('PMF_ALERT_FAILURES', true),
      dailySummary: envFlag('PMF_ALERT_DAILY_SUMMARY', false),
      pmfLate: envFlag('PMF_ALERT_PMF_LATE', false),
      timeoutMs: Number(process.env.PMF_ALERT_TIMEOUT_MS || readEnvValue('PMF_ALERT_TIMEOUT_MS') || 4000),
    };
  }

  function logPmfAlert(event) {
    try {
      fs.mkdirSync(logDir, { recursive: true });
      fs.appendFileSync(path.join(logDir, 'pmf_telegram_alerts.log'), `${new Date().toISOString()} ${JSON.stringify(event)}\n`);
    } catch {}
  }

  async function sendTelegramAlert(text, overrides = {}) {
    const token = overrides.token ?? process.env.TELEGRAM_BOT_TOKEN ?? process.env.TG_BOT_TOKEN ?? readEnvValue('TELEGRAM_BOT_TOKEN') ?? readEnvValue('TG_BOT_TOKEN');
    const chatId = overrides.chatId ?? process.env.TELEGRAM_CHAT_ID ?? process.env.TG_CHAT_ID ?? readEnvValue('TELEGRAM_CHAT_ID') ?? readEnvValue('TG_CHAT_ID');
    if (!token || !chatId) return { sent: false, reason: 'missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID' };
    const timeoutMs = Number(overrides.timeoutMs || pmfAlertConfig().timeoutMs || 4000);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
    try {
      const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text }),
        signal: controller.signal,
      });
      if (!r.ok) return { sent: false, reason: `telegram ${r.status}` };
      return { sent: true };
    } finally {
      clearTimeout(timeout);
    }
  }

  async function safeSendTelegramAlert(text, meta = {}, overrides = {}) {
    try {
      const sent = await sendTelegramAlert(text, overrides);
      logPmfAlert({ event: meta.event || 'telegram_alert', text, ...meta, ...sent });
      return sent;
    } catch (e) {
      const reason = e?.name === 'AbortError' ? 'telegram timeout' : (e?.message || String(e));
      logPmfAlert({ event: meta.event || 'telegram_alert_failed', text, ...meta, sent: false, reason });
      return { sent: false, reason };
    }
  }

  function queuePmfTelegramAlert(text, meta = {}, category = null) {
    const cfg = pmfAlertConfig();
    if (!cfg.enabled) return { queued: false, reason: 'PMF_ALERTS_ENABLED=false' };
    if (category && cfg[category] === false) return { queued: false, reason: `${category}=false` };
    Promise.resolve()
      .then(() => safeSendTelegramAlert(text, meta))
      .catch(e => logPmfAlert({ event: 'telegram_alert_unhandled_failure', text, ...meta, sent: false, reason: e?.message || String(e) }));
    return { queued: true };
  }

  function convictionLabel(gapPct) {
    const g = Math.abs(Number(gapPct) || 0);
    if (g >= 7) return 'high';
    if (g >= 5) return 'strong';
    if (g >= 3) return 'moderate';
    return 'watch';
  }

  function fmtPrice(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(n >= 1 ? 2 : 4) : 'n/a';
  }

  function formatPmfConfirmedMessage(rows) {
    const sorted = [...rows].sort((a, b) => Math.abs(Number(b.pre_market_gap_pct || 0)) - Math.abs(Number(a.pre_market_gap_pct || 0)));
    const lines = sorted.map(row => {
      const ticker = row.ticker || row.symbol;
      return `PMF: ${ticker} ${Number(row.pre_market_gap_pct || 0) >= 0 ? '+' : ''}${row.pre_market_gap_pct ?? 'n/a'}% (${row.pre_market_gap_atr_multiple ?? row.pmf_atr_multiple_at_stamp ?? 'n/a'}x ATR) | entry ${fmtPrice(effectiveEntry(row))} stop ${fmtPrice(effectiveStop(row))} target ${fmtPrice(effectiveTarget(row))} | conviction: ${convictionLabel(row.pre_market_gap_pct)}`;
    });
    return lines.join('\n');
  }

  function formatAutoExecMessage(result, idea) {
    const ticker = result.ticker || idea?.ticker || idea?.symbol || 'UNKNOWN';
    if (result.action === 'placed' && result.actual_entry_price) {
      return `FILLED: ${ticker} 1sh @ ${fmtPrice(result.actual_entry_price)} (slip ${idea?.entry_slippage_vs_model != null ? `${idea.entry_slippage_vs_model >= 0 ? '+' : ''}${idea.entry_slippage_vs_model}%` : 'n/a'}) | OCO stop ${fmtPrice(idea?.recalibrated_stop || result.recalibrated_stop)} target ${fmtPrice(idea?.recalibrated_target || result.recalibrated_target)}`;
    }
    if (result.action === 'risk_breaker_blocked') return `RISK BREAKER TRIPPED: ${result.breaker || 'unknown'} - ${ticker} blocked: ${result.reason || 'unknown'}`;
    if (['failed', 'bracket_attach_failed'].includes(result.action)) return `AUTO-EXEC FAILED: ${ticker} - ${result.reason || result.status || 'unknown'}`;
    return null;
  }

  function formatSyncAlertMessage(result, idea) {
    const ticker = result.ticker || idea?.ticker || idea?.symbol || 'UNKNOWN';
    if (result.external_close && result.external_close_alert_needed) {
      const integrity = cohortIntegrityPayload();
      return `⚠ EXTERNAL CLOSE DETECTED — ${ticker} closed by non-OCO order at ${fmtPrice(idea?.actual_exit_price || result.exit_price)}\ntrade excluded from clean cohort · clean-B unaffected count: ${integrity.clean_count}`;
    }
    if (String(result.action || '').startsWith('updated_exit')) {
      return `CLOSED: ${ticker} @ ${fmtPrice(idea?.actual_exit_price || result.exit_price)} | ${idea?.actual_exit_reason || 'ALPACA_EXIT'} | real R ${idea?.real_r != null ? `${Number(idea.real_r) >= 0 ? '+' : ''}${Number(idea.real_r).toFixed(2)}` : 'n/a'}`;
    }
    if (result.action === 'alert_no_exit_order') return `NAKED POSITION: ${ticker} has no exit order`;
    if (result.action === 'sync_failed') return `AUTO-EXEC FAILED: ${ticker} - sync failed: ${result.reason || 'unknown'}`;
    return null;
  }

  function cohortIntegrityPayload() {
    const entries = (db.data.ideas || []).filter(row => row?.test_order !== true && numericOrNull(row?.actual_entry_price) != null);
    const clean = entries.filter(row => row.cohort_label === 'B_CLEAN');
    const contaminated = entries.filter(row => row.cohort_label === 'B_CONTAMINATED_MANUAL_EXITS');
    const last = [...contaminated].sort((a, b) => String(b.actual_exit_time || '').localeCompare(String(a.actual_exit_time || '')))[0] || null;
    const denominator = clean.length + contaminated.length;
    const rate = denominator ? Number(((contaminated.length / denominator) * 100).toFixed(1)) : 0;
    return {
      clean_count: clean.length,
      clean_resolved_count: clean.filter(row => numericOrNull(row.actual_exit_price) != null && numericOrNull(row.real_r) != null).length,
      clean_open_count: clean.filter(row => numericOrNull(row.actual_exit_price) == null).length,
      contaminated_count: contaminated.length,
      contamination_rate_pct: rate,
      last_contamination_at: last?.actual_exit_time || null,
      last_contamination_ticker: last?.ticker || last?.symbol || null,
      at_risk: rate > 20,
    };
  }

  function cohortIntegrityLine(integrity = cohortIntegrityPayload()) {
    const lastDate = integrity.last_contamination_at ? String(integrity.last_contamination_at).slice(0, 10) : 'none';
    const lastTicker = integrity.last_contamination_ticker || 'none';
    const line = `COHORT INTEGRITY: clean ${integrity.clean_count} · contaminated ${integrity.contaminated_count} (${integrity.contamination_rate_pct}%) · last contamination: ${lastDate} ${lastTicker}`;
    return integrity.at_risk ? `${line}\nCOHORT AT RISK — external closes are invalidating the test.` : line;
  }

  function buildDailyPmfSummary() {
    const today = new Date().toISOString().slice(0, 10);
    const rows = db.data.ideas || [];
    const confirmed = rows.filter(r => String(r.pmf_stamp_time || '').slice(0, 10) === today && isPmfConfirmedAtStamp(r));
    const placed = rows.filter(r => String(r.auto_exec_at || '').slice(0, 10) === today && r.fill_source === 'alpaca_paper' && !r.test_order);
    const closed = rows.filter(r => String(r.actual_exit_time || '').slice(0, 10) === today && r.fill_source === 'alpaca_paper' && !r.test_order);
    const open = rows.filter(r => r.fill_source === 'alpaca_paper' && !r.test_order && r.actual_entry_price && !r.actual_exit_price);
    const cleanB = rows.filter(r => r.cohort_label === 'B_CLEAN' && r.test_order !== true && numericOrNull(r.actual_entry_price) != null);
    const cleanBResolved = cleanB.filter(r => numericOrNull(r.actual_exit_price) != null && numericOrNull(r.real_r) != null);
    const contaminatedB = rows.filter(r => r.cohort_label === 'B_CONTAMINATED_MANUAL_EXITS');
    const integrity = cohortIntegrityPayload();
    const totalR = closed.reduce((s, r) => s + (Number(r.real_r) || 0), 0);
    const peadRows = Array.isArray(db.data.pead_drift_paper) ? db.data.pead_drift_paper : [];
    const peadResolved = peadRows.filter(row => String(row.paper_status || '').toUpperCase() === 'RESOLVED').length;
    const peadNewest = [...peadRows].sort((a, b) => String(b.earnings_date || b.date || b.logged_at || '').localeCompare(String(a.earnings_date || a.date || a.logged_at || '')))[0];
    let peadCheck = 'FAIL';
    try {
      const raw = fs.readFileSync(path.join(system2Root, 'logs', 'pead_drift_tracker.log'), 'utf8');
      let depth = 0, start = -1, inString = false, escaped = false, latest = null;
      for (let idx = 0; idx < raw.length; idx += 1) {
        const ch = raw[idx];
        if (inString) {
          if (escaped) escaped = false;
          else if (ch === '\\') escaped = true;
          else if (ch === '"') inString = false;
          continue;
        }
        if (ch === '"') inString = true;
        else if (ch === '{') { if (depth === 0) start = idx; depth += 1; }
        else if (ch === '}' && depth > 0) {
          depth -= 1;
          if (depth === 0 && start >= 0) {
            try { latest = JSON.parse(raw.slice(start, idx + 1)); } catch {}
          }
        }
      }
      peadCheck = latest?.ok === true ? 'OK' : 'FAIL';
    } catch {}
    const pmfLine = `Today: ${confirmed.length} PMF confirmed, ${placed.length} orders placed, ${closed.length} closed (${totalR >= 0 ? '+' : ''}${totalR.toFixed(2)}R), ${open.length} open | jobs OK`;
    const peadLine = `PEAD: n=${peadRows.length} (${peadResolved} resolved) · newest: ${peadNewest?.ticker || 'none'} · next check ${peadCheck}`;
    const cohortLine = `Cohort B clean: ${cleanBResolved.length}/15 resolved, ${cleanB.length - cleanBResolved.length} open`;
    const contaminatedLine = `B-contaminated (manual exits, not a system test): n=${contaminatedB.length}, excluded`;
    return `${pmfLine}\n${cohortLine}\n${contaminatedLine}\n${cohortIntegrityLine(integrity)}\n${peadLine}`;
  }

  async function runPreMarketGapCheck(opts = {}) {
    ensure();
    const dryRun = Boolean(opts.dryRun || opts.dry_run);
    const cohort = String(opts.cohort || opts.pmf_cohort || 'primary').toLowerCase();
    const lateMode = cohort === 'late' || cohort === 'pmf_late';
    const thresholdAtrMultiple = Number(opts.thresholdAtrMultiple || opts.threshold_atr_multiple || 1.5);
    const at = now();
    const openIdeas = db.data.ideas
      .map(withDerivedStatus)
      .filter(i => i.paper !== false && i.paper_status === 'OPEN' && effectiveEntry(i));
    const quotes = await quoteRows(openIdeas.map(i => i.ticker));
    const checked = [];
    const alerts = [];
    const errors = [];
    const newlyStampedPmfs = [];
    const newlyStampedPmfLate = [];

    for (const idea of db.data.ideas) {
      const derived = withDerivedStatus(idea);
      if (isInvalidTicker(idea.ticker)) continue;
      if (!(derived.paper !== false && derived.paper_status === 'OPEN' && effectiveEntry(derived))) continue;
      const ticker = String(idea.ticker).toUpperCase();
      const wasPmf = isPreMarketFavourable(idea) || isPmfConfirmedAtStamp(idea);
      let pmfStamp = null;
      const q = quotes[ticker];
      const { price: pmPrice, source: priceSource } = quotePrice(q);
      const averageShareVolume = Number(q?.avgVolume ?? q?.averageVolume);
      const averageDollarVolume = Number.isFinite(averageShareVolume) && averageShareVolume > 0 && Number.isFinite(pmPrice) && pmPrice > 0
        ? Number((averageShareVolume * pmPrice).toFixed(2))
        : null;
      const atr = Number(idea.atr14 || idea.atr || effectiveRiskPerShare(idea));
      const entry = effectiveEntry(idea);
      const target = effectiveTarget(idea);
      const direction = Number.isFinite(target) && target < entry ? 'SHORT' : 'LONG';
      const update = {
        ticker,
        entry,
        direction,
        market_regime: idea.market_regime || idea.regime || null,
        regime: idea.regime || idea.market_regime || null,
        pre_market_checked_at: at,
        pre_market_price: pmPrice,
        pre_market_price_source: priceSource,
        pre_market_gap_adverse: false,
        pre_market_gap_favourable: false,
        pre_market_gap_favourable_late: Boolean(idea.pre_market_gap_favourable_late),
        pre_market_gap_pct: null,
        pre_market_gap_atr_multiple: null,
        pre_market_gap_threshold_atr: thresholdAtrMultiple,
        pre_market_gap_error: null,
        pmf_cohort: idea.pmf_cohort || null,
        pmf_late_checked_at: lateMode ? at : idea.pmf_late_checked_at || null,
        pmf_late_price: lateMode ? pmPrice : idea.pmf_late_price || null,
        pmf_late_price_source: lateMode ? priceSource : idea.pmf_late_price_source || null,
        pmf_late_gap_pct: null,
        pmf_late_gap_atr_multiple: null,
        pmf_late_threshold_atr: lateMode ? thresholdAtrMultiple : idea.pmf_late_threshold_atr || null,
        pmf_late_error: null,
      };

      if (!Number.isFinite(pmPrice) || pmPrice <= 0) {
        update.pre_market_gap_error = 'no pre-market/quote price from FMP';
        if (lateMode) update.pmf_late_error = update.pre_market_gap_error;
        errors.push(`${ticker}: ${update.pre_market_gap_error}`);
      } else if (!Number.isFinite(atr) || atr <= 0) {
        update.pre_market_gap_error = 'missing ATR; used by 1.5x ATR gap rule';
        if (lateMode) update.pmf_late_error = update.pre_market_gap_error;
        errors.push(`${ticker}: ${update.pre_market_gap_error}`);
      } else {
        const move = pmPrice - entry;
        const signedSetupMove = direction === 'LONG' ? move : -move;
        update.pre_market_gap_pct = Number(((move / entry) * 100).toFixed(2));
        update.pre_market_gap_atr_multiple = Number((Math.abs(move) / atr).toFixed(2));
        const signedAtrMultiple = Number((signedSetupMove / atr).toFixed(3));
        if (lateMode) {
          update.pmf_late_gap_pct = update.pre_market_gap_pct;
          update.pmf_late_gap_atr_multiple = update.pre_market_gap_atr_multiple;
        }
        update.pre_market_gap_adverse = signedSetupMove <= -(thresholdAtrMultiple * atr);
        const qualifies = signedSetupMove >= (thresholdAtrMultiple * atr);
        if (lateMode) {
          update.pre_market_gap_favourable = idea.pre_market_gap_favourable === true;
          update.pre_market_gap_favourable_late = qualifies && !wasPmf;
          update.pmf_cohort = update.pre_market_gap_favourable_late ? 'PMF_LATE' : (idea.pmf_cohort || null);
          if (update.pre_market_gap_favourable_late) {
            pmfStamp = { at, price: pmPrice, entry, atr, atrMultiple: signedAtrMultiple, cohort: 'PMF_LATE', averageShareVolume: averageShareVolume > 0 ? averageShareVolume : null, averageDollarVolume };
          }
        } else {
          update.pre_market_gap_favourable = qualifies;
          update.pre_market_gap_favourable_late = Boolean(idea.pre_market_gap_favourable_late);
          update.pmf_cohort = qualifies ? 'PMF' : (idea.pmf_cohort || null);
          if (qualifies) {
            pmfStamp = { at, price: pmPrice, entry, atr, atrMultiple: signedAtrMultiple, cohort: 'PMF', averageShareVolume: averageShareVolume > 0 ? averageShareVolume : null, averageDollarVolume };
          }
        }
        if (update.pre_market_gap_adverse && !dryRun) {
          const gapDirection = move < 0 ? 'down' : 'up';
          const msg = `⚠️ PRE-MARKET GAP: ${ticker} gapped ${gapDirection} ${Math.abs(update.pre_market_gap_pct)}% against setup. Review before market open.`;
          const queued = queuePmfTelegramAlert(msg, { event: 'adverse_gap_alert', ticker }, 'failures');
          alerts.push({ ticker, message: msg, ...queued });
        }
      }

      if (pmfStamp) Object.assign(update, easternSessionFields(pmfStamp.at) || {});
      if (!lateMode) Object.assign(update, normalNonPmfHalfsizeFields(idea, update));
      checked.push(update);
      if (!dryRun) {
        if (lateMode) {
          const lateUpdate = {
            pmf_late_checked_at: update.pmf_late_checked_at,
            pmf_late_price: update.pmf_late_price,
            pmf_late_price_source: update.pmf_late_price_source,
            pmf_late_gap_pct: update.pmf_late_gap_pct,
            pmf_late_gap_atr_multiple: update.pmf_late_gap_atr_multiple,
            pmf_late_threshold_atr: update.pmf_late_threshold_atr,
            pmf_late_error: update.pmf_late_error,
            pre_market_gap_favourable_late: update.pre_market_gap_favourable_late,
          };
          if (update.pre_market_gap_favourable_late) lateUpdate.pmf_cohort = 'PMF_LATE';
          Object.assign(idea, lateUpdate);
          if (update.pre_market_gap_favourable_late) writePmfConfirmationStamp(idea, pmfStamp);
        } else {
          Object.assign(idea, update);
          if (update.pre_market_gap_favourable) writePmfConfirmationStamp(idea, pmfStamp);
        }
        if (!lateMode && update.pre_market_gap_favourable && !wasPmf) newlyStampedPmfs.push(idea);
        if (lateMode && update.pre_market_gap_favourable_late) newlyStampedPmfLate.push(idea);
      }
    }

    let configDrift = { changed: false };
    if (!dryRun && !lateMode) {
      try {
        configDrift = pmfAutoExecutor.detectConfigDrift(readEnvValue, {
          pmfThresholdAtrMultiple: thresholdAtrMultiple,
          gapCheckCohort: 'PMF',
        });
      } catch (e) {
        configDrift = { changed: false, error: e.message };
      }
    }

    let autoExec = { ok: true, enabled: false, placed: 0, skipped: 0, results: [] };
    if (!dryRun && newlyStampedPmfs.length) {
      try {
        autoExec = await pmfAutoExecutor.maybeExecuteConfirmedPmfs({
          ideas: newlyStampedPmfs,
          allIdeas: db.data.ideas,
          readEnvValue,
          now,
        });
      } catch (e) {
        autoExec = { ok: false, error: e.message, placed: 0, results: [] };
      }
    }

    const alertEvents = [];
    if (!dryRun && configDrift.changed) {
      const fields = (configDrift.fields_changed || []).map(row => row.field).slice(0, 12).join(', ') || 'unknown fields';
      const text = `CONFIG CHANGED: ${configDrift.previous_hash} -> ${configDrift.current_hash} | ${fields}`;
      const sent = await safeSendTelegramAlert(text, { event: 'config_changed', old_hash: configDrift.previous_hash, new_hash: configDrift.current_hash });
      alertEvents.push({ type: 'config_changed', ...sent });
    }
    if (!dryRun && !lateMode && newlyStampedPmfs.length) {
      const msg = formatPmfConfirmedMessage(newlyStampedPmfs);
      const queued = queuePmfTelegramAlert(msg, { event: 'pmf_confirmed', count: newlyStampedPmfs.length, tickers: newlyStampedPmfs.map(i => i.ticker || i.symbol) }, 'pmfConfirmed');
      alertEvents.push({ type: 'pmf_confirmed', ...queued });
    }
    if (!dryRun && Array.isArray(autoExec?.results)) {
      for (const result of autoExec.results) {
        const idea = db.data.ideas.find(i => String(i.ticker || i.symbol || '').toUpperCase() === String(result.ticker || '').toUpperCase());
        const msg = formatAutoExecMessage(result, idea);
        if (!msg) continue;
        const category = result.action === 'placed' ? 'autoFilled' : 'failures';
        const queued = queuePmfTelegramAlert(msg, { event: result.action === 'placed' ? 'auto_exec_filled' : 'auto_exec_failed', ticker: result.ticker, action: result.action }, category);
        alertEvents.push({ type: result.action === 'placed' ? 'auto_exec_filled' : 'auto_exec_failed', ticker: result.ticker, ...queued });
      }
      if (autoExec.ok === false && autoExec.error) {
        const msg = `AUTO-EXEC FAILED: batch - ${autoExec.error}`;
        const queued = queuePmfTelegramAlert(msg, { event: 'auto_exec_failed', ticker: 'batch', error: autoExec.error }, 'failures');
        alertEvents.push({ type: 'auto_exec_failed', ticker: 'batch', ...queued });
      }
    }

    if (!dryRun) await db.write();
    const sizeRuleAffected = checked.filter(x => x.size_rule === 'NORMAL_nonPMF_halfsize');
    return {
      ok: true,
      dryRun,
      checked: openIdeas.length,
      updated: dryRun ? 0 : checked.length,
      cohort: lateMode ? 'PMF_LATE' : 'PMF',
      fmpCallEstimate: Math.max(1, Math.ceil(openIdeas.length / 50)),
      adverse: checked.filter(x => x.pre_market_gap_adverse).length,
      favourable: checked.filter(x => x.pre_market_gap_favourable).length,
      favourable_late: checked.filter(x => x.pre_market_gap_favourable_late).length,
      neutral: checked.filter(x => !x.pre_market_gap_adverse && !x.pre_market_gap_favourable && !x.pre_market_gap_error).length,
      size_rule_affected: sizeRuleAffected.length,
      size_rule_affected_examples: sizeRuleAffected.slice(0, 12).map(x => x.ticker),
      size_rule_pmf_affected: sizeRuleAffected.filter(x => isPreMarketFavourable(x)).length,
      size_rule_caution_nonpmf_affected: sizeRuleAffected.filter(x => ideaRegime(x) === 'CAUTION' && !isPreMarketFavourable(x)).length,
      errors: errors.slice(0, 50),
      alerts,
      alert_events: alertEvents,
      auto_exec: autoExec,
      config_drift: configDrift,
      newly_stamped_pmf_count: newlyStampedPmfs.length,
      newly_stamped_pmf_late_count: newlyStampedPmfLate.length,
      newly_stamped_pmf_late_examples: newlyStampedPmfLate.slice(0, 12).map(x => x.ticker),
      results: checked,
    };
  }

  async function runPaperMonitor() {
    ensure();
    const at = now();
    const openIdeas = db.data.ideas
      .map(withDerivedStatus)
      .filter(i => i.paper !== false && i.paper_status === 'OPEN' && effectiveEntry(i) && effectiveRiskPerShare(i));
    const prices = await quoteMap(openIdeas.map(i => i.ticker));
    const missingPrices = [];
    const snapshots = [];
    for (const idea of db.data.ideas) {
      const derived = withDerivedStatus(idea);
      if (isInvalidTicker(idea.ticker)) continue;
      if (!(derived.paper !== false && derived.paper_status === 'OPEN' && effectiveEntry(derived) && effectiveRiskPerShare(derived))) continue;
      const px = prices[String(idea.ticker).toUpperCase()];
      if (!px) {
        missingPrices.push(idea.ticker);
        continue;
      }
      freezeOriginalRisk(idea);
      const entry = effectiveEntry(idea);
      const unrealizedRRaw = rValue(entry, effectiveRiskPerShare(idea), px);
      const unrealizedR = riskAdjustedStoredR(idea, unrealizedRRaw);
      const gainPct = entry ? Number((((px - entry) / entry) * 100).toFixed(2)) : null;
      idea.current_price = px;
      idea.unrealized_r_raw = unrealizedRRaw;
      idea.unrealized_r = unrealizedR;
      idea.current_gain_pct = gainPct;
      idea.distance_to_target_pct = effectiveTarget(idea) ? Number((((effectiveTarget(idea) - px) / px) * 100).toFixed(2)) : null;
      idea.distance_to_stop_pct = effectiveStop(idea) ? Number((((px - effectiveStop(idea)) / px) * 100).toFixed(2)) : null;
      idea.monitor_peak_gain_pct = idea.monitor_peak_gain_pct == null ? gainPct : Math.max(idea.monitor_peak_gain_pct, gainPct);
      idea.monitor_worst_dd_pct = idea.monitor_worst_dd_pct == null ? gainPct : Math.min(idea.monitor_worst_dd_pct, gainPct);
      idea.monitor_updated_at = at;
      markSuspectIfNeeded(idea);
      if (idea.r_calculation_suspect) {
        idea.paper_status = 'INVALID';
        idea.paper_outcome = 'INVALID';
        continue;
      }
      let wouldExit = null;
      const target = effectiveTarget(idea);
      const stop = effectiveStop(idea);
      if (target != null && px >= target) wouldExit = 'TARGET';
      if (stop != null && px <= stop) wouldExit = 'STOP';
      const brokerManagedOpen = numericOrNull(idea.actual_entry_price) != null && numericOrNull(idea.actual_exit_price) == null && (idea.fill_source === 'alpaca_paper' || idea.alpaca_order_id);
      if (wouldExit && !idea.paper_exit_reason && !brokerManagedOpen) {
        idea.paper_exit_reason = wouldExit;
        idea.paper_exit_at = at;
        idea.paper_exit_price = px;
        idea.paper_exit_r_raw = unrealizedRRaw;
        idea.paper_exit_r = unrealizedR;
        if (positionRMultiplier(idea) !== 1) idea.r_values_risk_adjusted = true;
        markSuspectIfNeeded(idea);
        if (idea.r_calculation_suspect) {
          idea.paper_status = 'INVALID';
          idea.paper_outcome = 'INVALID';
        } else {
          idea.paper_status = 'CLOSED';
          idea.paper_outcome = wouldExit === 'TARGET' ? 'WIN' : 'LOSS';
        }
      }
      snapshots.push({
        id: `${at}_${idea.id}`,
        idea_id: idea.id,
        date: idea.date,
        ticker: idea.ticker,
        checked_at: at,
        current_price: px,
        unrealized_r: unrealizedR,
        current_gain_pct: gainPct,
        distance_to_target_pct: idea.distance_to_target_pct,
        distance_to_stop_pct: idea.distance_to_stop_pct,
        peak_gain_pct: idea.monitor_peak_gain_pct,
        worst_dd_pct: idea.monitor_worst_dd_pct,
        would_exit: wouldExit,
        paper_only: true,
      });
    }
    db.data.system2_monitor_snapshots.push(...snapshots);
    if (db.data.system2_monitor_snapshots.length > 5000) {
      db.data.system2_monitor_snapshots = db.data.system2_monitor_snapshots.slice(-5000);
    }
    await db.write();
    return { ok: true, checked: openIdeas.length, updated: snapshots.length, missingPrices: missingPrices.slice(0, 50), snapshots };
  }

  function dateRangeFromQuery(req) {
    const preset = String(req.query.preset || '30d');
    const today = new Date();
    const iso = d => d.toISOString().slice(0, 10);
    if (req.query.from || req.query.to) {
      return {
        from: req.query.from || '0000-01-01',
        to: req.query.to || '9999-12-31',
        preset: 'custom',
      };
    }
    if (preset === 'all') return { from: '0000-01-01', to: '9999-12-31', preset };
    const days = preset === '7d' ? 7 : preset === '90d' ? 90 : 30;
    const start = new Date(today.getTime() - days * 86400000);
    return { from: iso(start), to: iso(today), preset };
  }

  function scoredValue(row, field) {
    if (['r', 'actual_r', 'paper_exit_r', 'r_3d', 'r_10d', 'r_1d', 'canonical_r'].includes(field)) {
      return canonicalResolvedR(row);
    }
    if (row[field] == null || row[field] === '') return null;
    const v = Number(row[field]);
    return Number.isFinite(v) ? v : null;
  }

  function cohortStats(rows, rfield = 'r_3d', minSample = 30) {
    const scored = rows.filter(r => scoredValue(r, rfield) != null);
    const values = scored.map(r => scoredValue(r, rfield));
    const wins = values.filter(v => v > 0).length;
    const avg = values.length ? values.reduce((a,b)=>a+b,0) / values.length : null;
    return {
      count: scored.length,
      ready: scored.length >= minSample,
      insufficient: scored.length < minSample ? `insufficient sample (${scored.length}/${minSample})` : null,
      win_rate: scored.length >= minSample ? Math.round((wins / scored.length) * 100) : null,
      avg_r: scored.length >= minSample && avg != null ? Number(avg.toFixed(3)) : null,
      raw_win_rate: scored.length ? Math.round((wins / scored.length) * 100) : null,
      raw_avg_r: avg != null ? Number(avg.toFixed(3)) : null,
    };
  }

  function groupStats(rows, keyFn, rfield = 'r_3d') {
    const groups = {};
    for (const row of rows) {
      const key = keyFn(row) || 'unknown';
      if (!groups[key]) groups[key] = [];
      groups[key].push(row);
    }
    return Object.fromEntries(Object.entries(groups).map(([key, vals]) => [key, cohortStats(vals, rfield)]));
  }

  function verdict(withStats, withoutStats) {
    if (!withStats.ready || !withoutStats.ready) {
      const n = Math.min(withStats.count, withoutStats.count);
      return { verdict: 'ACCUMULATING', badge: `still accumulating - ${n}/30`, ready: false };
    }
    const edge = Number((withStats.raw_avg_r - withoutStats.raw_avg_r).toFixed(3));
    return {
      verdict: edge >= 0.05 ? 'KEEP' : 'DROP',
      ready: true,
      edge_delta_r: edge,
      rule: 'with cohort must beat without by >= 0.05R after >=30 scored ideas',
    };
  }

  function histogram(values) {
    const bins = [
      { label: '<=-2R', min: -Infinity, max: -2, count: 0 },
      { label: '-2 to -1R', min: -2, max: -1, count: 0 },
      { label: '-1 to 0R', min: -1, max: 0, count: 0 },
      { label: '0 to +1R', min: 0, max: 1, count: 0 },
      { label: '+1 to +2R', min: 1, max: 2, count: 0 },
      { label: '>=+2R', min: 2, max: Infinity, count: 0 },
    ];
    for (const v of values.filter(v => v != null && Number.isFinite(v))) {
      const bin = bins.find(b => v >= b.min && v < b.max);
      if (bin) bin.count++;
    }
    return bins.map(({ label, count }) => ({ label, count }));
  }

  function statisticsPayload(req) {
    ensure();
    const range = dateRangeFromQuery(req);
    const rows = db.data.ideas.map(withDerivedStatus).filter(r => r.paper_status !== 'INVALID' && r.date >= range.from && r.date <= range.to);
    const scored3 = rows.filter(r => canonicalResolvedR(r) != null);
    const resolved = scored3;
    const active = rows.filter(r => r.paper_status === 'OPEN');
    const headlineStats = cohortStats(rows, 'r_3d');
    const scoredValues = scored3.map(r => canonicalResolvedR(r));
    const best = scored3.slice().sort((a,b)=>canonicalResolvedR(b)-canonicalResolvedR(a))[0] || null;
    const worst = scored3.slice().sort((a,b)=>canonicalResolvedR(a)-canonicalResolvedR(b))[0] || null;
    const sources = ['scanner', 'catalyst', 'X', 'options_flag', 'vanta'];
    const bySource = Object.fromEntries(sources.map(src => [
      src,
      cohortStats(rows.filter(r => (r.source || 'scanner') === src), 'r_3d')
    ]));
    const catalystRows = rows.filter(r => (r.source || '') === 'catalyst');
    const catalystBySubType = groupStats(catalystRows, r => r.sub_type || r.catalyst_type || 'unknown', 'r_3d');
    const optionsGroups = {
      CONFIRM: cohortStats(rows.filter(r => r.options_verdict === 'CONFIRM'), 'r_3d'),
      NEUTRAL: cohortStats(rows.filter(r => r.options_verdict === 'NEUTRAL'), 'r_3d'),
      CAUTION: cohortStats(rows.filter(r => r.options_verdict === 'CAUTION'), 'r_3d'),
      NO_DATA: cohortStats(rows.filter(r => r.options_verdict === 'NO_DATA'), 'r_3d'),
    };
    const optionsWith = rows.filter(r => r.options_verdict === 'CONFIRM');
    const optionsWithout = rows.filter(r => r.options_verdict !== 'CONFIRM');
    const tight = rows.filter(r => r.chronos_band_pct != null && Number(r.chronos_band_pct) <= 3);
    const wide = rows.filter(r => r.chronos_band_pct != null && Number(r.chronos_band_pct) > 3);
    const council3 = rows.filter(r => Number(r.council_votes) >= 3);
    const council2 = rows.filter(r => Number(r.council_votes) === 2);
    const councilSingle = rows.filter(r => Number(r.council_votes) === 1);
    const preMarketAdverse = rows.filter(r => r.pre_market_gap_adverse === true);
    const preMarketFavourable = rows.filter(isPrimaryPmf);
    const preMarketLate = rows.filter(isPmfLate);
    const preMarketNeutral = rows.filter(r => r.pre_market_checked_at && !r.pre_market_gap_adverse && !isPreMarketFavourable(r) && !isPmfLate(r) && !r.pre_market_gap_error);
    const preMarketUnchecked = rows.filter(r => !r.pre_market_checked_at);
    const highConfluence = rows.filter(r => scoredValue(r, 'confluence_score') > 90);
    const mediumConfluence = rows.filter(r => {
      const score = scoredValue(r, 'confluence_score');
      return score != null && score >= 75 && score <= 90;
    });
    const lowConfluence = rows.filter(r => {
      const score = scoredValue(r, 'confluence_score');
      return score != null && score < 75;
    });
    return {
      ok: true,
      range,
      min_sample: 30,
      headline: {
        total_ideas: rows.length,
        resolved_count: resolved.length,
        percent_resolved: rows.length ? Math.round((resolved.length / rows.length) * 100) : 0,
        current_open_count: active.length,
        scored_3d_count: scored3.length,
        win_rate: headlineStats.win_rate,
        avg_r: headlineStats.avg_r,
        expectancy_r: headlineStats.avg_r,
        insufficient: headlineStats.insufficient,
        best_trade: best ? { date: best.date, ticker: best.ticker, r_3d: canonicalResolvedR(best), canonical_r: canonicalResolvedR(best), hit: best.hit || best.paper_exit_reason } : null,
        worst_trade: worst ? { date: worst.date, ticker: worst.ticker, r_3d: canonicalResolvedR(worst), canonical_r: canonicalResolvedR(worst), hit: worst.hit || worst.paper_exit_reason } : null,
      },
      by_source: bySource,
      catalyst_sub_type: catalystBySubType,
      by_confluence: {
        high: cohortStats(highConfluence, 'r_3d'),
        medium: cohortStats(mediumConfluence, 'r_3d'),
        low: cohortStats(lowConfluence, 'r_3d'),
      },
      layer_verdicts: {
        options: {
          groups: optionsGroups,
          decision: verdict(cohortStats(optionsWith, 'r_3d'), cohortStats(optionsWithout, 'r_3d')),
        },
        chronos: {
          tight_band: cohortStats(tight, 'r_3d'),
          wide_band: cohortStats(wide, 'r_3d'),
          decision: verdict(cohortStats(tight, 'r_3d'), cohortStats(wide, 'r_3d')),
        },
        council: {
          three_of_three: cohortStats(council3, 'r_3d'),
          two_of_three: cohortStats(council2, 'r_3d'),
          single_model: cohortStats(councilSingle, 'r_3d'),
          decision: verdict(cohortStats(council3, 'r_3d'), cohortStats([...council2, ...councilSingle], 'r_3d')),
        },
        pre_market_gap: {
          adverse: cohortStats(preMarketAdverse, 'r_3d'),
          favourable: cohortStats(preMarketFavourable, 'r_3d'),
          pmf_late: cohortStats(preMarketLate, 'r_3d'),
          pmf_late_true_r: statsFromTrueR(preMarketLate),
          neutral: cohortStats(preMarketNeutral, 'r_3d'),
          unchecked: cohortStats(preMarketUnchecked, 'r_3d'),
          decision: verdict(cohortStats(preMarketFavourable, 'r_3d'), cohortStats([...preMarketAdverse, ...preMarketNeutral], 'r_3d')),
        },
      },
      by_horizon: {
        '1d': cohortStats(rows, 'r_1d'),
        '3d': cohortStats(rows, 'r_3d'),
        '10d': cohortStats(rows, 'r_10d'),
      },
      by_sector: groupStats(rows, r => r.sector || 'unknown', 'r_3d'),
      by_regime: groupStats(rows, r => r.market_regime || r.regime || 'untagged', 'r_3d'),
      distribution: histogram(scoredValues),
      notes: [
        'Cells hide win rate and avg R until >=30 scored ideas.',
        'Regime is reported as untagged until ideas include market_regime/regime.',
        'This endpoint is measurement-only and does not alter funnel selection.',
      ],
    };
  }

  // ── 1. LOG AN IDEA  (called by the nightly n8n funnel, per finalist) ──
  // Body: { date, ticker, mode, entry, stop, target, council_votes,
  //         council_conf, chronos_dir, chronos_conf, chronos_band_pct,
  //         sector, setup, paper:true }
  app.post('/api/idea', serviceOrAdmin, async (req, res) => {
    try {
      ensure();
      const b = req.body || {};
      if (!b.ticker || !b.entry) return res.status(400).json({ error: 'ticker and entry required' });
      const ideaDate = b.date || now().slice(0, 10);
      const ideaTicker = String(b.ticker).toUpperCase();
      const ideaSource = b.source || null;
      const ideaPaper = b.paper !== false;
      const existing = db.data.ideas.find(i =>
        i.date === ideaDate &&
        i.ticker === ideaTicker &&
        (i.source || null) === ideaSource &&
        i.paper === ideaPaper
      );
      if (existing) {
        const refreshFields = [
          'chronos_dir', 'chronos_status', 'chronos2_1d', 'chronos2_3d', 'chronos2_5d',
          'forecastDecision', 'forecastTier', 'options_verdict', 'options_notes',
          'news_safety_status', 'hard_landmine', 'analyst_change',
          'regime', 'market_regime', 'regime_reason',
          'council_tier', 'council_size_mult', 'council_reasons',
          'confluence_bonuses',
          'family_scores', 'confirmation_score', 'confirmation_breakdown',
          'trade_quality_score', 'trade_quality_label', 'trade_quality_finalist',
          'core_setup_score', 'core_setup_breakdown',
          'risk_score', 'risk_breakdown',
          'data_quality_score', 'data_quality_label', 'data_quality_checks',
          'market_regime_detected', 'regime_caps_applied', 'regime_weights_applied',
          'bear_case_points', 'families_firing',
          'era',
          'rvol_tier',
          'dark_pool_signal',
          'insider_buy_signal',
          'insider_buy_value',
          'insider_buy_count',
          'short_squeeze_score',
          'short_percent_float',
          'tape_signal',
          'gex_regime',
          'danelfin_ai_score',
          'danelfin_data_available',
          'danelfin_technical',
          'danelfin_fundamental',
          'danelfin_sentiment',
          'intake_source_layer', 'source_layer', 'universe_source',
        ];
        for (const field of refreshFields) {
          if (b[field] !== undefined) existing[field] = b[field];
        }
        if (b.chronos_conf != null) existing.chronos_conf = Number(b.chronos_conf);
        if (b.chronos_band_pct != null) existing.chronos_band_pct = Number(b.chronos_band_pct);
        if (b.setup_score != null) existing.setup_score = Number(b.setup_score);
        if (b.confluence_score != null) existing.confluence_score = Number(b.confluence_score);
        if (b.forecastConviction != null) existing.forecastConviction = Number(b.forecastConviction);
        if (Array.isArray(b.forecastReasons)) existing.forecastReasons = b.forecastReasons;
        if (b.options_signals_count != null) existing.options_signals_count = Number(b.options_signals_count);
        if (b.news_recent_items_checked != null) existing.news_recent_items_checked = Number(b.news_recent_items_checked);
        if (b.iv_rank != null) existing.iv_rank = Number(b.iv_rank);
        if (b.vol_oi_ratio != null) existing.vol_oi_ratio = Number(b.vol_oi_ratio);
        if (b.put_call_vol_ratio != null) existing.put_call_vol_ratio = Number(b.put_call_vol_ratio);
        if (b.call_oi_skew != null) existing.call_oi_skew = Number(b.call_oi_skew);
        if (b.atr14 != null) existing.atr14 = Number(b.atr14);
        if (b.atrPct != null) existing.atrPct = Number(b.atrPct);
        if (b.spy_1d_pct != null) existing.spy_1d_pct = Number(b.spy_1d_pct);
        if (b.qqq_1d_pct != null) existing.qqq_1d_pct = Number(b.qqq_1d_pct);
        if (b.vix_current != null) existing.vix_current = Number(b.vix_current);
        if (b.vix_1d_chg != null) existing.vix_1d_chg = Number(b.vix_1d_chg);
        if (b.council_votes != null) existing.council_votes = Number(b.council_votes);
        if (b.council_conf != null) existing.council_conf = Number(b.council_conf);
        if (b.council_tier !== undefined) existing.council_tier = b.council_tier;
        if (b.council_size_mult != null) existing.council_size_mult = Number(b.council_size_mult);
        if (b.confirmation_score != null) existing.confirmation_score = Number(b.confirmation_score);
        if (b.trade_quality_score != null) existing.trade_quality_score = Number(b.trade_quality_score);
        if (b.core_setup_score != null) existing.core_setup_score = Number(b.core_setup_score);
        if (b.risk_score != null) existing.risk_score = Number(b.risk_score);
        if (b.data_quality_score != null) existing.data_quality_score = Number(b.data_quality_score);
        if (b.families_firing != null) existing.families_firing = Number(b.families_firing);
        if (Array.isArray(b.council_upgrade_sigs)) existing.council_upgrade_sigs = b.council_upgrade_sigs;
        if (Array.isArray(b.council_red_flags)) existing.council_red_flags = b.council_red_flags;
        for (const field of ['council_claude', 'council_gpt', 'council_gemini']) {
          if (b[field] !== undefined) existing[field] = b[field];
        }
        for (const field of ['council_claude_conf', 'council_gpt_conf', 'council_gemini_conf']) {
          if (b[field] != null) existing[field] = Number(b[field]);
        }
        if (b.council_force_skip != null) existing.council_force_skip = b.council_force_skip === true;
        if (b.rvol_tier !== undefined) existing.rvol_tier = b.rvol_tier;
        if (b.dark_pool_signal !== undefined) existing.dark_pool_signal = b.dark_pool_signal;
        if (b.insider_buy_signal !== undefined) existing.insider_buy_signal = b.insider_buy_signal;
        if (b.insider_buy_value !== undefined) existing.insider_buy_value = b.insider_buy_value;
        if (b.insider_buy_count !== undefined) existing.insider_buy_count = Number(b.insider_buy_count);
        if (b.short_squeeze_score !== undefined) existing.short_squeeze_score = Number(b.short_squeeze_score) || 0;
        if (b.short_percent_float !== undefined) existing.short_percent_float = Number(b.short_percent_float) || 0;
        if (b.tape_signal !== undefined) existing.tape_signal = b.tape_signal;
        if (b.gex_regime !== undefined) existing.gex_regime = b.gex_regime;
        if (b.danelfin_ai_score !== undefined) existing.danelfin_ai_score = b.danelfin_ai_score === null ? null : Number(b.danelfin_ai_score);
        if (b.danelfin_data_available !== undefined) existing.danelfin_data_available = Boolean(b.danelfin_data_available);
        if (b.danelfin_technical !== undefined) existing.danelfin_technical = Number(b.danelfin_technical) || null;
        if (b.danelfin_fundamental !== undefined) existing.danelfin_fundamental = Number(b.danelfin_fundamental) || null;
        if (b.danelfin_sentiment !== undefined) existing.danelfin_sentiment = Number(b.danelfin_sentiment) || null;
        if (b.era !== undefined) existing.era = b.era;
        existing.enrichment_refreshed_at = now();
        await db.write();
        return res.json({ ok: true, duplicate: true, refreshed: true, id: existing.id });
      }
      const idea = {
        id: `${ideaDate}_${ideaTicker}_${Date.now()}`,
        logged_at: now(),
        date: ideaDate,
        ticker: ideaTicker,
        mode: b.mode || 'SWING',            // SWING | DAY
        paper: ideaPaper,                   // default paper TRUE
        entry: Number(b.entry),
        stop: b.stop != null ? Number(b.stop) : null,
        target: b.target != null ? Number(b.target) : null,
        risk_per_share: (b.entry != null && b.stop != null) ? Number(b.entry) - Number(b.stop) : null,
        source: b.source || null,
        intake_source_layer: b.intake_source_layer || b.source_layer || b.universe_source || null,
        source_layer: b.source_layer || b.intake_source_layer || b.universe_source || null,
        universe_source: b.universe_source || null,
        grade: b.grade || null,
        sector: b.sector || null,
        setup: b.setup || null,
        sub_type: b.sub_type || b.catalyst_type || null,
        sub_types: Array.isArray(b.sub_types) ? b.sub_types : null,
        catalyst_summary: b.catalyst_summary || null,
        catalyst_date: b.catalyst_date || null,
        catalyst_datetime: b.catalyst_datetime || null,
        catalyst_score: b.catalyst_score != null ? Number(b.catalyst_score) : null,
        catalyst_sources: Array.isArray(b.catalyst_sources) ? b.catalyst_sources : null,
        market_regime: b.market_regime || b.regime || null,
        regime: b.regime || b.market_regime || null,
        regime_reason: b.regime_reason || null,
        spy_1d_pct: b.spy_1d_pct != null ? Number(b.spy_1d_pct) : null,
        qqq_1d_pct: b.qqq_1d_pct != null ? Number(b.qqq_1d_pct) : null,
        vix_current: b.vix_current != null ? Number(b.vix_current) : null,
        vix_1d_chg: b.vix_1d_chg != null ? Number(b.vix_1d_chg) : null,
        setup_score: b.setup_score != null ? Number(b.setup_score) : (b.convictionScore != null ? Number(b.convictionScore) : null),
        confluence_score: b.confluence_score != null ? Number(b.confluence_score) : null,
        confluence_bonuses: Array.isArray(b.confluence_bonuses) ? b.confluence_bonuses : [],
        atr14: b.atr14 != null ? Number(b.atr14) : (b.atr != null ? Number(b.atr) : null),
        atrPct: b.atrPct != null ? Number(b.atrPct) : null,
        // ── attribution: who said what, so we can score them later ──
        council_votes: b.council_votes != null ? Number(b.council_votes) : null, // 0-3
        council_conf:  b.council_conf  != null ? Number(b.council_conf)  : null, // 0-100
        council_tier: b.council_tier || null,
        council_size_mult: b.council_size_mult != null ? Number(b.council_size_mult) : null,
        council_upgrade_sigs: Array.isArray(b.council_upgrade_sigs) ? b.council_upgrade_sigs : null,
        council_red_flags: Array.isArray(b.council_red_flags) ? b.council_red_flags : null,
        council_claude: b.council_claude || null,
        council_gpt: b.council_gpt || null,
        council_gemini: b.council_gemini || null,
        council_claude_conf: b.council_claude_conf != null ? Number(b.council_claude_conf) : null,
        council_gpt_conf: b.council_gpt_conf != null ? Number(b.council_gpt_conf) : null,
        council_gemini_conf: b.council_gemini_conf != null ? Number(b.council_gemini_conf) : null,
        council_reasons: b.council_reasons || null,
        council_force_skip: b.council_force_skip === true,
        chronos_dir:   b.chronos_dir   || null,        // UP | DOWN | FLAT
        chronos_conf:  b.chronos_conf  != null ? Number(b.chronos_conf) : null,  // 0-100
        chronos_band_pct: b.chronos_band_pct != null ? Number(b.chronos_band_pct) : null, // quantile spread width %
        chronos_status: b.chronos_status || null,
        chronos2_1d: b.chronos2_1d || null,
        chronos2_3d: b.chronos2_3d || null,
        chronos2_5d: b.chronos2_5d || null,
        forecastConviction: b.forecastConviction != null ? Number(b.forecastConviction) : null,
        forecastDecision: b.forecastDecision || null,
        forecastTier: b.forecastTier || null,
        forecastReasons: Array.isArray(b.forecastReasons) ? b.forecastReasons : null,
        options_verdict: b.options_verdict || null, // CONFIRM | NEUTRAL | CAUTION | NO_DATA
        options_signals_count: b.options_signals_count != null
          ? Number(b.options_signals_count)
          : (b.signals_count != null ? Number(b.signals_count) : null),
        iv_rank: b.iv_rank != null
          ? Number(b.iv_rank)
          : (b.iv_rank_proxy != null ? Number(b.iv_rank_proxy) : null),
        vol_oi_ratio: b.vol_oi_ratio != null
          ? Number(b.vol_oi_ratio)
          : (b.call_vol_oi_ratio != null ? Number(b.call_vol_oi_ratio) : null),
        put_call_vol_ratio: b.put_call_vol_ratio != null ? Number(b.put_call_vol_ratio) : null,
        call_oi_skew: b.call_oi_skew != null ? Number(b.call_oi_skew) : null,
        options_notes: b.options_notes || b.notes || null,
        analyst_change: b.analyst_change || null,
        news_safety_status: b.news_safety_status || null,
        news_recent_items_checked: b.news_recent_items_checked != null ? Number(b.news_recent_items_checked) : null,
        hard_landmine: b.hard_landmine || null,
        family_scores: b.family_scores || null,
        confirmation_score: b.confirmation_score != null ? Number(b.confirmation_score) : null,
        confirmation_breakdown: b.confirmation_breakdown || null,
        trade_quality_score: b.trade_quality_score != null ? Number(b.trade_quality_score) : null,
        trade_quality_label: b.trade_quality_label || null,
        trade_quality_finalist: b.trade_quality_finalist === true,
        core_setup_score: b.core_setup_score != null ? Number(b.core_setup_score) : null,
        core_setup_breakdown: b.core_setup_breakdown || null,
        risk_score: b.risk_score != null ? Number(b.risk_score) : null,
        risk_breakdown: b.risk_breakdown || null,
        data_quality_score: b.data_quality_score != null ? Number(b.data_quality_score) : null,
        data_quality_label: b.data_quality_label || null,
        data_quality_checks: b.data_quality_checks || null,
        market_regime_detected: b.market_regime_detected || null,
        regime_caps_applied: b.regime_caps_applied || null,
        regime_weights_applied: b.regime_weights_applied || null,
        bear_case_points: Array.isArray(b.bear_case_points) ? b.bear_case_points : null,
        era: b.era || 'system2_v2',
        rvol_tier: b.rvol_tier || null,
        dark_pool_signal: b.dark_pool_signal || null,
        insider_buy_signal: b.insider_buy_signal || null,
        insider_buy_value: b.insider_buy_value || null,
        insider_buy_count: b.insider_buy_count != null ? Number(b.insider_buy_count) : null,
        short_squeeze_score: b.short_squeeze_score != null ? Number(b.short_squeeze_score) : null,
        short_percent_float: b.short_percent_float != null ? Number(b.short_percent_float) : null,
        tape_signal: b.tape_signal || null,
        gex_regime: b.gex_regime || null,
        danelfin_ai_score: b.danelfin_ai_score != null ? Number(b.danelfin_ai_score) : null,
        danelfin_data_available: Boolean(b.danelfin_data_available),
        danelfin_technical: b.danelfin_technical != null ? Number(b.danelfin_technical) : null,
        danelfin_fundamental: b.danelfin_fundamental != null ? Number(b.danelfin_fundamental) : null,
        danelfin_sentiment: b.danelfin_sentiment != null ? Number(b.danelfin_sentiment) : null,
        families_firing: b.families_firing != null ? Number(b.families_firing) : null,
        // ── outcomes, filled in later by the scorer ──
        px_1d: null, px_3d: null, px_10d: null,
        r_1d: null,  r_3d: null,  r_10d: null,
        max_gain_pct: null, max_dd_pct: null,
        hit: null,                          // TARGET | STOP | OPEN | TIME
        paper_status: 'OPEN',
        paper_outcome: null,
        paper_exit_reason: null,
        paper_exit_at: null,
        paper_exit_price: null,
        paper_exit_r: null,
        current_price: null,
        unrealized_r: null,
        current_gain_pct: null,
        distance_to_target_pct: null,
        distance_to_stop_pct: null,
        monitor_peak_gain_pct: null,
        monitor_worst_dd_pct: null,
        monitor_updated_at: null,
        pre_market_checked_at: null,
        pre_market_price: null,
        pre_market_price_source: null,
        pre_market_gap_adverse: false,
        pre_market_gap_favourable: false,
        pre_market_gap_pct: null,
        pre_market_gap_atr_multiple: null,
        pre_market_gap_threshold_atr: 1.5,
        pre_market_gap_error: null,
        chronos_helped: null,               // true/false after scoring
        council_helped: null,
        scored_at: null,
        scored_stage: 0                     // 0=new, 1=after1d, 3=after3d, 10=done
      };
      freezeOriginalRisk(idea);
      db.data.ideas.push(idea);
      await db.write();
      res.json({ ok: true, id: idea.id });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 2. SCORE DUE IDEAS  (called by n8n on a daily schedule) ──
  // Looks up each idea whose +1/+3/+10 day mark has passed and fills outcomes.
  app.post('/api/score/run', async (req, res) => {
    try {
      ensure();
      const today = new Date();
      const out = { checked: 0, updated: 0, checkedNearMisses: 0, updatedNearMisses: 0, errors: [] };
      const marks = [
        { days: 1,  field: 'px_1d',  rfield: 'r_1d',  stage: 1 },
        { days: 3,  field: 'px_3d',  rfield: 'r_3d',  stage: 3 },
        { days: 10, field: 'px_10d', rfield: 'r_10d', stage: 10 },
      ];

      for (const idea of db.data.ideas) {
        if (isInvalidTicker(idea.ticker)) continue;
        if (idea.scored_stage >= 10) continue;
        freezeOriginalRisk(idea);
        out.checked++;
        const ideaDate = new Date(idea.date + 'T00:00:00Z');
        const ageDays = Math.floor((today - ideaDate) / 86400000);

        // pull recent daily closes once per ticker per run
        let hist = null;
        for (const m of marks) {
          if (idea.scored_stage >= m.stage) continue;     // already done this mark
          if (ageDays < m.days) continue;                 // not due yet
          if (!hist) {
            try {
              hist = await fetchDailyHistory(idea.ticker);
            } catch (e) { out.errors.push(`${idea.ticker}: ${e.message}`); hist = []; }
          }
          if (!hist.length) continue;
          out.updated += applyMarks(idea, hist, marks, today);
          break;
        }

        // ── attribution scoring once we have the 3-day result ──
        if (idea.scored_stage >= 3 && idea.r_3d != null && idea.chronos_helped == null) {
          const wentRight = idea.r_3d > 0;
          if (idea.chronos_dir) {
            const chronosSaidUp = idea.chronos_dir === 'UP';
            idea.chronos_helped = (chronosSaidUp === wentRight);
          }
          if (idea.council_votes != null) {
            // council "helped" if high agreement matched a win, or low agreement matched a loss
            const highAgree = idea.council_votes >= 2;
            idea.council_helped = (highAgree === wentRight);
          }
        }
      }
      for (const row of db.data.system2_rejections.filter(r => r.near_miss && r.price_scoring_eligible)) {
        if ((row.scored_stage || 0) >= 10) continue;
        out.checkedNearMisses++;
        row.risk_per_share = row.risk_per_share || ((row.entry != null && row.stop != null) ? Number(row.entry) - Number(row.stop) : null);
        try {
          const hist = await fetchDailyHistory(row.ticker);
          out.updatedNearMisses += applyMarks(row, hist, marks, today);
        } catch (e) {
          out.errors.push(`near-miss ${row.ticker}: ${e.message}`);
        }
      }
      await db.write();
      res.json(out);
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 3. STATS  (dashboard reads this to show if the system actually works) ──
  app.get('/api/score/stats', adminOnly, (req, res) => {
    try {
      ensure();
      const rows = db.data.ideas.map(withDerivedStatus).filter(i => i.paper_status !== 'INVALID');
      const scored = rows.filter(i => canonicalResolvedR(i) != null);
      const n = scored.length;
      const avg = (arr) => arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : 0;

      const r3 = scored.map(i => canonicalResolvedR(i));
      const wins = r3.filter(r => r > 0).length;

      // does high council agreement actually beat low?
      const hi = scored.filter(i => (i.council_votes||0) >= 3);
      const mid = scored.filter(i => (i.council_votes||0) === 2);
      // does a tight chronos band actually predict better?
      const tight = scored.filter(i => i.chronos_band_pct != null && i.chronos_band_pct <= 3);
      const wide  = scored.filter(i => i.chronos_band_pct != null && i.chronos_band_pct > 3);
      const confluenceStats = (rows) => {
        const values = rows.map(i => canonicalResolvedR(i)).filter(v => v != null);
        return {
          count: values.length,
          win_rate: values.length ? Math.round((values.filter(v => v > 0).length / values.length) * 100) : null,
          avg_r: values.length ? Number(avg(values).toFixed(3)) : null,
        };
      };

      res.json({
        total_ideas: rows.length,
        scored_3d: n,
        win_rate_3d: n ? Math.round((wins/n)*100) : null,
        avg_r_3d: n ? Number(avg(r3).toFixed(3)) : null,
        chronos_accuracy: (() => {
          const c = scored.filter(i => i.chronos_helped != null);
          return c.length ? Math.round((c.filter(i=>i.chronos_helped).length / c.length)*100) : null;
        })(),
        council_accuracy: (() => {
          const c = scored.filter(i => i.council_helped != null);
          return c.length ? Math.round((c.filter(i=>i.council_helped).length / c.length)*100) : null;
        })(),
        tier_compare: {
          council_3of3_avg_r: hi.length ? Number(avg(hi.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3)) : null,
          council_2of3_avg_r: mid.length ? Number(avg(mid.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3)) : null,
        },
        chronos_band_compare: {
          tight_band_avg_r: tight.length ? Number(avg(tight.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3)) : null,
          wide_band_avg_r:  wide.length  ? Number(avg(wide.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3))  : null,
        },
        options_compare: (() => {
          const confirmed = scored.filter(i => i.options_verdict === 'CONFIRM' || (i.options_signals_count || 0) >= 2);
          const notConfirmed = scored.filter(i => !(i.options_verdict === 'CONFIRM' || (i.options_signals_count || 0) >= 2));
          return {
            confirm_count: confirmed.length,
            confirm_avg_r: confirmed.length ? Number(avg(confirmed.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3)) : null,
            non_confirm_count: notConfirmed.length,
            non_confirm_avg_r: notConfirmed.length ? Number(avg(notConfirmed.map(i=>canonicalResolvedR(i)).filter(v=>v!=null)).toFixed(3)) : null,
          };
        })(),
        by_confluence: {
          high: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) > 90)),
          medium: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) >= 75 && Number(i.confluence_score) <= 90)),
          low: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) < 75)),
        },
        source_compare: (() => {
          const bySource = {};
          for (const row of scored) {
            const src = row.source || 'unknown';
            if (!bySource[src]) bySource[src] = [];
            const v = canonicalResolvedR(row);
            if (v != null) bySource[src].push(v);
          }
          return Object.fromEntries(Object.entries(bySource).map(([src, vals]) => [
            src,
            { count: vals.length, avg_r_3d: vals.length ? Number(avg(vals).toFixed(3)) : null }
          ]));
        })(),
        note: n < 30
          ? `Only ${n} scored ideas. Need ~30+ before these numbers mean anything.`
          : `Sample size ${n} — numbers are becoming meaningful.`
      });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 4. RAW IDEAS  (for a dashboard table) ──
  app.get('/api/ideas', adminOnly, (req, res) => {
    ensure();
    const { date, ticker, mode, status, source, from, to, outcome, limit = 200 } = req.query;
    const danger = newsDangerMap();
    const livePrices = readJson(path.join(system2Root, 'data', 'live_prices.json'), {});
    let rows = [...db.data.ideas].map(withDerivedStatus).filter(isDisplayableIdea).map(i => withTraderReadiness(i, danger, livePrices)).map(withTrueR).reverse();
    if (date)   rows = rows.filter(i => i.date === date);
    if (ticker) rows = rows.filter(i => i.ticker === String(ticker).toUpperCase());
    if (mode)   rows = rows.filter(i => i.mode === mode);
    if (status) rows = rows.filter(i => i.paper_status === String(status).toUpperCase());
    if (source) rows = rows.filter(i => (i.source || 'scanner') === source);
    if (outcome) rows = rows.filter(i => (i.paper_outcome || i.hit || 'OPEN') === String(outcome).toUpperCase());
    if (from) rows = rows.filter(i => i.date >= from);
    if (to) rows = rows.filter(i => i.date <= to);
    res.json({ ideas: rows.slice(0, parseInt(limit)), total: rows.length });
  });

  app.post('/api/system2/mark-entered', adminOnly, async (req, res) => {
    try {
      ensure();
      const id = String(req.body?.id || '');
      const entered = req.body?.entered !== false;
      const idea = db.data.ideas.find(i => String(i.id) === id);
      if (!idea) return res.status(404).json({ ok: false, error: 'idea not found' });
      idea.user_marked_entered = entered;
      idea.user_marked_entered_at = entered ? new Date().toISOString() : null;
      await db.write();
      res.json({ ok: true, id, ticker: idea.ticker, user_marked_entered: idea.user_marked_entered, user_marked_entered_at: idea.user_marked_entered_at });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.get('/api/system2/non-pmf-entry-guardrail', adminOnly, (req, res) => {
    try {
      ensure();
      const resolved = cleanResolvedRows();
      const pmfRows = resolved.filter(isPrimaryPmf);
      const nonPmfRows = resolved.filter(r => !isPmfConfirmedAtStamp(r) && !isPmfLate(r));
      const trueStats = trueRComparison(resolved);
      const entryDate = row => String(
        row.actual_entry_date ||
        row.paper_entry_date ||
        row.user_marked_entered_at ||
        row.trigger_price_at ||
        row.date ||
        ''
      ).slice(0, 10);
      const enteredRows = db.data.ideas
        .map(withDerivedStatus)
        .filter(isDisplayableIdea)
        .filter(row => !isPmfConfirmedAtStamp(row) && !isPmfLate(row))
        .filter(row => row.actual_entry_price != null || row.paper_entry_price != null || row.entryRecorded === true || row.user_marked_entered === true);
      const now = new Date();
      const todayUtc = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
      const day = todayUtc.getUTCDay();
      const mondayOffset = day === 0 ? 6 : day - 1;
      const weekStart = new Date(todayUtc);
      weekStart.setUTCDate(todayUtc.getUTCDate() - mondayOffset);
      const monthStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
      const weekStartText = weekStart.toISOString().slice(0, 10);
      const monthStartText = monthStart.toISOString().slice(0, 10);
      const enteredThisWeek = enteredRows.filter(row => entryDate(row) >= weekStartText);
      const enteredThisMonth = enteredRows.filter(row => entryDate(row) >= monthStartText);
      res.json({
        ok: true,
        source: 'cleanResolvedRows() + trueRComparison() + live fund.json entry flags',
        generated_at: new Date().toISOString(),
        resolved_sample: {
          definition: 'v2 clean resolved rows with canonical R, enriched with True R realistic-fill calculation',
          count: resolved.length,
        },
        true_r: {
          pmf: statsFromTrueR(pmfRows),
          non_pmf: statsFromTrueR(nonPmfRows),
          source_path: 'trueRComparison(cleanResolvedRows())',
          comparison: trueStats.cohorts,
        },
        entries: {
          week_start: weekStartText,
          month_start: monthStartText,
          non_pmf_this_week: enteredThisWeek.length,
          non_pmf_this_month: enteredThisMonth.length,
          latest_non_pmf_entries: enteredRows
            .slice()
            .sort((a, b) => entryDate(b).localeCompare(entryDate(a)))
            .slice(0, 10)
            .map(row => ({
              id: row.id,
              ticker: row.ticker,
              entry_date: entryDate(row),
              user_marked_entered: row.user_marked_entered === true,
              actual_entry_price: numericOrNull(row.actual_entry_price ?? row.paper_entry_price),
            })),
        },
      });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.get('/api/score/performance', adminOnly, (req, res) => {
    ensure();
    const rows = db.data.ideas.map(withDerivedStatus).filter(i => i.paper_status !== 'INVALID');
    const resolved = rows.filter(i => canonicalResolvedR(i) != null);
    const active = rows.filter(i => i.paper_status === 'OPEN');
    const rValues = resolved.map(i => canonicalResolvedR(i)).filter(v => v != null);
    const wins = rValues.filter(v => v > 0);
    const best = resolved.slice().sort((a,b)=>canonicalResolvedR(b)-canonicalResolvedR(a))[0] || null;
    const worst = resolved.slice().sort((a,b)=>canonicalResolvedR(a)-canonicalResolvedR(b))[0] || null;
    const avg = rValues.length ? rValues.reduce((a,b)=>a+b,0)/rValues.length : null;
    res.json({
      ok: true,
      totalIdeas: rows.length,
      activeCount: active.length,
      resolvedCount: resolved.length,
      percentResolved: rows.length ? Math.round((resolved.length / rows.length) * 100) : 0,
      winRate: rValues.length ? Math.round((wins.length / rValues.length) * 100) : null,
      avgR: avg == null ? null : Number(avg.toFixed(3)),
      bestTrade: best ? { date: best.date, ticker: best.ticker, r_3d: canonicalResolvedR(best), canonical_r: canonicalResolvedR(best), hit: best.hit || best.paper_exit_reason } : null,
      worstTrade: worst ? { date: worst.date, ticker: worst.ticker, r_3d: canonicalResolvedR(worst), canonical_r: canonicalResolvedR(worst), hit: worst.hit || worst.paper_exit_reason } : null,
    });
  });

  app.get('/api/score/statistics', adminOnly, (req, res) => {
    try {
      res.json(statisticsPayload(req));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/repair-r-integrity', adminOnly, async (req, res) => {
    try {
      ensure();
      let frozen = 0;
      let flagged = 0;
      for (const idea of db.data.ideas) {
        const hadOriginal = idea.original_entry != null;
        freezeOriginalRisk(idea);
        if (!hadOriginal && idea.original_entry != null) frozen++;
        delete idea.r_calculation_suspect;
        delete idea.r_calculation_suspect_reason;
        markSuspectIfNeeded(idea);
        if (idea.r_calculation_suspect) flagged++;
      }
      await db.write();
      res.json({ ok: true, frozen, flagged });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/pead-drift/upsert-local', localOnly, async (req, res) => {
    try {
      ensure();
      const incoming = Array.isArray(req.body?.rows) ? req.body.rows : [];
      if (!Array.isArray(db.data.pead_drift_paper)) db.data.pead_drift_paper = [];
      const byId = new Map(db.data.pead_drift_paper.filter(Boolean).map(row => [String(row.id), row]));
      for (const row of incoming) {
        if (!row || !row.id || row.strategy !== 'PEAD_DRIFT') continue;
        byId.set(String(row.id), { ...(byId.get(String(row.id)) || {}), ...row });
      }
      db.data.pead_drift_paper = [...byId.values()];
      await db.write();
      res.json({ ok: true, count: db.data.pead_drift_paper.length });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/session-stamps/backfill-local', localOnly, async (req, res) => {
    try {
      ensure();
      const report = { pmf_classified: 0, pmf_changed: 0, pead_classified: 0, pead_changed: 0, anomalies: [] };
      for (const row of db.data.ideas || []) {
        if (!row || !(row.pmf_confirmed_at_stamp === true || row.pmf_stamp_time)) continue;
        const timestamp = row.pmf_stamp_time;
        if (!timestamp) {
          report.anomalies.push({ ticker: row.ticker || row.symbol, reason: 'missing immutable pmf_stamp_time; mutable pre_market_checked_at fallback prohibited' });
          continue;
        }
        const fields = easternSessionFields(timestamp);
        if (!fields) continue;
        report.pmf_classified += 1;
        const changed = writeOnceSessionFields(row, timestamp);
        if (changed) {
          row.session_state_backfilled = true;
          row.session_state_backfilled_at = now();
          row.session_state_source = 'pmf_stamp_time';
          report.pmf_changed += 1;
        }
        if (fields.minutes_from_open !== 30) report.anomalies.push({ ticker: row.ticker || row.symbol, timestamp, ...fields });
      }
      for (const row of db.data.pead_drift_paper || []) {
        if (!row?.logged_at) continue;
        const fields = easternSessionFields(row.logged_at);
        if (!fields) continue;
        report.pead_classified += 1;
        const changed = writeOnceSessionFields(row, row.logged_at);
        if (changed) {
          row.session_state_backfilled = true;
          row.session_state_backfilled_at = now();
          row.session_state_source = 'logged_at';
          report.pead_changed += 1;
        }
      }
      await db.write();
      res.json({ ok: true, ...report });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/run-metadata', serviceOrAdmin, async (req, res) => {
    try {
      ensure();
      const body = req.body || {};
      if (!body.date) return res.status(400).json({ error: 'date required' });
      db.data.system2_run_metadata = db.data.system2_run_metadata.filter(r => r.date !== body.date);
      db.data.system2_run_metadata.push({
        date: body.date,
        created_at: body.created_at || now(),
        stages: body.stages || [],
        counts: body.counts || {},
        stage1_reject_counts: body.stage1_reject_counts || {},
        stage1_breakdown: body.stage1_breakdown || {},
        stage3_options_verdict_counts: body.stage3_options_verdict_counts || {},
        stage5_news_safety: body.stage5_news_safety || {},
        source_breakdown: body.source_breakdown || {},
        stage7_report: body.stage7_report || {},
        regime_checked_at: body.regime_checked_at || null,
        regime: body.regime || null,
        regime_reason: body.regime_reason || null,
        regime_aborted: body.regime_aborted === true,
        spy_1d_pct: body.spy_1d_pct != null ? Number(body.spy_1d_pct) : null,
        qqq_1d_pct: body.qqq_1d_pct != null ? Number(body.qqq_1d_pct) : null,
        vix_current: body.vix_current != null ? Number(body.vix_current) : null,
        vix_1d_chg: body.vix_1d_chg != null ? Number(body.vix_1d_chg) : null,
        near_miss_count: body.near_miss_count || 0,
        safety_filter_active: body.safety_filter_active === true,
        safety_filter_removed_count: body.safety_filter_removed_count || 0,
        selection_logic_changed: body.selection_logic_changed === true,
        paper_only: body.paper_only !== false,
      });
      const incoming = Array.isArray(body.rejections) ? body.rejections : [];
      db.data.system2_rejections = db.data.system2_rejections.filter(r => r.date !== body.date);
      db.data.system2_rejections.push(...incoming.map((r, idx) => ({
        id: `${body.date}_${r.ticker}_${r.stage_rejected}_${idx}`,
        logged_at: now(),
        scored_stage: r.scored_stage || 0,
        hit: r.hit || null,
        paper_status: r.paper_status || 'REJECTED',
        ...r,
      })));
      await db.write();
      res.json({ ok: true, date: body.date, stages: body.stages?.length || 0, rejections: incoming.length });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/run-metadata', adminOnly, (req, res) => {
    ensure();
    const rows = [...db.data.system2_run_metadata].sort((a,b)=>(b.date||'').localeCompare(a.date||''));
    const date = req.query.date || rows[0]?.date;
    const row = rows.find(r => r.date === date) || null;
    res.json({ ok: true, date, metadata: row, available_dates: rows.map(r => r.date) });
  });

  function normalizeStageKey(value) {
    const raw = String(value || '').trim().toLowerCase();
    const aliases = {
      '0': 'universe', universe: 'universe',
      '1': 'stage1', stage1: 'stage1', cheap: 'stage1',
      '2': 'stage2', stage2: 'stage2', technical: 'stage2',
      '3': 'stage3', stage3: 'stage3', options: 'stage3',
      '4': 'stage4', stage4: 'stage4', chronos: 'stage4',
      '5': 'stage5', stage5: 'stage5', news: 'stage5',
      '6': 'stage6', stage6: 'stage6', council: 'stage6',
      '7': 'stage7', stage7: 'stage7', correlation: 'stage7',
      finalists: 'finalists', final: 'finalists',
    };
    return aliases[raw] || raw;
  }

  app.post('/api/system2/stage-detail', localOnly, async (req, res) => {
    try {
      ensure();
      const body = req.body || {};
      if (!body.date) return res.status(400).json({ error: 'date required' });
      const stages = Array.isArray(body.stages) ? body.stages : [];
      db.data.system2_stage_details = db.data.system2_stage_details.filter(r => r.date !== body.date);
      db.data.system2_stage_details.push({
        date: body.date,
        created_at: body.created_at || now(),
        paper_only: body.paper_only !== false,
        selection_logic_changed: body.selection_logic_changed === true,
        stages,
      });
      await db.write();
      res.json({ ok: true, date: body.date, stages: stages.length, tickers: stages.reduce((sum, s) => sum + ((s.tickers || []).length), 0) });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/stage-detail', adminOnly, (req, res) => {
    ensure();
    const rows = [...db.data.system2_stage_details].sort((a,b)=>(b.date||'').localeCompare(a.date||''));
    const date = req.query.date || rows[0]?.date;
    const row = rows.find(r => r.date === date) || null;
    if (!row) return res.json({ ok: true, date, available_dates: rows.map(r => r.date), stages: [], stage: null });
    const runMeta = (db.data.system2_run_metadata || []).find(r => r.date === date) || null;
    const correctedStageCount = (key) => {
      const counts = runMeta?.counts || {};
      if (key === 'universe') return counts.universe;
      if (key === 'stage1') return counts.stage1;
      if (key === 'stage2') return counts.stage2;
      if (key === 'stage5') return counts.stage5 ?? counts.stage3;
      if (key === 'stage6') return counts.stage6;
      if (key === 'stage7') return counts.stage7;
      if (key === 'finalists') return counts.finalists;
      return null;
    };
    const withCorrectedCount = (stage) => {
      const key = normalizeStageKey(stage.stage_key || stage.stage);
      const count = correctedStageCount(key);
      if (count == null) return stage;
      const entered = key === 'universe' || key === 'stage5' || key === 'stage6' || key === 'finalists' ? count : stage.entered;
      const rejected = Number.isFinite(Number(entered)) ? Math.max(0, Number(entered) - Number(count)) : stage.rejected;
      return {
        ...stage,
        entered,
        kept: count,
        rejected: key === 'stage5' || key === 'stage6' || key === 'finalists' ? 0 : rejected,
        metadata: { ...(stage.metadata || {}), count_source: 'system2_run_metadata.counts' },
      };
    };
    const enrichStage = (stage) => {
      if (!stage) return stage;
      stage = withCorrectedCount(stage);
      const key = normalizeStageKey(stage.stage_key || stage.stage);
      if (key === 'stage5') {
        const safe = rootArtifact('stage3_news_safe_top40.json', []) || [];
        const rejected = rootArtifact('stage3_news_rejections.json', []) || [];
        const rows = [
          ...(Array.isArray(safe) ? safe : []).map(r => ({
            ticker: r.ticker || r.symbol,
            status: 'KEPT',
            news_checked: true,
            hard_landmine: r.hard_landmine || null,
            news_recent_items_checked: r.news_recent_items_checked ?? r.news_items_checked ?? null,
            reason: r.news_safety_status || r.news_verdict || 'news safe',
            data: r,
          })),
          ...(Array.isArray(rejected) ? rejected : []).map(r => ({
            ticker: r.ticker || r.symbol,
            status: 'REJECTED',
            news_checked: true,
            hard_landmine: r.hard_landmine || r.stage3RejectDetail || null,
            news_recent_items_checked: r.news_recent_items_checked ?? r.news_items_checked ?? null,
            reason: r.stage3RejectReason || r.news_safety_status || 'news safety reject',
            data: r,
          })),
        ].filter(r => r.ticker);
        return {
          ...stage,
          entered: stage.entered ?? rows.length,
          kept: stage.kept ?? rows.filter(r => r.status === 'KEPT').length,
          rejected: stage.rejected ?? rows.filter(r => r.status === 'REJECTED').length,
          no_data: rows.filter(r => String(r.reason || '').includes('NO_DATA')).length,
          tickers: rows,
          metadata: { ...(stage.metadata || {}), source: 'stage3_news_safe_top40 + stage3_news_rejections' },
        };
      }
      if (key === 'stage6') {
        const council = councilRowsByTicker();
        return {
          ...stage,
          tickers: (stage.tickers || []).map(r => {
            const ticker = String(r.ticker || r.symbol || '').toUpperCase();
            const c = council[ticker] || {};
            return {
              ...r,
              status: c.council_final_verdict || r.status || 'ENRICHED',
              council_votes: c.council_votes ?? r.council_votes ?? null,
              council_conf: c.council_conf ?? r.council_conf ?? null,
              claude_verdict: c.claude_verdict ?? r.claude_verdict ?? null,
              gpt_verdict: c.gpt4o_verdict ?? c.gpt_verdict ?? r.gpt_verdict ?? null,
              gemini_verdict: c.gemini_verdict ?? r.gemini_verdict ?? null,
              red_flags: c.council_red_flags || c.red_flags || r.red_flags || [],
              reason: c.council_gates_trades === false ? 'Council ride-along only - verdict wired, not gating' : (r.reason || 'Council verdict'),
              data: { ...(r.data || {}), ...c, council_final_tier: c.council_final_verdict || c.council_tier || null },
            };
          }),
          metadata: { ...(stage.metadata || {}), council_gates_trades: false, gpt_field: 'gpt4o_verdict' },
        };
      }
      if (key !== 'finalists') return stage;
      const ideasByTicker = Object.fromEntries(
        db.data.ideas
          .filter(i => i.date === date)
          .map(i => [String(i.ticker).toUpperCase(), withDerivedStatus(i)])
      );
      return {
        ...stage,
        tickers: (stage.tickers || []).map(r => {
          const latest = ideasByTicker[String(r.ticker).toUpperCase()] || {};
          return { ...r, data: { ...(r.data || {}), ...latest } };
        }),
      };
    };
    const wanted = req.query.stage ? normalizeStageKey(req.query.stage) : null;
    if (wanted) {
      const stage = (row.stages || []).find(s => normalizeStageKey(s.stage_key || s.stage) === wanted) || null;
      const enriched = enrichStage(stage);
      return res.json({ ok: true, date, available_dates: rows.map(r => r.date), stage: enriched, stages: enriched ? [enriched] : [] });
    }
    res.json({ ok: true, date, available_dates: rows.map(r => r.date), stages: (row.stages || []).map(enrichStage) });
  });

  function sendSystem2Rejections(req, res) {
    ensure();
    const { date, stage, ticker, near_miss, limit = 1000 } = req.query;
    let rows = [...db.data.system2_rejections].reverse();
    if (date) rows = rows.filter(r => r.date === date);
    if (stage) rows = rows.filter(r => r.stage_rejected === stage);
    if (ticker) rows = rows.filter(r => String(r.ticker).toUpperCase() === String(ticker).toUpperCase());
    if (near_miss != null) rows = rows.filter(r => String(Boolean(r.near_miss)) === String(near_miss));
    const idx = shadowIndex();
    res.json({ ok: true, rejections: rows.slice(0, parseInt(limit)).map(r => enrichRejectionWithShadow(r, idx)), total: rows.length });
  }

  app.get('/api/system2/rejections', adminOnly, sendSystem2Rejections);
  app.get('/api/rejections/system2', adminOnly, sendSystem2Rejections);

  app.get('/api/system2/rejected-outcomes', adminOnly, (req, res) => {
    try {
      let rows = rejectedOutcomeRows();
      if (req.query.stage) rows = rows.filter(r => normalizeRejectionStage(r.stage_rejected) === normalizeRejectionStage(req.query.stage));
      if (req.query.ticker) rows = rows.filter(r => String(r.ticker || '').toUpperCase() === String(req.query.ticker).toUpperCase());
      const summary = rejectedOutcomeSummary(rows);
      const tracked = rows.filter(r => numericOrNull(r.would_be_r) != null && r.tracking_status !== 'tracking');
      const totalCost = tracked.filter(r => Number(r.would_be_r) > 0).reduce((s, r) => s + Number(r.would_be_r), 0);
      const totalSaved = tracked.filter(r => Number(r.would_be_r) <= 0).reduce((s, r) => s + Math.abs(Number(r.would_be_r)), 0);
      res.json({
        ok: true,
        source: 'system2_shadow.db shadow_portfolio',
        total: rows.length,
        tracked: tracked.length,
        tracking: rows.filter(r => r.tracking_status === 'tracking').length,
        total_cost_r: Number(totalCost.toFixed(2)),
        total_saved_r: Number(totalSaved.toFixed(2)),
        net_r: Number((totalCost - totalSaved).toFixed(2)),
        summary,
        stage_performance: stagePerformanceFrom(rows),
        outcomes: rows.slice(0, parseInt(req.query.limit || String(rows.length))),
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/suggested-outcomes', adminOnly, (req, res) => {
    try {
      ensure();
      const rows = ideaPerformanceRows({ days: parseInt(req.query.days || '3650'), includeAll: true });
      const priced = rows.filter(r => r.current_price != null && r.suggested_entry != null && r.pct_move != null);
      const up = priced.filter(r => r.pct_move > 0).length;
      const down = priced.filter(r => r.pct_move < 0).length;
      const avg = vals => vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(2)) : null;
      const pmf = priced.filter(r => r.pre_market_favourable);
      res.json({
        ok: true,
        source: 'fund.json ideas joined to data/live_prices.json',
        total: rows.length,
        priced: priced.length,
        up,
        down,
        average_move_pct: avg(priced.map(r => r.pct_move)),
        pre_market_favourable: {
          count: pmf.length,
          average_move_pct: avg(pmf.map(r => r.pct_move)),
        },
        outcomes: rows,
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  function ideaPerformanceStatus(row, priceForMove) {
    const outcome = String(row.paper_outcome || row.hit || row.paper_exit_reason || row.exit_reason || '').toUpperCase();
    const paperStatus = String(row.paper_status || '').toUpperCase();
    const actualR = canonicalResolvedR(row);
    if (outcome === 'TARGET' || outcome === 'WIN') return 'WON';
    if (outcome === 'STOP' || outcome === 'LOSS') return 'LOST';
    if (outcome === 'TIME' || outcome === 'TIMEOUT' || row.scored_stage >= 10) return 'TIMED_OUT';
    if (paperStatus === 'CLOSED' && actualR != null) return actualR > 0 ? 'WON' : actualR < 0 ? 'LOST' : 'TIMED_OUT';
    if (paperStatus === 'OPEN') return 'OPEN';
    if (priceForMove != null) return 'WATCHING';
    return paperStatus || 'WATCHING';
  }

  function ideaPerformanceRows({ days = 7, includeAll = false } = {}) {
    const livePrices = readJson(path.join(system2Root, 'data', 'live_prices.json'), {}) || {};
    const nowMs = Date.now();
    const cutoffMs = nowMs - Math.max(1, Number(days) || 7) * 86400000;
    return db.data.ideas
      .map(withDerivedStatus)
      .filter(isDisplayableIdea)
      .filter(i => {
        if (includeAll) return true;
        const d = String(i.date || '').slice(0, 10);
        const t = d ? new Date(d + 'T00:00:00Z').getTime() : NaN;
        return Number.isFinite(t) && t >= cutoffMs;
      })
      .map(i => {
        const ticker = String(i.ticker || '').toUpperCase();
        const live = livePrices[ticker] || {};
        const dateSuggested = String(i.date || '').slice(0, 10) || null;
        const suggestedEntry = effectiveEntry(i) ?? numericOrNull(i.suggested_entry ?? i.entry_price ?? (Array.isArray(i.entryZone) ? i.entryZone[0] : null) ?? (Array.isArray(i.entry_zone) ? i.entry_zone[0] : null));
        const stop = effectiveStop(i) ?? numericOrNull(i.stopLoss ?? i.stop_loss);
        const target = effectiveTarget(i) ?? numericOrNull(i.tp1 ?? i.take_profit);
        const risk = effectiveRiskPerShare(i);
        const paperStatus = String(i.paper_status || '').toUpperCase();
        const exitPrice = numericOrNull(i.paper_exit_price ?? i.actual_exit_price ?? i.exit_price);
        const livePrice = numericOrNull(live.last_price ?? live.price ?? live.current_price);
        const ideaPrice = numericOrNull(i.current_price ?? i.last_price);
        const priceForMove = paperStatus === 'CLOSED' || exitPrice != null ? exitPrice : (livePrice ?? ideaPrice);
        const direction = String(i.original_direction || '').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG';
        const signedMove = suggestedEntry != null && priceForMove != null
          ? (direction === 'SHORT' ? suggestedEntry - priceForMove : priceForMove - suggestedEntry)
          : null;
        const pctMove = suggestedEntry != null && priceForMove != null && suggestedEntry !== 0
          ? Number((((priceForMove - suggestedEntry) / suggestedEntry) * 100).toFixed(3))
          : null;
        const canonical = canonicalResolvedR(i);
        const rValue = canonical != null ? canonical : (signedMove != null && risk > 0 ? Number((signedMove / risk).toFixed(2)) : null);
        const suggestedMs = dateSuggested ? new Date(dateSuggested + 'T00:00:00Z').getTime() : null;
        const exitDate = String(i.paper_exit_at || i.actual_exit_at || i.exit_at || '').slice(0, 10) || null;
        const exitMs = exitDate ? new Date(exitDate + 'T00:00:00Z').getTime() : null;
        const daysSince = suggestedMs ? Math.max(0, Math.floor((nowMs - suggestedMs) / 86400000)) : null;
        const daysHeld = suggestedMs ? Math.max(0, Math.floor(((exitMs || nowMs) - suggestedMs) / 86400000)) : null;
        const status = ideaPerformanceStatus(i, priceForMove);
        const priceSource = exitPrice != null
          ? 'exit'
          : livePrice != null
            ? 'live_prices.json'
            : ideaPrice != null
              ? 'idea.current_price'
              : null;
        return {
          id: i.id,
          ticker,
          date_suggested: dateSuggested,
          suggested_entry: suggestedEntry,
          stop,
          target,
          current_or_exit_price: priceForMove,
          current_price: priceForMove,
          current_price_source: priceSource,
          price_updated_at: exitPrice != null ? exitDate : (live.updated_at || (live.timestamp ? new Date(Number(live.timestamp) * 1000).toISOString() : null)),
          pct_move: pctMove,
          r_value: rValue,
          canonical_r: canonical,
          canonical_r_quarantined: i.canonical_r_quarantined === true || isCanonicalRQuarantined(i),
          canonical_r_quarantine_reasons: i.canonical_r_quarantine_reasons || canonicalQuarantineReasons(i),
          would_be_r: rValue,
          days_since: daysSince,
          days_held: daysHeld,
          status,
          paper_status: i.paper_status,
          user_marked_entered: i.user_marked_entered === true,
          user_marked_entered_at: i.user_marked_entered_at || null,
          hit: i.paper_outcome || i.hit || i.paper_exit_reason || null,
          pre_market_favourable: isPrimaryPmf(i),
          pre_market_current_favourable: isPreMarketFavourable(i),
        intake_source_layer: intakeSourceLayer(i),
          council_tier: i.council_tier || i.council_final_verdict || i.trade_readiness_tier || null,
        };
      })
      .sort((a, b) => {
        const d = String(b.date_suggested || '').localeCompare(String(a.date_suggested || ''));
        if (d) return d;
        return String(b.ticker || '').localeCompare(String(a.ticker || ''));
      });
  }

  app.get('/api/system2/idea-performance', adminOnly, (req, res) => {
    try {
      ensure();
      const days = parseInt(req.query.days || '7');
      const rows = ideaPerformanceRows({ days, includeAll: false });
      const priced = rows.filter(r => r.suggested_entry != null && r.current_or_exit_price != null && r.pct_move != null);
      const winners = rows.filter(r => r.status === 'WON').length;
      const losers = rows.filter(r => r.status === 'LOST').length;
      const open = rows.filter(r => ['OPEN', 'WATCHING'].includes(r.status)).length;
      const statusTotal = winners + losers + open;
      if (statusTotal !== rows.length) {
        console.warn('[idea-performance] count mismatch', {
          days,
          total: rows.length,
          winners,
          losers,
          open,
          unmatched: rows.length - statusTotal,
        });
      }
      const avg = vals => vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(2)) : null;
      const pmf = priced.filter(r => r.pre_market_favourable);
      res.json({
        ok: true,
        source: 'fund.json ideas joined to data/live_prices.json, using frozen original risk fields',
        days,
        total: rows.length,
        priced: priced.length,
        winners,
        losers,
        open,
        average_move_pct: avg(priced.map(r => r.pct_move)),
        pre_market_favourable: {
          count: pmf.length,
          average_move_pct: avg(pmf.map(r => r.pct_move)),
        },
        outcomes: rows,
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/monitor', adminOnly, (req, res) => {
    ensure();
      const active = db.data.ideas.map(withDerivedStatus)
      .filter(i => isDisplayableIdea(i) && i.paper_status === 'OPEN')
      .map(i => {
        const currentPrice = numericOrNull(i.current_price);
        const hasPrice = currentPrice != null && currentPrice > 0;
        return {
        id: i.id,
        date: i.date,
        ticker: i.ticker,
        source: i.source || 'scanner',
        entry: i.entry,
        stop: i.stop,
        target: i.target,
        current_price: hasPrice ? currentPrice : null,
        unrealized_r: hasPrice ? i.unrealized_r : null,
        current_gain_pct: hasPrice ? i.current_gain_pct : null,
        distance_to_target_pct: hasPrice ? i.distance_to_target_pct : null,
        distance_to_stop_pct: hasPrice ? i.distance_to_stop_pct : null,
        peak_gain_pct: i.monitor_peak_gain_pct,
        worst_dd_pct: i.monitor_worst_dd_pct,
        paper_exit_reason: i.paper_exit_reason,
        paper_exit_at: i.paper_exit_at,
        monitor_updated_at: i.monitor_updated_at,
        };
      });
    res.json({
      ok: true,
      active,
      activeCount: active.length,
      latestSnapshots: db.data.system2_monitor_snapshots.slice(-200).reverse(),
    });
  });

  function buildDecidePayload() {
    ensure();
    const NEAR_LINE_PCT = 3;
    const today = new Date().toISOString().slice(0, 10);
    const latestStage = latestStageDetailRow(today) || latestStageDetailRow();
    const runDate = String(latestStage?.date || today).slice(0, 10);
    const finalistStage = stageByKey(latestStage, 'finalists') || { tickers: [] };
    const ideas = db.data.ideas.map(withDerivedStatus).filter(isDisplayableIdea);
    const ideaByTicker = new Map(ideas.map(i => [String(i.ticker || '').toUpperCase(), i]));
    const finalists = (finalistStage.tickers || []).map(r => {
      const d = r.data || r;
      const ticker = String(d.ticker || d.symbol || r.ticker || '').toUpperCase();
      return { ...(ideaByTicker.get(ticker) || {}), ...d, ticker };
    }).filter(r => r.ticker);

    const active = ideas.filter(i => i.paper_status === 'OPEN');
    const activePmf = active.filter(isPrimaryPmf).length;
    const unchecked = active.filter(i => !i.thesis_checked_at && !i.thesis_status).length;
    const nearLine = active.flatMap(i => {
      const toStop = numericOrNull(i.distance_to_stop_pct);
      const toTarget = numericOrNull(i.distance_to_target_pct);
      const rows = [];
      if (toStop != null && toStop <= NEAR_LINE_PCT) rows.push({
        tier: 'B',
        kind: 'near_stop',
        ticker: i.ticker,
        label: 'Near stop',
        reason: `${i.ticker} is ${toStop.toFixed(2)}% from stop; review in Live Monitor.`,
        distance_pct: toStop,
        link: { section: 'trade', tab: 'monitor' },
      });
      if (toTarget != null && toTarget <= NEAR_LINE_PCT) rows.push({
        tier: 'B',
        kind: 'near_target',
        ticker: i.ticker,
        label: 'Near target',
        reason: `${i.ticker} is ${toTarget.toFixed(2)}% from target; review in Live Monitor.`,
        distance_pct: toTarget,
        link: { section: 'trade', tab: 'monitor' },
      });
      return rows;
    }).sort((a, b) => a.distance_pct - b.distance_pct);

    const resolved = cleanResolvedRows();
    const tr = statsFromR(resolved);
    const trueTr = statsFromTrueR(resolved);
    const sizeRuleMonitor = normalNonPmfHalfsizeReadout(resolved);
    const pmfRows = resolved.filter(isPrimaryPmf);
    const pmfStats = statsFromR(pmfRows);
    const pmfTrueStats = statsFromTrueR(pmfRows);
    const regimeCoverage = regimeCoverageFromRows(resolved).regime_coverage || [];
    const latestRun = latestRunSummary() || {};
    const currentRegime =
      latestRun.market_regime || latestRun.regime || latestRun.marketRegime ||
      finalists.find(f => f.market_regime || f.regime)?.market_regime ||
      finalists.find(f => f.market_regime || f.regime)?.regime ||
      null;
    const caution = regimeCoverage.find(r => String(r.regime).toUpperCase() === 'CAUTION') || null;
    const normal = regimeCoverage.find(r => String(r.regime).toUpperCase() === 'NORMAL') || null;
    const optData = dataArtifact('options_positioning.json', { ideas: [] });
    const optionActiveCount = Array.isArray(optData.ideas) ? optData.ideas.length : 0;

    const tierA = finalists
      .filter(isPrimaryPmf)
      .map(f => ({
        tier: 'A',
        kind: 'proven_edge',
        ticker: f.ticker,
        label: 'Proven edge signal',
        reason: `${f.ticker} is pre-market favourable; src: ${intakeSourceLayer(f)}; this is the system's strongest verified setup (${pmfStats.win_rate ?? '-'}% win over ${pmfStats.count || 0} resolved trades).`,
        intake_source_layer: intakeSourceLayer(f),
        link: { section: 'trade', tab: 'finalists' },
      }));

    const councilRows = councilRowsByTicker();
    let sawCouncilModelFields = false;
    const tierC = finalists.filter(f => {
      const c = councilRows[f.ticker] || f;
      const gpt = String(c.gpt4o_verdict || c.council_gpt || c.gpt_verdict || '').toUpperCase();
      const others = [c.claude_verdict || c.council_claude, c.gemini_verdict || c.council_gemini, c.kimi_verdict || c.council_kimi].map(x => String(x || '').toUpperCase());
      if (gpt || others.some(Boolean)) sawCouncilModelFields = true;
      const tierish = v => v === 'TIER1' || v === 'UPGRADE';
      return tierish(gpt) && others.some(tierish);
    }).map(f => ({
      tier: 'C',
      kind: 'council_agreement',
      ticker: f.ticker,
      label: 'Best-calibrated council agreement',
      reason: `${f.ticker} has GPT-4o TIER1/UPGRADE agreement plus at least one other model.`,
      link: { section: 'trade', tab: 'finalists' },
    }));
    const tierCNote = sawCouncilModelFields
      ? (tierC.length ? null : 'No finalists currently meet GPT-4o plus one other TIER1/UPGRADE agreement.')
      : 'Per-ticker model agreement is not available for current finalists, so Tier C is not currently buildable without additional stored fields.';

    const tierD = unchecked > 0 ? [{
      tier: 'D',
      kind: 'unchecked_thesis',
      ticker: 'OPEN POSITIONS',
      label: 'Thesis checks not run',
      reason: `${unchecked} open positions have not had a thesis integrity check run; consider running checks before the next session.`,
      link: { section: 'trade', tab: 'monitor' },
    }] : [];

    const itemsAll = [...tierA, ...nearLine, ...tierC, ...tierD];
    const actionCount = nearLine.length + tierD.length;
    const brief = [];
    brief.push(`${today}: ${active.length} active trades are on the Live Monitor, ${activePmf} are pre-market favourable, and ${actionCount} decision item${actionCount === 1 ? '' : 's'} need attention.`);
    brief.push(`Track record: ${tr.count} resolved v2-era trades, ${tr.win_rate ?? '-'}% win rate, ${tr.avg_r != null ? `${tr.avg_r.toFixed(2)}R` : '-'} average R, ${tr.total_r != null ? `${tr.total_r.toFixed(2)}R` : '-'} total.`);
    if (caution || normal || currentRegime) {
      const parts = [];
      if (currentRegime) parts.push(`Current regime appears to be ${currentRegime}.`);
      if (caution && normal) parts.push(`In this system's own data, CAUTION has outperformed NORMAL (${caution.count} trades/${caution.win_rate}% win/${caution.avg_r}R vs ${normal.count} trades/${normal.win_rate}% win/${normal.avg_r}R).`);
      brief.push(parts.join(' '));
    }
    if (optionActiveCount === 0) brief.push('Options Positioning has no active ideas right now.');
    if (unchecked > 0) brief.push(`Thesis integrity checks have not been run on ${unchecked} open position${unchecked === 1 ? '' : 's'} yet.`);
    brief.push("See below for today's ranked list.");

    return {
      ok: true,
      generated_at: new Date().toISOString(),
      date: today,
      run_date: runDate,
      near_line_threshold_pct: NEAR_LINE_PCT,
      brief,
      counts: {
        active_trades: active.length,
        active_pre_market_favourable: activePmf,
        needs_attention: actionCount,
        finalists: finalists.length,
        option_positioning_active: optionActiveCount,
        thesis_unchecked: unchecked,
        tier_a: tierA.length,
        tier_b: nearLine.length,
        tier_c: tierC.length,
        tier_d: tierD.length,
        total_items: itemsAll.length,
      },
      track_record: {
        resolved_count: tr.count,
        win_rate: tr.win_rate,
        avg_r: tr.avg_r,
        total_r: tr.total_r,
        true_win_rate: trueTr.win_rate,
        true_avg_r: trueTr.avg_r,
        true_total_r: trueTr.total_r,
        pmf_strict_count: pmfStats.count,
        pmf_true_win_rate: pmfTrueStats.win_rate,
        pmf_true_avg_r: pmfTrueStats.avg_r,
        source: 'cleanResolvedRows / track-record-readiness source',
      },
      size_rule_monitor: sizeRuleMonitor,
      regime: { current: currentRegime, caution, normal },
      notes: { tier_c: tierCNote },
      items: itemsAll.slice(0, 10),
      more: itemsAll.slice(10),
    };
  }

  app.get('/api/system2/decide', adminOnly, (req, res) => {
    try {
      res.json(buildDecidePayload());
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/monitor/run', adminOnly, async (req, res) => {
    try {
      res.json(await runPaperMonitor());
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/monitor/run-local', localOnly, async (req, res) => {
    try {
      res.json(await runPaperMonitor());
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/pre-market-gap/run', adminOnly, async (req, res) => {
    try {
      res.json(await runPreMarketGapCheck(req.body || {}));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/pre-market-gap/run-local', localOnly, async (req, res) => {
    try {
      res.json(await runPreMarketGapCheck(req.body || {}));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });


  app.post('/api/system2/pmf-auto-exec/sync-local', localOnly, async (req, res) => {
    try {
      ensure();
      const result = await pmfAutoExecutor.syncOpenAutoPositions({
        allIdeas: db.data.ideas,
        readEnvValue,
        now,
        dryRun: Boolean(req.body?.dryRun || req.body?.dry_run),
      });
      const alertEvents = [];
      if (!result.dryRun && Array.isArray(result.results)) {
        for (const row of result.results) {
          const idea = db.data.ideas.find(i => String(i.ticker || i.symbol || '').toUpperCase() === String(row.ticker || '').toUpperCase());
          const msg = formatSyncAlertMessage(row, idea);
          if (!msg) continue;
          const closed = String(row.action || '').startsWith('updated_exit');
          const external = row.external_close && row.external_close_alert_needed;
          const event = external ? 'external_close_detected' : (closed ? 'position_closed' : 'auto_exec_failure');
          const queued = queuePmfTelegramAlert(msg, { event, ticker: row.ticker, action: row.action, account: row.account }, external ? 'failures' : (closed ? 'positionClosed' : 'failures'));
          alertEvents.push({ type: event, ticker: row.ticker, ...queued });
        }
      }
      if (!result.dryRun) await db.write();
      res.json({ ...result, alert_events: alertEvents });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/pmf-auto-exec/dry-run-local', localOnly, async (req, res) => {
    try {
      ensure();
      const body = req.body || {};
      const fallback = db.data.ideas.find(i => isPreMarketFavourable(i) && effectiveEntry(i) && effectiveStop(i) && effectiveTarget(i));
      const idea = body.idea || fallback || {};
      res.json(pmfAutoExecutor.dryRunPreview(idea, readEnvValue));
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/pmf-alerts/test-local', localOnly, async (req, res) => {
    try {
      const messages = [
        { type: 'naked_position', text: '[TEST] NAKED POSITION: TEST has no exit order' },
        { type: 'external_sell', text: '[TEST] EXTERNAL_SELL: TEST position was closed outside its attached OCO' },
        { type: 'risk_breaker', text: '[TEST] RISK BREAKER TRIPPED: simulated breaker - TEST blocked' },
        { type: 'config_changed', text: '[TEST] CONFIG CHANGED: simulated-old-hash -> simulated-new-hash' },
      ];
      if (envFlag('PMF_ALERT_DAILY_SUMMARY', false) || req.body?.includeDailySummary) {
        messages.push({ type: 'daily_summary', text: `[TEST] ${buildDailyPmfSummary()}` });
      }
      const results = [];
      for (const msg of messages) {
        const sent = await safeSendTelegramAlert(msg.text, { event: `test_${msg.type}`, test: true });
        results.push({ ...msg, ...sent });
      }
      res.json({ ok: true, enabled: pmfAlertConfig().enabled, results });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/pmf-alerts/daily-summary-local', localOnly, async (req, res) => {
    try {
      const text = buildDailyPmfSummary();
      const queued = queuePmfTelegramAlert(text, { event: 'daily_summary' }, 'dailySummary');
      res.json({ ok: true, text, ...queued });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.post('/api/system2/pmf-alerts/failure-sim-local', localOnly, async (req, res) => {
    try {
      const sent = await safeSendTelegramAlert('[TEST] forced Telegram failure isolation check', { event: 'test_forced_failure', test: true }, { token: 'bad-token-for-failure-sim', timeoutMs: 1 });
      const fallback = db.data.ideas.find(i => isPreMarketFavourable(i) && effectiveEntry(i) && effectiveStop(i) && effectiveTarget(i));
      const executorDryRun = fallback ? pmfAutoExecutor.dryRunPreview(fallback, readEnvValue) : { ok: false, reason: 'no PMF fallback idea available' };
      res.json({ ok: true, telegram_failure_was_swallowed: sent.sent === false, telegram_result: sent, executor_dry_run_ok: executorDryRun?.ok !== false, executor_dry_run_action: executorDryRun?.action || null });
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.get('/api/system2/config', adminOnly, (req, res) => {
    res.json({ ok: true, config: readSystem2Config(), path: configPath });
  });

  app.post('/api/system2/config', adminOnly, (req, res) => {
    try {
      const next = deepMerge(readSystem2Config(), req.body || {});
      fs.mkdirSync(system2Root, { recursive: true });
      fs.writeFileSync(configPath, JSON.stringify(next, null, 2));
      res.json({ ok: true, config: next, path: configPath });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/status', adminOnly, (req, res) => {
    try {
      ensure();
      const latestRun = latestRunSummary();
      const cron = (() => {
        try { return childProcess.execSync('crontab -l', { encoding: 'utf8', timeout: 5000 }); }
        catch { return ''; }
      })();
      res.json({
        ok: true,
        mode: 'paper',
        coreDir: system2Root,
        config: readSystem2Config(),
        cron,
        latestRun,
        runMetadataDates: [...db.data.system2_run_metadata].map(r => r.date).sort().reverse(),
        rejectionCount: db.data.system2_rejections.length,
        monitorSnapshotCount: db.data.system2_monitor_snapshots.length,
        activePaperIdeas: db.data.ideas.map(withDerivedStatus).filter(i => isDisplayableIdea(i) && i.paper_status === 'OPEN').length,
        nightlyLogTail: lastLines(path.join(logDir, 'nightly.log'), 120),
        artifacts: {
          universe: countArtifact('universe.json'),
          stage1Survivors: countArtifact('stage1_survivors.json'),
          stage2Top40: countArtifact('stage2_surgical_strike_top40.json'),
          stage4ChronosTop40: countArtifact('stage4_chronos_enriched_top40.json'),
          stage3OptionsTop40: countArtifact('stage3_options_enriched_top40.json'),
          stage3NewsSafeTop40: countArtifact('stage3_news_safe_top40.json'),
          stage5CombinedForecastTop40: countArtifact('stage5_combined_forecast_top40.json'),
          stage6CouncilEnriched: countArtifact('stage6_council_enriched.json'),
          confluenceTop40: countArtifact('stage2_confluence_ranked_top40.json'),
          stage5NewsSafeFinalists: countArtifact('stage5_news_safe_finalists.json'),
          stage7Survivors: countArtifact('stage7_clustered_survivors.json'),
          stage7Report: countArtifact('stage7_cluster_report.json'),
          ideaLog: countArtifact('phase_b_baseline_idea_log_results.json'),
        },
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/artifacts/:name', adminOnly, (req, res) => {
    const allowed = new Set([
      'universe.json',
      'stage1_metadata.json',
      'stage1_details.json',
      'stage1_survivors.json',
      'stage2_surgical_strike_metadata.json',
      'stage2_surgical_strike_scored.json',
      'stage2_surgical_strike_top40.json',
      'stage4_chronos_metadata.json',
      'stage4_chronos_enriched_top40.json',
      'stage3_options_metadata.json',
      'stage3_options_enriched_top40.json',
      'stage2_confluence_metadata.json',
      'stage2_confluence_ranked_top40.json',
      'stage5_news_metadata.json',
      'stage5_news_safe_finalists.json',
      'stage5_news_rejections.json',
      'stage6_council_enriched.json',
      'stage7_cluster_report.json',
      'stage7_clustered_survivors.json',
      'stage7_cluster_rejections.json',
      'phase_b_baseline_idea_log_results.json',
      'data/barchart_uoa.json',
      'data/gex_regime.json',
      'data/stockanalysis_scores.json',
      'data/danelfin_scores.json',
      'data/news_catalyst.json',
    ]);
    if (!allowed.has(req.params.name)) return res.status(400).json({ error: 'Artifact not allowed' });
    const file = path.join(system2Root, req.params.name);
    const data = readJson(file, null);
    if (data == null) return res.status(404).json({ error: 'Artifact not found' });
    res.json({ ok: true, name: req.params.name, data });
  });

  app.get('/api/system2/learning-engine', adminOnly, (req, res) => {
    try {
      const file = path.join(system2Root, 'data', 'learned_weights.json');
      const data = readJson(file, null);
      if (data == null) return res.json({ mode: 'DORMANT', error: 'file not found' });
      let generated = data.generated_at || null;
      try { generated = generated || fs.statSync(file).mtime.toISOString(); } catch {}
      res.json({
        ok: true,
        source: 'data/learned_weights.json',
        generated_at: generated,
        sample: data.ledger_count ?? data.total_resolved ?? 0,
        ...data,
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  function readJsonl(file) {
    try {
      return fs.readFileSync(file, 'utf8')
        .split(/\r?\n/)
        .filter(Boolean)
        .map(line => JSON.parse(line));
    } catch {
      return [];
    }
  }

  function councilCalibrationFromRealLedger() {
    const file = path.join(system2Root, 'data', 'signal_outcomes.jsonl');
    const rows = readJsonl(file);
    const resolved = rows.filter(r => numericOrNull(r.actual_r) != null && numericOrNull(r.actual_r) !== 0);
    const modelKeys = ['claude', 'gpt4o', 'gemini', 'kimi'];
    const abstain = new Set(['', '-', 'NONE', 'NULL', 'ERROR', 'TIMEOUT', 'ABSTAIN']);
    const groups = {
      tier1: new Set(['UPGRADE', 'TIER1']),
      tier2: new Set(['TIER2', 'TIER3']),
      skip: new Set(['SKIP', 'FORCE_SKIP']),
    };
    const groupFor = (verdict) => {
      const v = String(verdict || '').trim().toUpperCase();
      for (const [name, vals] of Object.entries(groups)) if (vals.has(v)) return name;
      return abstain.has(v) ? 'abstain' : 'other';
    };
    const avg = vals => vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null;
    const models = {};
    for (const model of modelKeys) {
      const modelRows = resolved.filter(r => !abstain.has(String(r[`${model}_verdict`] || r[`council_${model}`] || '').trim().toUpperCase()));
      const dist = {};
      for (const row of modelRows) {
        const v = String(row[`${model}_verdict`] || row[`council_${model}`] || '').trim().toUpperCase();
        dist[v] = (dist[v] || 0) + 1;
      }
      const groupStats = {};
      for (const [group, vals] of Object.entries(groups)) {
        const subset = modelRows.filter(r => vals.has(String(r[`${model}_verdict`] || r[`council_${model}`] || '').trim().toUpperCase()));
        const rVals = subset.map(r => numericOrNull(r.actual_r)).filter(v => v != null);
        groupStats[group] = {
          count: subset.length,
          avg_r: avg(rVals),
          win_rate: rVals.length ? Number((rVals.filter(v => v > 0).length / rVals.length * 100).toFixed(1)) : null,
        };
      }
      let agreement = 0;
      let checked = 0;
      for (const row of resolved) {
        const mv = String(row[`${model}_verdict`] || row[`council_${model}`] || '').trim().toUpperCase();
        if (abstain.has(mv)) continue;
        const fg = groupFor(row.council_final_verdict);
        const mg = groupFor(mv);
        checked += 1;
        if (mg === fg) agreement += 1;
      }
      const forceSkip = dist.FORCE_SKIP || 0;
      const abstainCount = resolved.length - modelRows.length;
      models[model] = {
        name: model === 'gpt4o' ? 'GPT-4o' : model.charAt(0).toUpperCase() + model.slice(1),
        total_verdicts: modelRows.length,
        distribution: dist,
        group_stats: groupStats,
        tier1_avg_r: groupStats.tier1.avg_r,
        skip_avg_r: groupStats.skip.avg_r,
        tier1_count: groupStats.tier1.count,
        skip_count: groupStats.skip.count,
        force_skip_rate: modelRows.length ? forceSkip / modelRows.length : 0,
        abstain_rate: resolved.length ? abstainCount / resolved.length : 0,
        agreement_rate: checked ? agreement / checked : null,
        calibrated: false,
        insufficient: modelRows.length < 30,
      };
    }
    let generated = null;
    let size = 0;
    try {
      const stat = fs.statSync(file);
      generated = stat.mtime.toISOString();
      size = stat.size;
    } catch {}
    return {
      ok: true,
      source: 'data/signal_outcomes.jsonl',
      generated_at: generated,
      file_bytes: size,
      total_resolved: resolved.length,
      sample: resolved.length,
      needed: 30,
      insufficient_data: resolved.length < 30,
      message: resolved.length < 30
        ? `No real council calibration data yet (${resolved.length} real resolved outcomes, need 30+). Council gating remains disabled. The numbers previously shown were test fixtures and have been removed.`
        : null,
      council_gates_trades: false,
      models: resolved.length >= 30 ? models : {},
      raw_rows_available: rows.length,
    };
  }

  app.get('/api/system2/council-calibration', adminOnly, (req, res) => {
    try {
      res.json(councilCalibrationFromRealLedger());
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/backtest/history', adminOnly, (req, res) => {
    try {
      const dataDir = path.join(system2Root, 'data');
      let files = [];
      try {
        files = fs.readdirSync(dataDir)
          .filter(f => f.startsWith('backtest_results_') && f.endsWith('.json'))
          .map(f => ({ name: f, mtime: fs.statSync(path.join(dataDir, f)).mtimeMs }))
          .sort((a, b) => b.mtime - a.mtime);
      } catch {}
      if (files.length === 0) return res.json({ ok: true, backtests: [], error: 'no backtest results found' });
      const latest = readJson(path.join(dataDir, files[0].name), null) || {};
      res.json({ ok: true, latest_file: files[0].name, ...latest });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/intelligence', adminOnly, (req, res) => {
    try {
      const report = readJson(path.join(system2Root, 'data', 'intelligence_report.json'), {}) || {};
      let runs = [];
      try {
        runs = fs.readdirSync(logDir)
          .filter(f => /^phase_b_core_.*\.json$/.test(f))
          .map(f => {
            const row = readJson(path.join(logDir, f), null);
            return row ? { file: f, ...row } : null;
          })
          .filter(Boolean)
          .sort((a, b) => String(a.runStartedAt || '').localeCompare(String(b.runStartedAt || '')));
      } catch {}
      const successful = runs.filter(r => r.ok === true && String(r.pipeline_status || '').toUpperCase() !== 'FAILED');
      const recent = runs.slice(-30);
      const latest = [...runs].reverse().find(r => r.ok != null) || {};
      const avg = (vals) => vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(2)) : null;
      const runtimeVals = recent.map(r => numericOrNull(r.duration_minutes ?? r.runtimeMinutes)).filter(v => v != null);
      const finalistVals = recent.map(r => numericOrNull(r.finalist_count ?? r.stage7FinalistCount ?? r.stage5NewsSafeCount)).filter(v => v != null);
      const run_summary = {
        success_rate: runs.length ? Number((successful.length / runs.length * 100).toFixed(1)) : null,
        successful_runs: successful.length,
        total_runs: runs.length,
        sample: runs.length,
        source: 'live phase_b_core logs',
        generated_at: new Date().toISOString(),
        scope_label: `over current full run set (${runs.length} runs)`,
        avg_finalists: avg(finalistVals),
        avg_runtime_minutes: avg(runtimeVals),
        latest_funnel: {
          universe: latest.universeCount ?? latest.counts?.universe ?? null,
          stage1: latest.stage1SurvivorCount ?? latest.counts?.stage1 ?? null,
          stage2: latest.stage2TopCount ?? latest.counts?.stage2 ?? null,
          finalists: latest.stage7FinalistCount ?? latest.stage5NewsSafeCount ?? latest.counts?.finalists ?? null,
        },
        latest_run: {
          date: String(latest.runStartedAt || '').slice(0, 10),
          status: latest.pipeline_status || (latest.ok ? 'SUCCESS' : 'UNKNOWN'),
          duration_minutes: latest.duration_minutes ?? latest.runtimeMinutes ?? null,
          file: latest.file || null,
        },
      };
      const outcomeRows = rejectedOutcomeRows();
      const outcomeSummary = rejectedOutcomeSummary(outcomeRows);
      const trackedOutcomes = outcomeRows.filter(r => numericOrNull(r.would_be_r) != null && r.tracking_status !== 'tracking');
      const rawShadowGroups = {};
      for (const row of shadowRows()) {
        const reason = plainReason(row.rejection_reason);
        const days = numericOrNull(row.trading_days_since_rejection) ?? 0;
        const move = numericOrNull(row.price_change_pct);
        if (move == null || days < 3) continue;
        if (!rawShadowGroups[reason]) rawShadowGroups[reason] = [];
        rawShadowGroups[reason].push(move);
      }
      const gates = Object.fromEntries(outcomeSummary.map(g => [
        g.reason,
        (() => {
          const rawMoves = rawShadowGroups[g.reason] || [];
          return {
          count: rawMoves.length || g.tracked,
          raw_shadow_count: rawMoves.length,
          would_be_r_tracked: g.tracked,
          avg_5d_return: rawMoves.length ? Number((rawMoves.reduce((s, v) => s + v, 0) / rawMoves.length).toFixed(2)) : null,
          avg_missed_r: g.tracked ? Number((g.total_r / g.tracked).toFixed(2)) : null,
          net_r: g.total_r,
          status: g.total_r > 0 ? 'gate may be too strict here' : 'gate saved risk',
          };
        })(),
      ]));
      const totalCost = trackedOutcomes.filter(r => Number(r.would_be_r) > 0).reduce((s, r) => s + Number(r.would_be_r), 0);
      const totalSaved = trackedOutcomes.filter(r => Number(r.would_be_r) <= 0).reduce((s, r) => s + Math.abs(Number(r.would_be_r)), 0);
      const shadow_portfolio = {
        source: 'system2_shadow.db shadow_portfolio',
        generated_at: new Date().toISOString(),
        sample: trackedOutcomes.length,
        metric_note: 'Rejected names use shadow avg 5d move / estimated would-be R. Directional only, not directly comparable to survivor realized canonical R.',
        total_tracked: trackedOutcomes.length,
        total_tracking: outcomeRows.filter(r => r.tracking_status === 'tracking').length,
        total_cost_r: Number(totalCost.toFixed(2)),
        total_saved_r: Number(totalSaved.toFixed(2)),
        net_r: Number((totalCost - totalSaved).toFixed(2)),
        net_verdict: totalCost > totalSaved ? 'gates net-negative so far, observe only' : 'gates net-positive so far',
        gates,
        gates_with_data: Object.keys(gates).length,
        gates_effective: Object.values(gates).filter(g => g.net_r <= 0).length,
        gates_to_review: Object.values(gates).filter(g => g.net_r > 0).length,
        overall_avg_5d_return: trackedOutcomes.length ? Number((trackedOutcomes.map(r => Number(r.shadow_move_pct)).filter(Number.isFinite).reduce((s, v, _, arr) => s + v / arr.length, 0)).toFixed(2)) : null,
        overall_pct_up: trackedOutcomes.length ? Number((trackedOutcomes.filter(r => Number(r.shadow_move_pct) > 0).length / trackedOutcomes.length * 100).toFixed(1)) : null,
      };
      res.json({ ok: true, report: { ...report, generated_at: new Date().toISOString(), run_summary, shadow_portfolio, stage_performance: stagePerformanceFrom(outcomeRows) } });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/command-view', adminOnly, (req, res) => {
    try {
      ensure();
      const danger = newsDangerMap();
      const livePrices = readJson(path.join(system2Root, 'data', 'live_prices.json'), {});
      const requestDate = String(req.query.date || '').slice(0, 10) || new Date().toISOString().slice(0, 10);
      const latestStage = latestStageDetailRow(requestDate) || latestStageDetailRow();
      const finalistStage = stageByKey(latestStage, 'finalists') || { tickers: [] };
      const ideasByTicker = Object.fromEntries(db.data.ideas.map(withDerivedStatus).filter(isDisplayableIdea).map(i => [String(i.ticker).toUpperCase(), i]));
      const finalists = (finalistStage.tickers || []).map(r => {
        const ticker = String(r.ticker || r.symbol || '').toUpperCase();
        const idea = ideasByTicker[ticker] || {};
        return { ticker, date: latestStage?.date || idea.date, setup: idea.setup || idea.setup_type || r.setup || null, grade: idea.grade || r.grade || null, trade_quality_score: idea.trade_quality_score ?? idea.confluence_score ?? idea.setup_score ?? null };
      }).filter(r => r.ticker);
      const lifecycle = db.data.ideas
        .filter(i => (i.paper_status === 'OPEN' || i.paper_status === 'WATCHING') && !isInvalidTicker(i.ticker) && !i.r_calculation_suspect)
        .map(i => withTraderReadiness(i, danger, livePrices))
        .map(i => ({
          ticker: i.ticker,
          date: i.date,
          status: i.paper_status,
          score: i.score,
          entry: i.entry,
          stop: i.stop,
          target: i.target,
          paper_entry_price: i.paper_entry_price,
          current_price: i.current_price,
          unrealized_r: i.live_r,
          live_r: i.live_r,
          live_r_direction: i.live_r_direction,
          news_danger: i.news_danger,
          news_danger_reason: i.news_danger_reason,
          pre_market_gap_favourable: i.pre_market_gap_favourable,
          r_calculation_suspect: i.r_calculation_suspect,
        }));
      const discovery = readJson(path.join(system2Root, 'data', 'discovery_feed.json'), { candidates: [] });
      const alertsRaw = readJson(path.join(system2Root, 'data', 'entry_alerts.json'), { alerts: [] });
      const today = new Date().toISOString().slice(0, 10);
      const alerts = (alertsRaw.alerts || []).filter(a => (a.date || '').slice(0, 10) === today);
      const trades = lifecycle.filter(i => i.status === 'OPEN');
      res.json({
        ok: true,
        date: requestDate,
        finalist_source_date: latestStage?.date || null,
        fast_tier: { discovered_today: alerts.length, alerts },
        slow_tier: { finalists_today: finalists.length, finalists },
        in_play: { active_trades: trades.length, trades },
        lifecycle,
        discovery: discovery.candidates || [],
        alerts,
        generated_at: new Date().toISOString(),
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/run', adminOnly, (req, res) => {
    try {
      const child = childProcess.spawn('/bin/bash', [path.join(system2Root, 'run_phase_b_core_baseline.sh')], {
        cwd: system2Root,
        detached: true,
        stdio: 'ignore',
      });
      child.unref();
      res.json({ ok: true, started: true, message: 'Phase B baseline runner started in background.' });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  // ════════════════════════════════════════════════════════════════════
  // LIVE PRICE ENDPOINT
  // ════════════════════════════════════════════════════════════════════
  app.get('/api/system2/live-price/:ticker', adminOnly, async (req, res) => {
    try {
      const ticker = req.params.ticker;
      if (!ticker || !/^[A-Z]{1,5}$/i.test(ticker)) {
        return res.status(400).json({ error: 'Invalid ticker symbol' });
      }
      const [stock, spy] = await Promise.all([
        httpGet(`https://financialmodelingprep.com/stable/quote?symbol=${ticker.toUpperCase()}&apikey=${effectiveFmpKey}`),
        httpGet(`https://financialmodelingprep.com/stable/quote?symbol=SPY&apikey=${effectiveFmpKey}`),
      ]);
      if (!stock || !stock.length) {
        return res.status(404).json({ error: 'Price data not available for ' + ticker });
      }
      const s = stock[0];
      const spyPrice = spy && spy.length ? spy[0].price : null;
      const vsSpy = spyPrice && s.price ? Number(((s.price / spyPrice - 1) * 100).toFixed(2)) : null;
      res.json({
        ok: true,
        ticker: s.symbol || ticker.toUpperCase(),
        price: s.price,
        change: s.change,
        change_pct: s.changesPercentage,
        vs_spy: vsSpy,
        timestamp: new Date().toISOString(),
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  // Dashboard data shims: keep missing screen endpoints from falling through to
  // the SPA catch-all. These routes only read existing artifacts and return JSON.
  function dataArtifact(file, fallback = {}) {
    return readJson(path.join(system2Root, 'data', file), fallback);
  }

  function rootArtifact(file, fallback = null) {
    return readJson(path.join(system2Root, file), fallback);
  }

  function generatedAt(...items) {
    return items
      .map(x => x && (x.generated_at || x._system2_generated_at || x.date || x.run_date))
      .filter(Boolean)
      .sort()
      .pop() || null;
  }

  function sendDataArtifact(res, file, fallback = {}) {
    const data = dataArtifact(file, fallback);
    res.json({ ok: true, file, generated_at: generatedAt(data), data });
  }

  app.get('/api/system2/artifacts/*', adminOnly, (req, res) => {
    const name = req.params[0] || '';
    const safe = !name.includes('..') && /^[A-Za-z0-9_./-]+\.json$/.test(name);
    if (!safe) return res.status(400).json({ ok: false, error: 'Invalid artifact path', name });
    const file = path.join(system2Root, name);
    const data = readJson(file, null);
    if (data == null) return res.json({ ok: true, name, data: Array.isArray(data) ? [] : null, missing: true });
    res.json({ ok: true, name, data });
  });

  function canonicalPerformanceMetricsPayload() {
    ensure();
    const rows = cleanResolvedRows().slice().sort((a, b) => String(a.date || '').localeCompare(String(b.date || '')));
    const vals = rows.map(r => canonicalResolvedR(r)).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    const avg = arr => arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : null;
    const round = (v, d = 3) => v == null || !Number.isFinite(Number(v)) ? null : Number(Number(v).toFixed(d));
    const stats = statsFromR(rows);
    let cumulative = 0;
    let peak = 0;
    let maxDrawdown = 0;
    let winStreak = 0;
    let lossStreak = 0;
    let maxWinStreak = 0;
    let maxLossStreak = 0;
    const equityCurve = rows.map(row => {
      const r = canonicalResolvedR(row) || 0;
      cumulative = Number((cumulative + r).toFixed(4));
      peak = Math.max(peak, cumulative);
      maxDrawdown = Math.max(maxDrawdown, peak - cumulative);
      if (r > 0) {
        winStreak += 1;
        lossStreak = 0;
      } else if (r < 0) {
        lossStreak += 1;
        winStreak = 0;
      }
      maxWinStreak = Math.max(maxWinStreak, winStreak);
      maxLossStreak = Math.max(maxLossStreak, lossStreak);
      return {
        date: String(row.date || row.paper_exit_at || '').slice(0, 10),
        ticker: row.ticker,
        r: round(r, 4),
        cumulative_r: round(cumulative, 4),
      };
    });
    const byGroup = (keyFn) => {
      const groups = {};
      for (const row of rows) {
        const key = keyFn(row) || 'unknown';
        if (!groups[key]) groups[key] = [];
        groups[key].push(row);
      }
      return Object.fromEntries(Object.entries(groups).map(([key, group]) => {
        const g = statsFromR(group);
        return [key, {
          trades: g.count,
          win_rate: g.win_rate,
          avg_r: g.avg_r,
          total_r: g.total_r,
        }];
      }));
    };
    return {
      ok: true,
      source: 'canonicalResolvedR / cleanResolvedRows',
      source_label: 'v2-clean resolved trades (post 2026-06-09, integrity-verified)',
      live_canonical: true,
      generated_at: new Date().toISOString(),
      trade_count: stats.count,
      win_rate: stats.win_rate,
      avg_r: stats.avg_r,
      expectancy: stats.avg_r,
      total_r: stats.total_r,
      profit_factor: stats.profit_factor,
      avg_win: round(avg(wins)),
      avg_loss: round(avg(losses)),
      best_trade: vals.length ? round(Math.max(...vals), 4) : null,
      worst_trade: vals.length ? round(Math.min(...vals), 4) : null,
      avg_hold_days: round(avg(rows.map(r => {
        const start = String(r.date || '').slice(0, 10);
        const end = String(r.paper_exit_at || r.actual_exit_at || r.exit_at || '').slice(0, 10);
        if (!start || !end) return null;
        const ms = new Date(end + 'T00:00:00Z').getTime() - new Date(start + 'T00:00:00Z').getTime();
        return Number.isFinite(ms) ? Math.max(0, ms / 86400000) : null;
      }).filter(v => v != null)), 1),
      max_drawdown_r: round(maxDrawdown),
      max_win_streak: maxWinStreak,
      max_loss_streak: maxLossStreak,
      sharpe_annual: null,
      sortino: null,
      calmar: maxDrawdown ? round(stats.total_r / maxDrawdown, 2) : null,
      rating_flags: [
        ['ProfitFactor', typeof stats.profit_factor === 'number' && stats.profit_factor >= 1.3 ? 'MEETS_BAR' : 'BELOW_BAR'],
        ['SampleSize', stats.count >= 200 ? 'MEETS_BAR' : 'BELOW_BAR'],
        ['Expectancy', Number(stats.avg_r) > 0 ? 'MEETS_BAR' : 'BELOW_BAR'],
      ],
      by_regime: byGroup(r => String(r.market_regime || r.regime || '').trim().toUpperCase() || 'unknown'),
      by_setup_type: byGroup(r => r.setup_type || r.setupType || r.setup || 'unknown'),
      equity_curve: equityCurve,
      stale_artifact_note: 'The previous data/performance_metrics.json artifact is no longer used for this endpoint.',
    };
  }

  function peadDriftStatsPayload() {
    ensure();
    if (!Array.isArray(db.data.pead_drift_paper)) db.data.pead_drift_paper = [];
    const rows = db.data.pead_drift_paper
      .filter(r => r && String(r.strategy || '').toUpperCase() === 'PEAD_DRIFT')
      .sort((a, b) => String(b.earnings_date || b.date || '').localeCompare(String(a.earnings_date || a.date || '')));
    const resolved = rows.filter(r => numericOrNull(r.canonical_r) != null && String(r.paper_status || '').toUpperCase() !== 'OPEN');
    const open = rows.filter(r => String(r.paper_status || '').toUpperCase() === 'OPEN');
    const vals = resolved.map(r => numericOrNull(r.canonical_r)).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    const avg = arr => arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : null;
    const sortedVals = vals.slice().sort((a, b) => a - b);
    const median = sortedVals.length
      ? (sortedVals.length % 2 ? sortedVals[(sortedVals.length - 1) / 2] : (sortedVals[sortedVals.length / 2 - 1] + sortedVals[sortedVals.length / 2]) / 2)
      : null;
    const round = (v, d = 3) => v == null || !Number.isFinite(Number(v)) ? null : Number(Number(v).toFixed(d));
    const binDefs = [
      ['<= -1R', v => v <= -1],
      ['-1R to 0R', v => v > -1 && v < 0],
      ['0R to +1R', v => v >= 0 && v < 1],
      ['+1R to +3R', v => v >= 1 && v < 3],
      ['> +3R', v => v >= 3],
    ];
    const distribution = binDefs.map(([label, fn]) => ({ label, count: vals.filter(fn).length }));
    return {
      ok: true,
      enabled: String(process.env.PEAD_DRIFT_ENABLED || 'false').toLowerCase() === 'true',
      strategy: 'PEAD_DRIFT',
      paper_only: true,
      source: 'fund.json pead_drift_paper',
      generated_at: new Date().toISOString(),
      config: {
        surprise_min_pct: 52.54,
        reaction_filter: 'reaction_close_gt_prior_close',
        entry_slippage_pct: 0.001,
        stop_atr_multiple: 2.0,
        hold_trading_days: 60,
        min_resolved_for_judgment: 40,
      },
      warning: 'PAPER ONLY. Low win-rate, fat-tail drift strategy. Most trades lose; winners are large. Separate from PMF. Do not judge until >=40-60 resolved 60-day trades.',
      total: rows.length,
      open_count: open.length,
      resolved_count: resolved.length,
      stats: {
        win_rate: vals.length ? round(wins.length / vals.length * 100, 1) : null,
        avg_r: round(avg(vals)),
        median_r: round(median),
        total_r: vals.length ? round(vals.reduce((s, v) => s + v, 0)) : 0,
        profit_factor: grossLoss ? round(grossWin / grossLoss, 2) : (grossWin ? 'infinity' : null),
      },
      distribution,
      rows: rows.slice(0, 200),
    };
  }

  app.get('/api/score/performance-metrics', adminOnly, (req, res) => {
    res.json(canonicalPerformanceMetricsPayload());
  });

  app.get('/api/system2/performance-metrics', adminOnly, (req, res) => {
    res.json(canonicalPerformanceMetricsPayload());
  });

  app.get('/api/system2/pead-drift', adminOnly, (req, res) => {
    res.json(peadDriftStatsPayload());
  });

  app.get('/api/system2/pmf-source-tally', adminOnly, (req, res) => {
    res.json(pmfSourceTallyPayload());
  });

  app.get('/api/score/scoreboard', adminOnly, (req, res) => {
    ensure();
    const rows = db.data.ideas
      .map(withDerivedStatus)
      .filter(isDisplayableIdea)
      .map(withTrueR)
      .map(i => ({
        id: i.id,
        date: i.date,
        ticker: i.ticker,
        idea_stream: i.idea_stream || 'swing_momentum',
        hold_period: i.hold_period || '3-10 day',
        r: canonicalResolvedR(i),
        actual_r: canonicalResolvedR(i),
        canonical_r: canonicalResolvedR(i),
        canonical_r_quarantined: i.canonical_r_quarantined === true || isCanonicalRQuarantined(i),
        canonical_r_quarantine_reasons: i.canonical_r_quarantine_reasons || canonicalQuarantineReasons(i),
        true_r: i.true_r,
        realistic_entry: i.realistic_entry,
        realistic_exit: i.realistic_exit,
        slippage_applied_pct: i.slippage_applied_pct,
        true_r_fill_source: i.true_r_fill_source,
        true_r_estimated_fill: i.true_r_estimated_fill,
        setup_type: i.setup_type || i.setupType || i.setup || null,
        regime: i.market_regime || i.regime || null,
        exit_reason: i.exit_reason || i.paper_exit_reason || i.hit || null,
        paper_status: i.paper_status,
        hit: i.paper_outcome || i.hit || null,
        pre_market_favourable: isPreMarketFavourable(i),
        council_tier: i.council_final_verdict || i.council_tier || i.council_verdict || null,
        options_verdict: i.options_verdict || null,
        rs_rank: i.rs_rank ?? null,
        sector_strength_rank: i.sector_strength_rank ?? null,
        pre_market_gap_favourable: isPreMarketFavourable(i),
        intake_source_layer: intakeSourceLayer(i),
        paper_exit_reason: i.paper_exit_reason || null,
      }));
    res.json(rows);
  });


  function alpacaPaperFillStats() {
    ensure();
    const alpacaRows = db.data.ideas.filter(i => i.fill_source === 'alpaca_paper' || i.real_r_fill_source === 'alpaca_paper' || i.alpaca_order_id);
    const rows = alpacaRows.filter(i => i.pmf_confirmed_at_fill === true && i.test_order !== true);
    const entries = rows.filter(i => numericOrNull(i.actual_entry_price) != null);
    const exits = rows.filter(i => numericOrNull(i.actual_exit_price) != null || numericOrNull(i.real_r) != null);
    const vals = exits.map(i => numericOrNull(i.real_r)).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    return {
      target: 30,
      order_count: rows.length,
      all_alpaca_paper_order_count: alpacaRows.length,
      excluded_without_pmf_at_fill_count: alpacaRows.length - rows.length,
      entry_fill_count: entries.length,
      exit_fill_count: exits.length,
      real_r_count: vals.length,
      win_rate: vals.length ? Number((wins.length / vals.length * 100).toFixed(1)) : null,
      avg_real_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null,
      profit_factor: grossLoss ? Number((grossWin / grossLoss).toFixed(2)) : (grossWin ? 'infinity' : null),
      source: 'alpaca_paper_pmf_confirmed_at_fill',
      cohort_rule: 'fill_source=alpaca_paper AND immutable pmf_confirmed_at_fill=true; test_order rows excluded.',
    };
  }

  const oneGlanceProtectionCache = { at: 0, data: null };

  function isoDateKey(value = new Date()) {
    const d = value instanceof Date ? value : new Date(value);
    return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : null;
  }

  function firstTimestamp(row, keys) {
    for (const key of keys) {
      const raw = row?.[key];
      if (!raw) continue;
      const d = new Date(raw);
      if (Number.isFinite(d.getTime())) return d.toISOString();
      const s = String(raw);
      if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return `${s}T00:00:00.000Z`;
    }
    return null;
  }

  function oneGlanceStats(rows) {
    const vals = rows.map(r => numericOrNull(r.real_r)).filter(v => v != null);
    const wins = vals.filter(v => v > 0);
    const losses = vals.filter(v => v < 0);
    const grossWin = wins.reduce((s, v) => s + v, 0);
    const grossLoss = Math.abs(losses.reduce((s, v) => s + v, 0));
    return {
      n: rows.length,
      resolved: vals.length,
      win_rate: vals.length ? Number((wins.length / vals.length * 100).toFixed(1)) : null,
      avg_real_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null,
      median_real_r: vals.length ? Number(vals.slice().sort((a, b) => a - b)[Math.floor(vals.length / 2)].toFixed(3)) : null,
      profit_factor: grossLoss ? Number((grossWin / grossLoss).toFixed(2)) : (grossWin ? 'infinity' : null),
    };
  }

  function oneGlanceRealPerformance() {
    ensure();
    const alpacaRows = db.data.ideas.filter(i => (
      (i.fill_source === 'alpaca_paper' || i.real_r_fill_source === 'alpaca_paper' || i.alpaca_order_id) &&
      i.pmf_confirmed_at_fill === true &&
      i.test_order !== true
    ));
    const entryRows = alpacaRows.filter(i => numericOrNull(i.actual_entry_price) != null);
    const cohortB = entryRows.filter(i => i.cohort_label === 'B_CLEAN');
    const contaminatedB = entryRows.filter(i => i.cohort_label === 'B_CONTAMINATED_MANUAL_EXITS');
    const cohortA = entryRows.filter(i => !cohortB.includes(i) && !contaminatedB.includes(i) && (numericOrNull(i.actual_exit_price) != null || numericOrNull(i.real_r) != null));
    const slips = entryRows.map(i => numericOrNull(i.entry_slippage_vs_model)).filter(v => v != null);
    const canonical = canonicalStatsObject()?.resolved_v2_clean || {};
    return {
      label: 'REAL FILLS (Alpaca paper, actual prices)',
      cohort_b_headline: {
        label: 'Cohort B (recalibrated geometry, GTC exits)',
        ...oneGlanceStats(cohortB),
        open: cohortB.filter(i => numericOrNull(i.actual_exit_price) == null && numericOrNull(i.real_r) == null).length,
      },
      cohort_a_reference: {
        label: 'Cohort A "old broken brackets — reference only"',
        ...oneGlanceStats(cohortA),
      },
      cohort_b_contaminated_reference: {
        label: 'B-contaminated (manual exits, not a system test)',
        ...oneGlanceStats(contaminatedB),
        excluded: true,
      },
      avg_entry_slippage_pct: slips.length ? Number((slips.reduce((s, v) => s + v, 0) / slips.length).toFixed(2)) : null,
      progress_to_verdict: {
        resolved_cohort_b: cohortB.filter(i => numericOrNull(i.real_r) != null).length,
        needed: 15,
        contaminated_excluded: contaminatedB.length,
      },
      cohort_integrity: cohortIntegrityPayload(),
      current_resolved_baseline: {
        label: 'Current resolved modeled-entry baseline',
        definition: canonical.definition || 'displayable v2 paper ideas with non-zero modeled exit price, canonical R available, and not quarantined',
        n: canonical.count ?? null,
        win_rate: canonical.win_rate ?? null,
        avg_r: canonical.avg_r ?? null,
        median_r: canonical.median_r ?? null,
        profit_factor: canonical.profit_factor ?? null,
      },
    };
  }

  function oneGlanceToday() {
    ensure();
    const today = isoDateKey();
    const rows = db.data.ideas || [];
    const pmfs = rows.filter(i => isPrimaryPmf(i) && firstTimestamp(i, ['pmf_stamp_time'])?.slice(0, 10) === today);
    const entries = rows.filter(i => (i.fill_source === 'alpaca_paper' || i.alpaca_order_id) && firstTimestamp(i, ['actual_entry_time', 'actual_entry_at', 'filled_at', 'entry_filled_at'])?.slice(0, 10) === today && i.test_order !== true);
    const closed = rows.filter(i => (i.real_r_fill_source === 'alpaca_paper' || i.alpaca_order_id) && firstTimestamp(i, ['actual_exit_time', 'actual_exit_at', 'exit_filled_at'])?.slice(0, 10) === today && i.test_order !== true);
    const symbols = new Map();
    for (const r of [...pmfs, ...entries, ...closed]) {
      const ticker = String(r.ticker || r.symbol || '').toUpperCase();
      if (!ticker) continue;
      const cur = symbols.get(ticker) || { ticker };
      if (pmfs.includes(r)) {
        cur.pmf_confirmed = true;
        cur.gap_pct = numericOrNull(r.pre_market_gap_pct);
        cur.atr_multiple = numericOrNull(r.pmf_atr_multiple_at_stamp ?? r.pre_market_gap_atr_multiple);
      }
      if (entries.includes(r)) {
        cur.filled = true;
        cur.fill_price = numericOrNull(r.actual_entry_price);
        cur.slippage_pct = numericOrNull(r.entry_slippage_vs_model);
        cur.oco_stop = numericOrNull(r.recalibrated_stop ?? r.oco_stop_price ?? r.stop);
        cur.oco_target = numericOrNull(r.recalibrated_target ?? r.oco_target_price ?? r.target);
      }
      if (closed.includes(r)) {
        cur.closed = true;
        cur.exit_price = numericOrNull(r.actual_exit_price);
        cur.exit_reason = r.actual_exit_reason || r.paper_exit_reason || r.hit || null;
        cur.real_r = numericOrNull(r.real_r);
      }
      cur.status = cur.closed ? 'CLOSED' : cur.filled ? 'OPEN' : 'PMF';
      symbols.set(ticker, cur);
    }
    const dailyCap = Number(process.env.PMF_AUTO_EXEC_DAILY_CAP || readEnvValue('PMF_AUTO_EXEC_DAILY_CAP') || 3);
    return {
      date: today,
      pmf_confirmed: pmfs.length,
      orders_placed: entries.length,
      cap_reached: dailyCap > 0 ? entries.length >= dailyCap : false,
      closed: closed.length,
      rows: [...symbols.values()].sort((a, b) => (b.gap_pct || -999) - (a.gap_pct || -999)),
    };
  }

  function fileMtimeIso(file) {
    try { return fs.statSync(file).mtime.toISOString(); }
    catch { return null; }
  }

  function recentLogStatus(name, file, maxAgeHours) {
    const last = fileMtimeIso(file);
    const ageHours = last ? (Date.now() - new Date(last).getTime()) / 3600000 : null;
    return {
      name,
      file,
      last,
      ok: ageHours != null && ageHours <= maxAgeHours,
      age_hours: ageHours == null ? null : Number(ageHours.toFixed(2)),
    };
  }

  function oneGlanceHealth() {
    const jobs = [
      recentLogStatus('nightly pipeline', path.join(logDir, 'cron_set2.log'), 42),
      recentLogStatus('PMF gap check', path.join(logDir, 'cron_gap_check.log'), 30),
      recentLogStatus('open confirmation', path.join(logDir, 'open-confirmation.log'), 30),
      recentLogStatus('PMF_LATE', path.join(logDir, 'cron_gap_check_late.log'), 30),
      recentLogStatus('PEAD tracker', path.join(logDir, 'pead_drift_tracker.log'), 42),
      recentLogStatus('auto-exec sync', path.join(logDir, 'pmf_auto_exec_sync.log'), 2),
      recentLogStatus('validated backup', '/root/system2-core/backups/fund-validated/cron.log', 30),
      recentLogStatus('intraday recorder', path.join(logDir, 'intraday_bar_recorder.log'), 30),
      recentLogStatus('drift checker', path.join(logDir, 'crontab_drift_check.log'), 30),
    ];
    const latestRun = latestScheduledNightlyRun();
    const latestManual = latestManualRun();
    const backupLog = '/root/system2-core/backups/fund-validated/backup.log';
    const alertFiles = [
      path.join(logDir, 'fund_integrity_alert.log'),
      path.join(logDir, 'crontab_drift_alert.txt'),
      path.join(logDir, 'pmf_risk_breaker_alert.json'),
    ].map(file => ({ file, present: fs.existsSync(file), last: fileMtimeIso(file) }));
    const issues = [];
    const pipelineStatus = phaseRunStatus(latestRun);
    const manualStatus = phaseRunStatus(latestManual);
    const manualRun = latestManual ? {
      file: latestManual.file || null,
      status: manualStatus,
      generated_at: latestManual.generated_at || latestManual.completed_at || latestManual.finished_at || latestManual.runFinishedAt || null,
      run_started_at: latestManual.runStartedAt || null,
      note: manualStatus === 'SUCCESS'
        ? `manual run ${String(latestManual.runStartedAt || latestManual.runFinishedAt || '').slice(11, 16)}Z: SUCCESS`
        : `manual run ${String(latestManual.runStartedAt || latestManual.runFinishedAt || '').slice(11, 16)}Z: ${manualStatus} (guard worked, finalists unaffected)`,
    } : null;
    for (const job of jobs) if (!job.ok) issues.push(`${job.name} stale or missing`);
    if (pipelineStatus !== 'SUCCESS') issues.push(`pipeline status is ${pipelineStatus}`);
    for (const a of alertFiles) if (a.present) issues.push(path.basename(a.file));
    return {
      state: issues.length ? 'red' : 'ok',
      issues,
      cron: {
        required: jobs.length,
        ok: jobs.filter(j => j.ok).length,
        jobs,
        last: jobs.map(j => j.last).filter(Boolean).sort().slice(-1)[0] || null,
      },
      pipeline: {
        scope: 'latest scheduled 02:15 UTC run',
        file: latestRun?.file || null,
        status: pipelineStatus,
        generated_at: latestRun?.generated_at || latestRun?.completed_at || latestRun?.finished_at || latestRun?.runFinishedAt || null,
        run_started_at: latestRun?.runStartedAt || null,
      },
      manual_run: manualRun,
      backup: {
        log: backupLog,
        cron_log: '/root/system2-core/backups/fund-validated/cron.log',
        last: fileMtimeIso(backupLog),
      },
      alerts: alertFiles,
    };
  }

  async function alpacaPaperGet(pathname, account = 'primary') {
    const legacy = account === 'legacy_shared';
    const keyName = legacy ? 'ALPACA_LEGACY_SHARED_API_KEY' : 'ALPACA_PAPER_API_KEY';
    const secretName = legacy ? 'ALPACA_LEGACY_SHARED_API_SECRET' : 'ALPACA_PAPER_API_SECRET';
    const baseName = legacy ? 'ALPACA_LEGACY_SHARED_BASE_URL' : 'ALPACA_PAPER_BASE_URL';
    const key = process.env[keyName] || readEnvValue(keyName);
    const secret = process.env[secretName] || readEnvValue(secretName);
    const base = (process.env[baseName] || readEnvValue(baseName) || (legacy ? '' : 'https://paper-api.alpaca.markets/v2')).replace(/\/$/, '');
    if (!base.includes('paper')) throw new Error('Alpaca paper base URL assertion failed');
    if (!key || !secret) throw new Error('Alpaca paper credentials missing');
    const r = await fetch(base + pathname, { headers: { 'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret } });
    if (!r.ok) throw new Error(`Alpaca ${pathname} -> ${r.status}`);
    return r.json();
  }

  async function oneGlanceProtection(req) {
    if (req?.query?.simulate === 'alpaca_fail') throw new Error('simulated Alpaca protection failure');
    const nowMs = Date.now();
    if (oneGlanceProtectionCache.data && nowMs - oneGlanceProtectionCache.at < 60000) {
      return { ...oneGlanceProtectionCache.data, cache: { hit: true, ttl_seconds: 60 } };
    }
    ensure();
    const dedicatedEnabled = envFlag('ALPACA_DEDICATED_ACCOUNT_ENABLED', false);
    const accounts = [{ tag: dedicatedEnabled ? 'dedicated' : 'legacy_shared', source: 'primary' }];
    if (dedicatedEnabled && (process.env.ALPACA_LEGACY_SHARED_API_KEY || readEnvValue('ALPACA_LEGACY_SHARED_API_KEY'))) accounts.push({ tag: 'legacy_shared', source: 'legacy_shared' });
    const brokerData = await Promise.all(accounts.map(async account => ({
      ...account,
      positions: await alpacaPaperGet('/positions', account.source),
      orders: await alpacaPaperGet('/orders?status=open&limit=500&nested=true', account.source),
    })));
    const localOpen = db.data.ideas.filter(i => (
      (i.fill_source === 'alpaca_paper' || i.alpaca_order_id) &&
      i.test_order !== true &&
      numericOrNull(i.actual_entry_price) != null &&
      numericOrNull(i.actual_exit_price) == null &&
      numericOrNull(i.real_r) == null
    ));
    const wanted = new Map();
    for (const account of brokerData) {
      for (const p of Array.isArray(account.positions) ? account.positions : []) {
        const sym = String(p.symbol || '').toUpperCase();
        if (sym) wanted.set(`${account.tag}:${sym}`, { ticker: sym, account: account.tag, alpaca_qty: numericOrNull(p.qty), current_price: numericOrNull(p.current_price), source: 'alpaca_position' });
      }
    }
    for (const r of localOpen) {
      const sym = String(r.ticker || r.symbol || '').toUpperCase();
      if (!sym) continue;
      const account = r.account || (dedicatedEnabled ? 'dedicated' : 'legacy_shared');
      const key = `${account}:${sym}`;
      wanted.set(key, { ...(wanted.get(key) || { ticker: sym, account }), local_row_id: r.id, entry_price: numericOrNull(r.actual_entry_price), source: wanted.has(key) ? 'alpaca_and_fund' : 'fund_open' });
    }
    const rows = [...wanted.values()].map(item => {
      const openOrders = brokerData.find(account => account.tag === item.account)?.orders || [];
      const symbolOrders = openOrders.filter(o => String(o.symbol || '').toUpperCase() === item.ticker || (o.legs || []).some(l => String(l.symbol || '').toUpperCase() === item.ticker));
      const sellOrders = [];
      for (const o of symbolOrders) {
        if (String(o.side || '').toLowerCase() === 'sell') sellOrders.push(o);
        for (const leg of o.legs || []) if (String(leg.side || '').toLowerCase() === 'sell') sellOrders.push({ ...leg, parent_order_class: o.order_class });
      }
      const liveSell = sellOrders.filter(o => ['new', 'accepted', 'pending_new', 'partially_filled', 'held'].includes(String(o.status || '').toLowerCase()));
      const gtc = liveSell.some(o => String(o.time_in_force || '').toLowerCase() === 'gtc');
      const limit = liveSell.find(o => String(o.type || '').toLowerCase() === 'limit');
      const stop = liveSell.find(o => String(o.type || '').toLowerCase().includes('stop'));
      return {
        ...item,
        exit_order_live: liveSell.length > 0,
        tif: gtc ? 'gtc' : (liveSell[0]?.time_in_force || null),
        order_count: liveSell.length,
        target: numericOrNull(limit?.limit_price),
        stop: numericOrNull(stop?.stop_price || stop?.limit_price),
        status: liveSell.length ? 'PROTECTED' : 'NAKED',
      };
    }).sort((a, b) => a.ticker.localeCompare(b.ticker));
    const data = {
      state: rows.some(r => !r.exit_order_live) ? 'red' : 'ok',
      open_auto_positions: rows.length,
      naked_count: rows.filter(r => !r.exit_order_live).length,
      rows,
      risk_breakers: {
        armed: envFlag('PMF_RISK_BREAKERS_ENABLED', false),
        alert_file_present: fs.existsSync(path.join(logDir, 'pmf_risk_breaker_alert.json')),
      },
      alpaca_calls: 2,
      cache: { hit: false, ttl_seconds: 60 },
    };
    oneGlanceProtectionCache.at = nowMs;
    oneGlanceProtectionCache.data = data;
    return data;
  }

  async function buildOneGlancePayload(req) {
    const blocks = {};
    for (const [name, fn] of Object.entries({
      today: () => oneGlanceToday(),
      health: () => oneGlanceHealth(),
      real_performance: () => oneGlanceRealPerformance(),
      protection: () => oneGlanceProtection(req),
    })) {
      try {
        blocks[name] = await fn();
      } catch (e) {
        blocks[name] = { state: 'unknown', error: e.message };
      }
    }
    const red = blocks.protection?.state === 'red' || blocks.health?.state === 'red';
    return { ok: true, generated_at: new Date().toISOString(), state: red ? 'red' : 'ok', ...blocks };
  }

  app.get('/api/system2/one-glance', adminOnly, async (req, res) => {
    try {
      ensure();
      res.json(await buildOneGlancePayload(req));
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });

  app.get('/api/system2/track-record-readiness', adminOnly, (req, res) => {
    const perf = dataArtifact('performance_metrics.json', {});
    const snaps = dataArtifact('weekly_snapshots.json', { snapshots: [] });
    const resolvedRows = cleanResolvedRows();
    const cleanStats = statsFromR(resolvedRows);
    const trueRStats = statsFromTrueR(resolvedRows);
    const pmfRows = resolvedRows.filter(isPrimaryPmf);
    const pmfStats = statsFromR(pmfRows);
    const pmfTrueStats = statsFromTrueR(pmfRows);
    const pmfMarkout5dStats = statsFromNumericField(pmfRows, 'would_be_r_markout_5d');
    const tradeCount = cleanStats.count;
    const stage = tradeCount >= 200 ? 'SELLABLE' : tradeCount >= 100 ? 'STATISTICAL' : tradeCount >= 30 ? 'INITIAL_SAMPLE' : tradeCount >= 1 ? 'BUILDING' : 'NOT_STARTED';
    const regimeCoverage = regimeCoverageFromRows(resolvedRows);
    const canonicalStats = canonicalStatsObject();
    res.json({
      ok: true,
      stage,
      progress_pct: Math.min(100, Math.round((tradeCount / 200) * 100)),
      resolved_count: tradeCount,
      source_label: 'v2-era resolved (post Jun-09, integrity-verified)',
      win_rate: cleanStats.win_rate || 0,
      avg_r: cleanStats.avg_r || 0,
      true_avg_r: trueRStats.avg_r || 0,
      true_win_rate: trueRStats.win_rate || 0,
      true_total_r: trueRStats.total_r || 0,
      true_profit_factor: trueRStats.profit_factor || 0,
      profit_factor: cleanStats.profit_factor || 0,
      total_r: cleanStats.total_r || 0,
      best_r: resolvedRows.map(r => canonicalResolvedR(r)).filter(v => v != null).sort((a,b)=>b-a)[0] ?? null,
      worst_r: resolvedRows.map(r => canonicalResolvedR(r)).filter(v => v != null).sort((a,b)=>a-b)[0] ?? null,
      metrics: { ...perf, clean_v2: cleanStats },
      canonical_stats: canonicalStats,
      true_r_comparison: trueRComparison(resolvedRows),
      pre_market_favourable: { label: 'Strict PMF', definition: 'pmf_confirmed_at_stamp === true (immutable stamp-time cohort)', count: pmfStats.count, target: 30, win_rate: pmfStats.win_rate, avg_r: pmfStats.avg_r, profit_factor: pmfStats.profit_factor, true_win_rate: pmfTrueStats.win_rate, true_avg_r: pmfTrueStats.avg_r, true_profit_factor: pmfTrueStats.profit_factor, true_total_r: pmfTrueStats.total_r, markout_5d_count: pmfMarkout5dStats.count, markout_5d_win_rate: pmfMarkout5dStats.win_rate, markout_5d_avg_r: pmfMarkout5dStats.avg_r, markout_5d_profit_factor: pmfMarkout5dStats.profit_factor },
      pmf_by_source: pmfSourceTallyPayload(),
      alpaca_paper_fills: alpacaPaperFillStats(),
      regimes_traded: regimeCoverage.regimes_traded,
      regime_coverage: regimeCoverage.regime_coverage,
      weekly_snapshots: snaps.snapshots || snaps.weeks || snaps,
    });
  });

  app.get('/api/system2/news-intel', adminOnly, (req, res) => {
    sendDataArtifact(res, 'news_catalyst.json', { results: {}, candidates: [] });
  });

  app.get('/api/system2/finalist-options', adminOnly, (req, res) => {
    sendDataArtifact(res, 'finalist_options.json', { tickers: {} });
  });

  app.get('/api/system2/confluence-signals', adminOnly, (req, res) => {
    sendDataArtifact(res, 'confluence_signals.json', { signals: [] });
  });

  app.get('/api/system2/dark-pool', adminOnly, (req, res) => {
    sendDataArtifact(res, 'finra_dark_pool.json', { stocks: {}, tickers: {} });
  });

  app.get('/api/system2/insider-flow', adminOnly, (req, res) => {
    sendDataArtifact(res, 'insider_trades.json', { tickers: {}, trades: [] });
  });

  app.get('/api/system2/congress-trades', adminOnly, (req, res) => {
    sendDataArtifact(res, 'congress_trades.json', { tickers: {}, trades: [] });
  });

  app.get('/api/system2/social-intelligence', adminOnly, (req, res) => {
    const sentiment = dataArtifact('social_sentiment.json', {});
    const apewisdom = dataArtifact('apewisdom_sentiment.json', {});
    const discoveryData = dataArtifact('social_discovery.json', {});
    res.json({
      ok: true,
      generated_at: generatedAt(sentiment, apewisdom, discoveryData),
      sentiment,
      apewisdom,
      spikes: apewisdom.top_spikes || apewisdom.spikes || [],
      discovery: discoveryData.candidates || discoveryData.discovery || [],
    });
  });

  app.get('/api/system2/morning-brief-live', adminOnly, (req, res) => {
    ensure();
    const ideas = db.data.ideas.map(withDerivedStatus).filter(i => i.paper !== false);
    const entered = ideas.filter(i => i.actual_entry_price || i.paper_entry_price || i.entryRecorded);
    const watchlist = ideas.filter(i => !i.actual_entry_price && !i.paper_entry_price && !i.entryRecorded && i.paper_status !== 'RESOLVED');
    res.json({
      ok: true,
      generated_at: now(),
      open_ideas_needing_action: [],
      watchlist_ready_to_enter: watchlist.slice(0, 10),
      portfolio_summary: {
        entered_trades: entered.length,
        watching: watchlist.length,
        total_r: 0,
      },
    });
  });

  app.get('/api/system2/options-positioning', adminOnly, (req, res) => {
    const data = dataArtifact('options_positioning.json', { active: false, ideas: [] });
    const outcomes = dataArtifact('options_positioning_outcomes.json', { ideas: [] });
    res.json({ ok: true, file: 'options_positioning.json', generated_at: generatedAt(data), data, outcomes });
  });

  function normalizedOptionsPositioningIdea(row, data = {}) {
    const entry = numericOrNull(row.entry ?? row.price_at_generation);
    const target = numericOrNull(row.target);
    const stop = numericOrNull(row.stop);
    const rr = numericOrNull(row.r_r ?? row.rr ?? row.risk_reward);
    return {
      ticker: String(row.ticker || row.symbol || '').toUpperCase(),
      idea_stream: 'options_positioning',
      hold_period: '1-3 day',
      direction: row.direction || null,
      entry,
      target,
      stop,
      rr,
      confidence: row.confidence || null,
      max_pain: numericOrNull(row.max_pain ?? row.maxPain),
      pcr: numericOrNull(row.pcr ?? row.put_call_ratio ?? row.putCallRatio),
      expiry_date: row.expiry_date || row.expiry || data.expiry || null,
      days_to_expiry: numericOrNull(row.days_to_expiry ?? row.dte ?? data.dte),
      reasoning: row.reasoning || row.thesis || row.reason || null,
      setup_type: row.setup_type || row.setupType || null,
      price_at_generation: numericOrNull(row.price_at_generation),
      generated_at: row.generated_at || data.generated_at || null,
      warning: row.warning || 'Short-hold options positioning. Use tight stops.',
      source: row.data_source || data.source || 'options_positioning.json',
    };
  }

  app.get('/api/system2/options-positioning-stream', adminOnly, (req, res) => {
    const data = dataArtifact('options_positioning.json', { active: false, ideas: [] });
    const ideas = (Array.isArray(data.ideas) ? data.ideas : [])
      .map(row => normalizedOptionsPositioningIdea(row, data))
      .filter(row => row.ticker);
    res.json({
      ok: true,
      source: 'options_positioning.json',
      idea_stream: 'options_positioning',
      hold_period: '1-3 day',
      active: data.active === true,
      paused: data.paused === true,
      pause_reason: data.pause_reason || data.reason || null,
      reason: data.reason || null,
      performance: data.performance || null,
      generated_at: generatedAt(data),
      dte: data.dte ?? null,
      expiry: data.expiry ?? null,
      count: ideas.length,
      ideas,
    });
  });

  function daysAgoDate(days) {
    const d = new Date();
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  }

  function tickerOf(row) {
    return String(row?.ticker || row?.symbol || '').toUpperCase();
  }

  function swingAgreementScore(row) {
    return measuredSupportPillars(row).filter(p => p.positive).length;
  }

  function measuredSupportPillars(row) {
    const council = String(row.council_final_verdict || row.council_tier || row.council_verdict || '').toUpperCase();
    return [
      {
        key: 'pre_market_favourable',
        label: 'Pre-market favourable',
        positive: isPreMarketFavourable(row),
      },
      {
        key: 'council',
        label: 'Council tier',
        positive: ['TIER1', 'TIER2', 'UPGRADE'].includes(council),
      },
    ];
  }

  function contextualSignalNotes(row) {
    const options = String(row.options_verdict || '').toUpperCase();
    const chronos = String(row.chronos_dir || row.forecastDecision || '').toUpperCase();
    return [
      {
        key: 'options',
        value: options || null,
        counted_positive: false,
        note: options.includes('CONFIRM') || options === 'BULLISH_CONFIRM'
          ? 'Options CONFIRM is under review/inverse in measured data; context only, not positive support.'
          : 'Options context only.',
      },
      {
        key: 'chronos',
        value: chronos || null,
        counted_positive: false,
        note: chronos
          ? 'Chronos direction is logged for context only; not counted as positive support.'
          : 'No Chronos context.',
      },
    ];
  }

  function displayableSwingIdeas() {
    ensure();
    return db.data.ideas.map(withDerivedStatus).filter(isDisplayableIdea).map(i => ({
      ...i,
      idea_stream: i.idea_stream || 'swing_momentum',
      hold_period: i.hold_period || '3-10 day',
    }));
  }

  function resolvedRForStats(row) {
    return canonicalResolvedR(row);
  }

  function compareConfluencePerformance(rows, flaggedTickers) {
    const resolved = rows.filter(r => resolvedRForStats(r) != null);
    const stats = (set) => {
      const vals = resolved.filter(r => set.has(`${String(r.date || '').slice(0, 10)}|${tickerOf(r)}`)).map(resolvedRForStats).filter(v => v != null);
      const wins = vals.filter(v => v > 0).length;
      return { count: vals.length, win_rate: vals.length ? Number((wins / vals.length * 100).toFixed(1)) : null, avg_r: vals.length ? Number((vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(3)) : null };
    };
    const allKeys = new Set(resolved.map(r => `${String(r.date || '').slice(0, 10)}|${tickerOf(r)}`));
    const non = new Set([...allKeys].filter(k => !flaggedTickers.has(k)));
    return { confluence: stats(flaggedTickers), non_confluence: stats(non), partial_data: resolved.length < 30 || flaggedTickers.size < 5 };
  }

  app.get('/api/system2/confluence-alerts', adminOnly, (req, res) => {
    try {
      const since = daysAgoDate(Number(req.query.days || 7));
      const swing = displayableSwingIdeas().filter(i => String(i.date || '').slice(0, 10) >= since && (i.idea_stream || 'swing_momentum') === 'swing_momentum');
      const optData = dataArtifact('options_positioning.json', { ideas: [] });
      const optRows = (Array.isArray(optData.ideas) ? optData.ideas : []).map(r => normalizedOptionsPositioningIdea(r, optData));
      const optOutcomes = dataArtifact('options_positioning_outcomes.json', { ideas: [] });
      const optHist = (Array.isArray(optOutcomes.ideas) ? optOutcomes.ideas : []).map(r => normalizedOptionsPositioningIdea(r, optData));
      const options = [...optRows, ...optHist].filter(r => String(r.generated_at || optData.generated_at || '').slice(0, 10) >= since || !r.generated_at);
      const optionsByTicker = new Map(options.map(r => [tickerOf(r), r]));
      const flaggedKeys = new Set();
      const alerts = [];
      for (const row of swing) {
        const score = swingAgreementScore(row);
        const ticker = tickerOf(row);
        const optionHit = optionsByTicker.get(ticker);
        const cross = Boolean(optionHit);
        const supportPillars = measuredSupportPillars(row);
        const contextSignals = contextualSignalNotes(row);
        if (score >= 2) {
          const date = String(row.date || '').slice(0, 10);
          flaggedKeys.add(`${date}|${ticker}`);
          alerts.push({
            ticker,
            date,
            idea_stream: 'swing_momentum',
            swing_confluence_score: score,
            measured_support_score: score,
            measured_support_total: supportPillars.length,
            measured_support_label: `${score}/${supportPillars.length} measured support`,
            support_pillars: supportPillars,
            context_signals: contextSignals,
            swing_3_of_3: false,
            swing_full_measured_support: score >= supportPillars.length,
            cross_stream_confluence: cross,
            badge: `${score}/${supportPillars.length} MEASURED SUPPORT`,
            rarity_score: (cross ? 10 : 0) + score,
            setup_type: row.setup_type || row.setup || null,
            council_tier: row.council_final_verdict || row.council_tier || row.council_verdict || null,
            options_verdict: row.options_verdict || null,
            chronos_dir: row.chronos_dir || null,
            r: resolvedRForStats(row),
            status: row.paper_status || row.hit || null,
          });
        }
      }
      alerts.sort((a, b) => (Number(b.support_pillars?.[0]?.positive === true) - Number(a.support_pillars?.[0]?.positive === true)) || (b.rarity_score - a.rarity_score) || String(b.date).localeCompare(String(a.date)));
      const performance = compareConfluencePerformance(displayableSwingIdeas(), flaggedKeys);
      const c = performance.confluence || {};
      const n = performance.non_confluence || {};
      const underReview = c.avg_r != null && n.avg_r != null && c.avg_r <= n.avg_r;
      res.json({
        ok: true,
        days: Number(req.query.days || 7),
        since,
        count: alerts.length,
        definition: 'Measured support counts only pre-market favourable gap/PMF and council tier. Options CONFIRM and Chronos are context-only until their measured value improves.',
        header_note: underReview
          ? `Moments when measured-supportive signals agreed. Under review: flagged confluence has not outperformed in the resolved sample (${c.win_rate ?? '-'}% win, ${c.avg_r ?? '-'}R avg vs non-flagged ${n.win_rate ?? '-'}% win, ${n.avg_r ?? '-'}R avg).`
          : `Moments when measured-supportive signals agreed (${c.count || 0} resolved flagged samples). Options and Chronos are context-only.`,
        alerts,
        performance,
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  function latestStageRun() {
    ensure();
    return [...(db.data.system2_stage_details || [])].sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')))[0] || null;
  }

  app.get('/api/system2/sector-strength', adminOnly, (req, res) => {
    try {
      const run = latestStageRun();
      const finalistStage = (run?.stages || []).find(s => normalizeStageKey(s.stage_key || s.stage) === 'finalists') || {};
      const stage7 = (run?.stages || []).find(s => normalizeStageKey(s.stage_key || s.stage) === 'stage7') || {};
      const clusterMeta = stage7.metadata?.clusters || [];
      const ideasByTicker = new Map(displayableSwingIdeas().map(i => [tickerOf(i), i]));
      const bySector = new Map();
      const touch = (sector) => {
        const key = sector || 'Unknown';
        if (!bySector.has(key)) bySector.set(key, { sector: key, rank: null, one_month_vs_spy_pct: null, finalists: 0, options_positioning: 0, cluster_cap_rejections_month: 0, source: 'stage/fund cached data' });
        return bySector.get(key);
      };
      for (const r of finalistStage.tickers || []) {
        const d = r.data || r;
        const idea = ideasByTicker.get(tickerOf(d) || tickerOf(r)) || {};
        const row = touch(d.sector || d.cluster_sector || idea.sector || idea.cluster_sector);
        row.finalists += 1;
        if (numericOrNull(d.sector_strength_rank ?? idea.sector_strength_rank) != null) row.rank = numericOrNull(d.sector_strength_rank ?? idea.sector_strength_rank);
        if (numericOrNull(d.sector_strength_1m_vs_spy_pct ?? idea.sector_strength_1m_vs_spy_pct) != null) row.one_month_vs_spy_pct = numericOrNull(d.sector_strength_1m_vs_spy_pct ?? idea.sector_strength_1m_vs_spy_pct);
      }
      for (const c of clusterMeta) {
        const row = touch(c.sector);
        row.cluster_cap_rejections_month += Number(c.rejectedCount || 0);
      }
      const optData = dataArtifact('options_positioning.json', { ideas: [] });
      for (const o of optData.ideas || []) {
        const ticker = tickerOf(o);
        const match = displayableSwingIdeas().find(i => tickerOf(i) === ticker);
        touch(match?.sector || match?.cluster_sector || 'Unknown').options_positioning += 1;
      }
      const rows = [...bySector.values()].sort((a, b) => {
        if (a.rank != null && b.rank != null) return a.rank - b.rank;
        if (a.one_month_vs_spy_pct != null && b.one_month_vs_spy_pct != null) return b.one_month_vs_spy_pct - a.one_month_vs_spy_pct;
        return (b.finalists + b.options_positioning) - (a.finalists + a.options_positioning);
      });
      const persistedCount = rows.filter(r => r.rank != null || r.one_month_vs_spy_pct != null).length;
      res.json({
        ok: true,
        date: run?.date || null,
        has_persisted_sector_strength: persistedCount > 0,
        source: persistedCount > 0
          ? 'sector_strength_rank / sector_strength_1m_vs_spy_pct from persisted Stage 2 quality fields'
          : 'No persisted sector-strength performance fields found; showing current distribution only',
        sectors: rows,
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  function lookupCachePath() {
    return path.join(system2Root, 'data', `lookup_cache_${new Date().toISOString().slice(0, 10)}.json`);
  }

  function lookupAllowed(req) {
    const key = req.ip || req.headers['x-forwarded-for'] || 'local';
    const nowMs = Date.now();
    const row = (lookupRate.get(key) || []).filter(t => nowMs - t < 60000);
    if (row.length >= 10) return false;
    row.push(nowMs);
    lookupRate.set(key, row);
    return true;
  }

  app.post('/api/system2/lookup', adminOnly, (req, res) => {
    try {
      const ticker = String(req.body?.ticker || '').trim().toUpperCase().replace(/[^A-Z0-9.-]/g, '');
      if (!ticker) return res.status(400).json({ error: 'ticker required' });
      if (!lookupAllowed(req)) return res.status(429).json({ error: 'lookup rate limit: max 10/minute' });
      const cacheFile = lookupCachePath();
      const cache = readJson(cacheFile, { date: new Date().toISOString().slice(0, 10), results: {} });
      if (cache.results?.[ticker]) return res.json({ ok: true, cached: true, result: cache.results[ticker] });
      const ideas = displayableSwingIdeas();
      const idea = [...ideas].reverse().find(i => tickerOf(i) === ticker) || null;
      const rejected = rejectedOutcomeRows().find(r => tickerOf(r) === ticker) || null;
      const scoredPool = dataArtifact('scored_pool.json', []);
      const poolRows = Array.isArray(scoredPool) ? scoredPool : (scoredPool.stocks || scoredPool.rows || scoredPool.data || []);
      const pool = poolRows.find(r => tickerOf(r) === ticker) || null;
      const opts = dataArtifact('finalist_options.json', { tickers: {} }).tickers?.[ticker] || null;
      const live = dataArtifact('live_prices.json', {})[ticker] || null;
      const result = {
        ticker,
        generated_at: now(),
        read_only: true,
        cache_scope: 'daily',
        scope_note: 'V1 uses cached nightly/stage data plus cached live_prices.json. It does not write to finalist pools or run the full council/news scraper live.',
        verdict: idea ? 'FOUND_IN_SWING_HISTORY' : rejected ? 'REJECTED_IN_PIPELINE' : pool ? 'IN_UNIVERSE_NOT_FINALIST' : 'LIMITED_DATA_NOT_IN_RECENT_UNIVERSE',
        live: { source: live ? 'live_prices.json cache' : 'not available', current_price: numericOrNull(live?.last_price ?? live?.price), updated_at: live?.updated_at || null },
        technical: pool || idea ? { source: pool ? 'scored_pool.json cache' : 'fund.json idea history', setup_score: pool?.setup_score ?? idea?.setup_score ?? null, trade_quality_score: pool?.trade_quality_score ?? idea?.trade_quality_score ?? null, rs_rank: pool?.rs_rank ?? idea?.rs_rank ?? null, sector_strength_rank: pool?.sector_strength_rank ?? idea?.sector_strength_rank ?? null, sector: pool?.sector ?? idea?.sector ?? null } : { source: 'not assessed by nightly universe', available: false },
        options: opts ? { source: 'finalist_options.json cache', verdict: opts.options_verdict || null, put_call_ratio: numericOrNull(opts.put_call_ratio), max_pain: numericOrNull(opts.max_pain), notes: opts.options_notes || null } : { source: 'not available for this ticker in cached finalist options', available: false },
        news: idea ? { source: 'fund.json cached nightly fields', status: idea.news_safety_status || idea.news_verdict || null, risk: idea.news_risk || null } : { source: 'not assessed live in v1', available: false },
        council: idea ? { source: 'fund.json cached nightly fields', tier: idea.council_final_verdict || idea.council_tier || idea.council_verdict || null, votes: idea.council_votes ?? null, confidence: idea.council_conf ?? null } : { source: 'not assessed by AI council for non-scanned ticker in v1', available: false },
        rejection: rejected ? { source: 'Rejected Outcomes / shadow portfolio', date: rejected.date, stage: rejected.stage_rejected, reason: rejected.reason || rejected.shadow_reason, would_be_r: rejected.would_be_r ?? null } : null,
        card: idea || pool || { ticker },
      };
      cache.results = cache.results || {};
      cache.results[ticker] = result;
      try { fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 2)); } catch {}
      res.json({ ok: true, cached: false, result });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/signal-lab/fields', adminOnly, (req, res) => {
    const pool = dataArtifact('scored_pool.json', []);
    const rows = Array.isArray(pool) ? pool : (pool.stocks || pool.rows || pool.data || pool.ideas || []);
    const fields = Array.from(new Set(rows.slice(0, 100).flatMap(r => Object.keys(r || {})))).sort();
    res.json({ ok: true, fields, count: fields.length });
  });

  app.get('/api/system2/signal-lab/presets', adminOnly, (req, res) => {
    sendDataArtifact(res, 'signal_lab_presets.json', { presets: [] });
  });

  app.post('/api/system2/signal-lab/query', adminOnly, (req, res) => {
    const pool = dataArtifact('scored_pool.json', []);
    const rows = Array.isArray(pool) ? pool : (pool.stocks || pool.rows || pool.data || pool.ideas || []);
    res.json({ ok: true, rows: rows.slice(0, 200), count: rows.length, note: 'Read-only scored pool response' });
  });

  app.post('/api/system2/signal-lab/save-preset', adminOnly, (req, res) => {
    res.json({ ok: false, error: 'Preset writes are disabled in this route shim' });
  });

  app.get('/api/system2/backtest/status/:jobId', adminOnly, (req, res) => {
    const file = `backtest_job_${req.params.jobId}.json`;
    sendDataArtifact(res, file, { status: 'not_found', job_id: req.params.jobId });
  });

  app.post('/api/system2/backtest/run', adminOnly, (req, res) => {
    res.json({ ok: false, status: 'disabled', error: 'Backtest execution route not available in read-only shim' });
  });

  app.post('/api/system2/thesis-check', adminOnly, (req, res) => {
    res.json({ ok: false, error: 'Thesis check route not available' });
  });

  app.post('/api/system2/ai-decision', adminOnly, (req, res) => {
    res.json({ ok: false, error: 'AI decision route not available' });
  });

  app.post('/api/system2/enter-paper-trade', adminOnly, (req, res) => {
    res.status(501).json({ ok: false, error: 'Paper entry route is not available in this deployment' });
  });

  app.get('/api/system2/tape/:ticker', adminOnly, (req, res) => {
    const tape = dataArtifact('tape_state.json', {});
    res.json({ ok: true, ticker: req.params.ticker, tape });
  });

  app.post('/api/system2/weekly-snapshot/generate', adminOnly, (req, res) => {
    sendDataArtifact(res, 'weekly_snapshots.json', { snapshots: [] });
  });
};
