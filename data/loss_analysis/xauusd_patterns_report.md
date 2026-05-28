# XAUUSD W/L Pattern Extraction — Strict SPEC rules
Generated: 2026-05-27T04:20:53.108373+00:00Z  •  read-only analysis
Source: 16y Dukascopy CSV (Bid+Ask) replayed through production
``analyzer.dws_smt`` + ``analyzer.signal_validator`` — no rule edits.
Win/loss split: net_pts = trade.points / point − bar_spread_pts > 0.

---

## BASE = M15   (3TF stack: H4/H1/M15)
- trades total : 233,416
- win count    : 76,145  (median net = +3685.0 pts)
- loss count   : 157,271  (median net = -2080.0 pts)
- win rate     : 32.6%

### Win patterns (k = 4)

**Win pattern #1** — size = 13,367  •  median net = +4373.0 pts
```
base_rsi=+63.98  base_adx=+34.66  base_di_diff=+15.57  base_atr_pct=+0.15  base_ema_dist=+1.36  base_ema_slope=+0.63  base_close_vs_ema50=+9.06  mid_rsi=+70.38  mid_adx=+34.46  mid_di_diff=+21.62  mid_atr_pct=+0.29  mid_ema_dist=+2.19  mid_close_vs_ema50=+20.82  top_rsi=+68.81  top_adx=+30.48  top_di_diff=+18.00  top_ema_dist=+2.09  top_close_vs_ema50=+34.59  hour_jst=+10.31  dow=+2.47  atr_pct_90d=+0.67
```

**Win pattern #2** — size = 10,720  •  median net = +4431.5 pts
```
base_rsi=+35.38  base_adx=+38.71  base_di_diff=+18.72  base_atr_pct=+0.17  base_ema_dist=+1.44  base_ema_slope=+0.71  base_close_vs_ema50=+9.51  mid_rsi=+29.64  mid_adx=+35.81  mid_di_diff=+24.19  mid_atr_pct=+0.32  mid_ema_dist=+2.26  mid_close_vs_ema50=+19.32  top_rsi=+33.72  top_adx=+29.91  top_di_diff=+18.65  top_ema_dist=+1.91  top_close_vs_ema50=+23.60  hour_jst=+11.41  dow=+2.43  atr_pct_90d=+0.70
```

**Win pattern #3** — size = 16,037  •  median net = +4386.0 pts
```
base_rsi=+52.46  base_adx=+30.08  base_di_diff=+21.59  base_atr_pct=+0.15  base_ema_dist=+2.14  base_ema_slope=+2.00  base_close_vs_ema50=+7.35  mid_rsi=+51.58  mid_adx=+21.95  mid_di_diff=+8.38  mid_atr_pct=+0.27  mid_ema_dist=+1.15  mid_close_vs_ema50=+4.79  top_rsi=+50.97  top_adx=+25.04  top_di_diff=-1.83  top_ema_dist=+0.07  top_close_vs_ema50=-2.79  hour_jst=+11.85  dow=+2.39  atr_pct_90d=+0.64
```

**Win pattern #4** — size = 36,021  •  median net = +3050.0 pts
```
base_rsi=+50.57  base_adx=+21.66  base_di_diff=+7.43  base_atr_pct=+0.11  base_ema_dist=+0.70  base_ema_slope=+0.37  base_close_vs_ema50=+2.11  mid_rsi=+50.58  mid_adx=+21.38  mid_di_diff=+6.18  mid_atr_pct=+0.23  mid_ema_dist=+0.71  mid_close_vs_ema50=+4.12  top_rsi=+50.65  top_adx=+24.19  top_di_diff=+4.42  top_ema_dist=+0.54  top_close_vs_ema50=+6.46  hour_jst=+12.53  dow=+2.20  atr_pct_90d=+0.40
```

### Loss patterns (k = 4)

**Loss pattern #1** — size = 75,802  •  median net = -1570.0 pts
```
base_rsi=+50.62  base_adx=+21.40  base_di_diff=+6.55  base_atr_pct=+0.10  base_ema_dist=+0.56  base_ema_slope=+0.24  base_close_vs_ema50=+2.10  mid_rsi=+50.88  mid_adx=+23.10  mid_di_diff=+8.24  mid_atr_pct=+0.23  mid_ema_dist=+0.83  mid_close_vs_ema50=+5.58  top_rsi=+51.10  top_adx=+24.14  top_di_diff=+7.12  top_ema_dist=+0.82  top_close_vs_ema50=+9.24  hour_jst=+11.62  dow=+2.26  atr_pct_90d=+0.38
```

**Loss pattern #2** — size = 28,464  •  median net = -2765.0 pts
```
base_rsi=+62.55  base_adx=+34.11  base_di_diff=+13.94  base_atr_pct=+0.14  base_ema_dist=+1.16  base_ema_slope=+0.46  base_close_vs_ema50=+7.74  mid_rsi=+70.57  mid_adx=+37.08  mid_di_diff=+22.10  mid_atr_pct=+0.28  mid_ema_dist=+2.16  mid_close_vs_ema50=+20.06  top_rsi=+70.25  top_adx=+31.55  top_di_diff=+19.95  top_ema_dist=+2.27  top_close_vs_ema50=+34.11  hour_jst=+10.76  dow=+2.33  atr_pct_90d=+0.62
```

**Loss pattern #3** — size = 29,295  •  median net = -2937.0 pts
```
base_rsi=+51.40  base_adx=+31.92  base_di_diff=+21.86  base_atr_pct=+0.14  base_ema_dist=+2.10  base_ema_slope=+1.83  base_close_vs_ema50=+7.29  mid_rsi=+51.25  mid_adx=+22.51  mid_di_diff=+11.53  mid_atr_pct=+0.27  mid_ema_dist=+1.41  mid_close_vs_ema50=+7.05  top_rsi=+50.96  top_adx=+23.68  top_di_diff=+1.63  top_ema_dist=+0.41  top_close_vs_ema50=+1.91  hour_jst=+11.60  dow=+2.31  atr_pct_90d=+0.61
```

**Loss pattern #4** — size = 23,710  •  median net = -2862.5 pts
```
base_rsi=+37.55  base_adx=+36.70  base_di_diff=+15.88  base_atr_pct=+0.18  base_ema_dist=+1.16  base_ema_slope=+0.44  base_close_vs_ema50=+8.64  mid_rsi=+29.90  mid_adx=+38.26  mid_di_diff=+23.96  mid_atr_pct=+0.34  mid_ema_dist=+2.17  mid_close_vs_ema50=+20.07  top_rsi=+32.33  top_adx=+31.44  top_di_diff=+20.39  top_ema_dist=+2.08  top_close_vs_ema50=+26.62  hour_jst=+9.73  dow=+2.55  atr_pct_90d=+0.70
```

### Top discriminating features (mean of centroids)
| feature | win mean | loss mean | Δ (win − loss) |
|---|---:|---:|---:|
| top_close_vs_ema50 | +15.462 | +17.970 | -2.508 |
| top_di_diff | +9.811 | +12.271 | -2.460 |
| mid_adx | +28.398 | +30.237 | -1.839 |
| mid_di_diff | +15.092 | +16.459 | -1.366 |
| base_di_diff | +15.827 | +14.557 | +1.270 |
| mid_close_vs_ema50 | +12.262 | +13.191 | -0.929 |
| hour_jst | +11.524 | +10.927 | +0.598 |
| base_close_vs_ema50 | +7.006 | +6.445 | +0.562 |

## BASE = H1   (3TF stack: D1/H4/H1)
- trades total : 56,935
- win count    : 21,524  (median net = +8549.0 pts)
- loss count   : 35,411  (median net = -4274.0 pts)
- win rate     : 37.8%

### Win patterns (k = 4)

**Win pattern #1** — size = 2,089  •  median net = +20160.0 pts
```
base_rsi=+62.99  base_adx=+33.16  base_di_diff=+13.39  base_atr_pct=+0.36  base_ema_dist=+1.28  base_ema_slope=+0.61  base_close_vs_ema50=+24.79  mid_rsi=+68.87  mid_adx=+38.56  mid_di_diff=+21.48  mid_atr_pct=+0.69  mid_ema_dist=+2.10  mid_close_vs_ema50=+60.91  top_rsi=+71.95  top_adx=+35.72  top_di_diff=+24.40  top_ema_dist=+2.65  top_close_vs_ema50=+149.44  hour_jst=+11.60  dow=+2.24  atr_pct_90d=+0.80
```

**Win pattern #2** — size = 4,657  •  median net = +7360.0 pts
```
base_rsi=+68.21  base_adx=+35.23  base_di_diff=+19.96  base_atr_pct=+0.25  base_ema_dist=+1.89  base_ema_slope=+1.24  base_close_vs_ema50=+14.36  mid_rsi=+67.58  mid_adx=+27.50  mid_di_diff=+17.08  mid_atr_pct=+0.48  mid_ema_dist=+1.97  mid_close_vs_ema50=+21.91  top_rsi=+59.12  top_adx=+23.11  top_di_diff=+7.93  top_ema_dist=+1.06  top_close_vs_ema50=+31.75  hour_jst=+11.20  dow=+2.53  atr_pct_90d=+0.51
```

**Win pattern #3** — size = 10,688  •  median net = +8196.5 pts
```
base_rsi=+51.10  base_adx=+21.80  base_di_diff=+7.89  base_atr_pct=+0.23  base_ema_dist=+0.83  base_ema_slope=+0.58  base_close_vs_ema50=+5.10  mid_rsi=+51.22  mid_adx=+21.78  mid_di_diff=+5.26  mid_atr_pct=+0.48  mid_ema_dist=+0.68  mid_close_vs_ema50=+7.92  top_rsi=+51.01  top_adx=+22.71  top_di_diff=+3.35  top_ema_dist=+0.45  top_close_vs_ema50=+13.92  hour_jst=+12.75  dow=+2.27  atr_pct_90d=+0.39
```

**Win pattern #4** — size = 4,090  •  median net = +7918.5 pts
```
base_rsi=+33.42  base_adx=+37.19  base_di_diff=+20.58  base_atr_pct=+0.29  base_ema_dist=+1.72  base_ema_slope=+1.07  base_close_vs_ema50=+15.15  mid_rsi=+32.77  mid_adx=+31.49  mid_di_diff=+20.46  mid_atr_pct=+0.56  mid_ema_dist=+1.94  mid_close_vs_ema50=+23.85  top_rsi=+42.06  top_adx=+23.79  top_di_diff=+6.60  top_ema_dist=+0.93  top_close_vs_ema50=+11.96  hour_jst=+11.81  dow=+2.35  atr_pct_90d=+0.58
```

### Loss patterns (k = 4)

**Loss pattern #1** — size = 7,838  •  median net = -5465.0 pts
```
base_rsi=+34.45  base_adx=+38.81  base_di_diff=+19.39  base_atr_pct=+0.29  base_ema_dist=+1.57  base_ema_slope=+0.86  base_close_vs_ema50=+15.07  mid_rsi=+31.77  mid_adx=+33.64  mid_di_diff=+21.78  mid_atr_pct=+0.57  mid_ema_dist=+2.04  mid_close_vs_ema50=+26.64  top_rsi=+39.67  top_adx=+24.14  top_di_diff=+9.43  top_ema_dist=+1.20  top_close_vs_ema50=+22.97  hour_jst=+10.77  dow=+2.48  atr_pct_90d=+0.61
```

**Loss pattern #2** — size = 16,541  •  median net = -3187.0 pts
```
base_rsi=+50.41  base_adx=+22.05  base_di_diff=+6.65  base_atr_pct=+0.23  base_ema_dist=+0.68  base_ema_slope=+0.38  base_close_vs_ema50=+4.94  mid_rsi=+50.72  mid_adx=+22.85  mid_di_diff=+7.21  mid_atr_pct=+0.48  mid_ema_dist=+0.80  mid_close_vs_ema50=+10.94  top_rsi=+50.37  top_adx=+23.14  top_di_diff=+6.39  top_ema_dist=+0.75  top_close_vs_ema50=+23.33  hour_jst=+11.33  dow=+2.26  atr_pct_90d=+0.39
```

**Loss pattern #3** — size = 2,244  •  median net = -11408.5 pts
```
base_rsi=+58.90  base_adx=+35.98  base_di_diff=+14.30  base_atr_pct=+0.48  base_ema_dist=+1.24  base_ema_slope=+0.53  base_close_vs_ema50=+31.10  mid_rsi=+63.69  mid_adx=+42.84  mid_di_diff=+24.17  mid_atr_pct=+0.88  mid_ema_dist=+2.28  mid_close_vs_ema50=+76.96  top_rsi=+65.92  top_adx=+37.32  top_di_diff=+25.37  top_ema_dist=+2.75  top_close_vs_ema50=+158.82  hour_jst=+11.52  dow=+2.07  atr_pct_90d=+0.87
```

**Loss pattern #4** — size = 8,788  •  median net = -5061.5 pts
```
base_rsi=+65.60  base_adx=+34.30  base_di_diff=+16.55  base_atr_pct=+0.25  base_ema_dist=+1.53  base_ema_slope=+0.88  base_close_vs_ema50=+13.09  mid_rsi=+68.59  mid_adx=+30.40  mid_di_diff=+18.00  mid_atr_pct=+0.50  mid_ema_dist=+1.97  mid_close_vs_ema50=+25.83  top_rsi=+62.89  top_adx=+25.52  top_di_diff=+11.77  top_ema_dist=+1.48  top_close_vs_ema50=+45.39  hour_jst=+11.07  dow=+2.33  atr_pct_90d=+0.53
```

### Top discriminating features (mean of centroids)
| feature | win mean | loss mean | Δ (win − loss) |
|---|---:|---:|---:|
| top_close_vs_ema50 | +51.768 | +62.628 | -10.860 |
| mid_close_vs_ema50 | +28.644 | +35.093 | -6.448 |
| top_di_diff | +10.568 | +13.238 | -2.670 |
| mid_adx | +29.832 | +32.432 | -2.600 |
| mid_di_diff | +16.072 | +17.790 | -1.718 |
| base_rsi | +53.930 | +52.341 | +1.589 |
| mid_rsi | +55.114 | +53.694 | +1.420 |
| top_rsi | +56.034 | +54.712 | +1.322 |

## BASE = H4   (3TF stack: W1/D1/H4)
- trades total : 14,131
- win count    : 5,928  (median net = +17661.0 pts)
- loss count   : 8,203  (median net = -9635.0 pts)
- win rate     : 42.0%

### Win patterns (k = 4)

**Win pattern #1** — size = 1,246  •  median net = +16882.0 pts
```
base_rsi=+54.16  base_adx=+39.13  base_di_diff=+25.63  base_atr_pct=+0.54  base_ema_dist=+2.42  base_ema_slope=+1.84  base_close_vs_ema50=+32.97  mid_rsi=+52.02  mid_adx=+23.71  mid_di_diff=+15.26  mid_atr_pct=+1.32  mid_ema_dist=+1.86  mid_close_vs_ema50=+44.03  top_rsi=+52.30  top_adx=+23.52  top_di_diff=+4.06  top_ema_dist=+0.63  top_close_vs_ema50=+31.16  hour_jst=+12.89  dow=+2.19  atr_pct_90d=+0.54
```

**Win pattern #2** — size = 1,670  •  median net = +16874.5 pts
```
base_rsi=+55.85  base_adx=+24.73  base_di_diff=+7.80  base_atr_pct=+0.54  base_ema_dist=+0.78  base_ema_slope=+0.35  base_close_vs_ema50=+15.60  mid_rsi=+56.60  mid_adx=+27.32  mid_di_diff=+12.71  mid_atr_pct=+1.42  mid_ema_dist=+1.28  mid_close_vs_ema50=+54.89  top_rsi=+58.65  top_adx=+32.62  top_di_diff=+16.07  top_ema_dist=+1.50  top_close_vs_ema50=+135.76  hour_jst=+13.12  dow=+2.20  atr_pct_90d=+0.54
```

**Win pattern #3** — size = 2,449  •  median net = +14672.0 pts
```
base_rsi=+49.90  base_adx=+23.75  base_di_diff=+9.76  base_atr_pct=+0.44  base_ema_dist=+1.00  base_ema_slope=+0.68  base_close_vs_ema50=+10.27  mid_rsi=+48.65  mid_adx=+18.79  mid_di_diff=+4.30  mid_atr_pct=+1.23  mid_ema_dist=+0.61  mid_close_vs_ema50=+13.50  top_rsi=+47.89  top_adx=+20.72  top_di_diff=+1.07  top_ema_dist=+0.17  top_close_vs_ema50=+11.43  hour_jst=+13.02  dow=+2.18  atr_pct_90d=+0.28
```

**Win pattern #4** — size = 563  •  median net = +54263.0 pts
```
base_rsi=+64.36  base_adx=+36.33  base_di_diff=+15.98  base_atr_pct=+0.69  base_ema_dist=+1.56  base_ema_slope=+0.84  base_close_vs_ema50=+59.41  mid_rsi=+71.66  mid_adx=+37.69  mid_di_diff=+24.33  mid_atr_pct=+1.51  mid_ema_dist=+2.59  mid_close_vs_ema50=+176.73  top_rsi=+71.75  top_adx=+45.18  top_di_diff=+28.47  top_ema_dist=+2.67  top_close_vs_ema50=+423.23  hour_jst=+12.85  dow=+2.15  atr_pct_90d=+0.77
```

### Loss patterns (k = 4)

**Loss pattern #1** — size = 1,746  •  median net = -11803.0 pts
```
base_rsi=+38.36  base_adx=+39.68  base_di_diff=+21.50  base_atr_pct=+0.62  base_ema_dist=+1.79  base_ema_slope=+1.11  base_close_vs_ema50=+31.23  mid_rsi=+37.15  mid_adx=+26.57  mid_di_diff=+18.11  mid_atr_pct=+1.55  mid_ema_dist=+1.93  mid_close_vs_ema50=+54.31  top_rsi=+42.84  top_adx=+23.61  top_di_diff=+5.80  top_ema_dist=+0.84  top_close_vs_ema50=+32.34  hour_jst=+12.88  dow=+2.37  atr_pct_90d=+0.57
```

**Loss pattern #2** — size = 2,605  •  median net = -11005.0 pts
```
base_rsi=+59.79  base_adx=+26.18  base_di_diff=+8.98  base_atr_pct=+0.55  base_ema_dist=+0.97  base_ema_slope=+0.50  base_close_vs_ema50=+19.91  mid_rsi=+62.62  mid_adx=+30.22  mid_di_diff=+14.83  mid_atr_pct=+1.47  mid_ema_dist=+1.51  mid_close_vs_ema50=+70.20  top_rsi=+63.56  top_adx=+33.72  top_di_diff=+18.32  top_ema_dist=+1.76  top_close_vs_ema50=+156.87  hour_jst=+12.88  dow=+2.17  atr_pct_90d=+0.58
```

**Loss pattern #3** — size = 3,231  •  median net = -6908.0 pts
```
base_rsi=+52.28  base_adx=+24.34  base_di_diff=+9.31  base_atr_pct=+0.44  base_ema_dist=+0.93  base_ema_slope=+0.54  base_close_vs_ema50=+11.12  mid_rsi=+50.70  mid_adx=+20.01  mid_di_diff=+7.41  mid_atr_pct=+1.24  mid_ema_dist=+0.86  mid_close_vs_ema50=+22.01  top_rsi=+48.55  top_adx=+20.82  top_di_diff=+3.75  top_ema_dist=+0.48  top_close_vs_ema50=+27.62  hour_jst=+12.89  dow=+2.23  atr_pct_90d=+0.31
```

**Loss pattern #4** — size = 621  •  median net = -29148.0 pts
```
base_rsi=+66.77  base_adx=+42.63  base_di_diff=+20.68  base_atr_pct=+0.78  base_ema_dist=+1.90  base_ema_slope=+1.07  base_close_vs_ema50=+71.76  mid_rsi=+71.78  mid_adx=+39.52  mid_di_diff=+28.39  mid_atr_pct=+1.62  mid_ema_dist=+2.99  mid_close_vs_ema50=+195.02  top_rsi=+71.27  top_adx=+42.41  top_di_diff=+28.94  top_ema_dist=+2.85  top_close_vs_ema50=+388.16  hour_jst=+12.72  dow=+1.91  atr_pct_90d=+0.82
```

### Top discriminating features (mean of centroids)
| feature | win mean | loss mean | Δ (win − loss) |
|---|---:|---:|---:|
| mid_close_vs_ema50 | +72.288 | +85.385 | -13.097 |
| base_close_vs_ema50 | +29.560 | +33.504 | -3.944 |
| mid_di_diff | +14.150 | +17.186 | -3.036 |
| base_adx | +30.986 | +33.206 | -2.220 |
| mid_adx | +26.876 | +29.079 | -2.203 |
| top_di_diff | +12.419 | +14.201 | -1.782 |
| base_rsi | +56.067 | +54.300 | +1.767 |
| mid_rsi | +57.234 | +55.560 | +1.674 |
