# Clean data set — HW4

These 8 files are produced by `Python Scripts/02_clean.py` from `Data/Raw/`
(the raw files are never modified). Each file has the same name and the same
columns as its raw version, plus the flag columns listed below. Every decision
refers to `decision_log.md` (#1–#19).

| Table | Raw rows | Clean rows | Quarantined |
|---|---:|---:|---:|
| dim_advisor | 25 | 25 | 0 |
| dim_benchmark | 8 | 8 | 0 |
| dim_client | 2,500 | 2,500 | 0 |
| dim_date | 1,827 | 1,827 | 0 |
| dim_security | 500 | 500 | 0 |
| fact_fees | 122,197 | 122,197 | 0 |
| fact_portfolio_snapshot | 129,547 | 129,547 | 0 |
| fact_transactions | 298,572 | 296,162 | 2,410 |

Audit trail is in `Data/Audit/`:

- `cleaning_log.csv`: every step, check and decision.
- `values_changed.csv`: one row per changed cell, 3,225 rows in total.
- `quarantine_fact_transactions_post_departure.csv`: the removed rows, with their source line and the reason.

To rebuild everything, run `python3 "Python Scripts/02_clean.py"`. The script
stops without writing anything if any check fails.

---

## 1. Relationships

This is a star schema. Every relationship is **many (fact) → one (dimension)**,
with single-direction filtering from the dimension to the fact.

| From (many) | To (one) | Notes |
|---|---|---|
| fact_fees.client_id | dim_client.client_id | |
| fact_fees.advisor_id | dim_advisor.advisor_id | |
| fact_fees.fee_date | dim_date.date | always the 1st of the month |
| fact_portfolio_snapshot.client_id | dim_client.client_id | |
| fact_portfolio_snapshot.advisor_id | dim_advisor.advisor_id | |
| fact_portfolio_snapshot.benchmark_id | dim_benchmark.benchmark_id | |
| fact_portfolio_snapshot.snapshot_date | dim_date.date | always the 1st of the month |
| fact_transactions.client_id | dim_client.client_id | |
| fact_transactions.advisor_id | dim_advisor.advisor_id | |
| fact_transactions.security_id | dim_security.security_id | blank for cash movements (#12) |
| fact_transactions.txn_date | dim_date.date | daily |
| dim_client.advisor_id | dim_advisor.advisor_id | snowflake link; see below |
| dim_security.benchmark_id | dim_benchmark.benchmark_id | snowflake link |

Things to know about the joins:

- **Ambiguous paths in Power BI.** Each fact table reaches `dim_advisor` both directly and through `dim_client`. In the clean data both paths give the same advisor for 100% of rows. Keep the fact → `dim_advisor` relationship **active** and mark `dim_client` → `dim_advisor` **inactive**. Use `USERELATIONSHIP` only when you need the client's assigned advisor.
- **Fees and snapshots describe the same client-month.** Join them on `client_id` + date. There is exactly one fee row per client-month, and `fee.aum_basis = snapshot.aum` in every matched row.
- **Primary keys.** Every table's key is unique and non-null. Every foreign key in the clean set resolves (0 orphans).

## 2. Column types for a BI tool

Set these explicitly. Don't let the tool auto-detect them: auto-detect will turn
`zip_code` and `account_type = 529` into numbers.

| Type (Power BI name) | Columns |
|---|---|
| **Whole number** | all `*_id` columns; `age`, `years_experience`, `aum_target_mm`; `dim_date.year`, `quarter`, `month`, `week_of_year`; snapshot `year`, `month`; `expense_ratio_bps` (blank allowed) |
| **Fixed decimal / currency** | `initial_aum`, `aum`, `aum_basis`, `amount` (fees and transactions), `price` |
| **Decimal number** | `shares` (4 dp), `bps_charged`, `return_ytd`, `benchmark_return_ytd`, `active_alpha_ytd`, `cash_pct` (these four are fractions: 0.05 = 5%; format them as %) |
| **Date** | `client_since`, `dim_date.date`, `fee_date`, `snapshot_date`, `txn_date` (all YYYY-MM-DD) |
| **True/False** | every `is_*` and `has_*` column (stored as 1/0) |
| **Text** | `zip_code` (must stay text, because 228 zips have a leading zero), `primary_account_type` and `account_type` (contain "529"), plus every name, category and ticker column |

Blank cells that are not errors: `security_id`, `shares` and `price` for cash
transactions, `sector` for ETFs, and `expense_ratio_bps` for individual stocks.
Leave them blank (null). Don't replace them with 0.

## 3. Flag columns added

Every added flag is 0/1, and no flag changes the value it describes.

| Table | Column | 1 means | Rows = 1 | Decision |
|---|---|---|---:|---|
| fact_portfolio_snapshot | `is_aum_floor` | `aum` is the 1,000.00 placeholder floor, not a real balance | 420 | #1 |
| fact_fees | `is_aum_floor` | the fee was billed on the 1,000.00 floor (fee $0.65–$0.80) | 386 | #1 |
| fact_portfolio_snapshot | `has_fee_record` | a fee row exists for this client-month (**0 = no fee billed**) | 122,197 (7,350 are 0) | #4 |
| dim_client | `is_risk_profile_unknown` | `risk_profile` is "Unknown"; the client was not assessed | 75 | #6 |

Existing source flags that were kept:

- **`is_return_suspect`** (3,875 rows): this already covers all 871 extreme `return_ytd` values (#2). Exclude these rows from performance analysis.
- **`is_post_departure_txn`**: the column is kept so the schema stays identical. It is 0 for every clean row, because the 2,410 rows that were 1 are in quarantine (#3).

Suggested default filter for performance and revenue visuals:
`is_return_suspect = 0 AND is_aum_floor = 0`.

## 4. What was changed

| Decision | Change | Cells |
|---|---|---:|
| #5 | `certifications` "None" → "No Certification" | 6 |
| #7 | `is_active`, `is_etf` True/False → 1/0 (dim_date and fact flags were already 0/1) | 3,000 |
| #9 | `expense_ratio_bps` "17.0" → "17" | 219 |
| #3 | 2,410 transactions moved to quarantine | 2,410 rows |

Every other value is byte-for-byte identical to raw. The script proves this by
re-reading the clean files and diffing them against raw.

## 5. Known limitations I chose not to fix

| Limitation | Why it was left |
|---|---|
| **146 floor rows are not `is_return_suspect`.** 274 of the 420 `is_aum_floor` snapshots carry the source's suspect flag and 146 do not (#1). | I don't know the source's rule for that flag. `is_aum_floor` covers all 420, so filter on both flags. |
| **Months next to a floor aren't flagged.** E.g. client 29 shows $1,013.46 and $1,016.47 right after the floor. | Only exactly 1,000.00 is provably a placeholder. Flagging "near" values would be a guess. |
| **7,350 snapshot months have no fee** (`has_fee_record = 0`) (#4). | The data can't tell missed billing from a waived fee. Creating fee rows would invent revenue. |
| **75 clients have risk_profile "Unknown"** (#6). | Filling them with "Balanced" from their benchmark would be circular, since the benchmark was itself a default. |
| **Quarantined transactions may include real wind-down cash flows** (#3). | All 2,410 rows are on inactive accounts after the last snapshot. They are kept in `Data/Audit`, so they can be restored if the business says otherwise. |
| **930 snapshot rows and 858 fee rows are dated before `client_since`** (#11). | Every one falls in the month the client joined. Monthly rows are dated the 1st, so this is a convention, not an error. |
| **`client_tier` is not updated as AUM changes** (#15). | The tier matches the `initial_aum` band for 100% of clients. It is an onboarding attribute, so don't use it as a current segment. |
| **1,532 clients joined before 2020, outside `dim_date`** (#16). | `client_since` is an attribute, not a join key. Don't relate it to `dim_date`. |
| **`asset_class` means different things in `dim_benchmark` and `dim_security`** (#17). | Index category vs. security type. Don't relate the two columns or merge their values. |
| **`is_trading_day` = 1 on every weekday, including market holidays** (#18). | It only matters when counting trading days. Use a holiday calendar for that. |
| **708 trades share client + security + date** (#14; 712 in raw, 4 were quarantined). | 0 exact duplicates. These are mostly a Buy and a Sell, or a Buy and a Dividend, on the same day. |
| **Derived columns were not recomputed** (#19). | `amount = aum_basis × bps / 120000`, `return_ytd = benchmark + alpha` and `amount = shares × price` already hold for every row, to the cent or to 6 dp. |
