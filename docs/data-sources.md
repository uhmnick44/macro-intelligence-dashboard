# Data sources

The machine-readable registry is `config/series.yaml` (indicators) and
`config/sources.yaml` (sources). This page explains the choices and the known
limitations. Series IDs marked *verify* are checked against FRED metadata on every
ingest; mismatches are reported, never silently accepted.

## Known constraints (checked October 2026)

| Constraint | Consequence |
|---|---|
| ISM data was removed from FRED at ISM's request ([FRED, 2016](https://news.research.stlouisfed.org/2016/06/institute-for-supply-management-data-to-be-removed-from-fred/)); ISM's terms restrict reproducing or redistributing its PMI content without permission | ISM is entered manually into a local, gitignored `data/manual/ism.csv`; values are never committed |
| FRED limits ICE BofA index series (incl. HY OAS) to a rolling 3-year window from April 2026 | Chicago Fed NFCI is the primary financial-conditions measure; HY OAS is context only |
| LBMA gold and silver prices were removed from FRED | Gold comes from a labelled non-primary (`third_party`) daily source |
| FRED carries 10 years of daily S&P / Dow Jones history | Sufficient for MVP market context |
| Consensus forecasts have no free primary source | One provider, entered manually, registered with `terms_checked_at` before use |
| Intraday market data is not on FRED | MVP reactions are daily; intraday needs a vendor (Phase 9) |

## Indicators by pillar

**Growth:** Real GDP (`GDPC1`), GDP growth (`A191RL1Q225SBEA`, display only),
ISM Manufacturing and Services (manual), retail sales (`RSAFS`, nominal, context
only), industrial production (`INDPRO`), real PCE (`PCEC96`).

**Inflation:** CPI (`CPIAUCSL`), core CPI (`CPILFESL`), PCE prices (`PCEPI`),
core PCE (`PCEPILFE`), PPI final demand (`PPIFIS`, *verify*). NSA indexes
`CPIAUCNS` and `CPILFENS` (*verify*) are used only to compute the published
12-month CPI changes, which BLS reports before seasonal adjustment.

**Labor:** nonfarm payrolls (`PAYEMS`), unemployment rate (`UNRATE`), average
hourly earnings (`CES0500000003`, counted in Labor only), initial and continuing
claims (`ICSA`, `CCSA`), JOLTS openings (`JTSJOL`). ADP is deferred (ID unverified).

**Financial conditions / policy:** fed funds target range (`DFEDTARU`, `DFEDTARL`),
effective fed funds (`DFF`), 2Y and 10Y Treasuries (`DGS2`, `DGS10`), 10Y–2Y spread
(`T10Y2Y`), broad dollar (`DTWEXBGS`, used instead of the proprietary DXY), NFCI
(`NFCI`), HY OAS (`BAMLH0A0HYM2`, 3-year window), Fed total assets (`WALCL`).

**Markets (daily context):** S&P 500 (`SP500`), Nasdaq-100 (`NASDAQ100`, *verify*),
VIX (`VIXCLS`), gold (non-primary source).

Regime roles (`input` / `context` / `display`) are provisional until Phase 7.

## Vintages

CPI, core CPI (SA and NSA), payrolls, unemployment and average hourly earnings are
pulled with full ALFRED vintage history from 2024-06-01, so first prints and
revisions can be reconstructed. Other series use latest-revised values, stored
with `vintage_basis = 'retrieval_date'` so they are never mistaken for
point-in-time data.

## Manual files

Templates with the exact headers are in `data/manual/templates/`. Copy one into
`data/manual/` to use it; everything in `data/manual/` except the templates is
gitignored.
