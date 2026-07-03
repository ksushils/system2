#!/usr/bin/env python3
from __future__ import annotations

import json
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "council_v5_current_export.json"
OUT = ROOT / "council-of-ais-v5.1-ridealong-council-logging.json"

SYSTEM_PROMPT = """You are a SENIOR TRADE REVIEWER for a systematic US equity swing
trading system. Your job has two equally important parts:

PART 1 — RED FLAG DETECTION (primary job):
Identify anything that would make a trader regret taking this trade.
Be specific and honest. Do not manufacture concerns to seem cautious.

PART 2 — UPGRADE DETECTION (secondary job):
Identify if this setup has genuine positive signals BEYOND what the
technical scanner already measured. An upgrade requires something the
scanner cannot see — institutional footprints, catalyst timing,
multi-layer convergence. Do not upgrade simply because the technical
signals look strong — the scanner already measured those.

YOU ARE NOT A STOCK PICKER OR ANALYST. You do not predict price
targets, recommend companies, or opine on fundamentals. You review
the specific trade setup data provided and flag what is unusually
good or bad about it right now for a 2-10 day swing hold.

OUTPUT RULE: return strict JSON only. No preamble, no explanation
outside the JSON, no markdown, no code blocks. Raw JSON only.
Your output is parsed by code. Any deviation causes a system error.

DO NOT: hallucinate news or events, use information past your
knowledge cutoff, refuse to return JSON, give vague generic
responses. Be specific or be silent."""

USER_TEMPLATE = """REVIEW THIS SWING TRADE SETUP. Return ONLY the JSON object below.

DATE: {{date}}
TICKER: {{ticker}}
SECTOR: {{sector}}
SETUP_TYPE: {{setup_type}}
GRADE: {{grade}} ({{setup_score}}/100)
CONFLUENCE SCORE: {{confluence_score}}/130
TIME_HORIZON: Swing 2-10 days

TECHNICAL SIGNALS:
  RVOL: {{rvol}}x
  RS vs SPY: {{rs_vs_spy}}%
  VWAP distance: {{vwap_pct}}%
  ATR (daily): {{atr_daily}}
  Sector RS: {{sector_rs}}%

TRADE PLAN:
  Entry: {{entry_low}} - {{entry_high}}
  Stop: {{stop}} ({{stop_atr_multiple}}x daily ATR)
  TP1: {{tp1}} / TP2: {{tp2}}
  R:R: {{rr}}
  Size: {{shares}} shares / ${{risk_dollars}} risk

ENRICHMENT:
  Options: {{options_verdict}} ({{options_signals_count}}/4
    signals, IV rank {{iv_rank}}, P/C {{put_call_vol_ratio}})
  Chronos: {{chronos_dir}} conviction {{chronos_conviction}}
    cone {{chronos_band_pct}}%
  Catalyst: {{catalyst_summary}} ({{sub_type}})
  Analyst change: {{analyst_change}}
  Dark pool elevated: {{dark_pool_elevated}}

MARKET:
  Regime: {{regime}}
  SPY today: {{spy_1d_pct}}%
  QQQ today: {{qqq_1d_pct}}%
  VIX: {{vix_current}}
  Sector ETF: {{sector_etf_pct}}%

YOUR TASK:
1. Does anything suggest this trade should be SKIPPED or DOWNGRADED?
2. Does anything suggest this setup is EXCEPTIONALLY strong beyond
   the scanner's technical read?
3. Choose from the EXACT string lists below only.
4. Confidence 0-100 for your verdict.
5. One sentence reason, max 25 words, specific to this exact setup.

UPGRADE SIGNALS — only flag if genuinely present:
  "dark_pool_accumulation"    dark pool elevated with large block prints
  "options_sweep_bullish"     aggressive ask-side call sweeps confirmed
  "multi_layer_convergence"   scanner + options + chronos all align bullish
  "catalyst_imminent"         catalyst event about to become public
  "sector_breakout_timing"    sector in confirmed multi-week breakout
  "insider_cluster_buy"       multiple insiders buying same week
  "chronos_tight_bullish"     Chronos UP with cone width under 3%

RED FLAGS — only flag if genuinely present:
  "earnings_risk"             earnings within 5 days
  "sector_weakness"           sector ETF showing significant weakness
  "overextended"              price moved too far, poor entry risk/reward
  "low_liquidity"             liquidity concern at this price level
  "adverse_news"              recent negative headline confirmed
  "dilution_risk"             secondary offering or dilution signal
  "low_options_conviction"    options CAUTION with weak signal count
  "wide_chronos_cone"         Chronos uncertainty too high (band above 5%)
  "regime_mismatch"           setup type conflicts with current regime
  "correlation_warning"       too similar to another open position
  "weak_catalyst"             catalyst is thin or already priced in
  "gap_risk"                  pre-market move has invalidated the setup
  "fundamental_concern"       known structural business problem
  "momentum_fading"           RS or RVOL weakening in recent bars

VERDICT OPTIONS:
  "STRONG"      upgrade signals present beyond the scanner's read
  "CLEAR"       no problems found, proceed at normal size
  "WEAK"        concerns noted, consider reduced size
  "SKIP"        serious problems, recommend skipping
  "FORCE_SKIP"  hard landmine only — use for:
                earnings confirmed within 48 hours, OR
                confirmed halt / fraud / dilution announcement, OR
                VIX above 35 AND setup is long, OR
                gap more than 2x ATR against setup direction

SIZE VIEW:
  "increase"    STRONG with 2+ upgrade signals (suggest 1.25x size)
  "full"        CLEAR (normal size)
  "reduce"      WEAK with minor concerns (0.75x size)
  "half"        WEAK with significant concerns (0.5x size)
  "zero"        SKIP or FORCE_SKIP

IMPORTANT: STRONG is rare. Only use it when you can name at least
one specific upgrade signal from the list above. A technically
good setup with no additional institutional or catalyst signals
is CLEAR, not STRONG. If unsure between STRONG and CLEAR, use CLEAR.

RETURN EXACTLY THIS JSON — nothing else:
{
  "ticker": "{{ticker}}",
  "verdict": "STRONG" | "CLEAR" | "WEAK" | "SKIP" | "FORCE_SKIP",
  "confidence": <integer 0-100>,
  "upgrade_signals": ["exact strings from upgrade list, or empty []"],
  "red_flags": ["exact strings from red flag list, or empty []"],
  "reason": "<one sentence max 25 words specific to this setup>",
  "force_skip": true | false,
  "size_view": "increase" | "full" | "reduce" | "half" | "zero"
}

NOTE: You are one of three independent reviewers. Your verdict
combines with two others. You cannot see their responses. Be honest
and specific. Vague concerns belong in the confidence score, not
in force_skip."""


def js_string(value: str) -> str:
    return json.dumps(value)


PREPARE_CODE = f"""
const data = $input.first().json;
const sourceRows = data.stage5NewsSafeFinalists || data.finalists || data.top10 || data.finalTrades || [];
const top10 = sourceRows.slice(0, Number($vars.COUNCIL_TEST_LIMIT || sourceRows.length));
const macro = data.macro || {{}};
const accountSize = Number($vars.ACCOUNT_SIZE || 25000);
const riskDollars = Math.round(accountSize * Number($vars.RISK_PCT || 0.01));
const SYSTEM_PROMPT = {js_string(SYSTEM_PROMPT)};
const USER_TEMPLATE = {js_string(USER_TEMPLATE)};
const num = (x, d='-') => {{ const n = Number(x); return Number.isFinite(n) ? n : d; }};
const first = (...vals) => vals.find(v => v !== undefined && v !== null && v !== '') ?? '-';
function render(template, vars) {{
  return template.replace(/{{{{\\s*([a-zA-Z0-9_]+)\\s*}}}}/g, (_, k) => String(vars[k] ?? '-'));
}}
function varsFor(row) {{
  const s = row.scannerData || row;
  const entryZone = first(s.entryZone, s.entry_zone, row.entryZone, row.entry_zone, []);
  const entryLow = Array.isArray(entryZone) ? entryZone[0] : first(s.entry, s.price, row.entry, row.price);
  const entryHigh = Array.isArray(entryZone) ? entryZone[1] ?? entryZone[0] : entryLow;
  const riskPerShare = num(first(s.riskPerShare, s.risk_per_share, row.risk_per_share, Number(entryLow)-Number(first(s.stopLoss, s.stop_loss, row.stop))));
  return {{
    date: first(data.date, row.date, new Date().toISOString().slice(0,10)),
    ticker: first(row.ticker, row.symbol, s.ticker, s.symbol),
    sector: first(row.sector, s.sector),
    setup_type: first(row.setup_type, row.setupType, s.setup_type, s.setupType, s.setup),
    grade: first(row.grade, s.grade),
    setup_score: first(row.setupQualityScore, row.setup_score, row.councilScore, s.setupQualityScore, s.council_score),
    confluence_score: first(row.confluence_score, row.confluenceScore, s.confluence_score, s.confluenceScore),
    rvol: first(row.volumeRatio, row.rvol, s.volumeRatio, s.volume_ratio),
    rs_vs_spy: first(row.rsVsSpy, row.rs_vs_spy, s.rsVsSpy, s.rs_vs_spy),
    vwap_pct: first(row.distanceFromVWAP, row.vwap_distance, s.distanceFromVWAP, s.distance_from_vwap_pct),
    atr_daily: first(row.atr_daily, row.atr14, row.atr, s.atr_daily, s.atr14, s.atr),
    sector_rs: first(row.sectorAlpha, row.sector_rs, s.sectorAlpha, s.sector_rs),
    entry_low: entryLow,
    entry_high: entryHigh,
    stop: first(row.stopLoss, row.stop, s.stopLoss, s.stop_loss),
    stop_atr_multiple: first(row.stop_atr_multiple, row.stopAtrMultiple, s.stop_atr_multiple, s.stopAtrMultiple),
    tp1: first(row.tp1, row.target, s.tp1),
    tp2: first(row.tp2, s.tp2),
    rr: first(row.rewardRisk, row.rr, s.rewardRisk, s.reward_risk),
    shares: first(row.positionShares, row.cluster?.shares, row.riskEngine?.shares),
    risk_dollars: first(row.positionRiskDollars, row.cluster?.actualRiskDollars, row.riskEngine?.riskDollars, riskDollars),
    options_verdict: first(row.options_verdict, s.options_verdict),
    options_signals_count: first(row.options_signals_count, row.options_signals, s.options_signals_count, 0),
    iv_rank: first(row.iv_rank, row.iv_rank_proxy, s.iv_rank),
    call_vol_oi_ratio: first(row.call_vol_oi_ratio, row.vol_oi_ratio, s.call_vol_oi_ratio),
    put_call_vol_ratio: first(row.put_call_vol_ratio, s.put_call_vol_ratio),
    chronos_dir: first(row.chronos_dir, row.chronos_direction, row.forecastDecision),
    chronos_conviction: first(row.forecastConviction, row.chronos_conf),
    chronos_band_pct: first(row.chronos_band_pct),
    catalyst_summary: first(row.catalyst_summary, s.catalyst_summary),
    sub_type: first(row.sub_type, s.sub_type),
    analyst_change: JSON.stringify(first(row.analyst_change, s.analyst_change, '-')),
    seasonality_score: first(row.seasonality_score, s.seasonality_score),
    dark_pool_elevated: first(row.dark_pool_elevated, s.dark_pool_elevated),
    regime: first(row.regime, row.market_regime, macro.regime),
    spy_1d_pct: first(row.spy_1d_pct, macro.spyChg, macro.spy_pct),
    qqq_1d_pct: first(row.qqq_1d_pct, macro.qqqChg, macro.qqq_pct),
    vix_current: first(row.vix_current, macro.vixLevel),
    sector_etf_pct: first(row.sector_etf_pct, row.sectorReturnPct, s.sector_etf_pct)
  }};
}}
const councilPayloads = top10.map((row) => {{
  const vars = varsFor(row);
  return {{ ticker: vars.ticker, original: row, systemPrompt: SYSTEM_PROMPT, userPrompt: render(USER_TEMPLATE, vars) }};
}});
return [{{ json: {{ ...data, top10, councilPayloads, systemPrompt: SYSTEM_PROMPT, scanTimestamp: data.scanTimestamp || new Date().toISOString() }} }}];
"""

EVALUATOR_TEMPLATE = r"""
const data = $input.first().json;
const apiKey = API_KEY_EXPR;
const MODEL = MODEL_EXPR;
const http = this.helpers.httpRequest.bind(this);
function fallback(ticker, reason='Model returned unparseable response') {
  return { ticker, verdict: 'CLEAR', confidence: 50, upgrade_signals: [], red_flags: ['parse_error'], reason, force_skip: false, size_view: 'full' };
}
function sanitize(p, ticker) {
  const verdicts = ['STRONG','CLEAR','WEAK','SKIP','FORCE_SKIP'];
  const sizes = ['increase','full','reduce','half','zero'];
  p.ticker = ticker;
  p.verdict = verdicts.includes(String(p.verdict).toUpperCase()) ? String(p.verdict).toUpperCase() : 'CLEAR';
  p.confidence = Math.max(0, Math.min(100, Math.round(Number(p.confidence ?? 50))));
  p.upgrade_signals = Array.isArray(p.upgrade_signals) ? p.upgrade_signals : [];
  p.red_flags = Array.isArray(p.red_flags) ? p.red_flags : [];
  p.reason = String(p.reason || '').split(/\s+/).slice(0,25).join(' ') || 'No specific concern found';
  p.force_skip = p.force_skip === true || p.verdict === 'FORCE_SKIP';
  p.size_view = sizes.includes(p.size_view) ? p.size_view : (p.verdict === 'SKIP' || p.force_skip ? 'zero' : 'full');
  return p;
}
function parse(text, ticker) {
  try {
    const clean = String(text || '').replace(/```json|```/g, '').trim();
    const match = clean.match(/\{[\s\S]*\}/);
    return sanitize(JSON.parse(match ? match[0] : clean), ticker);
  } catch (e) {
    return fallback(ticker);
  }
}
async function callOne(payload) {
  if (!apiKey) return fallback(payload.ticker, 'API key missing');
  const text = await MODEL_CALL;
  return { ...parse(text, payload.ticker), raw_response: text, _model: MODEL_NAME };
}
const results = [];
for (const payload of data.councilPayloads || []) results.push(await callOne(payload));
return [{ json: { [RESULT_KEY]: results, ...data } }];
"""

CLAUDE_CALL = """(async () => {
    const res = await http({
      method: 'POST',
      url: 'https://api.anthropic.com/v1/messages',
      headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json' },
      body: { model: MODEL, max_tokens: 500, temperature: 0, system: payload.systemPrompt, messages: [{ role: 'user', content: payload.userPrompt }] },
      json: true, timeout: 45000
    });
    return (res.content || []).filter(b => b.type === 'text').map(b => b.text).join('\\n');
  })()"""

GPT_CALL = """(async () => {
    const res = await http({
      method: 'POST',
      url: 'https://api.openai.com/v1/chat/completions',
      headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: { model: MODEL, temperature: 0, max_tokens: 500, messages: [{ role: 'system', content: payload.systemPrompt }, { role: 'user', content: payload.userPrompt }] },
      json: true, timeout: 45000
    });
    return res?.choices?.[0]?.message?.content || '';
  })()"""

GEMINI_CALL = """(async () => {
    const res = await http({
      method: 'POST',
      url: `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${apiKey}`,
      headers: { 'Content-Type': 'application/json' },
      body: { system_instruction: { parts: [{ text: payload.systemPrompt }] }, contents: [{ parts: [{ text: payload.userPrompt }] }], generationConfig: { temperature: 0, maxOutputTokens: 500 } },
      json: true, timeout: 45000
    });
    return res?.candidates?.[0]?.content?.parts?.[0]?.text || '';
  })()"""


def evaluator_code(model_name: str, api_expr: str, model_expr: str, call_expr: str, result_key: str) -> str:
    return (EVALUATOR_TEMPLATE
            .replace("API_KEY_EXPR", api_expr)
            .replace("MODEL_EXPR", model_expr)
            .replace("MODEL_CALL", call_expr)
            .replace("MODEL_NAME", json.dumps(model_name))
            .replace("RESULT_KEY", json.dumps(result_key)))


VERDICT_CODE = r"""
const inputs = $input.all();
let claudeResults = [], gptResults = [], geminiResults = [], baseData = {};
for (const inp of inputs) {
  const j = inp.json || {};
  if (Array.isArray(j.claudeResults)) claudeResults = j.claudeResults;
  if (Array.isArray(j.gptResults)) gptResults = j.gptResults;
  if (Array.isArray(j.geminiResults)) geminiResults = j.geminiResults;
  if (j.councilPayloads && (!baseData.councilPayloads || j.councilPayloads.length >= baseData.councilPayloads.length)) baseData = j;
}
function byTicker(rows, ticker) { return rows.find(r => r.ticker === ticker) || { ticker, verdict: 'CLEAR', confidence: 50, upgrade_signals: [], red_flags: ['parse_error'], reason: 'Missing model response', force_skip: false, size_view: 'full' }; }
function mergeOne(stock, models) {
  const any_force_skip = models.some(m => m.force_skip === true);
  if (any_force_skip) {
    const skipper = models.find(m => m.force_skip);
    return { ...stock, ticker: models[0].ticker, council_tier: 'FORCE_SKIP', tier: 'FORCE_SKIP', size_multiplier: 0, sizeMultiplier: 0, yes_count: 0, yesCount: 0, avg_confidence: 0, avgConfidence: 0, upgrade_signals: [], all_red_flags: ['force_skip'], allRedFlags: ['force_skip'], council_claude: models[0].verdict, council_gpt: models[1].verdict, council_gemini: models[2].verdict, council_claude_conf: models[0].confidence, council_gpt_conf: models[1].confidence, council_gemini_conf: models[2].confidence, council_reasons: models.map(m => m.reason).join(' | '), council_force_skip: true, force_skip_reason: skipper.reason, remove_from_list: true, scannerData: stock };
  }
  const strong = models.filter(m => m.verdict === 'STRONG').length;
  const clear = models.filter(m => m.verdict === 'CLEAR').length;
  const weak = models.filter(m => m.verdict === 'WEAK').length;
  const skip = models.filter(m => m.verdict === 'SKIP').length;
  const positive = strong + clear;
  const avg_conf = Math.round(models.reduce((s, m) => s + (m.confidence || 50), 0) / 3);
  const all_upgrade = [...new Set(models.flatMap(m => m.upgrade_signals || []))];
  const all_flags = [...new Set(models.flatMap(m => m.red_flags || []))];
  let council_tier, size_multiplier;
  if (strong === 3 && avg_conf >= 75) { council_tier = 'UPGRADE'; size_multiplier = 1.25; }
  else if (positive === 3 && avg_conf >= 75) { council_tier = 'TIER1'; size_multiplier = 1.0; }
  else if (positive === 3 && avg_conf >= 55) { council_tier = 'TIER2'; size_multiplier = 0.75; }
  else if (positive === 2 && avg_conf >= 70) { council_tier = 'TIER2'; size_multiplier = 0.75; }
  else if (positive === 2 && avg_conf < 70) { council_tier = 'TIER3'; size_multiplier = 0.5; }
  else if (skip >= 2) { council_tier = 'SKIP'; size_multiplier = 0; }
  else if (skip === 1 && weak === 2) { council_tier = 'SKIP'; size_multiplier = 0; }
  else if (weak >= 2) { council_tier = 'TIER3'; size_multiplier = 0.5; }
  else { council_tier = 'TIER3'; size_multiplier = 0.5; }
  const council_gates_trades = false;
  const remove_from_list = council_gates_trades && council_tier === 'SKIP';
  return { ...stock, ticker: models[0].ticker, council_tier, tier: council_tier, size_multiplier, sizeMultiplier: size_multiplier, yes_count: positive, yesCount: positive, avg_confidence: avg_conf, avgConfidence: avg_conf, upgrade_signals: all_upgrade, all_red_flags: all_flags, allRedFlags: all_flags, council_claude: models[0].verdict, council_gpt: models[1].verdict, council_gemini: models[2].verdict, council_claude_conf: models[0].confidence, council_gpt_conf: models[1].confidence, council_gemini_conf: models[2].confidence, council_reasons: models.map(m => m.reason).join(' | '), council_force_skip: false, remove_from_list, scannerData: stock, raw_council: { claude: models[0], gpt: models[1], gemini: models[2] } };
}
const verdicts = [];
for (const payload of (baseData.councilPayloads || [])) {
  const ticker = payload.ticker;
  verdicts.push(mergeOne(payload.original || {}, [byTicker(claudeResults, ticker), byTicker(gptResults, ticker), byTicker(geminiResults, ticker)]));
}
const tradeCandidates = verdicts.filter(v => v.council_tier !== 'FORCE_SKIP');
const forceSkipped = verdicts.filter(v => v.council_tier === 'FORCE_SKIP');
return [{ json: { ...baseData, verdicts, tradeCandidates, watchList: verdicts.filter(v => ['TIER2','TIER3','SKIP'].includes(v.council_tier)), skipped: forceSkipped, summary: { total: verdicts.length, finalApproved: tradeCandidates.length, forceSkipped: forceSkipped.length }, verdictTimestamp: new Date().toISOString() } }];
"""

RISK_CODE = r"""
const data = $input.first().json;
const ACCOUNT = Number($vars.ACCOUNT_SIZE || 25000);
const RISK_PCT = Number($vars.RISK_PCT || 0.01);
const BASE_RISK_DOLLARS = Math.round(ACCOUNT * RISK_PCT);
const MAX_TRADES = Number($vars.MAX_TRADES || 30);
const num = (x, d = 0) => { const n = Number(x); return Number.isFinite(n) ? n : d; };
const finalTrades = [], riskRejected = [];
for (const v of (data.tradeCandidates || [])) {
  const s = v.scannerData || v;
  const entry = num(s.entry || s.price || (Array.isArray(s.entryZone) ? s.entryZone[0] : null));
  const stop = num(s.stopLoss || s.stop_loss || s.stop);
  const rps = num(s.riskPerShare || s.risk_per_share || (entry - stop));
  if (!(entry > 0 && stop > 0 && rps > 0)) { riskRejected.push({ ...v, riskRejectReason: 'No valid stop distance' }); continue; }
  if (finalTrades.length >= MAX_TRADES) { riskRejected.push({ ...v, riskRejectReason: `Max ${MAX_TRADES} trades/day reached` }); continue; }
  const riskDollars = BASE_RISK_DOLLARS * Math.min(1, Math.max(0, num(v.size_multiplier, 1)));
  const shares = Math.floor(riskDollars / rps);
  if (shares < 1) { riskRejected.push({ ...v, riskRejectReason: 'Position rounds to 0 shares' }); continue; }
  finalTrades.push({ ...v, riskEngine: { shares, riskDollars: Math.round(riskDollars), stopLoss: stop, entryZone: s.entryZone || s.entry_zone || [entry, entry], tp1: s.tp1 || s.target, tp2: s.tp2, rewardRisk: s.rewardRisk || s.reward_risk, sizeNote: `${v.council_tier} ${v.size_multiplier}x council ride-along size view` } });
}
return [{ json: { ...data, finalTrades, riskRejected, summary: { ...(data.summary || {}), finalApproved: finalTrades.length, riskRejected: riskRejected.length }, riskTimestamp: new Date().toISOString() } }];
"""

TELEGRAM_CODE = r"""
const data = $input.first().json;
const macro = data.macro || {}; const trades = data.finalTrades || []; const watch = data.watchList || []; const s = data.summary || {};
let msg = `COUNCIL OF AIs v5.1 - PAPER TRADE PLAN\n`;
msg += `Regime: ${macro.regime || data.regime || '?'} | VIX: ${macro.vixLevel || data.vix_current || '?'}\n`;
msg += `Reviewed: ${s.total || 0} | Finalists after FORCE_SKIP only: ${s.finalApproved || trades.length}\n\n`;
for (const t of trades) {
  const r = t.riskEngine || {};
  msg += `${t.ticker} ${t.council_tier} (${t.yes_count}/3 positive, avg conf ${t.avg_confidence}%)\n`;
  if ((t.upgrade_signals || []).length) msg += `* ${t.upgrade_signals.join(', ')}\n`;
  if ((t.all_red_flags || []).length) msg += `! ${t.all_red_flags.join(', ')}\n`;
  msg += `Claude: ${t.council_claude} | GPT: ${t.council_gpt} | Gemini: ${t.council_gemini}\n`;
  msg += `Reasons: ${t.council_reasons}\n`;
  msg += `Size view: ${t.size_multiplier}x normal | Entry: ${r.entryZone || '-'} | Stop: ${r.stopLoss || '-'} | Shares: ${r.shares || '-'}\n\n`;
}
if (watch.length) msg += `Watch/ride-along rows: ${watch.length}\n`;
msg += `\nPAPER MODE - log outcome. Not real money. Council SKIP is ride-along only; FORCE_SKIP is hard safety removal.`;
return [{ json: { ...data, telegramMessage: msg.length > 3900 ? msg.slice(0, 3890) + '...' : msg } }];
"""

PREPARE_LOG_CODE = r"""
const data = $input.first().json;
const rows = data.finalTrades || [];
function first(...vals) { return vals.find(v => v !== undefined && v !== null && v !== '') ?? null; }
return rows.map((trade) => {
  const s = trade.scannerData || trade;
  const risk = trade.riskEngine || {};
  const entryZone = first(s.entryZone, s.entry_zone, risk.entryZone, []);
  const entry = Array.isArray(entryZone) ? entryZone[0] : first(s.entry, s.price, trade.entry);
  return { json: {
    date: new Date().toISOString().slice(0, 10),
    ticker: first(trade.ticker, s.ticker, s.symbol),
    mode: 'SWING',
    paper: true,
    source: first(s.source, trade.source, 'scanner'),
    entry,
    stop: first(s.stopLoss, s.stop_loss, s.stop, risk.stopLoss),
    target: first(s.tp1, s.target, risk.tp1),
    sector: first(s.sector, trade.sector),
    setup: first(s.setup, s.setup_type, s.setupType),
    grade: first(s.grade, trade.grade),
    setup_score: first(trade.setup_score, trade.setupQualityScore, s.setup_score, s.setupQualityScore),
    confluence_score: first(trade.confluence_score, trade.confluenceScore, s.confluence_score, s.confluenceScore),
    council_tier: trade.council_tier,
    council_votes: trade.yes_count,
    council_conf: trade.avg_confidence,
    council_size_mult: trade.size_multiplier,
    council_upgrade_sigs: trade.upgrade_signals || [],
    council_red_flags: trade.all_red_flags || [],
    council_claude: trade.council_claude,
    council_gpt: trade.council_gpt,
    council_gemini: trade.council_gemini,
    council_claude_conf: trade.council_claude_conf,
    council_gpt_conf: trade.council_gpt_conf,
    council_gemini_conf: trade.council_gemini_conf,
    council_reasons: trade.council_reasons,
    council_force_skip: trade.council_force_skip === true,
    chronos_dir: first(trade.chronos_dir, s.chronos_dir),
    chronos_conf: first(trade.forecastConviction, s.forecastConviction),
    chronos_band_pct: first(trade.chronos_band_pct, s.chronos_band_pct),
    options_verdict: first(trade.options_verdict, s.options_verdict),
    options_signals_count: first(trade.options_signals_count, s.options_signals_count),
    iv_rank: first(trade.iv_rank, trade.iv_rank_proxy, s.iv_rank),
    vol_oi_ratio: first(trade.call_vol_oi_ratio, trade.vol_oi_ratio, s.call_vol_oi_ratio),
    analyst_change: first(trade.analyst_change, s.analyst_change),
    regime: first(trade.regime, data.regime, data.macro?.regime)
  }};
});
"""


def node(workflow: dict, name: str) -> dict:
    return next(n for n in workflow["nodes"] if n["name"] == name)


def main() -> None:
    workflow = json.loads(SRC.read_text(encoding="utf-8"))
    if isinstance(workflow, list):
        workflow = workflow[0]
    workflow.pop("id", None)
    workflow["name"] = "Council of AIs — Surgical Strike v5.1 — Ride-Along Council Logging"
    workflow["active"] = False
    workflow["versionId"] = str(uuid.uuid4())

    node(workflow, "Prepare Council Payloads")["parameters"]["jsCode"] = PREPARE_CODE
    node(workflow, "Claude Evaluator")["parameters"]["jsCode"] = evaluator_code(
        "claude",
        "$vars.ANTHROPIC_API_KEY || $env.ANTHROPIC_API_KEY",
        "$vars.ANTHROPIC_MODEL || 'claude-sonnet-4-20250514'",
        CLAUDE_CALL,
        "claudeResults",
    )
    node(workflow, "ChatGPT Evaluator")["parameters"]["jsCode"] = evaluator_code(
        "gpt",
        "$vars.OPENAI_API_KEY || $env.OPENAI_API_KEY",
        "$vars.OPENAI_MODEL || 'gpt-4o'",
        GPT_CALL,
        "gptResults",
    )
    node(workflow, "Gemini Evaluator")["parameters"]["jsCode"] = evaluator_code(
        "gemini",
        "$vars.GEMINI_API_KEY || $env.GEMINI_API_KEY",
        "$vars.GEMINI_MODEL || 'gemini-2.5-flash'",
        GEMINI_CALL,
        "geminiResults",
    )
    node(workflow, "Verdict Node")["parameters"]["jsCode"] = VERDICT_CODE
    node(workflow, "Risk Engine")["parameters"]["jsCode"] = RISK_CODE
    node(workflow, "Format Telegram")["parameters"]["jsCode"] = TELEGRAM_CODE

    existing_names = {n["name"] for n in workflow["nodes"]}
    if "Prepare Idea Log Items" not in existing_names:
        workflow["nodes"].append({
            "parameters": {"jsCode": PREPARE_LOG_CODE},
            "id": "node-prepare-idea-log-items-v51",
            "name": "Prepare Idea Log Items",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1550, 520],
        })
    else:
        node(workflow, "Prepare Idea Log Items")["parameters"]["jsCode"] = PREPARE_LOG_CODE
    if "Log Paper Idea to Scoring Loop" not in existing_names:
        workflow["nodes"].append({
            "parameters": {
                "method": "POST",
                "url": "http://72.62.134.167:3210/api/idea",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json) }}",
                "options": {"timeout": 120000},
            },
            "id": "node-log-paper-idea-v51",
            "name": "Log Paper Idea to Scoring Loop",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1950, 520],
        })

    con = workflow.setdefault("connections", {})
    risk_outputs = con.setdefault("Risk Engine", {"main": [[]]})["main"][0]
    if not any(x.get("node") == "Prepare Idea Log Items" for x in risk_outputs):
        risk_outputs.append({"node": "Prepare Idea Log Items", "type": "main", "index": 0})
    con["Prepare Idea Log Items"] = {"main": [[{"node": "Log Paper Idea to Scoring Loop", "type": "main", "index": 0}]]}

    OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
