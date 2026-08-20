const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const childProcess = require('child_process');

const DEFAULT_QTY = 1;
const DEFAULT_MAX_ORDERS_PER_DAY = 3;
const ENTRY_LIMIT_BUFFER_PCT = 0.002;
const TERMINAL_AUTO_STATUSES = new Set(['failed', 'skipped', 'exit_filled', 'closed', 'disabled']);
const RECALIBRATED_BRACKET_MODE = 'recalibrated_to_fill';
const NAKED_POSITION_ALERT = 'NAKED_AUTO_POSITION_NO_EXIT_ORDER';
const RISK_BREAKER_ALERT = 'PMF_RISK_BREAKER_TRIPPED';
const DEFAULT_MAX_CONCURRENT_AUTO_POSITIONS = 5;
const DEFAULT_MAX_DAILY_LOSS_R = 3;
const DEFAULT_MAX_POSITIONS_PER_SECTOR = 2;
const DEFAULT_CONSECUTIVE_LOSS_HALT = 5;
const CONFIG_SNAPSHOT_VERSION = 1;
const CONFIG_LEDGER_FILE = 'config_change_ledger.jsonl';
const CONFIG_LATEST_FILE = 'latest_seen_config_hash.json';
const CAP_SELECTION_RULE = 'structural_extension_asc_adv_desc_ticker_asc';

function num(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function roundPrice(value) {
  const n = num(value);
  if (n == null || n <= 0) return null;
  return Number(n >= 1 ? n.toFixed(2) : n.toFixed(4));
}

function isoNow(nowFn) {
  return typeof nowFn === 'function' ? nowFn() : new Date().toISOString();
}

function readEnv(name, readEnvValue) {
  if (process.env[name]) return process.env[name];
  if (typeof readEnvValue === 'function') return readEnvValue(name);
  return null;
}

function envFlag(name, readEnvValue, fallback = false) {
  const raw = readEnv(name, readEnvValue);
  if (raw == null || raw === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(raw).toLowerCase());
}

function loadConfig(readEnvValue) {
  const baseUrl = String(readEnv('ALPACA_PAPER_BASE_URL', readEnvValue) || '').replace(/\/+$/, '');
  if (!baseUrl || !baseUrl.toLowerCase().includes('paper')) {
    throw new Error('Alpaca paper endpoint assertion failed: ALPACA_PAPER_BASE_URL must contain "paper".');
  }
  const maxOrdersPerDay = Number(readEnv('PMF_AUTO_EXEC_MAX_PER_DAY', readEnvValue) || DEFAULT_MAX_ORDERS_PER_DAY);
  return {
    enabled: String(readEnv('PMF_AUTO_EXEC_ENABLED', readEnvValue) || 'false').toLowerCase() === 'true',
    baseUrl,
    apiKey: readEnv('ALPACA_PAPER_API_KEY', readEnvValue),
    apiSecret: readEnv('ALPACA_PAPER_API_SECRET', readEnvValue),
    qty: DEFAULT_QTY,
    maxOrdersPerDay,
    riskBreakersEnabled: envFlag('PMF_RISK_BREAKERS_ENABLED', readEnvValue, true),
    riskKillSwitchEnabled: envFlag('PMF_RISK_KILL_SWITCH_ENABLED', readEnvValue, false),
    riskMaxConcurrentAutoPositions: Number(readEnv('PMF_RISK_MAX_CONCURRENT_AUTO_POSITIONS', readEnvValue) || DEFAULT_MAX_CONCURRENT_AUTO_POSITIONS),
    riskMaxDailyLossR: Number(readEnv('PMF_RISK_MAX_DAILY_LOSS_R', readEnvValue) || DEFAULT_MAX_DAILY_LOSS_R),
    riskMaxPositionsPerSector: Number(readEnv('PMF_RISK_MAX_POSITIONS_PER_SECTOR', readEnvValue) || DEFAULT_MAX_POSITIONS_PER_SECTOR),
    riskConsecutiveLossHalt: Number(readEnv('PMF_RISK_CONSECUTIVE_LOSS_HALT', readEnvValue) || DEFAULT_CONSECUTIVE_LOSS_HALT),
    riskConsecutiveLossResetAt: readEnv('PMF_RISK_CONSECUTIVE_LOSS_RESET_AT', readEnvValue) || null,
    system2Root: readEnv('SYSTEM2_CORE_DIR', readEnvValue) || '/root/system2-core',
    accountTag: envFlag('ALPACA_DEDICATED_ACCOUNT_ENABLED', readEnvValue, false) ? 'dedicated' : 'legacy_shared',
  };
}

function loadLegacyConfig(readEnvValue) {
  const baseUrl = String(readEnv('ALPACA_LEGACY_SHARED_BASE_URL', readEnvValue) || '').replace(/\/+$/, '');
  const apiKey = readEnv('ALPACA_LEGACY_SHARED_API_KEY', readEnvValue);
  const apiSecret = readEnv('ALPACA_LEGACY_SHARED_API_SECRET', readEnvValue);
  if (!baseUrl || !apiKey || !apiSecret) return null;
  if (!baseUrl.toLowerCase().includes('paper')) throw new Error('Legacy Alpaca endpoint assertion failed: ALPACA_LEGACY_SHARED_BASE_URL must contain "paper".');
  return { ...loadConfig(readEnvValue), baseUrl, apiKey, apiSecret, accountTag: 'legacy_shared' };
}

function stableNormalize(value) {
  if (value == null) return null;
  if (Array.isArray(value)) return value.map(stableNormalize);
  if (typeof value === 'number') return Number.isFinite(value) ? Number(value) : null;
  if (typeof value === 'boolean' || typeof value === 'string') return value;
  if (typeof value === 'object') {
    return Object.keys(value).sort().reduce((out, key) => {
      out[key] = stableNormalize(value[key]);
      return out;
    }, {});
  }
  return String(value);
}

function stableStringify(value) {
  return JSON.stringify(stableNormalize(value));
}

function sha256(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

function fileSha256(file) {
  try {
    return fs.existsSync(file) ? crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') : null;
  } catch {
    return null;
  }
}

function readJsonFile(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function selectedCronLines(system2Root) {
  try {
    const cron = childProcess.execSync('crontab -l', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    return cron.split(/\r?\n/)
      .map(line => line.trim())
      .filter(line => line && !line.startsWith('#'))
      .filter(line => /pre-market-gap|pmf-auto-exec|run_phase_b_core_baseline|run_system2_paper_monitor|pead_drift|performance_metrics|validated_fund_backup/.test(line))
      .sort();
  } catch {
    return [];
  }
}

function frozenConfigSnapshot(config, readEnvValue, extra = {}) {
  const system2Root = config.system2Root || readEnv('SYSTEM2_CORE_DIR', readEnvValue) || '/root/system2-core';
  const fundRoot = path.resolve(__dirname, '..');
  const paths = {
    system2_config: path.join(system2Root, 'system2-config.json'),
    core_config: path.join(system2Root, 'config.json'),
    universe_builder: path.join(system2Root, 'universe_builder.py'),
    executor: __filename,
    scoring_endpoints: path.join(__dirname, 'scoring-endpoints.cjs'),
  };
  const env = {
    PMF_AUTO_EXEC_ENABLED: Boolean(config.enabled),
    PMF_AUTO_EXEC_MAX_PER_DAY: Number(config.maxOrdersPerDay),
    PMF_RISK_BREAKERS_ENABLED: Boolean(config.riskBreakersEnabled),
    PMF_RISK_KILL_SWITCH_ENABLED: Boolean(config.riskKillSwitchEnabled),
    PMF_RISK_MAX_CONCURRENT_AUTO_POSITIONS: Number(config.riskMaxConcurrentAutoPositions),
    PMF_RISK_MAX_DAILY_LOSS_R: Number(config.riskMaxDailyLossR),
    PMF_RISK_MAX_POSITIONS_PER_SECTOR: Number(config.riskMaxPositionsPerSector),
    PMF_RISK_CONSECUTIVE_LOSS_HALT: Number(config.riskConsecutiveLossHalt),
    PMF_RISK_CONSECUTIVE_LOSS_RESET_AT: config.riskConsecutiveLossResetAt || null,
    ALPACA_DEDICATED_ACCOUNT_ENABLED: config.accountTag === 'dedicated',
  };
  const snapshot = {
    snapshot_version: CONFIG_SNAPSHOT_VERSION,
    generated_by: 'pmf-auto-executor.config-freeze',
    trading_rules: {
      pmf_threshold_atr_multiple: Number(extra.pmfThresholdAtrMultiple ?? extra.thresholdAtrMultiple ?? 1.5),
      pmf_price_source_fallback_order: extra.priceSourceFallbackOrder || ['preMarketPrice', 'price', 'close', 'previousClose'],
      gap_check_cron_times_utc: extra.gapCheckCronTimesUtc || { primary: '14:00', late: '15:30' },
      gap_check_cohort: extra.gapCheckCohort || null,
      entry_order_type: 'limit',
      entry_time_in_force: 'day',
      entry_limit_buffer_pct: ENTRY_LIMIT_BUFFER_PCT,
      order_flow: 'entry_then_recalibrated_oco',
      bracket_mode: RECALIBRATED_BRACKET_MODE,
      exit_order_class: 'oco',
      exit_time_in_force: 'gtc',
      stop_target_geometry: 'derived_per_idea_from_original_entry_stop_target_and_atr',
      qty: DEFAULT_QTY,
      daily_cap_selection: CAP_SELECTION_RULE,
      daily_cap_primary_key: 'favourable_direction_stamp_vs_modeled_entry_pct_ascending',
      daily_cap_tie_break: 'stamp_time_average_dollar_volume_descending_then_ticker_ascending',
    },
    auto_exec_config: env,
    risk_defaults: {
      DEFAULT_MAX_ORDERS_PER_DAY,
      DEFAULT_MAX_CONCURRENT_AUTO_POSITIONS,
      DEFAULT_MAX_DAILY_LOSS_R,
      DEFAULT_MAX_POSITIONS_PER_SECTOR,
      DEFAULT_CONSECUTIVE_LOSS_HALT,
    },
    selection_scoring_config: {
      system2_config: readJsonFile(paths.system2_config, null),
      core_config: readJsonFile(paths.core_config, null),
      tracked_files: paths,
      file_hashes: Object.fromEntries(Object.entries(paths).map(([key, file]) => [key, fileSha256(file)])),
      cron_relevant_lines: selectedCronLines(system2Root),
    },
    runtime: {
      node: process.version,
      fund_root: fundRoot,
      system2_root: system2Root,
      alpaca_paper_endpoint_asserted: String(config.baseUrl || '').toLowerCase().includes('paper'),
    },
  };
  return stableNormalize(snapshot);
}

function configHash(snapshot) {
  return sha256(stableStringify(snapshot));
}

function snapshotDir(system2Root) {
  return path.join(system2Root || '/root/system2-core', 'config_snapshots');
}

function ensureConfigSnapshot(snapshot, system2Root) {
  const hash = configHash(snapshot);
  const dir = snapshotDir(system2Root);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${hash}.json`);
  if (!fs.existsSync(file)) {
    fs.writeFileSync(file, `${JSON.stringify(snapshot, null, 2)}\n`);
  }
  return { hash, snapshotRef: file, snapshot };
}

function currentConfigSnapshot(readEnvValue, extra = {}) {
  const config = loadConfig(readEnvValue);
  const snapshot = frozenConfigSnapshot(config, readEnvValue, extra);
  return ensureConfigSnapshot(snapshot, config.system2Root);
}

function flattenSnapshot(value, prefix = '', out = {}) {
  if (Array.isArray(value)) {
    out[prefix] = stableStringify(value);
    return out;
  }
  if (value && typeof value === 'object') {
    for (const key of Object.keys(value).sort()) {
      flattenSnapshot(value[key], prefix ? `${prefix}.${key}` : key, out);
    }
    return out;
  }
  out[prefix] = stableStringify(value);
  return out;
}

function diffConfigSnapshots(oldSnapshot, newSnapshot) {
  const a = flattenSnapshot(stableNormalize(oldSnapshot || {}));
  const b = flattenSnapshot(stableNormalize(newSnapshot || {}));
  const fields = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
  return fields
    .filter(field => a[field] !== b[field])
    .map(field => ({ field, old_value: a[field] == null ? null : JSON.parse(a[field]), new_value: b[field] == null ? null : JSON.parse(b[field]) }));
}

function appendJsonl(file, row) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(row)}\n`);
}

function detectConfigDrift(readEnvValue, extra = {}) {
  const meta = currentConfigSnapshot(readEnvValue, extra);
  const system2Root = loadConfig(readEnvValue).system2Root;
  const dir = snapshotDir(system2Root);
  const latestFile = path.join(dir, CONFIG_LATEST_FILE);
  const previous = readJsonFile(latestFile, null);
  const latest = { hash: meta.hash, snapshot_ref: meta.snapshotRef, checked_at: new Date().toISOString() };
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(latestFile, `${JSON.stringify(latest, null, 2)}\n`);
  if (!previous || !previous.hash || previous.hash === meta.hash) {
    return { changed: false, current_hash: meta.hash, current_snapshot_ref: meta.snapshotRef, previous_hash: previous?.hash || null, fields_changed: [] };
  }
  const previousSnapshot = readJsonFile(previous.snapshot_ref || path.join(dir, `${previous.hash}.json`), {});
  const fieldsChanged = diffConfigSnapshots(previousSnapshot, meta.snapshot);
  const ledgerFile = path.join(system2Root, 'logs', CONFIG_LEDGER_FILE);
  const row = {
    at: latest.checked_at,
    event: 'CONFIG_CHANGED',
    old_hash: previous.hash,
    new_hash: meta.hash,
    old_snapshot_ref: previous.snapshot_ref || null,
    new_snapshot_ref: meta.snapshotRef,
    fields_changed: fieldsChanged,
  };
  appendJsonl(ledgerFile, row);
  console.error(`[CONFIG_CHANGED] ${previous.hash} -> ${meta.hash} | fields: ${fieldsChanged.map(f => f.field).join(', ')}`);
  return { changed: true, current_hash: meta.hash, current_snapshot_ref: meta.snapshotRef, previous_hash: previous.hash, previous_snapshot_ref: previous.snapshot_ref || null, fields_changed: fieldsChanged, ledger_ref: ledgerFile };
}

function stampConfigOnIdea(idea, meta, nowFn) {
  if (!idea || !meta) return;
  idea.config_hash = meta.hash;
  idea.config_snapshot_ref = meta.snapshotRef;
  idea.config_hash_time = isoNow(nowFn);
}

function authHeaders(config) {
  if (!config.apiKey || !config.apiSecret) throw new Error('Missing ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET.');
  return {
    'Content-Type': 'application/json',
    'APCA-API-KEY-ID': config.apiKey,
    'APCA-API-SECRET-KEY': config.apiSecret,
  };
}

function ideaTicker(idea) {
  return String(idea?.ticker || idea?.symbol || '').toUpperCase();
}

function effectiveEntry(idea) {
  return num(idea?.original_entry ?? idea?.entry ?? idea?.planned_entry ?? idea?.idea_entry_price);
}

function effectiveStop(idea) {
  return num(idea?.original_stop ?? idea?.stop ?? idea?.stopLoss);
}

function effectiveTarget(idea) {
  return num(idea?.original_target ?? idea?.target ?? idea?.tp1);
}

function effectiveRisk(idea) {
  const actualEntry = num(idea?.actual_entry_price);
  const recalibratedStop = num(idea?.recalibrated_stop);
  if (idea?.bracket_mode === RECALIBRATED_BRACKET_MODE && actualEntry != null && recalibratedStop != null) {
    return Math.abs(actualEntry - recalibratedStop);
  }
  const explicit = num(idea?.original_risk_per_share ?? idea?.risk_per_share);
  if (explicit && explicit > 0) return explicit;
  const entry = effectiveEntry(idea);
  const stop = effectiveStop(idea);
  return entry != null && stop != null ? Math.abs(entry - stop) : null;
}

function referencePrice(idea) {
  return num(idea?.pre_market_price ?? idea?.current_price ?? idea?.price ?? effectiveEntry(idea));
}

function structuralExtensionPct(idea) {
  const entry = effectiveEntry(idea);
  const stampPrice = num(idea?.pmf_price_at_stamp ?? idea?.pre_market_price_at_fill ?? idea?.pre_market_price ?? referencePrice(idea));
  const target = effectiveTarget(idea);
  if (!(entry > 0 && stampPrice > 0)) return null;
  const isShort = target != null && target < entry;
  const favourableExtension = isShort ? (entry - stampPrice) / entry : (stampPrice - entry) / entry;
  return Number((favourableExtension * 100).toFixed(4));
}

function stampAverageDollarVolume(idea) {
  return num(idea?.pmf_average_dollar_volume_at_stamp ?? idea?.pmf_adv_at_stamp);
}

function stampCompleteness(idea) {
  return [
    idea?.pmf_confirmed_at_stamp,
    idea?.pmf_confirmed_at_stamp_time,
    idea?.pmf_price_at_stamp,
    idea?.pmf_atr_at_stamp,
    effectiveEntry(idea),
    effectiveStop(idea),
    effectiveTarget(idea),
    stampAverageDollarVolume(idea),
  ].filter(value => value !== null && value !== undefined && value !== '').length;
}

function deterministicIdeaId(idea) {
  return String(idea?.id ?? idea?.idea_id ?? '');
}

function deduplicateCapCandidates(ideas = []) {
  const groups = new Map();
  for (const idea of ideas) {
    const ticker = ideaTicker(idea);
    const key = ticker || `__missing__${deterministicIdeaId(idea)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(idea);
  }
  const kept = [];
  for (const group of groups.values()) {
    group.sort((a, b) => {
      const extensionA = structuralExtensionPct(a);
      const extensionB = structuralExtensionPct(b);
      if (extensionA == null && extensionB != null) return 1;
      if (extensionA != null && extensionB == null) return -1;
      if (extensionA != null && extensionB != null && extensionA !== extensionB) return extensionA - extensionB;
      const completeness = stampCompleteness(b) - stampCompleteness(a);
      if (completeness) return completeness;
      return deterministicIdeaId(a).localeCompare(deterministicIdeaId(b), undefined, { numeric: true });
    });
    const survivor = group[0];
    kept.push(survivor);
    for (const suppressed of group.slice(1)) {
      suppressed.dedup_suppressed = true;
      suppressed.dedup_kept_id = survivor?.id ?? survivor?.idea_id ?? null;
      suppressed.pmf_cap_rank = null;
      suppressed.pmf_cap_selected = false;
      appendAudit(suppressed, { event: 'dedup_suppressed', ticker: ideaTicker(suppressed), dedup_kept_id: suppressed.dedup_kept_id });
    }
  }
  return kept;
}

function rankCapCandidates(ideas = [], nowFn) {
  const ranked = deduplicateCapCandidates(ideas).sort((a, b) => {
    const extensionA = structuralExtensionPct(a);
    const extensionB = structuralExtensionPct(b);
    if (extensionA == null && extensionB != null) return 1;
    if (extensionA != null && extensionB == null) return -1;
    if (extensionA != null && extensionB != null && extensionA !== extensionB) return extensionA - extensionB;
    const advA = stampAverageDollarVolume(a);
    const advB = stampAverageDollarVolume(b);
    if (advA == null && advB != null) return 1;
    if (advA != null && advB == null) return -1;
    if (advA != null && advB != null && advA !== advB) return advB - advA;
    return ideaTicker(a).localeCompare(ideaTicker(b));
  });
  ranked.forEach((idea, index) => {
    idea.pmf_cap_rank = index + 1;
    idea.pmf_cap_candidate_count = ranked.length;
    idea.pmf_structural_extension_at_stamp_pct = structuralExtensionPct(idea);
    idea.pmf_cap_selection_rule = CAP_SELECTION_RULE;
    idea.pmf_cap_ranked_at = isoNow(nowFn);
  });
  return ranked;
}

function effectiveAtr(idea) {
  return num(idea?.atr_at_fill ?? idea?.pmf_atr_at_stamp ?? idea?.atr14 ?? idea?.atr ?? idea?.original_atr ?? idea?.atr_at_signal ?? effectiveRisk(idea));
}

function pmfAtrMultipleAtFill(idea) {
  const entry = effectiveEntry(idea);
  const target = effectiveTarget(idea);
  const ref = referencePrice(idea);
  const atr = effectiveAtr(idea);
  if (!(entry > 0 && ref > 0 && atr > 0)) return null;
  const isShort = target < entry;
  const favourableMove = isShort ? entry - ref : ref - entry;
  return Number((favourableMove / atr).toFixed(3));
}

function writePmfAtFillStamp(idea, nowFn) {
  if (!idea || idea.pmf_confirmed_at_fill != null) return;
  const entry = effectiveEntry(idea);
  const atr = effectiveAtr(idea);
  const ref = referencePrice(idea);
  idea.pmf_confirmed_at_fill = true;
  idea.pmf_atr_multiple_at_fill = pmfAtrMultipleAtFill(idea);
  idea.pmf_entry_cohort = idea.pmf_cohort === 'PMF_LATE' ? 'PMF_LATE' : 'PMF';
  idea.pmf_confirmed_at_fill_time = isoNow(nowFn);
  idea.pre_market_price_at_fill = ref;
  idea.entry_at_fill = entry;
  idea.atr_at_fill = atr;
}

function intendedAtrGeometry(idea) {
  const entry = effectiveEntry(idea);
  const stop = effectiveStop(idea);
  const target = effectiveTarget(idea);
  const atr = effectiveAtr(idea);
  if (!(entry > 0 && stop > 0 && target > 0 && atr > 0)) throw new Error('missing entry/stop/target/atr for bracket geometry');
  return {
    entry,
    stop,
    target,
    atr,
    isShort: target < entry,
    stopAtr: Math.abs(entry - stop) / atr,
    targetAtr: Math.abs(target - entry) / atr,
    intendedRewardToRisk: Math.abs(target - entry) / Math.abs(entry - stop),
  };
}

function recalibratedExitLevels(idea, actualFill, currentPrice = actualFill) {
  const fill = num(actualFill);
  const ref = num(currentPrice);
  if (!(fill > 0 && ref > 0)) throw new Error('missing actual fill/current price for recalibrated bracket');
  const geometry = intendedAtrGeometry(idea);
  const stop = geometry.isShort ? fill + geometry.stopAtr * geometry.atr : fill - geometry.stopAtr * geometry.atr;
  const target = geometry.isShort ? fill - geometry.targetAtr * geometry.atr : fill + geometry.targetAtr * geometry.atr;
  if (!geometry.isShort && !(stop < ref && target > ref)) {
    throw new Error(`invalid recalibrated long OCO: expected stop < current price < target (${roundPrice(stop)} < ${roundPrice(ref)} < ${roundPrice(target)})`);
  }
  if (geometry.isShort && !(target < ref && stop > ref)) {
    throw new Error(`invalid recalibrated short OCO: expected target < current price < stop (${roundPrice(target)} < ${roundPrice(ref)} < ${roundPrice(stop)})`);
  }
  return {
    ...geometry,
    actualFill: fill,
    currentPrice: ref,
    stop,
    target,
    bracket_mode: RECALIBRATED_BRACKET_MODE,
  };
}

function buildEntryOrder(idea, config) {
  const symbol = ideaTicker(idea);
  const entry = effectiveEntry(idea);
  const stop = effectiveStop(idea);
  const target = effectiveTarget(idea);
  const ref = referencePrice(idea);
  if (!symbol) throw new Error('missing ticker');
  if (!(entry > 0 && stop > 0 && target > 0 && ref > 0)) throw new Error('missing entry/stop/target/reference price');
  const isShort = target < entry;
  if (!isShort && !(stop < ref && target > ref)) throw new Error('invalid long bracket: expected stop < reference price < target');
  if (isShort && !(target < ref && stop > ref)) throw new Error('invalid short bracket: expected target < reference price < stop');
  const side = isShort ? 'sell' : 'buy';
  const limit = roundPrice(isShort ? ref * (1 - ENTRY_LIMIT_BUFFER_PCT) : ref * (1 + ENTRY_LIMIT_BUFFER_PCT));
  return {
    endpoint: `${config.baseUrl}/orders`,
    paper_endpoint_asserted: config.baseUrl.toLowerCase().includes('paper'),
    payload: {
      symbol,
      side,
      type: 'limit',
      limit_price: String(limit),
      qty: String(config.qty),
      time_in_force: 'day',
      client_order_id: `pmf_auto_${String(idea.id || symbol).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 32)}_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`,
    },
    model: { entry, stop, target, reference_price: ref, entry_limit_buffer_pct: ENTRY_LIMIT_BUFFER_PCT, qty: config.qty, order_flow: 'entry_then_recalibrated_oco' },
  };
}

function buildOcoExitOrder(idea, config, actualFill, filledQty) {
  const symbol = ideaTicker(idea);
  if (!symbol) throw new Error('missing ticker');
  const levels = recalibratedExitLevels(idea, actualFill, actualFill);
  const side = levels.isShort ? 'buy' : 'sell';
  const qty = num(filledQty) || config.qty;
  const takeProfit = roundPrice(levels.target);
  const stopLoss = roundPrice(levels.stop);
  if (!(qty > 0 && takeProfit > 0 && stopLoss > 0)) throw new Error('invalid recalibrated OCO qty/stop/target');
  return {
    endpoint: `${config.baseUrl}/orders`,
    paper_endpoint_asserted: config.baseUrl.toLowerCase().includes('paper'),
    payload: {
      symbol,
      side,
      type: 'limit',
      qty: String(qty),
      time_in_force: 'gtc',
      order_class: 'oco',
      take_profit: { limit_price: String(takeProfit) },
      stop_loss: { stop_price: String(stopLoss) },
      client_order_id: `pmf_oco_${String(idea.id || symbol).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 32)}_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`,
    },
    model: {
      bracket_mode: RECALIBRATED_BRACKET_MODE,
      actual_fill: levels.actualFill,
      current_price: levels.currentPrice,
      atr: levels.atr,
      stop: levels.stop,
      target: levels.target,
      stop_atr_mult: levels.stopAtr,
      target_atr_mult: levels.targetAtr,
      intended_reward_to_risk: levels.intendedRewardToRisk,
      recalibrated_reward_to_risk: Math.abs(levels.target - levels.actualFill) / Math.abs(levels.actualFill - levels.stop),
      qty,
    },
  };
}

function buildBracketOrder(idea, config) {
  return buildEntryOrder(idea, config);
}

function appendAudit(idea, event) {
  if (!idea) return;
  if (!Array.isArray(idea.auto_exec_audit)) idea.auto_exec_audit = [];
  idea.auto_exec_audit.push({ at: new Date().toISOString(), ...event });
  if (idea.auto_exec_audit.length > 50) idea.auto_exec_audit = idea.auto_exec_audit.slice(-50);
}

function loudAutoExecAlert(idea, message, extra = {}) {
  const ticker = ideaTicker(idea) || 'UNKNOWN';
  const alert = { event: 'auto_exec_alert', alert: extra.alert || NAKED_POSITION_ALERT, ticker, message, ...extra };
  appendAudit(idea, alert);
  console.error(`[PMF_AUTO_EXEC_ALERT] ${ticker}: ${message}`);
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function sameTradingDay(value) {
  return String(value || '').slice(0, 10) === todayIso();
}

function hasOpenAutoPosition(allIdeas, ticker) {
  return (allIdeas || []).some(row => {
    if (ideaTicker(row) !== ticker) return false;
    if (!row.alpaca_order_id && row.fill_source !== 'alpaca_paper') return false;
    if (row.actual_exit_price || row.actual_exit_time) return false;
    const status = String(row.auto_exec_status || '').toLowerCase();
    return !TERMINAL_AUTO_STATUSES.has(status);
  });
}

function isOpenAutoPosition(row) {
  if (!row) return false;
  if (!row.alpaca_order_id && row.fill_source !== 'alpaca_paper') return false;
  if (row.actual_exit_price || row.actual_exit_time) return false;
  const status = String(row.auto_exec_status || '').toLowerCase();
  return !TERMINAL_AUTO_STATUSES.has(status);
}

function ordersToday(allIdeas) {
  return (allIdeas || []).filter(row => row.alpaca_order_id && sameTradingDay(row.auto_exec_at)).length;
}

function orderedTickerToday(allIdeas, ticker) {
  return (allIdeas || []).some(row => ideaTicker(row) === ticker && row.alpaca_order_id && sameTradingDay(row.auto_exec_at));
}

function ideaSector(idea) {
  return String(idea?.sector || idea?.gics_sector || idea?.profile_sector || idea?.company_sector || 'UNKNOWN');
}

function realisedTodayR(allIdeas) {
  return (allIdeas || [])
    .filter(row => row.fill_source === 'alpaca_paper' && !row.test_order && sameTradingDay(row.actual_exit_time))
    .reduce((sum, row) => sum + (num(row.real_r) || 0), 0);
}

function openUnrealizedR(allIdeas) {
  return (allIdeas || [])
    .filter(isOpenAutoPosition)
    .reduce((sum, row) => {
      const entry = num(row.actual_entry_price);
      const current = num(row.current_price ?? row.last_price ?? row.pre_market_price);
      const risk = effectiveRisk(row);
      if (!(entry > 0 && current > 0 && risk > 0)) return sum;
      const isShort = effectiveTarget(row) < effectiveEntry(row);
      const raw = isShort ? (entry - current) / risk : (current - entry) / risk;
      return sum + raw;
    }, 0);
}

function resolvedAutoRows(allIdeas, resetAt = null) {
  const resetMs = Date.parse(resetAt || 0) || 0;
  return (allIdeas || [])
    .filter(row => row.fill_source === 'alpaca_paper' && !row.test_order && row.actual_exit_time && num(row.real_r) != null)
    .filter(row => (Date.parse(row.actual_exit_time) || 0) > resetMs)
    .sort((a, b) => (Date.parse(b.actual_exit_time) || 0) - (Date.parse(a.actual_exit_time) || 0));
}

function consecutiveLosses(allIdeas, resetAt = null) {
  let count = 0;
  for (const row of resolvedAutoRows(allIdeas, resetAt)) {
    const r = num(row.real_r);
    if (r == null || r >= 0) break;
    count += 1;
  }
  return count;
}

function exposureSnapshot(allIdeas, candidate = null) {
  const openRows = (allIdeas || []).filter(isOpenAutoPosition);
  const sector = ideaSector(candidate);
  const sectorOpen = openRows.filter(row => ideaSector(row) === sector).length;
  const realisedR = realisedTodayR(allIdeas);
  const openR = openUnrealizedR(allIdeas);
  return {
    open_auto_positions: openRows.length,
    open_symbols: openRows.map(ideaTicker).filter(Boolean).sort(),
    candidate_sector: sector,
    open_positions_in_candidate_sector: sectorOpen,
    today_realized_r: Number(realisedR.toFixed(3)),
    open_unrealized_r_estimate: Number(openR.toFixed(3)),
    today_realized_plus_open_r: Number((realisedR + openR).toFixed(3)),
  };
}

function writeRiskBreakerAlert(config, payload) {
  try {
    const dir = path.join(config.system2Root || '/root/system2-core', 'logs');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'pmf_risk_breaker_alert.json'), JSON.stringify(payload, null, 2));
    fs.appendFileSync(path.join(dir, 'pmf_risk_breakers.log'), `${payload.at} ${JSON.stringify(payload)}\n`);
  } catch {}
}

function riskBreakerBlock(config, allIdeas, idea, breaker, reason, extra = {}) {
  const payload = {
    event: RISK_BREAKER_ALERT,
    at: isoNow(),
    ticker: ideaTicker(idea),
    breaker,
    reason,
    exposure_snapshot: exposureSnapshot(allIdeas, idea),
    ...extra,
  };
  appendAudit(idea, { event: 'risk_breaker_blocked', ...payload });
  if (!extra.dryRun) writeRiskBreakerAlert(config, payload);
  return payload;
}

function evaluateRiskBreakers(config, allIdeas, idea, placedToday, dryRun = false) {
  if (!config.riskBreakersEnabled) return null;
  if (config.riskKillSwitchEnabled) {
    return riskBreakerBlock(config, allIdeas, idea, 'kill_switch', 'PMF_RISK_KILL_SWITCH_ENABLED=true', { dryRun });
  }
  if (placedToday >= config.maxOrdersPerDay) {
    return riskBreakerBlock(config, allIdeas, idea, 'daily_order_cap', `daily order cap reached (${config.maxOrdersPerDay})`, { placed_today: placedToday, dryRun });
  }
  const snap = exposureSnapshot(allIdeas, idea);
  if (Number.isFinite(config.riskMaxConcurrentAutoPositions) && config.riskMaxConcurrentAutoPositions >= 0 && snap.open_auto_positions >= config.riskMaxConcurrentAutoPositions) {
    return riskBreakerBlock(config, allIdeas, idea, 'max_concurrent_auto_positions', `open auto positions ${snap.open_auto_positions} >= cap ${config.riskMaxConcurrentAutoPositions}`, { dryRun });
  }
  if (Number.isFinite(config.riskMaxDailyLossR) && config.riskMaxDailyLossR > 0 && snap.today_realized_plus_open_r <= -Math.abs(config.riskMaxDailyLossR)) {
    return riskBreakerBlock(config, allIdeas, idea, 'max_daily_loss_r', `today realized+open R ${snap.today_realized_plus_open_r} <= -${Math.abs(config.riskMaxDailyLossR)}`, { dryRun });
  }
  if (Number.isFinite(config.riskMaxPositionsPerSector) && config.riskMaxPositionsPerSector >= 0 && snap.open_positions_in_candidate_sector >= config.riskMaxPositionsPerSector) {
    return riskBreakerBlock(config, allIdeas, idea, 'max_positions_per_sector', `${snap.candidate_sector} open positions ${snap.open_positions_in_candidate_sector} >= cap ${config.riskMaxPositionsPerSector}`, { dryRun });
  }
  const losses = consecutiveLosses(allIdeas, config.riskConsecutiveLossResetAt);
  if (Number.isFinite(config.riskConsecutiveLossHalt) && config.riskConsecutiveLossHalt > 0 && losses >= config.riskConsecutiveLossHalt) {
    return riskBreakerBlock(config, allIdeas, idea, 'consecutive_loss_halt', `${losses} consecutive resolved losses >= halt ${config.riskConsecutiveLossHalt}`, { consecutive_losses: losses, manual_reset_env: 'PMF_RISK_CONSECUTIVE_LOSS_RESET_AT', dryRun });
  }
  return null;
}

async function alpacaGet(config, route) {
  const res = await fetch(`${config.baseUrl}${route}`, { headers: authHeaders(config) });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(`Alpaca GET ${route} ${res.status}: ${text.slice(0, 300)}`);
  return body;
}

async function alpacaPost(config, route, payload) {
  const res = await fetch(`${config.baseUrl}${route}`, { method: 'POST', headers: authHeaders(config), body: JSON.stringify(payload) });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(`Alpaca POST ${route} ${res.status}: ${text.slice(0, 300)}`);
  return body;
}

async function alpacaGetOptional(config, route) {
  const res = await fetch(`${config.baseUrl}${route}`, { headers: authHeaders(config) });
  const text = await res.text();
  if (res.status === 404) return null;
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(`Alpaca GET ${route} ${res.status}: ${text.slice(0, 300)}`);
  return body;
}

async function marketIsOpen(config) {
  const clock = await alpacaGet(config, '/clock');
  return clock?.is_open === true;
}

async function pollOrder(config, orderId, attempts = 6, delayMs = 1500) {
  for (let i = 0; i < attempts; i++) {
    const order = await alpacaGet(config, `/orders/${orderId}?nested=true`);
    if (order?.filled_avg_price || String(order?.status || '').toLowerCase() === 'filled') return order;
    await new Promise(resolve => setTimeout(resolve, delayMs));
  }
  return alpacaGet(config, `/orders/${orderId}?nested=true`);
}

function writeEntryFill(idea, order, preview, nowFn) {
  const filled = num(order?.filled_avg_price);
  idea.alpaca_order_id = order?.id || idea.alpaca_order_id;
  idea.alpaca_client_order_id = order?.client_order_id || preview.payload.client_order_id;
  idea.auto_exec_at = idea.auto_exec_at || isoNow(nowFn);
  idea.auto_exec_status = order?.status || 'submitted';
  idea.auto_exec_order_class = preview?.model?.order_flow === 'entry_then_recalibrated_oco' ? 'entry_then_oco' : 'bracket';
  idea.auto_exec_qty = DEFAULT_QTY;
  idea.fill_source = 'alpaca_paper';
  idea.account = preview?.accountTag || idea.account || 'legacy_shared';
  idea.cohort_label = 'B_CLEAN';
  if (filled != null) {
    if (idea.paper_status === 'CLOSED' || idea.paper_exit_reason || idea.paper_exit_price != null) {
      idea.modeled_paper_status = idea.modeled_paper_status ?? idea.paper_status ?? null;
      idea.modeled_paper_outcome = idea.modeled_paper_outcome ?? idea.paper_outcome ?? null;
      idea.modeled_paper_exit_reason = idea.modeled_paper_exit_reason ?? idea.paper_exit_reason ?? null;
      idea.modeled_paper_exit_at = idea.modeled_paper_exit_at ?? idea.paper_exit_at ?? null;
      idea.modeled_paper_exit_price = idea.modeled_paper_exit_price ?? idea.paper_exit_price ?? null;
      idea.modeled_paper_exit_r = idea.modeled_paper_exit_r ?? idea.paper_exit_r ?? null;
    }
    idea.paper_status = 'RECONCILIATION_PENDING';
    idea.paper_outcome = null;
    idea.paper_exit_reason = null;
    idea.paper_exit_at = null;
    idea.paper_exit_price = null;
    idea.paper_exit_r = null;
    const modelEntry = effectiveEntry(idea);
    idea.actual_entry_price = filled;
    idea.actual_entry_time = order?.filled_at || order?.updated_at || isoNow(nowFn);
    idea.trade_entered = true;
    idea.entry_slippage_vs_model = modelEntry ? Number((((filled - modelEntry) / modelEntry) * 100).toFixed(3)) : null;
    idea.entry_trigger_price = filled;
    idea.true_r_fill_source = 'alpaca_paper';
    idea.true_r_estimated_fill = false;
  }
}

function normalizeBrokerPendingState(idea) {
  if (num(idea?.actual_entry_price) == null || num(idea?.actual_exit_price) != null) return false;
  if (idea.paper_status !== 'CLOSED' && !idea.paper_exit_reason && idea.paper_exit_price == null) return false;
  idea.modeled_paper_status = idea.modeled_paper_status ?? idea.paper_status ?? null;
  idea.modeled_paper_outcome = idea.modeled_paper_outcome ?? idea.paper_outcome ?? null;
  idea.modeled_paper_exit_reason = idea.modeled_paper_exit_reason ?? idea.paper_exit_reason ?? null;
  idea.modeled_paper_exit_at = idea.modeled_paper_exit_at ?? idea.paper_exit_at ?? null;
  idea.modeled_paper_exit_price = idea.modeled_paper_exit_price ?? idea.paper_exit_price ?? null;
  idea.modeled_paper_exit_r = idea.modeled_paper_exit_r ?? idea.paper_exit_r ?? null;
  idea.paper_status = 'RECONCILIATION_PENDING';
  idea.paper_outcome = null;
  idea.paper_exit_reason = null;
  idea.paper_exit_at = null;
  idea.paper_exit_price = null;
  idea.paper_exit_r = null;
  appendAudit(idea, { event: 'broker_state_normalized', reason: 'actual entry is open; modeled paper close preserved separately' });
  return true;
}

function persistRecalibratedBracket(idea, exitPreview, exitOrder) {
  idea.bracket_mode = RECALIBRATED_BRACKET_MODE;
  idea.recalibrated_stop = Number(exitPreview.model.stop.toFixed(4));
  idea.recalibrated_target = Number(exitPreview.model.target.toFixed(4));
  idea.recalibrated_stop_atr_mult = Number(exitPreview.model.stop_atr_mult.toFixed(4));
  idea.recalibrated_target_atr_mult = Number(exitPreview.model.target_atr_mult.toFixed(4));
  idea.recalibrated_reward_to_risk = Number(exitPreview.model.recalibrated_reward_to_risk.toFixed(4));
  idea.recalibrated_oco_order_id = exitOrder?.id || idea.recalibrated_oco_order_id || null;
  idea.recalibrated_oco_client_order_id = exitOrder?.client_order_id || exitPreview.payload.client_order_id;
  idea.auto_exec_order_class = 'entry_then_oco';
  const orders = [exitOrder, ...(Array.isArray(exitOrder?.legs) ? exitOrder.legs : [])].filter(Boolean);
  const stopOrder = orders.find(order => String(order?.type || '').toLowerCase().startsWith('stop') || order?.stop_price);
  const targetOrder = orders.find(order => order?.id && order?.id !== stopOrder?.id && (String(order?.type || '').toLowerCase() === 'limit' || order?.limit_price));
  idea.recalibrated_oco_leg_ids = orders.map(order => order?.id).filter(Boolean);
  idea.recalibrated_oco_stop_order_id = stopOrder?.id || idea.recalibrated_oco_stop_order_id || null;
  idea.recalibrated_oco_target_order_id = targetOrder?.id || idea.recalibrated_oco_target_order_id || null;
}

function markBracketAttachFailed(idea, error) {
  const reason = error?.message || String(error);
  idea.auto_exec_status = 'bracket_attach_failed';
  idea.auto_exec_last_error = reason;
  idea.auto_exec_alert = NAKED_POSITION_ALERT;
  idea.auto_exec_alert_at = new Date().toISOString();
  loudAutoExecAlert(idea, `entry filled but recalibrated OCO attach failed: ${reason}`, { alert: NAKED_POSITION_ALERT, reason });
}

function ocoOrderIds(idea, parentOrder) {
  const parentMatchesStoredOco = parentOrder?.id && parentOrder.id === idea?.recalibrated_oco_order_id;
  const ids = new Set([
    idea?.recalibrated_oco_order_id,
    idea?.recalibrated_oco_stop_order_id,
    idea?.recalibrated_oco_target_order_id,
    ...(Array.isArray(idea?.recalibrated_oco_leg_ids) ? idea.recalibrated_oco_leg_ids : []),
    ...(parentMatchesStoredOco ? [parentOrder.id] : []),
    ...(parentMatchesStoredOco && Array.isArray(parentOrder?.legs) ? parentOrder.legs.map(leg => leg?.id) : []),
  ].filter(Boolean));
  return ids;
}

function classifyExitOrder(idea, leg, parentOrder = null) {
  const storedOcoId = idea?.recalibrated_oco_order_id;
  const ids = ocoOrderIds(idea, parentOrder);
  const orderId = leg?.id;
  if (storedOcoId || ids.size) {
    if (!orderId || !ids.has(orderId)) return { reason: 'EXTERNAL_SELL', attribution: 'external_order_id', externalClose: true };
    const type = String(leg?.type || '').toLowerCase();
    if (orderId === idea?.recalibrated_oco_stop_order_id || type.startsWith('stop') || leg?.stop_price) return { reason: 'STOP', attribution: 'oco_leg_id', externalClose: false };
    return { reason: 'TARGET', attribution: 'oco_leg_id', externalClose: false };
  }
  return { reason: inferExitReason(leg) || 'ALPACA_SELL', attribution: 'inferred_legacy', externalClose: false };
}

function writeExitFill(idea, parentOrder, leg, nowFn) {
  const exitPrice = num(leg?.filled_avg_price || leg?.limit_price || leg?.stop_price);
  if (exitPrice == null) return false;
  const entry = num(idea.actual_entry_price);
  const risk = effectiveRisk(idea);
  const isShort = effectiveTarget(idea) < effectiveEntry(idea);
  idea.actual_exit_price = exitPrice;
  idea.actual_exit_time = leg?.filled_at || leg?.updated_at || isoNow(nowFn);
  const classification = classifyExitOrder(idea, leg, parentOrder);
  const externalAlertNeeded = classification.externalClose && !idea.external_close_alerted_at;
  idea.actual_exit_reason = classification.reason;
  idea.paper_status = 'CLOSED';
  idea.paper_outcome = classification.reason === 'TARGET' ? 'WIN' : 'LOSS';
  idea.exit_attribution = classification.attribution;
  idea.external_close = classification.externalClose;
  if (classification.externalClose) {
    if (idea.cohort_label === 'B_CLEAN') idea.cohort_label = 'B_CONTAMINATED_MANUAL_EXITS';
    if (externalAlertNeeded) {
      idea.external_close_alerted_at = isoNow(nowFn);
      idea.external_close_alert_version = 1;
    }
  }
  idea.alpaca_exit_order_id = leg?.id || null;
  idea.auto_exec_status = 'exit_filled';
  idea.real_r_fill_source = 'alpaca_paper';
  if (entry != null && risk > 0) {
    const raw = isShort ? (entry - exitPrice) / risk : (exitPrice - entry) / risk;
    idea.real_r = Number(raw.toFixed(3));
  }
  appendAudit(idea, { event: 'exit_filled', parent_order_id: parentOrder?.id, exit_order_id: leg?.id, reason: idea.actual_exit_reason, exit_attribution: idea.exit_attribution, external_close: idea.external_close, price: exitPrice });
  if (externalAlertNeeded) {
    idea.auto_exec_alert = 'EXTERNAL_CLOSE_DETECTED';
    idea.auto_exec_alert_at = isoNow(nowFn);
    loudAutoExecAlert(idea, 'EXTERNAL CLOSE DETECTED — experiment contaminated', { alert: 'EXTERNAL_CLOSE_DETECTED', exit_order_id: leg?.id });
  }
  return { written: true, classification, externalAlertNeeded };
}

function orderTime(order) {
  return Date.parse(order?.filled_at || order?.updated_at || order?.created_at || order?.submitted_at || 0) || 0;
}

function isFilled(order) {
  return String(order?.status || '').toLowerCase() === 'filled' || num(order?.filled_avg_price) != null;
}

function isExitSideForIdea(idea, order) {
  const isShort = effectiveTarget(idea) < effectiveEntry(idea);
  const side = String(order?.side || '').toLowerCase();
  return isShort ? side === 'buy' : side === 'sell';
}

function inferExitReason(order) {
  const type = String(order?.type || '').toLowerCase();
  if (type === 'stop' || type === 'stop_limit' || order?.stop_price) return 'STOP';
  if (type === 'limit' || order?.limit_price) return 'TARGET';
  return null;
}

function flattenOrders(orders) {
  const out = [];
  for (const order of orders || []) {
    out.push(order);
    if (Array.isArray(order?.legs)) out.push(...order.legs.map(leg => ({ ...leg, parent_order_id: order.id, parent_order_class: order.order_class, symbol: leg.symbol || order.symbol })));
  }
  return out;
}

function findFilledExitOrder(idea, orders) {
  const ticker = ideaTicker(idea);
  const entryTime = Date.parse(idea.actual_entry_time || idea.auto_exec_at || 0) || 0;
  return flattenOrders(orders)
    .filter(order => order?.id !== idea.alpaca_order_id)
    .filter(order => String(order?.symbol || ticker) === ticker)
    .filter(order => isFilled(order) && isExitSideForIdea(idea, order))
    .filter(order => orderTime(order) >= entryTime)
    .sort((a, b) => orderTime(a) - orderTime(b))[0] || null;
}

async function maybeExecuteConfirmedPmfs({ ideas = [], allIdeas = [], readEnvValue, now, dryRun = false } = {}) {
  const config = loadConfig(readEnvValue);
  const configMeta = currentConfigSnapshot(readEnvValue, { gapCheckCohort: 'PMF' });
  const results = [];
  if (!config.enabled && !dryRun) return { ok: true, enabled: false, placed: 0, skipped: ideas.length, results: ideas.map(i => ({ ticker: ideaTicker(i), action: 'skipped', reason: 'PMF_AUTO_EXEC_ENABLED=false' })) };
  if (!config.apiKey || !config.apiSecret) return { ok: false, enabled: config.enabled, placed: 0, skipped: ideas.length, error: 'Missing ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET' };
  let open = true;
  if (!dryRun) {
    try { open = await marketIsOpen(config); } catch (e) { open = false; results.push({ action: 'guard_failed', reason: e.message }); }
  }
  let placedToday = ordersToday(allIdeas);
  const rankedIdeas = rankCapCandidates(ideas, now);
  for (const idea of rankedIdeas) {
    const ticker = ideaTicker(idea);
    try {
      if (!ticker) throw new Error('missing ticker');
      if (!open && !dryRun) throw new Error('market is not open');
      if (orderedTickerToday(allIdeas, ticker)) throw new Error('ticker already auto-ordered today');
      if (hasOpenAutoPosition(allIdeas, ticker)) throw new Error('ticker already has open auto-position');
      if (placedToday >= config.maxOrdersPerDay) {
        const reason = `daily order cap reached (${config.maxOrdersPerDay}) after ranked selection`;
        idea.pmf_cap_selected = false;
        idea.pmf_cap_block_reason = reason;
        appendAudit(idea, { event: 'ranked_cap_blocked', rank: idea.pmf_cap_rank, candidate_count: idea.pmf_cap_candidate_count, structural_extension_pct: idea.pmf_structural_extension_at_stamp_pct, average_dollar_volume: stampAverageDollarVolume(idea), selection_rule: CAP_SELECTION_RULE, reason });
        results.push({ ticker, action: 'cap_blocked', reason, rank: idea.pmf_cap_rank, candidate_count: idea.pmf_cap_candidate_count, structural_extension_pct: idea.pmf_structural_extension_at_stamp_pct, average_dollar_volume: stampAverageDollarVolume(idea), selection_rule: CAP_SELECTION_RULE });
        continue;
      }
      const riskBlock = evaluateRiskBreakers(config, allIdeas, idea, placedToday, dryRun);
      if (riskBlock) {
        idea.pmf_cap_selected = false;
        idea.pmf_cap_block_reason = riskBlock.reason;
        idea.auto_exec_last_error = riskBlock.reason;
        if (!dryRun) idea.auto_exec_status = 'risk_blocked';
        results.push({ ticker, action: 'risk_breaker_blocked', breaker: riskBlock.breaker, reason: riskBlock.reason, exposure_snapshot: riskBlock.exposure_snapshot, alert: RISK_BREAKER_ALERT });
        continue;
      }
      const preview = buildEntryOrder(idea, config);
      preview.accountTag = config.accountTag;
      idea.pmf_cap_selected = true;
      idea.pmf_cap_block_reason = null;
      appendAudit(idea, { event: 'ranked_cap_selected', rank: idea.pmf_cap_rank, candidate_count: idea.pmf_cap_candidate_count, structural_extension_pct: idea.pmf_structural_extension_at_stamp_pct, average_dollar_volume: stampAverageDollarVolume(idea), selection_rule: CAP_SELECTION_RULE });
      if (dryRun) {
        const simulatedFill = num(idea.simulated_fill_price ?? idea.actual_entry_price ?? idea.entry_trigger_price ?? referencePrice(idea));
        let simulatedOco = null;
        if (simulatedFill > 0) simulatedOco = buildOcoExitOrder(idea, config, simulatedFill, config.qty);
        results.push({ ticker, action: 'would_place_entry_then_recalibrated_oco', account: config.accountTag, cohort_label: 'B_CLEAN', rank: idea.pmf_cap_rank, candidate_count: idea.pmf_cap_candidate_count, structural_extension_pct: idea.pmf_structural_extension_at_stamp_pct, average_dollar_volume: stampAverageDollarVolume(idea), selection_rule: CAP_SELECTION_RULE, endpoint: preview.endpoint, paper_endpoint_asserted: preview.paper_endpoint_asserted, order: preview.payload, entry_order: preview.payload, simulated_fill_price: simulatedFill, simulated_oco_order: simulatedOco?.payload || null, model: preview.model, oco_model: simulatedOco?.model || null, bracket_mode: RECALIBRATED_BRACKET_MODE, config_hash: configMeta.hash, config_snapshot_ref: configMeta.snapshotRef, config_hash_time: isoNow(now) });
        placedToday += 1;
        continue;
      }
      stampConfigOnIdea(idea, configMeta, now);
      const order = await alpacaPost(config, '/orders', preview.payload);
      idea.alpaca_order_id = order.id;
      idea.alpaca_client_order_id = order.client_order_id || preview.payload.client_order_id;
      idea.auto_exec_at = isoNow(now);
      idea.auto_exec_status = order.status || 'submitted';
      idea.auto_exec_order_class = 'entry_then_oco';
      idea.auto_exec_qty = config.qty;
      idea.fill_source = 'alpaca_paper';
      writePmfAtFillStamp(idea, now);
      appendAudit(idea, { event: 'entry_order_placed', order_id: order.id, client_order_id: idea.alpaca_client_order_id, endpoint: preview.endpoint, order: preview.payload, config_hash: idea.config_hash, config_snapshot_ref: idea.config_snapshot_ref });
      const filled = await pollOrder(config, order.id);
      writeEntryFill(idea, filled, preview, now);
      appendAudit(idea, { event: 'entry_polled', order_id: order.id, status: filled?.status, filled_avg_price: filled?.filled_avg_price || null });
      if (idea.actual_entry_price) {
        try {
          const exitPreview = buildOcoExitOrder(idea, config, idea.actual_entry_price, filled?.filled_qty || config.qty);
          const exitOrder = await alpacaPost(config, '/orders', exitPreview.payload);
          persistRecalibratedBracket(idea, exitPreview, exitOrder);
          appendAudit(idea, { event: 'recalibrated_oco_attached', order_id: exitOrder?.id || null, client_order_id: idea.recalibrated_oco_client_order_id, endpoint: exitPreview.endpoint, order: exitPreview.payload, model: exitPreview.model });
        } catch (attachError) {
          markBracketAttachFailed(idea, attachError);
          placedToday += 1;
          results.push({ ticker, action: 'bracket_attach_failed', order_id: order.id, status: idea.auto_exec_status, actual_entry_price: idea.actual_entry_price || null, reason: attachError.message });
          continue;
        }
      }
      placedToday += 1;
      results.push({ ticker, action: 'placed', account: idea.account, cohort_label: idea.cohort_label, order_id: order.id, oco_order_id: idea.recalibrated_oco_order_id || null, status: idea.auto_exec_status, actual_entry_price: idea.actual_entry_price || null, bracket_mode: idea.bracket_mode || null, recalibrated_stop: idea.recalibrated_stop || null, recalibrated_target: idea.recalibrated_target || null });
    } catch (e) {
      appendAudit(idea, { event: 'skipped_or_failed', reason: e.message });
      idea.auto_exec_last_error = e.message;
      if (!dryRun) idea.auto_exec_status = 'failed';
      results.push({ ticker, action: dryRun ? 'would_skip' : 'failed', reason: e.message });
    }
  }
  return { ok: true, enabled: config.enabled, dryRun, placed: results.filter(r => r.action === 'placed').length, results };
}

async function syncOpenAutoPositions({ allIdeas = [], readEnvValue, now, dryRun = false } = {}) {
  const primaryConfig = loadConfig(readEnvValue);
  const legacyConfig = loadLegacyConfig(readEnvValue);
  const rows = (allIdeas || []).filter(row => row.alpaca_order_id && row.fill_source === 'alpaca_paper' && !row.actual_exit_price);
  const results = [];
  if (!primaryConfig.apiKey || !primaryConfig.apiSecret) return { ok: false, checked: 0, updated: 0, error: 'Missing ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET' };
  const accountCache = new Map();
  for (const idea of rows) {
    try {
      if (!dryRun) normalizeBrokerPendingState(idea);
      const requestedAccount = idea.account || (primaryConfig.accountTag === 'dedicated' ? 'dedicated' : 'legacy_shared');
      const config = requestedAccount === 'legacy_shared'
        ? (legacyConfig || (primaryConfig.accountTag === 'legacy_shared' ? primaryConfig : null))
        : primaryConfig;
      if (!config) throw new Error('legacy shared account credentials unavailable for legacy watcher');
      if (requestedAccount === 'legacy_shared' && primaryConfig.accountTag === 'dedicated' && ideaTicker(idea) !== 'ZS') {
        throw new Error('legacy watcher is restricted to ZS only');
      }
      if (!accountCache.has(requestedAccount)) {
        const allOrders = await alpacaGet(config, '/orders?status=all&limit=500&direction=desc&nested=true');
        const positions = await alpacaGet(config, '/positions');
        accountCache.set(requestedAccount, {
          allOrders,
          livePositionSymbols: new Set((positions || []).map(position => String(position?.symbol || '').toUpperCase()).filter(Boolean)),
        });
      }
      const { allOrders, livePositionSymbols } = accountCache.get(requestedAccount);
      const order = await alpacaGet(config, `/orders/${idea.alpaca_order_id}?nested=true`);
      if (!idea.actual_entry_price && order?.filled_avg_price) writeEntryFill(idea, order, { accountTag: config.accountTag, payload: { client_order_id: idea.alpaca_client_order_id }, model: { order_flow: idea.auto_exec_order_class === 'entry_then_oco' ? 'entry_then_recalibrated_oco' : null } }, now);
      let exitOrder = null;
      if (idea.recalibrated_oco_order_id) {
        exitOrder = await alpacaGetOptional(config, `/orders/${idea.recalibrated_oco_order_id}?nested=true`);
      }
      const parentLegs = Array.isArray(order?.legs) ? order.legs : [];
      const exitLegs = exitOrder ? [exitOrder, ...(Array.isArray(exitOrder?.legs) ? exitOrder.legs : [])] : [];
      const ticker = ideaTicker(idea);
      const filledSymbolExit = findFilledExitOrder(idea, allOrders);
      if (filledSymbolExit && !livePositionSymbols.has(String(ticker || '').toUpperCase())) {
        const classification = classifyExitOrder(idea, filledSymbolExit, exitOrder);
        const writeResult = !dryRun ? writeExitFill(idea, exitOrder, filledSymbolExit, now) : null;
        results.push({ ticker, account: requestedAccount, action: dryRun ? 'would_update_exit_from_position_reconcile' : 'updated_exit_from_position_reconcile', order_id: order.id, exit_order_id: filledSymbolExit.id, exit_price: filledSymbolExit.filled_avg_price || null, exit_reason: classification.reason, exit_attribution: classification.attribution, external_close: classification.externalClose, external_close_alert_needed: dryRun ? classification.externalClose && !idea.external_close_alerted_at : Boolean(writeResult?.externalAlertNeeded) });
        continue;
      }
      const hasExitOrder = Boolean(exitOrder || parentLegs.some(leg => !['expired', 'canceled', 'cancelled', 'rejected'].includes(String(leg?.status || '').toLowerCase())));
      if (idea.actual_entry_price && !hasExitOrder && !idea.actual_exit_price) {
        idea.auto_exec_alert = NAKED_POSITION_ALERT;
        idea.auto_exec_alert_at = isoNow(now);
        loudAutoExecAlert(idea, 'open auto-position has no detected exit order attached during sync', { alert: NAKED_POSITION_ALERT, parent_order_id: idea.alpaca_order_id });
        results.push({ ticker: ideaTicker(idea), account: requestedAccount, action: 'alert_no_exit_order', order_id: idea.alpaca_order_id, status: idea.auto_exec_status, alert: NAKED_POSITION_ALERT });
        continue;
      }
      const filledLeg = [...parentLegs, ...exitLegs].find(leg => leg?.filled_avg_price || String(leg?.status || '').toLowerCase() === 'filled');
      if (filledLeg) {
        const writeResult = !dryRun ? writeExitFill(idea, exitOrder || order, filledLeg, now) : null;
        const classification = classifyExitOrder(idea, filledLeg, exitOrder || order);
        results.push({ ticker: ideaTicker(idea), account: requestedAccount, action: dryRun ? 'would_update_exit' : 'updated_exit', order_id: order.id, exit_order_id: filledLeg.id, exit_price: filledLeg.filled_avg_price || null, exit_reason: classification.reason, exit_attribution: classification.attribution, external_close: classification.externalClose, external_close_alert_needed: dryRun ? classification.externalClose && !idea.external_close_alerted_at : Boolean(writeResult?.externalAlertNeeded) });
      } else {
        results.push({ ticker: ideaTicker(idea), account: requestedAccount, action: 'open', order_id: order.id, oco_order_id: idea.recalibrated_oco_order_id || null, status: order.status });
      }
    } catch (e) {
      appendAudit(idea, { event: 'sync_failed', reason: e.message });
      results.push({ ticker: ideaTicker(idea), action: 'sync_failed', reason: e.message });
    }
  }
  return { ok: true, dryRun, checked: rows.length, updated: results.filter(r => String(r.action || '').startsWith('updated_exit')).length, results };
}

function dryRunPreview(idea, readEnvValue) {
  const config = loadConfig(readEnvValue);
  const configMeta = currentConfigSnapshot(readEnvValue, { gapCheckCohort: 'PMF' });
  const preview = buildEntryOrder(idea, config);
  const simulatedFill = num(idea.simulated_fill_price ?? idea.actual_entry_price ?? idea.entry_trigger_price ?? referencePrice(idea));
  const simulatedOco = simulatedFill > 0 ? buildOcoExitOrder(idea, config, simulatedFill, config.qty) : null;
  return { ok: true, enabled: config.enabled, action: 'would_place_entry_then_recalibrated_oco', account: config.accountTag, cohort_label: 'B_CLEAN', endpoint: preview.endpoint, paper_endpoint_asserted: preview.paper_endpoint_asserted, order: preview.payload, entry_order: preview.payload, simulated_fill_price: simulatedFill, simulated_oco_order: simulatedOco?.payload || null, model: preview.model, oco_model: simulatedOco?.model || null, bracket_mode: RECALIBRATED_BRACKET_MODE, config_hash: configMeta.hash, config_snapshot_ref: configMeta.snapshotRef, config_hash_time: new Date().toISOString() };
}

module.exports = {
  DEFAULT_QTY,
  DEFAULT_MAX_ORDERS_PER_DAY,
  CAP_SELECTION_RULE,
  ENTRY_LIMIT_BUFFER_PCT,
  loadConfig,
  loadLegacyConfig,
  buildEntryOrder,
  buildBracketOrder,
  buildOcoExitOrder,
  intendedAtrGeometry,
  recalibratedExitLevels,
  exposureSnapshot,
  evaluateRiskBreakers,
  currentConfigSnapshot,
  detectConfigDrift,
  diffConfigSnapshots,
  configHash,
  stableStringify,
  classifyExitOrder,
  writeExitFill,
  normalizeBrokerPendingState,
  structuralExtensionPct,
  rankCapCandidates,
  deduplicateCapCandidates,
  dryRunPreview,
  maybeExecuteConfirmedPmfs,
  syncOpenAutoPositions,
};
