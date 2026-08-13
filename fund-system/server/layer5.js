import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER5_POOL_MAX || 2), application_name: 'fund-system-layer5' }) : null;

const SCANNERS = ['fmp', 'fmp_alpaca', 'forex', 'comm', 'pa', 'failed_breakout', 'volume', 'mean_reversion', 'indices', 'crypto'];
let weeklyStarted = false;
let lastWeeklyRunDate = '';

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function weekEnding(date = new Date()) {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const add = (7 - d.getUTCDay()) % 7;
  d.setUTCDate(d.getUTCDate() + add);
  return d.toISOString().slice(0, 10);
}

function mdTable(rows, cols) {
  if (!rows.length) return '_No rows._';
  const header = `| ${cols.join(' |')} |`;
  const sep = `| ${cols.map(() => '---').join(' |')} |`;
  const body = rows.map(r => `| ${cols.map(c => String(r[c] ?? '').replace(/\|/g, '\\|')).join(' |')} |`);
  return [header, sep, ...body].join('\n');
}

function envValue(...keys) {
  for (const key of keys) {
    if (process.env[key]) return process.env[key];
  }
  return '';
}

function findingClass(avgRet1d, pctWouldHaveWon) {
  const avg = Number(avgRet1d);
  const wins = Number(pctWouldHaveWon);
  if (!Number.isFinite(avg) || !Number.isFinite(wins)) return 'INSUFFICIENT_DATA';
  if (avg <= -0.002) return 'SAVING_YOU';
  if (avg >= 0.003 && wins >= 55) return 'COSTING_YOU';
  return 'NO_EFFECT';
}

export async function initLayer5() {
  if (!pool) return false;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS weekly_reports (
      id BIGSERIAL PRIMARY KEY,
      week_ending DATE NOT NULL,
      report_md TEXT NOT NULL,
      data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_weekly_reports_latest ON weekly_reports(created_at DESC);
  `);
  return true;
}

async function latestComputedAt(table) {
  const { rows } = await pool.query(`SELECT max(computed_at) AS ts FROM ${table}`);
  return rows[0]?.ts || null;
}

async function gatherFilterVerdicts() {
  const latest = await latestComputedAt('filter_verdicts');
  if (!latest) return { computed_at: null, top10: [], all: [] };
  const { rows: top10 } = await pool.query(`
    SELECT scanner, reason, verdict, n_labeled, avg_ret_1d::float8, avg_ret_3d::float8,
      pct_would_have_won::float8, est_missed_r_upper_bound::float8, dq_flag
    FROM filter_verdicts
    WHERE computed_at=$1
      AND COALESCE(lower(dq_flag),'') <> 'too_thin'
    ORDER BY est_missed_r_upper_bound DESC NULLS LAST, n_labeled DESC
    LIMIT 10
  `, [latest]);
  const { rows: all } = await pool.query(`
    SELECT scanner, reason, verdict, n_labeled, avg_ret_1d::float8, avg_ret_3d::float8,
      pct_would_have_won::float8, est_missed_r_upper_bound::float8, dq_flag
    FROM filter_verdicts
    WHERE computed_at=$1
      AND COALESCE(lower(dq_flag),'') <> 'too_thin'
    ORDER BY scanner, n_labeled DESC
  `, [latest]);
  const { rows: gauntletRows } = await pool.query(`
    WITH buckets AS (
      SELECT DISTINCT ON(scanner,reason) scanner, reason, verdict, window_days,
        $1::timestamptz-(window_days||' days')::interval window_start,
        $1::timestamptz-(window_days||' days')::interval/2 window_mid
      FROM filter_verdicts
      WHERE computed_at=$1 AND n_labeled >= 30
        AND COALESCE(lower(dq_flag),'') <> 'too_thin'
      ORDER BY scanner,reason,n_labeled DESC,id DESC
    ), labeled AS (
      SELECT b.scanner, b.reason, b.verdict, o.event_ts, o.ret_1d::float8 ret_1d,
        b.window_mid,
        row_number() OVER (PARTITION BY b.scanner,b.reason ORDER BY
          CASE WHEN b.verdict='COSTING_YOU' THEN o.ret_1d WHEN b.verdict='SAVING_YOU' THEN -o.ret_1d ELSE abs(o.ret_1d) END DESC,o.id) contribution_rank
      FROM buckets b
      JOIN signal_outcomes o ON o.scanner=b.scanner
      JOIN rejections r ON r.id::text=o.source_id
      WHERE o.source_type='rejection'
        AND o.event_ts >= $1::timestamptz - (b.window_days || ' days')::interval
        AND o.event_ts < $1::timestamptz
        AND o.ret_1d IS NOT NULL
        AND COALESCE(NULLIF(r.data->>'reason',''), NULLIF(r.data->>'rejection_reason',''), NULLIF(r.data->>'message',''), 'UNKNOWN')=b.reason
        AND ${analyticsFirewallSql('o','r')}
    )
    SELECT scanner, reason, max(verdict) verdict, count(*)::int n,
      avg(ret_1d) FILTER (WHERE contribution_rank > 3)::float8 top3_removed_avg_ret_1d,
      (100.0 * avg((ret_1d > 0)::int) FILTER (WHERE contribution_rank > 3))::float8 top3_removed_pct_won,
      count(*) FILTER (WHERE event_ts < window_mid)::int first_half_n,
      avg(ret_1d) FILTER (WHERE event_ts < window_mid)::float8 first_half_avg_ret_1d,
      (100.0 * avg((ret_1d > 0)::int) FILTER (WHERE event_ts < window_mid))::float8 first_half_pct_won,
      count(*) FILTER (WHERE event_ts >= window_mid)::int second_half_n,
      avg(ret_1d) FILTER (WHERE event_ts >= window_mid)::float8 second_half_avg_ret_1d,
      (100.0 * avg((ret_1d > 0)::int) FILTER (WHERE event_ts >= window_mid))::float8 second_half_pct_won
    FROM labeled
    GROUP BY scanner,reason,verdict
  `, [latest]);
  const gauntlet = gauntletRows.map(r => {
    const top3Class = findingClass(r.top3_removed_avg_ret_1d, r.top3_removed_pct_won);
    const firstClass = findingClass(r.first_half_avg_ret_1d, r.first_half_pct_won);
    const secondClass = findingClass(r.second_half_avg_ret_1d, r.second_half_pct_won);
    return {
      ...r,
      top3_removed_class: top3Class,
      first_half_class: firstClass,
      second_half_class: secondClass,
      holds_without_top3: top3Class === r.verdict,
      holds_in_both_halves: Number(r.first_half_n)>=15 && Number(r.second_half_n)>=15 && firstClass === r.verdict && secondClass === r.verdict,
      recommendation_eligible: top3Class === r.verdict && Number(r.first_half_n)>=15 && Number(r.second_half_n)>=15 && firstClass === r.verdict && secondClass === r.verdict
    };
  });
  const byKey = new Map(gauntlet.map(r => [`${r.scanner}\u0000${r.reason}`, r]));
  const decorate = row => ({ ...row, gauntlet: byKey.get(`${row.scanner}\u0000${row.reason}`) || null });
  return { computed_at: latest, top10: top10.map(decorate), all: all.map(decorate), gauntlet };
}

async function gatherStrategyHealth() {
  const latest = await latestComputedAt('strategy_health');
  if (!latest) return { computed_at: null, rows: [] };
  const { rows } = await pool.query(`
    SELECT scanner, window_trades, expectancy_r::float8, win_rate::float8, avg_win_r::float8,
      avg_loss_r::float8, baseline_expectancy_r::float8, state
    FROM strategy_health
    WHERE computed_at=$1
    ORDER BY array_position($2::text[], scanner)
  `, [latest, SCANNERS]);
  return { computed_at: latest, rows };
}

async function gatherBrainShadowStats() {
  const { rows } = await pool.query(`
    WITH shadow AS (
      SELECT r.id::text rejection_id, r.scanner, o.ret_1d::float8
      FROM rejections r
      LEFT JOIN signal_outcomes o ON o.source_type='rejection' AND o.source_id=r.id::text
      WHERE COALESCE(r.data->>'reason','') LIKE 'BRAIN_SHADOW_VETO:%' AND ${analyticsFirewallSql('o','r')}
    )
    SELECT count(*)::int total_shadow_vetoes,
      count(ret_1d)::int labeled,
      count(*) FILTER (WHERE ret_1d < 0)::int actually_lost,
      round((100.0 * count(*) FILTER (WHERE ret_1d < 0) / NULLIF(count(ret_1d),0))::numeric, 2)::float8 pct_actually_lost,
      round(avg(ret_1d)::numeric, 6)::float8 avg_ret_1d
    FROM shadow
  `);
  const byScanner = await pool.query(`
    SELECT r.scanner,
      count(*)::int total_shadow_vetoes,
      count(o.ret_1d)::int labeled,
      count(*) FILTER (WHERE o.ret_1d < 0)::int actually_lost,
      round(avg(o.ret_1d)::numeric, 6)::float8 avg_ret_1d
    FROM rejections r
    LEFT JOIN signal_outcomes o ON o.source_type='rejection' AND o.source_id=r.id::text
    WHERE COALESCE(r.data->>'reason','') LIKE 'BRAIN_SHADOW_VETO:%' AND ${analyticsFirewallSql('o','r')}
    GROUP BY r.scanner
    ORDER BY total_shadow_vetoes DESC, r.scanner
  `);
  return { overall: rows[0], by_scanner: byScanner.rows };
}

async function gatherRegimePerformance() {
  const { rows } = await pool.query(`
    WITH joined AS (
      SELECT o.scanner, o.ret_1d::float8, rs.spy_trend, rs.vix::float8, rs.session_hour_et
      FROM signal_outcomes o
      JOIN LATERAL (
        SELECT *
        FROM regime_snapshots rs
        WHERE rs.ts BETWEEN o.event_ts - interval '45 minutes' AND o.event_ts + interval '45 minutes'
        ORDER BY abs(extract(epoch from (rs.ts - o.event_ts))) ASC, rs.id ASC
        LIMIT 1
      ) rs ON true
      WHERE o.ret_1d IS NOT NULL
        AND ${analyticsFirewallSql('o')}
    ),
    terciles AS (
      SELECT percentile_cont(0.333) WITHIN GROUP (ORDER BY vix) AS p33,
             percentile_cont(0.667) WITHIN GROUP (ORDER BY vix) AS p67
      FROM joined WHERE vix IS NOT NULL
    ),
    grouped AS (
      SELECT j.scanner,
        COALESCE(j.spy_trend,'BTC_ONLY') spy_trend,
        CASE
          WHEN j.vix IS NULL THEN 'UNKNOWN'
          WHEN j.vix <= t.p33 THEN 'LOW'
          WHEN j.vix <= t.p67 THEN 'MID'
          ELSE 'HIGH'
        END AS vix_tercile,
        j.session_hour_et,
        count(*)::int n,
        round(avg(j.ret_1d)::numeric, 6)::float8 avg_ret_1d
      FROM joined j CROSS JOIN terciles t
      GROUP BY j.scanner, COALESCE(j.spy_trend,'BTC_ONLY'), vix_tercile, j.session_hour_et
    )
    SELECT *, (n < 30) insufficient
    FROM grouped
    WHERE n >= 30
    ORDER BY scanner, n DESC, avg_ret_1d DESC
  `);
  return { rows };
}

async function gatherChangelog() {
  const { rows } = await pool.query(`
    SELECT date, scanner, parameter, old_value, new_value, reason, approved_by
    FROM intelligence_changelog
    WHERE date >= now() - interval '7 days'
    ORDER BY date DESC
  `);
  return { rows };
}

async function gatherWeekDeltas() {
  const { rows } = await pool.query(`
    WITH events AS (
      SELECT scanner, 'signals' kind, NULLIF(data->>'ts','')::timestamptz ts FROM signals
      UNION ALL
      SELECT scanner, 'rejections' kind, COALESCE(NULLIF(data->>'rejected_time',''), NULLIF(data->>'ts',''))::timestamptz ts FROM rejections r WHERE ${analyticsFirewallSql('','r')}
      UNION ALL
      SELECT scanner, 'trades' kind, COALESCE(NULLIF(data->>'opened_at',''), NULLIF(opened_at::text,''))::timestamptz ts FROM trades
    )
    SELECT scanner,
      count(*) FILTER (WHERE kind='signals' AND ts >= now() - interval '7 days')::int signals_7d,
      count(*) FILTER (WHERE kind='signals' AND ts < now() - interval '7 days' AND ts >= now() - interval '14 days')::int signals_prev_7d,
      count(*) FILTER (WHERE kind='rejections' AND ts >= now() - interval '7 days')::int rejections_7d,
      count(*) FILTER (WHERE kind='rejections' AND ts < now() - interval '7 days' AND ts >= now() - interval '14 days')::int rejections_prev_7d,
      count(*) FILTER (WHERE kind='trades' AND ts >= now() - interval '7 days')::int trades_7d,
      count(*) FILTER (WHERE kind='trades' AND ts < now() - interval '7 days' AND ts >= now() - interval '14 days')::int trades_prev_7d
    FROM events
    WHERE scanner IS NOT NULL AND scanner <> ''
    GROUP BY scanner
    ORDER BY scanner
  `);
  return { rows };
}

export async function gatherWeeklyAnalystData() {
  if (!pool) throw new Error('Postgres is required for weekly reports');
  await initLayer5();
  const [filter_verdicts, strategy_health, brain_shadow_stats, regime_performance, intelligence_changelog, week_over_week_deltas] = await Promise.all([
    gatherFilterVerdicts(),
    gatherStrategyHealth(),
    gatherBrainShadowStats(),
    gatherRegimePerformance(),
    gatherChangelog(),
    gatherWeekDeltas()
  ]);
  const change_evaluations = (await pool.query(`SELECT * FROM change_evaluations ORDER BY changed_at DESC LIMIT 50`)).rows;
  const challenger_evals = (await pool.query(`SELECT DISTINCT ON(scanner) * FROM challenger_evals ORDER BY scanner,computed_at DESC`)).rows;
  return {
    generated_at: new Date().toISOString(),
    week_ending: weekEnding(),
    rules: [
      'Recommend at most ONE parameter change per scanner.',
      'Never recommend a change on n<30.',
      'A recommendation is eligible only when the finding keeps the same classification after removing the top three absolute-return contributors and in both chronological halves.',
      'Prefer removing NO_EFFECT filters over adding anything.',
      'If a prior week change exists in changelog, evaluate whether it worked before proposing anything new.',
      'All returns from outcome labels are modeled evidence, never achievable P&L.',
      'Gemini has no write access; all changes require human approval and an intelligence_changelog row.'
    ],
    filter_verdicts,
    strategy_health,
    brain_shadow_stats,
    regime_performance,
    intelligence_changelog,
    week_over_week_deltas
    ,change_evaluations
    ,challenger_evals
  };
}

function conservativeReport(data, reason = '') {
  const healthRows = data.strategy_health.rows.map(r => ({
    scanner: r.scanner,
    state: r.state,
    trades: r.window_trades,
    expectancy_r: r.expectancy_r ?? 'n/a'
  }));
  const enough = data.filter_verdicts.all.filter(r => Number(r.n_labeled) >= 30);
  const costing = enough.filter(r => r.verdict === 'COSTING_YOU').slice(0, 3);
  const noEffect = enough.filter(r => r.verdict === 'NO_EFFECT').slice(0, 3);
  const lines = [
    `# Weekly Intelligence Report`,
    ``,
    `Week ending: ${data.week_ending}`,
    `Generated: ${data.generated_at}`,
    reason ? `Analyst mode: conservative fallback (${reason}).` : `Analyst mode: deterministic guardrail summary.`,
    ``,
    `## Executive Summary`,
    `No automatic entry-parameter changes are approved or applied. Layer 3 paper_only protection remains the only automated action.`,
    `Current evidence is thin outside the labeled crypto/regime bucket and a small number of index rejection labels. The correct recommendation is mostly no change until n>=30 cells mature.`,
    ``,
    `## Strategy Health`,
    mdTable(healthRows, ['scanner', 'state', 'trades', 'expectancy_r']),
    ``,
    `## Filter Evidence`,
    `Rows with n<30 are not eligible for parameter recommendations. Eligible COSTING_YOU rows: ${costing.length}. Eligible NO_EFFECT rows: ${noEffect.length}.`,
    costing.length ? mdTable(costing.map(r => ({
      scanner: r.scanner, reason: r.reason, n: r.n_labeled, avg_ret_1d: r.avg_ret_1d, est_missed_r_upper_bound: r.est_missed_r_upper_bound
    })), ['scanner', 'reason', 'n', 'avg_ret_1d', 'est_missed_r_upper_bound']) : `_No eligible COSTING_YOU rows._`,
    ``,
    `## Recommendations`,
    `No live parameter changes recommended this week. Any future human-approved change must be submitted through /api/params with a required reason so it lands in intelligence_changelog.`,
    ``,
    `## Watch List`,
    `Continue collecting labels for comm/indices now that Capital candle fetching is enabled. Re-run filter verdicts after enough n>=30 cells exist.`
  ];
  return lines.join('\n');
}

function buildPrompt(data) {
  return [
    'You are the read-only weekly analyst for an algorithmic trading dashboard.',
    'You cannot write settings, change parameters, or place trades. You only produce a markdown report.',
    '',
    'Rules:',
    '- Recommend at most ONE parameter change per scanner.',
    '- Never recommend a change on n<30.',
    '- Evidence must survive a basic gauntlet: state whether the finding holds with the top-3 contributing rows removed and whether it holds in BOTH halves of the window chronologically. If either is false, do not recommend.',
    '- Only rows whose gauntlet.recommendation_eligible is true may support a recommendation.',
    '- Prefer removing NO_EFFECT filters over adding anything.',
    '- For each recommendation state: parameter, current value, proposed value, evidence (n, avg return), expected effect.',
    '- If a prior week change exists in the changelog, evaluate whether it worked before proposing anything new for that scanner.',
    '- If data is thin, say "insufficient data, no changes recommended". Do not invent confidence.',
    '- Never present modeled or label-derived returns as achievable P&L. Call them modeled returns or modeled evidence.',
    '- Standing rule: nothing changes entry parameters automatically; Layer 3 paper_only protection is the only automated action.',
    '',
    'Return concise markdown with sections: Executive Summary, Evidence, Recommendations, Prior Changes Review, Risks/Data Gaps.',
    '',
    'DATA_JSON:',
    JSON.stringify(data)
  ].join('\n');
}

async function callGemini(data) {
  const key = envValue('GEMINI_API_KEY', 'GOOGLE_API_KEY');
  if (!key) return { ok: false, reason: 'GEMINI_API_KEY missing' };
  const model = envValue('GEMINI_MODEL') || 'gemini-2.5-flash';
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(key)}`;
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      contents: [{ role: 'user', parts: [{ text: buildPrompt(data) }] }],
      generationConfig: { temperature: 0.15, maxOutputTokens: 4096 }
    }),
    signal: AbortSignal.timeout(30000)
  });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) return { ok: false, reason: `Gemini ${response.status}`, body };
  const report = body?.candidates?.[0]?.content?.parts?.map(p => p.text || '').join('\n').trim();
  return report ? { ok: true, report, model } : { ok: false, reason: 'Gemini returned empty report', body };
}

function telegramSummary(reportMd) {
  const recSection = reportMd.split(/## Recommendations/i)[1] || reportMd;
  const lines = recSection.split('\n').map(l => l.trim()).filter(Boolean);
  const top = lines.filter(l => /^[-*]\s/.test(l) || /^\d+\./.test(l)).slice(0, 3);
  const summary = top.length ? top.join('\n') : 'No parameter changes recommended.';
  return `Weekly Intelligence Report\n\n${summary}`.slice(0, 3500);
}

export function validateGeminiRecommendations(report, eligible) {
  const section=String(report||'').split(/## Recommendations/i)[1]?.split(/\n##\s/)[0]||'';
  if(!section.trim()||/no (?:live )?(?:parameter )?changes? recommended/i.test(section))return {ok:true,dropped:[]};
  const lower=section.toLowerCase();const dropped=[];
  for(const scanner of SCANNERS){if(!new RegExp(`\\b${scanner.replace('_','[_ -]')}\\b`,'i').test(section))continue;const rows=eligible.filter(r=>r.scanner===scanner);const supported=rows.some(r=>lower.includes(String(r.reason||'').toLowerCase()));if(!supported)dropped.push({scanner,reason:'no eligible scanner+reason evidence cited'});}
  return {ok:dropped.length===0,dropped};
}

export async function runWeeklyAnalyst({ sendTelegramAlert } = {}) {
  if (!pool) throw new Error('Postgres is required for weekly reports');
  await initLayer5();
  const data = await gatherWeeklyAnalystData();
  const gemini = await callGemini(data).catch(e => ({ ok: false, reason: e.message }));
  const eligible = data.filter_verdicts.gauntlet.filter(r => r.recommendation_eligible);
  const validation=gemini.ok?validateGeminiRecommendations(gemini.report,eligible):{ok:false,dropped:[]};
  const forcedFallback = gemini.ok && (eligible.length === 0 || !validation.ok);
  const fallbackReason=eligible.length===0?'no evidence passed the mandatory gauntlet':!validation.ok?`Gemini recommendations failed eligibility validation: ${JSON.stringify(validation.dropped)}`:gemini.reason;
  const reportMd = gemini.ok && !forcedFallback
    ? gemini.report
    : conservativeReport(data, forcedFallback ? fallbackReason : gemini.reason);
  data.analyst = gemini.ok && !forcedFallback
    ? { provider: 'gemini', model: gemini.model }
    : { provider: 'fallback', reason: forcedFallback ? fallbackReason : gemini.reason, body: gemini.body || null };
  data.analyst.recommendation_validation=validation;
  data.analyst.guardrail_fallback = forcedFallback;
  const { rows } = await pool.query(`
    INSERT INTO weekly_reports(week_ending, report_md, data_json)
    VALUES($1,$2,$3)
    RETURNING id, week_ending, report_md, data_json, created_at
  `, [data.week_ending, reportMd, data]);
  const telegram = sendTelegramAlert ? await sendTelegramAlert(telegramSummary(reportMd)).catch(e => ({ ok: false, error: e.message })) : null;
  return { ...rows[0], telegram };
}

export async function latestWeeklyReport() {
  if (!pool) throw new Error('Postgres is required for weekly reports');
  await initLayer5();
  const { rows } = await pool.query(`
    SELECT id, week_ending, report_md, data_json, created_at
    FROM weekly_reports
    ORDER BY created_at DESC
    LIMIT 1
  `);
  return rows[0] || null;
}

function sundayEighteenUtc(date = new Date()) {
  return date.getUTCDay() === 0 && date.getUTCHours() === 18;
}

export function attachLayer5(app, _db, { adminOnly, sendTelegramAlert }) {
  app.post('/api/intelligence/report/run', adminOnly, async (_req, res) => {
    try { res.json(await runWeeklyAnalyst({ sendTelegramAlert })); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/report/latest', adminOnly, async (_req, res) => {
    try {
      const report = await latestWeeklyReport();
      if (!report) return res.status(404).json({ error: 'No weekly report yet' });
      res.json(report);
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
}

export function startLayer5Jobs({ sendTelegramAlert } = {}) {
  if (!pool || weeklyStarted || process.env.DISABLE_LAYER5_JOBS === 'true') return;
  weeklyStarted = true;
  setInterval(() => {
    const now = new Date();
    const dateKey = now.toISOString().slice(0, 10);
    if (!sundayEighteenUtc(now) || lastWeeklyRunDate === dateKey) return;
    lastWeeklyRunDate = dateKey;
    runWeeklyAnalyst({ sendTelegramAlert }).catch(e => console.error('[weekly-analyst]', e.message));
  }, 5 * 60_000).unref?.();
}
