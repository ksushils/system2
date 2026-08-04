const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const BACKUP_ROOT = '/root/system2-core/backups/fund-validated';
const MANIFEST_PATH = path.join(BACKUP_ROOT, 'manifest.jsonl');
const ALERT_PATH = path.join(BACKUP_ROOT, 'INTEGRITY_ALERT.json');
const EXPECTED_KEYS = ['fund', 'investors', 'trades', 'ideas', 'pead_drift_paper'];
const ROW_DROP_FRACTION = 0.10;

function sha256(filePath) {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(filePath));
  return h.digest('hex');
}

function ensureDir() {
  fs.mkdirSync(BACKUP_ROOT, { recursive: true });
}

function readLastGoodManifest() {
  try {
    if (!fs.existsSync(MANIFEST_PATH)) return null;
    const lines = fs.readFileSync(MANIFEST_PATH, 'utf8').split(/\r?\n/).filter(Boolean);
    let last = null;
    for (const line of lines) {
      try {
        const row = JSON.parse(line);
        if (row && row.ok === true) last = row;
      } catch (_) {}
    }
    return last;
  } catch (_) {
    return null;
  }
}

function writeAlert(message, extra = {}) {
  ensureDir();
  const payload = { at: new Date().toISOString(), message, ...extra };
  fs.writeFileSync(ALERT_PATH, JSON.stringify(payload, null, 2));
  console.error('?? FUND_JSON_INTEGRITY_ALERT', JSON.stringify(payload));
}

function validateObject(data, filePath, prior = readLastGoodManifest()) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('top-level JSON is not an object');
  const missing = EXPECTED_KEYS.filter(k => !(k in data));
  if (missing.length) throw new Error(`missing expected top-level keys: ${missing.join(', ')}`);
  if (!Array.isArray(data.ideas)) throw new Error('ideas is not a list');
  if (!Array.isArray(data.pead_drift_paper)) throw new Error('pead_drift_paper is not a list');
  const ideasCount = data.ideas.length;
  if (prior && Number.isFinite(Number(prior.ideas_count))) {
    const prev = Number(prior.ideas_count);
    const minAllowed = Math.max(1, Math.floor(prev * (1 - ROW_DROP_FRACTION)));
    if (ideasCount < minAllowed) throw new Error(`ideas row count dropped unexpectedly: ${ideasCount} < ${minAllowed} from prior ${prev}`);
  } else if (ideasCount < 1) {
    throw new Error(`ideas row count implausibly low: ${ideasCount}`);
  }
  return {
    ok: true,
    file: filePath,
    size: fs.existsSync(filePath) ? fs.statSync(filePath).size : null,
    sha256: fs.existsSync(filePath) ? sha256(filePath) : null,
    ideas_count: ideasCount,
    pead_drift_paper_count: data.pead_drift_paper.length,
    prior_backup: prior ? prior.path : null,
  };
}

function validateFundFileOnLoad(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const data = JSON.parse(raw);
    const result = validateObject(data, filePath);
    console.log('? fund.json integrity check passed', JSON.stringify({ ideas_count: result.ideas_count, pead_drift_paper_count: result.pead_drift_paper_count, size: result.size }));
    return result;
  } catch (err) {
    writeAlert('fund.json failed load-time integrity check; no auto-restore performed', { file: filePath, error: err.message });
    return { ok: false, error: err.message, file: filePath };
  }
}

module.exports = {
  validateObject,
  validateFundFileOnLoad,
  readLastGoodManifest,
};
