# Feature inventory (synthetic fixture)

Tier legend, not features:

| Tier | Meaning |
|---|---|
| **P0** | skeleton |
| A9 | P0 | a row-shaped line outside any area section, ignored |

## A. Widgets

| # | Feature | Tier | Surfaces |
|---|---|---|---|
| A1 | Spin a widget | P0 | core |
| A2 | Paint a widget | P1 | core |
| A3 | Plain colours first, gradients later | P0 → P1 | core |
| A4 | Old blinking mode (dropped by a decision) | — | core |

## B. Exports

| # | Feature | Tier | Surfaces |
|---|---|---|---|
| B1 | Export widgets | P2 | CLI |
| B2 | Basic export now, rich export later | P1 → P2 | CLI |
| B3 | Fancy export | P2 → P3 | CLI |
| C1 | Row of another area inside B, ignored | P0 | CLI |

## Counting it up

| Tier | Rows |
|---|---|
| P0 | 3 |
| P1 | 2 |
| — (dropped) | 1 |

| Area | Rows | P0 | P1 |
|---|---|---|---|
| A widgets | 4 | 2 | 1 |
