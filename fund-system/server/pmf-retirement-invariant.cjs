'use strict';

const fs = require('fs');
const https = require('https');
const path = require('path');
const executor = require('./pmf-auto-executor.cjs');

const ENV_FILE = process.env.SYSTEM2_ENV_FILE || '/root/system2-core/.env';
const STATE_FILE = '/root/system2-core/logs/pmf_retirement_invariant_state.json';

function envFile() {
  try {
    return Object.fromEntries(fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/).filter(line => line && !line.trim().startsWith('#') && line.includes('=')).map(line => {
      const at=line.indexOf('='); return [line.slice(0,at).trim(),line.slice(at+1).trim().replace(/^['"]|['"]$/g,'')];
    }));
  } catch { return {}; }
}

function sendAlert(text, values) {
  const token=values.TELEGRAM_BOT_TOKEN, chat=values.TELEGRAM_CHAT_ID;
  if (!token || !chat) return Promise.resolve({sent:false,reason:'missing Telegram credentials'});
  const body=new URLSearchParams({chat_id:chat,text}).toString();
  return new Promise(resolve => {
    const req=https.request({hostname:'api.telegram.org',path:`/bot${token}/sendMessage`,method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Content-Length':Buffer.byteLength(body)}},res=>{res.resume();res.on('end',()=>resolve({sent:res.statusCode===200,status:res.statusCode}));});
    req.on('error',error=>resolve({sent:false,reason:error.message}));req.end(body);
  });
}

(async()=>{
  const fileValues=envFile();
  const readEnvValue=name => process.env[name] ?? fileValues[name] ?? null;
  const result=await executor.retirementInvariantCheck(readEnvValue);
  const source=(process.argv.find(arg=>arg.startsWith('--source='))||'--source=manual').slice(9);
  result.checked_at=new Date().toISOString();result.source=source;result.alert={sent:false,reason:'not required'};
  if (!result.ok && process.argv.includes('--alert')) {
    let previous={};try{previous=JSON.parse(fs.readFileSync(STATE_FILE,'utf8'));}catch{}
    const now=Date.now(), throttle=60*60*1000;
    if (!previous.alerted_at || now-Date.parse(previous.alerted_at)>throttle || previous.source!==source) {
      result.alert=await sendAlert(`🚨 HIGH PRIORITY — PMF RETIREMENT INVARIANT FAILED\nsource=${source}\nauto_exec=${result.flags.PMF_AUTO_EXEC_ENABLED}\nretired_guard=${result.flags.PMF_V1_RETIRED_NO_NEW_ENTRIES}\naction=${result.self_test.action}\nbroker_calls=${result.self_test.broker_calls}`,fileValues);
      if (result.alert.sent) result.alerted_at=new Date().toISOString();
    } else result.alert={sent:false,reason:'throttled'};
  }
  fs.mkdirSync(path.dirname(STATE_FILE),{recursive:true});fs.writeFileSync(STATE_FILE,JSON.stringify({...result,alerted_at:result.alerted_at||undefined},null,2)+'\n');
  console.log(JSON.stringify(result));process.exit(result.ok?0:2);
})().catch(error=>{console.error(JSON.stringify({ok:false,error:error.message,broker_calls:0}));process.exit(2);});
