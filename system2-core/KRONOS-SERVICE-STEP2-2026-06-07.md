# Kronos Service Step 2

Date: 2026-06-07

## Deployment

- Location: `/root/kronos-service`
- Port: `8001`
- Process: PM2 `kronos-service`
- Model: `NeoQuasar/Kronos-base`
- Device: CPU, two PyTorch threads
- Default ensemble: `n_samples=10`
- Weights cache: `/root/kronos-service/weights` (406 MB)
- Model loading: once during FastAPI startup
- PM2 process list saved
- Pipeline wiring: not performed

Health response:

```json
{
  "ok": true,
  "service": "kronos",
  "model": "kronos-base",
  "device": "cpu",
  "n_samples": 10,
  "load_seconds": 10.239
}
```

## Real FMP Test

The test used 60 daily FMP OHLCV bars for NVDA, AAPL, and FERG. The first
request completed in 14.784 seconds.

| Symbol | Direction | Band % | Conviction | 1d % | 3d % | 5d % |
|---|---:|---:|---:|---:|---:|---:|
| NVDA | DOWN | 8.0965 | 19.03 | -0.2822 | -0.8839 | -2.8433 |
| AAPL | UP | 5.3250 | 46.75 | 0.1096 | 0.0181 | 0.7167 |
| FERG | UP | 7.3404 | 26.60 | 1.0917 | 0.8667 | 0.8168 |

The second request completed in 14.864 seconds. All health checks before,
between, and after the requests returned the same model instance ID and load
timestamp. The model therefore stayed loaded; both HTTP requests were warm.
The 0.08-second difference is ordinary runtime variance.

Kronos sampling is stochastic, so repeated ten-sample ensembles can produce
different medians and uncertainty bands.

## Error Isolation

An invalid ticker payload returned `status="ERROR"` with all forecast fields
set to null. The service remained healthy and retained the same model instance.

