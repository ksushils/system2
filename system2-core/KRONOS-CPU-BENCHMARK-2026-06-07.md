# Kronos CPU Benchmark

Date: 2026-06-07

## VPS

- CPU: 2 vCPU, AMD EPYC 9354P host
- RAM: 7.8 GiB
- Swap: 2 GiB `/swapfile`, persistent in `/etc/fstab`
- GPU: none
- Existing Chronos service remained healthy on port 8000

## Workload

- Official repository commit: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`
- Device: CPU
- PyTorch threads: 2
- Tickers: 40
- History: 252 daily sessions per ticker
- Forecast horizon: 5 daily sessions
- Ensemble: `n_samples=10`
- Processing: sequential per ticker
- Model weights pre-cached before timing
- Cold start: fresh-process model/tokenizer load from local cache

## Results

| Metric | Kronos-small | Kronos-base |
|---|---:|---:|
| Cold start | 1.463 s | 2.583 s |
| Warm inference, 40 tickers | 368.303 s | 909.339 s |
| Warm inference | 6m 8.3s | 15m 9.3s |
| Mean per ticker | 9.207 s | 22.733 s |
| P95 per ticker | 10.032 s | 24.430 s |
| Peak process RAM | 0.876 GiB | 1.305 GiB |
| Peak total system RAM used | 2.767 GiB | 3.129 GiB |
| Minimum system RAM available | 4.988 GiB | 4.625 GiB |
| RAM available after inference | 5.085 GiB | 4.982 GiB |
| Swap used after inference | 0 GiB | 0.005 GiB |

Both models are below the accepted four-hour total-pipeline threshold. No
FastAPI service or pipeline integration was performed.

