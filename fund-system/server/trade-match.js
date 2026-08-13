export function findTradeForMutation(trades, { deal_id, ticker } = {}) {
  return deal_id
    ? (trades || []).find(t => t.deal_id === deal_id)
    : (trades || []).find(t => ticker && t.ticker === ticker && ['OPEN','PARTIAL'].includes(t.status));
}
