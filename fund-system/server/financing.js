import pg from 'pg';
const pool=process.env.DATABASE_URL?new pg.Pool({connectionString:process.env.DATABASE_URL,max:2,application_name:'fund-financing'}):null;
let lastLondonDay='';
const londonDay=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
export function computeNetPnl({pnl_gross,spread_cost=0,commission=0,financing_accrued=0}){return Number(pnl_gross)-Number(spread_cost||0)-Number(commission||0)-Number(financing_accrued||0);}
async function session(){
  const base=String(process.env.CAP_BASE_URL||'https://demo-api-capital.backend-capital.com').replace(/\/$/,'')+'/api/v1',key=process.env.CAP_API_KEY;
  const r=await fetch(base+'/session',{method:'POST',headers:{'X-CAP-API-KEY':key,'Content-Type':'application/json'},body:JSON.stringify({identifier:process.env.CAP_IDENTIFIER,password:process.env.CAP_PASSWORD,encryptedPassword:false}),signal:AbortSignal.timeout(20000)});
  if(!r.ok)throw new Error(`Capital login ${r.status}`);
  return {base,headers:{'X-CAP-API-KEY':key,CST:r.headers.get('cst'),'X-SECURITY-TOKEN':r.headers.get('x-security-token')}};
}
export async function initFinancing(){if(!pool)return false;await pool.query(`CREATE TABLE IF NOT EXISTS financing_accruals(id bigserial primary key,trade_id text not null,deal_id text,accrual_date date not null,amount numeric not null,source text not null,broker_reference text,details jsonb default '{}'::jsonb,created_at timestamptz default now(),unique(trade_id,accrual_date,source,broker_reference)); ALTER TABLE trades ADD COLUMN IF NOT EXISTS spread_cost numeric; ALTER TABLE trades ADD COLUMN IF NOT EXISTS commission numeric default 0; ALTER TABLE trades ADD COLUMN IF NOT EXISTS financing_accrued numeric default 0; ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_gross numeric; ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_net numeric;`);return true;}
export async function runFinancing(db){
  await initFinancing();const day=londonDay(),open=(db.data.trades||[]).filter(t=>['OPEN','PARTIAL'].includes(t.status)&&String(t.scanner)!=='crypto'&&String(t.scanner)!=='fmp_alpaca');
  if(!open.length)return {day,rows:[]};
  const {base,headers}=await session(),fmt=d=>d.toISOString().slice(0,19),from=new Date(Date.now()-2*86400000),to=new Date();
  const tx=await fetch(`${base}/history/transactions?from=${encodeURIComponent(fmt(from))}&to=${encodeURIComponent(fmt(to))}&pageSize=500&pageNumber=1`,{headers,signal:AbortSignal.timeout(20000)}).then(r=>r.json());
  const swaps=(tx.transactions||[]).filter(x=>x.transactionType==='SWAP'&&x.status==='PROCESSED'),rows=[];
  for(const t of open){
    let candidates=swaps.filter(x=>String(x.dealId||x.deal_id||x.positionId||'')===String(t.deal_id)&&new Date(x.dateUtc||x.date)>=new Date(t.opened_at||t.ts));
    if(!candidates.length){
      const m=await fetch(`${base}/markets/${encodeURIComponent(t.ticker)}`,{headers,signal:AbortSignal.timeout(20000)}).then(r=>r.json());
      const rate=Number(['SELL','SHORT'].includes(String(t.direction).toUpperCase())?m.instrument?.overnightFee?.shortRate:m.instrument?.overnightFee?.longRate);
      if(Number.isFinite(rate)){const exposure=Number(t.entry||m.snapshot?.bid||0)*Number(t.size||0);candidates=[{size:String(exposure*rate/100),dateUtc:new Date().toISOString(),reference:`modeled:${day}:${t.deal_id}`,modeled:true,rate,exposure,currency:m.instrument?.currency}];}
    }
    for(const x of candidates){
      const amount=-Number(x.size),source=x.modeled?'capital_published_rate':'capital_actual';
      const q=await pool.query(`INSERT INTO financing_accruals(trade_id,deal_id,accrual_date,amount,source,broker_reference,details) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING RETURNING *`,[String(t.id),t.deal_id,String(x.dateUtc||x.date).slice(0,10),amount,source,String(x.reference),x]);
      if(!q.rowCount)continue;
      t.financing_accrued=Number(t.financing_accrued||0)+amount;t.financing_source=source;t.financing_nights=Number(t.financing_nights||0)+1;
      await pool.query(`UPDATE trades SET financing_accrued=$2,data=jsonb_set(jsonb_set(jsonb_set(coalesce(data,'{}'::jsonb),'{financing_accrued}',to_jsonb($2::numeric),true),'{financing_source}',to_jsonb($4::text),true),'{financing_nights}',to_jsonb($3::int),true) WHERE id=$1`,[String(t.id),t.financing_accrued,t.financing_nights,source]);
      rows.push({trade_id:t.id,ticker:t.ticker,amount,source,reference:x.reference,nights:t.financing_nights});
    }
  }
  return {day,rows};
}
export async function financingDigestLines(){if(!pool)return[];const {rows}=await pool.query(`SELECT t.ticker,count(a.id)::int nights,sum(a.amount)::float8 amount FROM financing_accruals a JOIN trades t ON t.id::text=a.trade_id WHERE a.accrual_date>=current_date-7 GROUP BY t.ticker ORDER BY t.ticker`);return rows.map(r=>`${r.ticker} held ${r.nights} night${r.nights===1?'':'s'}: ${r.amount>0?'-':'+'}£${Math.abs(Number(r.amount)).toFixed(2)}`);}
export function startFinancingJob(db,{save}){setInterval(async()=>{const p=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',hour:'2-digit',minute:'2-digit',hour12:false,hourCycle:'h23'}).format(new Date()),d=londonDay();if(p<'00:30'||lastLondonDay===d)return;lastLondonDay=d;const r=await runFinancing(db);if(r.rows.length&&save)await save();},60000).unref?.();}
