'use strict';
const childProcess = require('child_process');
const fs = require('fs');
const https = require('https');
const path = require('path');
const executor = require('./pmf-auto-executor.cjs');
const ENV_FILE = process.env.SYSTEM2_ENV_FILE || '/root/system2-core/.env';
const STATE_FILE = '/root/system2-core/logs/pmf_retirement_invariant_state.json';
const BOOLEAN_KEYS = ['PMF_AUTO_EXEC_ENABLED', 'PMF_V1_RETIRED_NO_NEW_ENTRIES'];
function parseEnvFile(file = ENV_FILE) { try { return Object.fromEntries(fs.readFileSync(file, 'utf8').split(/\r?\n/).filter(line => line && !line.trim().startsWith('#') && line.includes('=')).map(line => { const at = line.indexOf('='); return [line.slice(0, at).trim(), line.slice(at + 1).trim().replace(/^['"]|['"]$/g, '')]; })); } catch { return {}; } }
function parseBoolean(value) { if (value == null || value === '') return null; return ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase()); }
function resolveFundSystemPid(explicitPid = null) { if (explicitPid && /^\d+$/.test(String(explicitPid))) return Number(explicitPid); try { const output = childProcess.execFileSync('pm2', ['pid', 'fund-system'], { encoding: 'utf8', timeout: 5000 }).trim(); return /^\d+$/.test(output) && Number(output) > 0 ? Number(output) : null; } catch { return null; } }
function readProcessEnv(pid) { if (!pid) return {}; try { return Object.fromEntries(fs.readFileSync(`/proc/${pid}/environ`).toString('utf8').split('\0').filter(Boolean).map(item => { const at = item.indexOf('='); return at < 0 ? [item, ''] : [item.slice(0, at), item.slice(at + 1)]; })); } catch { return {}; } }
async function evaluateInvariant({ desiredValues, effectiveValues, targetPid = null, runSelfTest = executor.retirementInvariantCheck }) {
  const readEffectiveValue = name => effectiveValues[name] ?? desiredValues[name] ?? null;
  const admission = await runSelfTest(readEffectiveValue);
  const desired = Object.fromEntries(BOOLEAN_KEYS.map(key => [key, parseBoolean(desiredValues[key])]));
  const effective = Object.fromEntries(BOOLEAN_KEYS.map(key => [key, parseBoolean(effectiveValues[key] ?? desiredValues[key])]));
  const paths = admission.self_test || {}, brokerCalls = Number(paths.broker_calls ?? admission.broker_calls ?? 0);
  const admissionsBlocked = ['primary', 'pmf_late', 'retry', 'manual'].every(key => paths[key] === 'retired_blocked');
  const ok = desired.PMF_AUTO_EXEC_ENABLED === false && desired.PMF_V1_RETIRED_NO_NEW_ENTRIES === true && effective.PMF_AUTO_EXEC_ENABLED === false && effective.PMF_V1_RETIRED_NO_NEW_ENTRIES === true && admissionsBlocked && brokerCalls === 0;
  return { ok, status: ok ? 'HEALTHY' : 'PMF_RETIREMENT_INVARIANT_BROKEN', invariant: 'PMF_V1_NEW_BROKER_ENTRIES_DISABLED', desired, effective, target_pid: targetPid, self_test: { primary: paths.primary, pmf_late: paths.pmf_late, retry: paths.retry, manual: paths.manual, broker_calls: brokerCalls }, reconciliation_untouched: true };
}
function sendAlert(text, values) { const token = values.TELEGRAM_BOT_TOKEN, chat = values.TELEGRAM_CHAT_ID; if (!token || !chat) return Promise.resolve({ sent: false, reason: 'missing Telegram credentials' }); const body = new URLSearchParams({ chat_id: chat, text }).toString(); return new Promise(resolve => { const req = https.request({ hostname: 'api.telegram.org', path: `/bot${token}/sendMessage`, method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body) } }, res => { res.resume(); res.on('end', () => resolve({ sent: res.statusCode === 200, status: res.statusCode })); }); req.on('error', error => resolve({ sent: false, reason: error.message })); req.end(body); }); }
function priorAlertedAt() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')).alerted_at; } catch { return undefined; } }
async function main(argv = process.argv.slice(2)) {
  const desiredValues = parseEnvFile(), explicitPidArg = argv.find(arg => arg.startsWith('--pid='));
  const targetPid = resolveFundSystemPid(explicitPidArg ? explicitPidArg.slice(6) : null), effectiveValues = readProcessEnv(targetPid);
  const result = await evaluateInvariant({ desiredValues, effectiveValues, targetPid });
  const source = (argv.find(arg => arg.startsWith('--source=')) || '--source=manual').slice(9);
  result.checked_at = new Date().toISOString(); result.source = source; result.alert = { sent: false, reason: 'not required' };
  const previousAlertedAt = priorAlertedAt();
  if (!result.ok && argv.includes('--alert')) { let previous = {}; try { previous = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch {} const now = Date.now(), throttle = 60 * 60 * 1000; if (!previous.alerted_at || now - Date.parse(previous.alerted_at) > throttle || previous.source !== source) { result.alert = await sendAlert(`🚨 HIGH PRIORITY — PMF_RETIREMENT_INVARIANT_BROKEN\nsource=${source}\ndesired_auto_exec=${result.desired.PMF_AUTO_EXEC_ENABLED}\neffective_auto_exec=${result.effective.PMF_AUTO_EXEC_ENABLED}\nretired_guard=${result.effective.PMF_V1_RETIRED_NO_NEW_ENTRIES}\nprimary=${result.self_test.primary}\npmf_late=${result.self_test.pmf_late}\nretry=${result.self_test.retry}\nmanual=${result.self_test.manual}\nbroker_calls=${result.self_test.broker_calls}`, desiredValues); if (result.alert.sent) result.alerted_at = new Date().toISOString(); } else result.alert = { sent: false, reason: 'throttled' }; }
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true }); fs.writeFileSync(STATE_FILE, JSON.stringify({ ...result, alerted_at: result.alerted_at || previousAlertedAt }, null, 2) + '\n'); console.log(JSON.stringify(result)); return result.ok ? 0 : 2;
}
module.exports = { parseBoolean, resolveFundSystemPid, readProcessEnv, evaluateInvariant };
if (require.main === module) main().then(code => process.exit(code)).catch(error => { console.error(JSON.stringify({ ok: false, status: 'PMF_RETIREMENT_INVARIANT_BROKEN', error: error.message, broker_calls: 0 })); process.exit(2); });
