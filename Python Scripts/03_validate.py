#!/usr/bin/env python3
"""
03_validate.py -- validate Data/Raw and Data/Clean side by side
================================================================

Reads the 8 tables from BOTH stages as text (dtype=str, keep_default_na=False)
and runs the same checks on each:

  1. Referential integrity   10 ID relationships + 3 joins to dim_date,
                             using check_referential_integrity()
  2. Business rules          every rule lives in the RULES table below
  3. Cross-table reconciliation   numbers that should equal other numbers
  4. IQR outlier flags       columns with no natural bound
  5. Scorecard               Reports/data_quality_scorecard.csv
                             (one row per table per stage)

Run:  python3 "Python Scripts/03_validate.py"
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STAGES = {"raw": ROOT / "Data" / "Raw", "clean": ROOT / "Data" / "Clean"}
REPORTS = ROOT / "Reports"

TABLES = ["dim_advisor", "dim_benchmark", "dim_client", "dim_date", "dim_security",
          "fact_fees", "fact_portfolio_snapshot", "fact_transactions"]

PK = {"dim_advisor": "advisor_id", "dim_benchmark": "benchmark_id",
      "dim_client": "client_id", "dim_date": "date_id", "dim_security": "security_id",
      "fact_fees": "fee_id", "fact_portfolio_snapshot": "snapshot_id",
      "fact_transactions": "txn_id"}

# ---------------------------------------------------------------------------
# RELATIONSHIPS  (child table, child key, parent table, parent key)
# ---------------------------------------------------------------------------
RELATIONSHIPS = [
    # 10 ID-based relationships
    ("dim_client", "advisor_id", "dim_advisor", "advisor_id"),
    ("dim_security", "benchmark_id", "dim_benchmark", "benchmark_id"),
    ("fact_fees", "client_id", "dim_client", "client_id"),
    ("fact_fees", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_portfolio_snapshot", "client_id", "dim_client", "client_id"),
    ("fact_portfolio_snapshot", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_portfolio_snapshot", "benchmark_id", "dim_benchmark", "benchmark_id"),
    ("fact_transactions", "client_id", "dim_client", "client_id"),
    ("fact_transactions", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_transactions", "security_id", "dim_security", "security_id"),
    # 3 joins to the calendar
    ("fact_fees", "fee_date", "dim_date", "date"),
    ("fact_portfolio_snapshot", "snapshot_date", "dim_date", "date"),
    ("fact_transactions", "txn_date", "dim_date", "date"),
]
# keys that are legitimately blank for some rows (decision log #12)
EXPECTED_NULL_KEYS = {("fact_transactions", "security_id"):
                      "blank for Deposit / Withdrawal / Advisory Fee (cash has no security)"}

# ---------------------------------------------------------------------------
# COLUMN TYPES  (used for % type validity)
#   int, float, date, bool, code5 (5-digit text), text
# ---------------------------------------------------------------------------
TYPES = {
    "dim_advisor": dict(advisor_id="int", first_name="text", last_name="text", region="text",
                        years_experience="int", aum_target_mm="int", certifications="text"),
    "dim_benchmark": dict(benchmark_id="int", name="text", asset_class="text", ticker_proxy="text"),
    "dim_client": dict(client_id="int", first_name="text", last_name="text", age="int", city="text",
                       state="text", zip_code="code5", risk_profile="text", client_tier="text",
                       primary_account_type="text", initial_aum="float", client_since="date",
                       advisor_id="int", is_active="bool"),
    "dim_date": dict(date_id="int", date="date", year="int", quarter="int", month="int",
                     month_name="text", week_of_year="int", day_of_week="text", is_month_end="bool",
                     is_quarter_end="bool", is_year_end="bool", is_trading_day="bool"),
    "dim_security": dict(security_id="int", ticker="text", name="text", asset_class="text",
                         sector="text", benchmark_id="int", is_etf="bool", expense_ratio_bps="int"),
    "fact_fees": dict(fee_id="int", client_id="int", advisor_id="int", fee_date="date",
                      fee_type="text", aum_basis="float", bps_charged="float", amount="float",
                      is_aum_floor="bool"),
    "fact_portfolio_snapshot": dict(snapshot_id="int", client_id="int", advisor_id="int",
                                    benchmark_id="int", snapshot_date="date", year="int", month="int",
                                    aum="float", return_ytd="float", benchmark_return_ytd="float",
                                    active_alpha_ytd="float", cash_pct="float", account_type="text",
                                    is_return_suspect="bool", is_aum_floor="bool",
                                    has_fee_record="bool"),
    "fact_transactions": dict(txn_id="int", client_id="int", advisor_id="int", security_id="int",
                              txn_date="date", txn_type="text", shares="float", price="float",
                              amount="float", is_post_departure_txn="bool"),
}
TYPES["dim_client"]["is_risk_profile_unknown"] = "bool"     # flag added in clean

# Blanks that are part of the design, not missing data (excluded from % completeness)
CASH_TYPES = ["Deposit", "Withdrawal", "Advisory Fee"]
OPTIONAL_BLANKS = {
    ("fact_transactions", "security_id"): ("txn_type", CASH_TYPES),
    ("fact_transactions", "shares"): ("txn_type", CASH_TYPES),
    ("fact_transactions", "price"): ("txn_type", CASH_TYPES),
    ("dim_security", "sector"): ("is_etf", ["True", "1"]),
    ("dim_security", "expense_ratio_bps"): ("is_etf", ["False", "0"]),
}
# Text that means "no value" even though the cell is not empty
PLACEHOLDERS = {"none", "null", "nan", "n/a", "na", "unknown", "-", "?"}

# ---------------------------------------------------------------------------
# RULES  -- every business rule in one table.
#   kind: range (lo, hi) | positive | allowed [list] | regex pattern
#         in_calendar (within dim_date min..max) | first_of_month | equals value
#   Rules are evaluated on non-blank cells; blanks are scored by completeness.
# ---------------------------------------------------------------------------
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
             "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
BOOL_TOKENS = ["True", "False", "1", "0"]

RULE_COLUMNS = ["rule_id", "table", "column", "kind", "spec", "why"]
RULES = [
    # --- dim_advisor
    ("A1", "dim_advisor", "years_experience", "range", (0, 60), "a career is 0-60 years"),
    ("A2", "dim_advisor", "aum_target_mm", "allowed", ["50", "75", "100", "150", "200"], "target bands in use"),
    ("A3", "dim_advisor", "region", "allowed", ["Northeast", "Mid-Atlantic", "Southeast", "Midwest", "West"], "sales regions"),
    ("A4", "dim_advisor", "certifications", "allowed",
     ["CFA", "CFP", "CFP, CFA", "CFP, CIMA", "No Certification"], "known credentials (#5)"),
    # --- dim_benchmark
    ("B1", "dim_benchmark", "asset_class", "allowed",
     ["Equity", "Fixed Income", "Multi-Asset", "International", "Small Cap", "Emerging", "Cash"], "index categories"),
    ("B2", "dim_benchmark", "ticker_proxy", "regex", r"^[A-Z0-9_]{3,12}$", "ticker-style code"),
    # --- dim_client
    ("C1", "dim_client", "age", "range", (18, 110), "adult clients only"),
    ("C2", "dim_client", "state", "allowed", US_STATES, "US state / DC code"),
    ("C3", "dim_client", "zip_code", "regex", r"^\d{5}$", "5-digit ZIP, leading zeros kept (#8)"),
    ("C4", "dim_client", "risk_profile", "allowed",
     ["Conservative", "Moderately Conservative", "Balanced", "Moderately Aggressive", "Aggressive"],
     "assessed profiles ('Unknown' = not assessed, #6)"),
    ("C5", "dim_client", "client_tier", "allowed", ["Retail", "HNW", "UHNW"], "service tiers"),
    ("C6", "dim_client", "primary_account_type", "allowed", ["Taxable", "IRA", "Roth IRA", "Trust", "529"], "account types"),
    ("C7", "dim_client", "initial_aum", "positive", None, "a client brings money"),
    ("C8", "dim_client", "client_since", "range_date", ("1990-01-01", "2024-12-31"), "not before the firm, not after the data"),
    ("C9", "dim_client", "is_active", "allowed", BOOL_TOKENS, "boolean"),
    # --- dim_date
    ("D1", "dim_date", "year", "range", (2020, 2024), "calendar coverage"),
    ("D2", "dim_date", "quarter", "range", (1, 4), "quarters"),
    ("D3", "dim_date", "month", "range", (1, 12), "months"),
    ("D4", "dim_date", "week_of_year", "range", (1, 53), "ISO weeks"),
    ("D5", "dim_date", "month_name", "allowed",
     ["January", "February", "March", "April", "May", "June", "July", "August", "September",
      "October", "November", "December"], "month names"),
    ("D6", "dim_date", "day_of_week", "allowed",
     ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], "day names"),
    ("D7", "dim_date", "is_trading_day", "allowed", BOOL_TOKENS, "boolean"),
    # --- dim_security
    ("S1", "dim_security", "ticker", "regex", r"^[A-Z]{2,5}$", "2-5 capital letters"),
    ("S2", "dim_security", "asset_class", "allowed", ["Equity", "Fixed Income", "Alternative", "Cash Equivalent"], "security types"),
    ("S3", "dim_security", "sector", "allowed",
     ["Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials",
      "Healthcare", "Industrials", "Materials", "Real Estate", "Technology", "Utilities"], "GICS-style sectors"),
    ("S4", "dim_security", "expense_ratio_bps", "range", (0, 300), "fund fees are 0-3% a year"),
    ("S5", "dim_security", "is_etf", "allowed", BOOL_TOKENS, "boolean"),
    # --- fact_fees
    ("F1", "fact_fees", "fee_date", "in_calendar", None, "date exists in dim_date's range"),
    ("F2", "fact_fees", "fee_date", "first_of_month", None, "monthly billing is dated the 1st"),
    ("F3", "fact_fees", "fee_type", "allowed", ["Management", "Advisory", "Custodial", "Performance"], "fee types"),
    ("F4", "fact_fees", "aum_basis", "positive", None, "fees are billed on a positive balance"),
    ("F5", "fact_fees", "bps_charged", "range", (0, 300), "annual fee 0-3% of AUM"),
    ("F6", "fact_fees", "amount", "positive", None, "a fee is a positive charge"),
    # --- fact_portfolio_snapshot
    ("P1", "fact_portfolio_snapshot", "snapshot_date", "in_calendar", None, "date exists in dim_date's range"),
    ("P2", "fact_portfolio_snapshot", "snapshot_date", "first_of_month", None, "month-start snapshots"),
    ("P3", "fact_portfolio_snapshot", "year", "range", (2020, 2024), "calendar coverage"),
    ("P4", "fact_portfolio_snapshot", "month", "range", (1, 12), "months"),
    ("P5", "fact_portfolio_snapshot", "aum", "positive", None, "market value is positive"),
    ("P6", "fact_portfolio_snapshot", "return_ytd", "range", (-1, 3), "cannot lose >100%; >+300% YTD implausible (#2)"),
    ("P7", "fact_portfolio_snapshot", "benchmark_return_ytd", "range", (-1, 1), "index YTD return"),
    ("P8", "fact_portfolio_snapshot", "cash_pct", "range", (0, 1), "a share of the portfolio"),
    ("P9", "fact_portfolio_snapshot", "account_type", "allowed", ["Taxable", "IRA", "Roth IRA", "Trust", "529"], "account types"),
    # --- fact_transactions
    ("T1", "fact_transactions", "txn_date", "in_calendar", None, "date exists in dim_date's range"),
    ("T2", "fact_transactions", "txn_type", "allowed",
     ["Buy", "Sell", "Dividend", "Deposit", "Withdrawal", "Advisory Fee"], "transaction types"),
    ("T3", "fact_transactions", "amount", "positive", None, "amounts are unsigned, positive"),
    ("T4", "fact_transactions", "shares", "positive", None, "trades move a positive share count"),
    ("T5", "fact_transactions", "price", "positive", None, "prices are positive"),
    ("T6", "fact_transactions", "is_post_departure_txn", "equals", "0", "no activity on closed accounts (#3)"),
]

# IQR outlier columns (no natural upper bound)
IQR_COLUMNS = [("fact_portfolio_snapshot", "aum"), ("fact_transactions", "amount"),
               ("dim_client", "initial_aum")]

MONEY_TOL = 0.0051       # amounts are rounded to the cent
RETURN_TOL = 1.5e-6      # returns are rounded to 6 dp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load(stage: str) -> dict:
    out = {}
    for t in TABLES:
        out[t] = pd.read_csv(STAGES[stage] / f"{t}.csv", dtype=str,
                             keep_default_na=False, na_filter=False)
    return out


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.where(s.str.strip() != ""), errors="coerce")


def section(title: str):
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")


def pct(a, b):
    return round(100.0 * a / b, 3) if b else np.nan


# ---------------------------------------------------------------------------
# 1. Referential integrity
# ---------------------------------------------------------------------------
def check_referential_integrity(fact_df: pd.DataFrame, fact_key: str,
                                dim_df: pd.DataFrame, dim_key: str) -> dict:
    """Compare keys as TEXT (so '1' vs '1.0' or ' 1' would count as orphans).

    Returns match rate (% of non-null keys found in the dimension),
    orphan rows, distinct orphan keys, and null-key rate (% of all rows).
    """
    fk = fact_df[fact_key].astype(str)
    is_null = fk.str.strip().eq("")
    valid = set(dim_df[dim_key].astype(str))
    present = fk[~is_null]
    orphan = ~present.isin(valid)
    n, nn = len(fk), len(present)
    return {
        "rows": n,
        "non_null_keys": nn,
        "match_rate_pct": pct(nn - int(orphan.sum()), nn),
        "orphan_rows": int(orphan.sum()),
        "distinct_orphan_keys": int(present[orphan].nunique()),
        "null_key_rate_pct": pct(int(is_null.sum()), n),
    }


def run_referential(data: dict, stage: str) -> pd.DataFrame:
    rows = []
    for ct, ck, pt, pk in RELATIONSHIPS:
        r = check_referential_integrity(data[ct], ck, data[pt], pk)
        rows.append({"stage": stage, "relationship": f"{ct}.{ck} -> {pt}.{pk}",
                     "child": ct, **r})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Business rules
# ---------------------------------------------------------------------------
def evaluate_rule(data: dict, rule) -> tuple[int, int, list]:
    rid, t, c, kind, spec, _ = rule
    s = data[t][c].astype(str)
    present = s.str.strip() != ""
    v = s[present]
    if kind == "range":
        x = pd.to_numeric(v, errors="coerce")
        ok = x.between(spec[0], spec[1])
    elif kind == "positive":
        ok = pd.to_numeric(v, errors="coerce") > 0
    elif kind == "allowed":
        ok = v.isin(spec)
    elif kind == "regex":
        ok = v.str.fullmatch(spec)
    elif kind == "equals":
        ok = v == spec
    elif kind in ("in_calendar", "range_date", "first_of_month"):
        d = pd.to_datetime(v, format="%Y-%m-%d", errors="coerce")
        if kind == "in_calendar":
            cal = pd.to_datetime(data["dim_date"]["date"], format="%Y-%m-%d", errors="coerce")
            ok = d.between(cal.min(), cal.max())
        elif kind == "range_date":
            ok = d.between(pd.Timestamp(spec[0]), pd.Timestamp(spec[1]))
        else:
            ok = d.dt.day == 1
    else:
        raise ValueError(f"unknown rule kind {kind}")
    ok = ok.fillna(False).astype(bool)
    fails = v[~ok]
    return len(v), int(ok.sum()), list(pd.unique(fails))[:3]


def run_rules(data: dict, stage: str) -> pd.DataFrame:
    rows = []
    for rule in RULES:
        rid, t, c, kind, spec, why = rule
        evaluated, passed, ex = evaluate_rule(data, rule)
        rows.append({"stage": stage, "rule_id": rid, "table": t, "column": c, "kind": kind,
                     "evaluated": evaluated, "passed": passed, "failed": evaluated - passed,
                     "pass_pct": pct(passed, evaluated), "fail_examples": ", ".join(map(str, ex)),
                     "why": why})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. Cross-table reconciliation
# ---------------------------------------------------------------------------
def recon_row(stage, name, compared, diff, tol, note=""):
    diff = diff.dropna().abs()
    match = int((diff <= tol).sum())
    return {"stage": stage, "check": name, "compared": int(compared), "matching": match,
            "match_pct": pct(match, compared),
            "mean_abs_diff": round(float(diff.mean()), 6) if len(diff) else np.nan,
            "max_abs_diff": round(float(diff.max()), 6) if len(diff) else np.nan,
            "note": note}


def run_reconciliation(data: dict, stage: str) -> pd.DataFrame:
    f, s, t = data["fact_fees"], data["fact_portfolio_snapshot"], data["fact_transactions"]
    c = data["dim_client"]
    out = []

    # R1 trade amount = shares x price
    tr = t[t["shares"] != ""]
    d = num(tr["amount"]) - num(tr["shares"]) * num(tr["price"])
    out.append(recon_row(stage, "R1 txn amount = shares x price (trades)", len(tr), d, MONEY_TOL))

    # R2 fee amount = aum_basis x bps / 12 / 10,000
    d = num(f["amount"]) - num(f["aum_basis"]) * num(f["bps_charged"]) / 120000
    out.append(recon_row(stage, "R2 fee amount = aum_basis x bps / 120,000", len(f), d, MONEY_TOL))

    # R3 fee aum_basis = snapshot aum for the same client + month (two tables, one fact)
    m = f.merge(s[["client_id", "snapshot_date", "aum"]], left_on=["client_id", "fee_date"],
                right_on=["client_id", "snapshot_date"], how="inner")
    d = num(m["aum_basis"]) - num(m["aum"])
    out.append(recon_row(stage, "R3 fact_fees.aum_basis = snapshot.aum (client+month)", len(m), d, 0.005,
                         f"{len(f) - len(m)} fee rows had no snapshot"))

    # R4 return_ytd = benchmark_return_ytd + active_alpha_ytd
    d = num(s["return_ytd"]) - (num(s["benchmark_return_ytd"]) + num(s["active_alpha_ytd"]))
    out.append(recon_row(stage, "R4 return_ytd = benchmark + active_alpha", len(s), d, RETURN_TOL))

    # R5 every client on the same benchmark + month reports the same benchmark return
    br = num(s["benchmark_return_ytd"])
    med = br.groupby([s["benchmark_id"], s["snapshot_date"]]).transform("median")
    out.append(recon_row(stage, "R5 benchmark_return_ytd identical per benchmark+month", len(s),
                         br - med, RETURN_TOL))

    # R6 snapshot advisor = client's advisor in dim_client
    adv = s["client_id"].map(c.set_index("client_id")["advisor_id"])
    same = (adv == s["advisor_id"])
    out.append({"stage": stage, "check": "R6 snapshot.advisor_id = dim_client.advisor_id",
                "compared": len(s), "matching": int(same.sum()), "match_pct": pct(int(same.sum()), len(s)),
                "mean_abs_diff": np.nan, "max_abs_diff": np.nan, "note": "categorical: equal / not equal"})

    # R7 fees billed (fact_fees) vs Advisory Fee debits (fact_transactions), per client-month
    fm = num(f["amount"]).groupby([f["client_id"], f["fee_date"].str[:7]]).sum()
    af = t[t["txn_type"] == "Advisory Fee"]
    tm = num(af["amount"]).groupby([af["client_id"], af["txn_date"].str[:7]]).sum()
    j = pd.concat([fm.rename("billed"), tm.rename("debited")], axis=1)
    both = j.dropna()
    ratio = (both["debited"] / both["billed"]).median()
    out.append(recon_row(stage, "R7 fact_fees.amount vs Advisory Fee debits (client+month)", len(both),
                         both["debited"] - both["billed"], 0.01,
                         f"only-billed months {int(j['debited'].isna().sum()):,}; only-debited "
                         f"{int(j['billed'].isna().sum()):,}; median debit/billed = {ratio:.2f}x; "
                         f"totals billed ${fm.sum():,.0f} vs debited ${tm.sum():,.0f}"))
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# 4. IQR outliers
# ---------------------------------------------------------------------------
def run_iqr(data: dict, stage: str) -> pd.DataFrame:
    rows = []
    for t, c in IQR_COLUMNS:
        x = num(data[t][c].astype(str)).dropna()
        q1, q3 = x.quantile([0.25, 0.75])
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        low, high = int((x < lo).sum()), int((x > hi).sum())
        rows.append({"stage": stage, "column": f"{t}.{c}", "values": len(x),
                     "Q1": round(q1, 2), "Q3": round(q3, 2), "lower_fence": round(lo, 2),
                     "upper_fence": round(hi, 2), "flagged_low": low, "flagged_high": high,
                     "flagged_pct": pct(low + high, len(x)), "max_value": round(float(x.max()), 2)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. Scorecard
# ---------------------------------------------------------------------------
def type_ok(s: pd.Series, typ: str) -> pd.Series:
    if typ == "int":
        return s.str.fullmatch(r"[+-]?\d+")
    if typ == "float":
        return pd.to_numeric(s, errors="coerce").notna()
    if typ == "date":
        return pd.to_datetime(s, format="%Y-%m-%d", errors="coerce").notna()
    if typ == "bool":
        return s.isin(BOOL_TOKENS)
    if typ == "code5":
        return s.str.fullmatch(r"\d{5}")
    return pd.Series(True, index=s.index)


def scorecard(data: dict, stage: str, rules: pd.DataFrame, ri: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for t in TABLES:
        d = data[t]
        n = len(d)
        expected = filled = typed_total = typed_ok = 0
        for c in d.columns:
            s = d[c].astype(str)
            blank = s.str.strip().eq("")
            placeholder = s.str.strip().str.lower().isin(PLACEHOLDERS)
            need = pd.Series(True, index=d.index)
            if (t, c) in OPTIONAL_BLANKS:                 # blank by design -> not expected
                col, vals = OPTIONAL_BLANKS[(t, c)]
                need = ~d[col].isin(vals)
            expected += int(need.sum())
            filled += int((need & ~blank & ~placeholder).sum())
            typ = TYPES[t].get(c, "text")
            v = s[~blank]
            typed_total += len(v)
            typed_ok += int(type_ok(v, typ).fillna(False).sum())
        k = d[PK[t]].astype(str)
        uniq = int((~k.duplicated(keep=False) & k.str.strip().ne("")).sum())
        r = rules[rules.table == t]
        rel = ri[ri.child == t]
        rows.append({
            "table": t, "stage": stage, "rows": n,
            "pct_completeness": pct(filled, expected),
            "pct_key_uniqueness": pct(uniq, n),
            "pct_type_validity": pct(typed_ok, typed_total),
            "pct_rule_pass": pct(int(r.passed.sum()), int(r.evaluated.sum())) if len(r) else np.nan,
            "pct_referential_match": pct(int((rel.non_null_keys - rel.orphan_rows).sum()),
                                         int(rel.non_null_keys.sum())) if len(rel) else np.nan,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
SHORT = {"rows": "rows", "match_rate_pct": "match%", "orphan_rows": "orphans",
         "distinct_orphan_keys": "orph_keys", "null_key_rate_pct": "null%",
         "compared": "compared", "match_pct": "match%", "mean_abs_diff": "mean|diff|",
         "max_abs_diff": "max|diff|", "values": "values", "lower_fence": "low_fence",
         "upper_fence": "high_fence", "flagged_low": "flag_low", "flagged_high": "flag_high",
         "flagged_pct": "flag%"}


def side_by_side(raw_df, clean_df, key, cols):
    """raw and clean next to each other: '<metric> raw' | '<metric> clean'."""
    a = raw_df.set_index(key)[cols]
    b = clean_df.set_index(key)[cols]
    out = pd.DataFrame(index=a.index.union(b.index, sort=False))
    for c in cols:
        out[f"{SHORT.get(c, c)} raw"] = a[c]
        out[f"{SHORT.get(c, c)} clean"] = b[c]
    out.index.name = key
    return out


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 70)
    data = {st: load(st) for st in STAGES}
    for st in STAGES:
        print(f"{st:5s}: " + ", ".join(f"{t}={len(data[st][t]):,}" for t in TABLES))

    # ---- 1. referential integrity ------------------------------------------
    section("1. REFERENTIAL INTEGRITY  (10 ID relationships + 3 joins to dim_date)")
    ri = {st: run_referential(data[st], st) for st in STAGES}
    view = side_by_side(ri["raw"], ri["clean"], "relationship",
                        ["rows", "match_rate_pct", "orphan_rows", "distinct_orphan_keys", "null_key_rate_pct"])
    print(view.to_string())
    for (t, c), why in EXPECTED_NULL_KEYS.items():
        print(f"\n  note: {t}.{c} nulls are expected -- {why}.")
    for st in STAGES:
        bad = ri[st][ri[st].orphan_rows > 0]
        print(f"  {st:5s}: {13 - len(bad)}/13 relationships fully matched"
              + ("" if bad.empty else f"; orphans in {', '.join(bad.relationship)}"))

    # ---- 2. rules ----------------------------------------------------------
    section(f"2. BUSINESS RULES  ({len(RULES)} rules from the RULES table)")
    rules = {st: run_rules(data[st], st) for st in STAGES}
    rr = rules["raw"].set_index("rule_id")
    rc = rules["clean"].set_index("rule_id")
    rule_view = pd.DataFrame({
        "table.column": rr.table + "." + rr.column, "kind": rr.kind,
        "failed (raw)": rr.failed, "pass % (raw)": rr.pass_pct,
        "failed (clean)": rc.failed, "pass % (clean)": rc.pass_pct,
        "examples (clean)": rc.fail_examples.where(rc.failed > 0, rr.fail_examples.where(rr.failed > 0, "")),
    })
    print(rule_view.to_string())
    for st in STAGES:
        r = rules[st]
        print(f"  {st:5s}: {int((r.failed == 0).sum())}/{len(r)} rules pass 100%; "
              f"{int(r.failed.sum()):,} failing values")
    print("  note: failures still in clean are known and flagged, not fixed:\n"
          "        C4 'Unknown' risk profile -> is_risk_profile_unknown (#6);\n"
          "        P6 extreme return_ytd -> is_return_suspect (#2).")

    # ---- 3. reconciliation -------------------------------------------------
    section("3. CROSS-TABLE RECONCILIATION  (numbers that should match other numbers)")
    rec = {st: run_reconciliation(data[st], st) for st in STAGES}
    rec_view = side_by_side(rec["raw"], rec["clean"], "check",
                            ["compared", "match_pct", "mean_abs_diff", "max_abs_diff"])
    print(rec_view.to_string())
    print("\n  notes:")
    for _, r in rec["clean"].iterrows():
        if r.note:
            print(f"   - {r.check[:2]} (clean): {r.note}")
    print("   - R1-R6 reconcile: derived columns are internally consistent and fees/snapshots\n"
          "     describe the same AUM. R7 does NOT reconcile: the Advisory Fee debits in\n"
          "     fact_transactions are not the fees billed in fact_fees for the same month.\n"
          "     Treat fact_fees as the billing source; do not sum both as revenue.")

    # ---- 4. IQR ------------------------------------------------------------
    section("4. IQR OUTLIER FLAGS  (below Q1 - 1.5xIQR or above Q3 + 1.5xIQR)")
    iqr = {st: run_iqr(data[st], st) for st in STAGES}
    iqr_view = side_by_side(iqr["raw"], iqr["clean"], "column",
                            ["values", "lower_fence", "upper_fence", "flagged_low", "flagged_high", "flagged_pct"])
    print(iqr_view.to_string())
    print("\n  NOTE: flagged does NOT mean wrong. These columns have no natural upper bound and are\n"
          "  heavily right-skewed: a UHNW client with $40M is a real client, not an error. The flag\n"
          "  only says 'far from the middle of the distribution' -- review before excluding anything.\n"
          "  (The IQR fence is also below zero for skewed data, so almost nothing is flagged low.)")

    # ---- 5. scorecard ------------------------------------------------------
    section("5. DATA QUALITY SCORECARD  (Reports/data_quality_scorecard.csv)")
    sc = pd.concat([scorecard(data[st], st, rules[st], ri[st]) for st in STAGES], ignore_index=True)
    sc["_o"] = sc.table.map({t: i for i, t in enumerate(TABLES)})
    sc = sc.sort_values(["_o", "stage"], ascending=[True, False]).drop(columns="_o")
    REPORTS.mkdir(parents=True, exist_ok=True)
    sc.to_csv(REPORTS / "data_quality_scorecard.csv", index=False)
    print(sc.to_string(index=False))
    print("\n  completeness counts placeholder text ('None', 'Unknown') as missing and excludes blanks\n"
          "  that are blank by design (cash transactions' security/shares/price; ETF sector;\n"
          "  stock expense ratio). Referential match is blank for tables with no foreign key.")

    # ---- summary -------------------------------------------------------------
    section("SUMMARY  raw -> clean (average across the 8 tables)")
    agg = sc.groupby("stage")[["pct_completeness", "pct_key_uniqueness", "pct_type_validity",
                                "pct_rule_pass", "pct_referential_match"]].mean().round(3)
    print(agg.loc[["raw", "clean"]].to_string())
    r_raw, r_cln = rules["raw"], rules["clean"]
    print(f"\n  referential : raw {13 - int((ri['raw'].orphan_rows > 0).sum())}/13 clean "
          f"{13 - int((ri['clean'].orphan_rows > 0).sum())}/13 relationships with 0 orphans")
    print(f"  rules       : failing values raw {int(r_raw.failed.sum()):,} -> clean {int(r_cln.failed.sum()):,}")
    fixed = r_raw[(r_raw.failed > 0)].merge(r_cln[["rule_id", "failed"]], on="rule_id", suffixes=("_raw", "_clean"))
    for _, r in fixed.iterrows():
        print(f"                {r.rule_id} {r.table}.{r.column}: {r.failed_raw:,} -> {r.failed_clean:,}")
    print("  reconcile   : " + "; ".join(f"{a[:2]} {b:.1f}%" for a, b in zip(rec['clean'].check, rec['clean'].match_pct)))
    print("  IQR flagged : " + "; ".join(f"{c} {n:,}" for c, n in
                                         zip(iqr['clean'].column, iqr['clean'].flagged_low + iqr['clean'].flagged_high)))
    print(f"\nwrote {REPORTS / 'data_quality_scorecard.csv'}")


if __name__ == "__main__":
    main()
