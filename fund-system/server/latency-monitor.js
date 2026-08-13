import pg from 'pg';
const pool = process.env.DATABASE_URL ? new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 1, application_name: 'fund-latency-monitor' }) : null;
let lastAlertDay = '';
export async function latencyStats() {
  if (!pool) return [];
  const { rows } = await pool.query(`SELECT scanner,count(*)::int n,
    percentile_cont(.5) within group(order by coalesce(signal_to_order_ms,0)+coalesce(order_to_fill_ms,0))::bigint p50_ms,
    percentile_cont(.9) within group(order by coalesce(signal_to_order_ms,0)+coalesce(order_to_fill_ms,0))::bigint p90_ms
    FROM trades WHERE opened_at >= now()-interval '1 day' AND (signal_to_order_ms IS NOT NULL OR order_to_fill_ms IS NOT NULL) GROUP BY scanner`);
  return rows.map(r => ({ ...r, too_thin: Number(r.n) < 30 }));
}
export function startLatencyMonitor({ sendTelegramAlert }) {
  const run = async () => {
    const day = new Date().toISOString().slice(0,10); if (lastAlertDay === day) return;
    const slow = (await latencyStats()).filter(r => Number(r.p90_ms) > 60000); if (!slow.length) return;
    await sendTelegramAlert(`EXECUTION LATENCY p90 >60s (24h):\n${slow.map(r=>`${r.scanner}: ${(r.p90_ms/1000).toFixed(1)}s (n=${r.n})`).join('\n')}`); lastAlertDay=day;
  };
  const timer=setInterval(()=>run().catch(e=>console.error('latency monitor failed',e.message)),15*60*1000); timer.unref?.(); run().catch(()=>{});
}
