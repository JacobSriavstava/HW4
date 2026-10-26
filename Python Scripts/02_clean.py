#!/usr/bin/env python3
"""
02_clean.py -- clean the 8 raw HW4 tables according to decision_log.md
=======================================================================

Reads   Data/Raw/*.csv          (never modified)
Writes  Data/Clean/<same name>.csv      same columns + documented 0/1 flags
        Data/Audit/cleaning_log.csv     one row per transformation / check
        Data/Audit/values_changed.csv   one row per cell whose value changed
        Data/Audit/quarantine_*.csv     one file per quarantined set

Rules this script follows
  * Every file is read as TEXT (dtype=str, keep_default_na=False), then each
    column is converted on purpose.
  * One transformation per step. Each step prints the rows affected and a
    before/after check, then asserts the after state. If an assertion fails
    the script stops (CleaningError) and nothing is written.
  * Fixes are reconciled against other columns / tables, not assumed.
  * Nothing is deleted silently: removed rows go to a quarantine file with
    every original column plus trace columns, and for every table
        raw rows = clean rows + quarantined rows
    is proven before anything is written.
  * After writing, the clean files are re-read and diffed cell-by-cell
    against the raw files. Every difference must be in values_changed.csv.

Decision numbers (#1 ... #19) refer to decision_log.md.

Run from anywhere:
    python3 "Python Scripts/02_clean.py"
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "Data" / "Raw"
CLEAN = ROOT / "Data" / "Clean"
AUDIT = ROOT / "Data" / "Audit"

TABLES = ["dim_advisor", "dim_benchmark", "dim_client", "dim_date",
          "dim_security", "fact_fees", "fact_portfolio_snapshot",
          "fact_transactions"]

PK = {"dim_advisor": "advisor_id", "dim_benchmark": "benchmark_id",
      "dim_client": "client_id", "dim_date": "date_id",
      "dim_security": "security_id", "fact_fees": "fee_id",
      "fact_portfolio_snapshot": "snapshot_id", "fact_transactions": "txn_id"}

# child table, child column, parent table, parent column
FK = [("dim_client", "advisor_id", "dim_advisor", "advisor_id"),
      ("dim_security", "benchmark_id", "dim_benchmark", "benchmark_id"),
      ("fact_fees", "client_id", "dim_client", "client_id"),
      ("fact_fees", "advisor_id", "dim_advisor", "advisor_id"),
      ("fact_portfolio_snapshot", "client_id", "dim_client", "client_id"),
      ("fact_portfolio_snapshot", "advisor_id", "dim_advisor", "advisor_id"),
      ("fact_portfolio_snapshot", "benchmark_id", "dim_benchmark", "benchmark_id"),
      ("fact_transactions", "client_id", "dim_client", "client_id"),
      ("fact_transactions", "advisor_id", "dim_advisor", "advisor_id"),
      ("fact_transactions", "security_id", "dim_security", "security_id")]

# Target type of every column. text = keep as string (never numeric).
SCHEMA = {
    "dim_advisor": {"advisor_id": "int", "first_name": "text", "last_name": "text",
                    "region": "text", "years_experience": "int",
                    "aum_target_mm": "int", "certifications": "text"},
    "dim_benchmark": {"benchmark_id": "int", "name": "text", "asset_class": "text",
                      "ticker_proxy": "text"},
    "dim_client": {"client_id": "int", "first_name": "text", "last_name": "text",
                   "age": "int", "city": "text", "state": "text", "zip_code": "text",
                   "risk_profile": "text", "client_tier": "text",
                   "primary_account_type": "text", "initial_aum": "float",
                   "client_since": "date", "advisor_id": "int", "is_active": "bool"},
    "dim_date": {"date_id": "int", "date": "date", "year": "int", "quarter": "int",
                 "month": "int", "month_name": "text", "week_of_year": "int",
                 "day_of_week": "text", "is_month_end": "bool",
                 "is_quarter_end": "bool", "is_year_end": "bool",
                 "is_trading_day": "bool"},
    "dim_security": {"security_id": "int", "ticker": "text", "name": "text",
                     "asset_class": "text", "sector": "text", "benchmark_id": "int",
                     "is_etf": "bool", "expense_ratio_bps": "int_nullable"},
    "fact_fees": {"fee_id": "int", "client_id": "int", "advisor_id": "int",
                  "fee_date": "date", "fee_type": "text", "aum_basis": "float",
                  "bps_charged": "float", "amount": "float"},
    "fact_portfolio_snapshot": {"snapshot_id": "int", "client_id": "int",
                                "advisor_id": "int", "benchmark_id": "int",
                                "snapshot_date": "date", "year": "int", "month": "int",
                                "aum": "float", "return_ytd": "float",
                                "benchmark_return_ytd": "float",
                                "active_alpha_ytd": "float", "cash_pct": "float",
                                "account_type": "text", "is_return_suspect": "bool"},
    "fact_transactions": {"txn_id": "int", "client_id": "int", "advisor_id": "int",
                          "security_id": "int_nullable", "txn_date": "date",
                          "txn_type": "text", "shares": "float_nullable",
                          "price": "float_nullable", "amount": "float",
                          "is_post_departure_txn": "bool"},
}

CASH_TXN_TYPES = {"Deposit", "Withdrawal", "Advisory Fee"}
MONEY_TOL = 0.0051          # amounts are rounded to the cent -> max error 0.005
RETURN_TOL = 1.5e-6         # returns are rounded to 6 dp


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------
class CleaningError(AssertionError):
    """Raised when a fix did not do what it claimed. Stops the script."""


raw: dict[str, pd.DataFrame] = {}        # exactly as read (text)
df: dict[str, pd.DataFrame] = {}         # working copies
quarantine: dict[str, list] = {t: [] for t in TABLES}   # (set_name, frame)
LOG: list[dict] = []
CHANGES: list[dict] = []
ADDED_FLAGS: dict[str, list] = {t: [] for t in TABLES}
STEP = [0]


def banner(ref: str, title: str):
    STEP[0] += 1
    print(f"\n{'=' * 78}\nStep {STEP[0]:02d}  [{ref}]  {title}\n{'-' * 78}")


def require(cond, msg: str):
    """Assert the after-state. Uses an explicit raise so `python -O` can't skip it."""
    if not bool(cond):
        raise CleaningError(f"Step {STEP[0]:02d} FAILED: {msg}")
    print(f"    [ok] {msg}")


def log(ref, table, issue, action, rows, notes="", key_columns=None):
    LOG.append({"step": STEP[0], "decision_ref": ref, "table": table,
                "key_columns": key_columns or PK.get(table, ""),
                "issue": issue, "action": action,
                "rows_affected": int(rows), "notes": notes})


def to_text(v) -> str:
    """How a typed value will be written to the clean CSV."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (float, np.floating)):
        return str(float(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return str(v)


def counts(s: pd.Series, already_counted: bool = False) -> dict:
    """value_counts() as a plain {text: int} dict for readable printing."""
    vc = s if already_counted else s.value_counts()
    return {to_text(k): int(v) for k, v in vc.items()}


def record_changes(table, column, mask, old, new, ref):
    keys = df[table].loc[mask, PK[table]]
    for k, o, n in zip(keys, old[mask], new[mask]):
        CHANGES.append({"table": table, "key_column": PK[table], "key_value": to_text(k),
                        "column": column, "old_value": o, "new_value": to_text(n),
                        "step": STEP[0], "decision_ref": ref})


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
def step_load():
    banner("setup", "Load the 8 raw files as text")
    for t in TABLES:
        p = RAW / f"{t}.csv"
        require(p.exists(), f"{p.name} exists")
        raw[t] = pd.read_csv(p, dtype=str, keep_default_na=False, na_filter=False)
        df[t] = raw[t].copy()
        require(list(raw[t].columns) == list(SCHEMA[t]),
                f"{t}: {len(raw[t]):,} rows; columns match the expected schema")
        log("setup", t, "load raw file as text", "read (dtype=str, keep_default_na=False)",
            len(raw[t]), f"{len(raw[t].columns)} columns")


def step_trim_whitespace():
    banner("general", "Trim leading/trailing whitespace in every text cell")
    total = 0
    for t in TABLES:
        for c in df[t].columns:
            s = df[t][c]
            new = s.str.strip()
            mask = s != new
            if mask.any():
                record_changes(t, c, mask, s, new, "general")
                df[t][c] = new
            total += int(mask.sum())
        after = sum(int((df[t][c] != df[t][c].str.strip()).sum()) for c in df[t].columns)
        require(after == 0, f"{t}: 0 cells with leading/trailing whitespace remain")
    print(f"    cells trimmed: {total}")
    log("general", "(all tables)", "leading/trailing whitespace", "trim (str.strip)", total,
        "profile showed none; step kept as a guard so the join keys are safe", "(all columns)")


def step_certifications():
    ref = "#5"
    banner(ref, "dim_advisor.certifications: 'None' -> 'No Certification'")
    a = df["dim_advisor"]
    mask = a["certifications"] == "None"
    print(f"    rows affected: {int(mask.sum())}")
    print(f"    before: {a['certifications'].value_counts().to_dict()}")
    # reconcile: these are experienced advisors, i.e. a real 'no certification' value
    yrs = a.loc[mask, "years_experience"].astype(int)
    print(f"    years_experience of these advisors: {sorted(yrs.tolist())}")
    require(mask.sum() == 6, "6 advisors have the literal 'None' (decision log evidence)")
    require((yrs > 0).all(), "all of them have >0 years of experience (real value, not missing)")
    old = a["certifications"].copy()
    new = old.where(~mask, "No Certification")
    record_changes("dim_advisor", "certifications", mask, old, new, ref)
    a["certifications"] = new
    print(f"    after:  {a['certifications'].value_counts().to_dict()}")
    require((a["certifications"] == "None").sum() == 0, "no 'None' values remain")
    require((a["certifications"] == "No Certification").sum() == 6, "exactly 6 'No Certification'")
    require((a["certifications"] == "").sum() == 0, "no blank certifications")
    log(ref, "dim_advisor", "certifications = literal 'None' (read as NaN by default pandas)",
        "standardize -> 'No Certification'", mask.sum(),
        "advisors have 5-23 yrs experience; value means 'holds no certification'")


def step_booleans():
    ref = "#7"
    banner(ref, "Convert every boolean column to one encoding (1 = True, 0 = False)")
    mapping = {"True": 1, "False": 0, "1": 1, "0": 0}
    for t in TABLES:
        for c, typ in SCHEMA[t].items():
            if typ != "bool":
                continue
            s = df[t][c]
            before = s.value_counts().to_dict()
            require(s.isin(mapping.keys()).all(), f"{t}.{c}: every value is one of {list(mapping)}")
            true_before = int(s.isin(["True", "1"]).sum())
            new = s.map(mapping).astype("Int64")
            changed = s != new.astype(str)
            record_changes(t, c, changed, s, new, ref)
            df[t][c] = new
            print(f"    {t}.{c}: before {before} -> after {counts(new)}"
                  f"  (cells re-encoded: {int(changed.sum())})")
            require(int((new == 1).sum()) == true_before and new.notna().all(),
                    f"{t}.{c}: True count unchanged ({true_before}) and no nulls")
            log(ref, t, f"{c}: boolean encoding ({'/'.join(sorted(before))})",
                "convert -> 0/1 integer boolean", changed.sum(),
                "counts verified identical before/after", PK[t])


def step_dates():
    ref = "#10"
    banner(ref, "Convert every date column to datetime (YYYY-MM-DD)")
    for t in TABLES:
        for c, typ in SCHEMA[t].items():
            if typ != "date":
                continue
            s = df[t][c]
            parsed = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
            fails = int(parsed.isna().sum())
            print(f"    {t}.{c}: {len(s):,} values, parse failures {fails}, "
                  f"range {parsed.min().date()} .. {parsed.max().date()}")
            require(fails == 0, f"{t}.{c}: every value parses as YYYY-MM-DD")
            require((parsed.dt.strftime("%Y-%m-%d") == s).all(),
                    f"{t}.{c}: round-trip back to text is identical (no value changed)")
            df[t][c] = parsed
            log(ref, t, f"{c}: date stored as text", "convert -> datetime", 0,
                "0 parse failures; written back as YYYY-MM-DD, no values changed")


def step_zip():
    ref = "#8"
    banner(ref, "dim_client.zip_code stays TEXT (never numeric)")
    z = df["dim_client"]["zip_code"]
    lead0 = int(z.str.match(r"^0").sum())
    print(f"    rows with a leading zero: {lead0}; example: {z[z.str.match('^0')].head(3).tolist()}")
    print(f"    what a numeric conversion would do: {z[z.str.match('^0')].head(3).astype(int).tolist()}")
    df["dim_client"]["zip_code"] = z.astype("string").astype(object)
    z2 = df["dim_client"]["zip_code"]
    require(z2.str.fullmatch(r"\d{5}").all(), "every zip is exactly 5 digits after the step")
    require(int(z2.str.match(r"^0").sum()) == lead0 == 228, "all 228 leading zeros preserved")
    log(ref, "dim_client", "zip_code has leading zeros (228)", "keep as text", 0,
        "numeric type would turn 03897 into 3897; BI tool must import as Text")


def step_expense_ratio():
    ref = "#9"
    banner(ref, "dim_security.expense_ratio_bps: '17.0' -> 17 (nullable integer)")
    s = df["dim_security"]["expense_ratio_bps"]
    present = s != ""
    num = pd.to_numeric(s.where(present), errors="coerce")
    print(f"    non-blank: {int(present.sum())}, blank: {int((~present).sum())}, "
          f"examples before: {s[present].head(3).tolist()}")
    require(num[present].notna().all(), "every non-blank value is numeric")
    require((num[present] == num[present].round()).all(),
            "every value is a whole number (converting to integer loses nothing)")
    new = num.round().astype("Int64")
    changed = present & (s != new.astype(str))
    record_changes("dim_security", "expense_ratio_bps", changed, s, new, ref)
    df["dim_security"]["expense_ratio_bps"] = new
    print(f"    examples after: {new[present].head(3).tolist()}; rows re-written: {int(changed.sum())}")
    require((new.isna() == ~present).all(), "blanks are still blank (281), nothing filled")
    require(np.allclose(new[present].astype(float), num[present]), "numeric values identical")
    log(ref, "dim_security", "expense_ratio_bps stored as '17.0'", "convert -> integer bps",
        changed.sum(), "whole numbers only, verified no rounding; 281 blanks left blank (#13)")


def step_numeric_types():
    ref = "types"
    banner(ref, "Convert remaining id / integer / decimal columns on purpose")
    for t in TABLES:
        for c, typ in SCHEMA[t].items():
            if typ not in ("int", "float", "float_nullable") and not (typ == "int_nullable" and t != "dim_security"):
                continue
            s = df[t][c]
            present = s != ""
            num = pd.to_numeric(s.where(present), errors="coerce")
            bad = present & num.isna()
            require(not bad.any(), f"{t}.{c}: every non-blank value is numeric")
            if typ in ("int", "float"):
                require(present.all(), f"{t}.{c}: no blanks (required column)")
            if typ.startswith("int"):
                require((num[present] == num[present].round()).all(), f"{t}.{c}: whole numbers only")
                new = num.astype("Int64")
                same = (new.astype(str).where(present, "") == s)
            else:
                new = num.astype(float)
                same = (new.map(to_text) == s)
            require(same.all(), f"{t}.{c}: converted values write back identically ({typ})")
            df[t][c] = new
    log(ref, "(all tables)", "numbers stored as text", "convert ids/ints -> Int64, decimals -> float",
        0, "every value round-trips to identical text; nullable only for security_id/shares/price",
        "(all numeric columns)")


def step_quarantine_post_departure():
    ref = "#3"
    banner(ref, "fact_transactions: quarantine is_post_departure_txn = 1")
    t = df["fact_transactions"]
    c = df["dim_client"]
    s = df["fact_portfolio_snapshot"]
    mask = t["is_post_departure_txn"] == 1
    n_before = len(t)
    q = t[mask]
    print(f"    rows affected: {int(mask.sum()):,} from {q.client_id.nunique()} clients")
    # --- reconcile against other tables before moving anything -------------
    active = c.set_index("client_id")["is_active"]
    last_snap = s.groupby("client_id")["snapshot_date"].max()
    q_active = q["client_id"].map(active)
    q_last = q["client_id"].map(last_snap)
    after_last = q["txn_date"].dt.to_period("M") > q_last.dt.to_period("M")
    adv_last_normal = t[~mask].groupby("advisor_id")["txn_date"].max()
    adv_still = q["advisor_id"].map(adv_last_normal) > q["txn_date"]
    print(f"    clients' is_active: {counts(q_active)}")
    print(f"    txn after the client's last snapshot month: {after_last.mean():.1%}")
    print(f"    client's advisor has normal txns after this txn: {adv_still.mean():.1%}")
    require(mask.sum() == 2410 and q.client_id.nunique() == 131, "2,410 rows / 131 clients (decision log)")
    require((q_active == 0).all(), "every one of these clients is inactive in dim_client")
    require(after_last.all(), "every one is dated after the client's last portfolio snapshot")
    require(adv_still.all(), "the advisor kept transacting -> 'departure' = client left, not advisor")
    # --- move to quarantine with full trace ---------------------------------
    qfile = raw["fact_transactions"].loc[q.index].copy()
    qfile.insert(0, "quarantine_reason", "transaction on a closed account (is_post_departure_txn = 1)")
    qfile.insert(1, "decision_ref", ref)
    qfile.insert(2, "source_file", "Data/Raw/fact_transactions.csv")
    qfile.insert(3, "source_line", q.index + 2)
    qfile["client_is_active"] = q_active.values
    qfile["client_last_snapshot_date"] = q_last.dt.strftime("%Y-%m-%d").values
    quarantine["fact_transactions"].append(("post_departure_txn", qfile))
    df["fact_transactions"] = t[~mask].copy()
    n_after = len(df["fact_transactions"])
    print(f"    before: {n_before:,} rows -> after: {n_after:,} clean + {len(qfile):,} quarantined")
    require(n_after + len(qfile) == n_before, "clean + quarantined = rows before the step")
    require((df["fact_transactions"]["is_post_departure_txn"] == 0).all(),
            "no is_post_departure_txn = 1 rows left in the clean table")
    require(not df["fact_transactions"].txn_id.isin(qfile.txn_id.astype(int)).any(),
            "no quarantined txn_id is still in the clean table")
    log(ref, "fact_transactions", "transactions on closed accounts (is_post_departure_txn = 1)",
        "quarantine -> quarantine_fact_transactions_post_departure.csv", len(qfile),
        "131 inactive clients; all dated after their last snapshot; advisors still active")


def step_flag_aum_floor():
    ref = "#1"
    banner(ref, "Flag AUM stuck at the 1,000.00 floor: is_aum_floor (0/1)")
    s = df["fact_portfolio_snapshot"]
    f = df["fact_fees"]
    s_mask = s["aum"] == 1000.0
    f_mask = f["aum_basis"] == 1000.0
    print(f"    rows affected: snapshot {int(s_mask.sum())}, fees {int(f_mask.sum())}; "
          f"clients {s.loc[s_mask, 'client_id'].nunique()}")
    require(s_mask.sum() == 420 and f_mask.sum() == 386, "420 snapshot rows / 386 fee rows (decision log)")
    require(s.loc[s_mask, "client_id"].nunique() == 92, "92 clients affected")
    # reconcile: fees describe the same AUM as the snapshot for that client-month
    m = f.merge(s[["client_id", "snapshot_date", "aum"]], left_on=["client_id", "fee_date"],
                right_on=["client_id", "snapshot_date"], how="left")
    require(m["aum"].notna().all(), "every fee row has a snapshot for the same client + month")
    require((m["aum_basis"] - m["aum"]).abs().max() < 0.005,
            "fee aum_basis equals snapshot aum for every client-month (two tables, same fact)")
    require((m.loc[f_mask.values, "aum"] == 1000.0).all(),
            "every floored fee row sits on a floored snapshot month")
    amt = f.loc[f_mask, "amount"]
    print(f"    fees billed on floored rows: ${amt.min():.2f} - ${amt.max():.2f}")
    print(f"    is_return_suspect on floored snapshots: "
          f"{counts(s.loc[s_mask, 'is_return_suspect'])}")
    require(amt.max() < 1.0, "fees on floored rows are under $1 (consistent with a fake balance)")
    s["is_aum_floor"] = s_mask.astype("Int64")
    f["is_aum_floor"] = f_mask.astype("Int64")
    ADDED_FLAGS["fact_portfolio_snapshot"].append("is_aum_floor")
    ADDED_FLAGS["fact_fees"].append("is_aum_floor")
    require(int(s["is_aum_floor"].sum()) == 420 and int(f["is_aum_floor"].sum()) == 386,
            "flags set on exactly 420 + 386 rows; aum values unchanged")
    for t, n in (("fact_portfolio_snapshot", 420), ("fact_fees", 386)):
        log(ref, t, "AUM = 1,000.00 floor placeholder", "flag is_aum_floor = 1 (values kept)", n,
            "fees aum_basis == snapshot aum in 100% of client-months; floored fees $0.65-$0.80; "
            "274 of 420 snapshots already is_return_suspect")


def step_verify_return_suspect():
    ref = "#2"
    banner(ref, "return_ytd extremes are covered by the existing is_return_suspect flag")
    s = df["fact_portfolio_snapshot"]
    low = s["return_ytd"] <= -0.9
    high = s["return_ytd"] > 3
    ext = low | high
    print(f"    rows: <= -90%: {int(low.sum())}, > +300%: {int(high.sum())}")
    print(f"    is_return_suspect among extremes: {counts(s.loc[ext, 'is_return_suspect'])}")
    print(f"    extremes that are also AUM-floor months: {int((ext & (s.is_aum_floor == 1)).sum())}")
    require(low.sum() == 498 and high.sum() == 373, "498 + 373 = 871 extreme rows (decision log)")
    require((s.loc[ext, "is_return_suspect"] == 1).all(), "all 871 already have is_return_suspect = 1")
    log(ref, "fact_portfolio_snapshot", "return_ytd <= -90% or > +300% (871 rows)",
        "flag (existing is_return_suspect); no new column", 0,
        "verified 100% of extremes already flagged; values kept")


def step_flag_has_fee_record():
    ref = "#4"
    banner(ref, "Flag snapshot months with no fee row: has_fee_record (0/1)")
    s = df["fact_portfolio_snapshot"]
    f = df["fact_fees"]
    per = f.groupby(["client_id", "fee_date"]).size()
    require((per == 1).all(), "fact_fees has exactly one row per client-month (gap is not from duplicates)")
    fee_keys = set(zip(f["client_id"], f["fee_date"]))
    has = pd.Series([k in fee_keys for k in zip(s["client_id"], s["snapshot_date"])], index=s.index)
    missing = ~has
    print(f"    rows affected (has_fee_record = 0): {int(missing.sum()):,} "
          f"from {s.loc[missing, 'client_id'].nunique():,} clients")
    print(f"    share of those that are AUM-floor months: {(s.loc[missing, 'is_aum_floor'] == 1).mean():.2%}")
    s["has_fee_record"] = has.astype("Int64")
    ADDED_FLAGS["fact_portfolio_snapshot"].append("has_fee_record")
    require(int((s["has_fee_record"] == 0).sum()) == 7350, "7,350 snapshot months flagged 0 (decision log)")
    require(int((s["has_fee_record"] == 1).sum()) == len(f), "flag = 1 count equals the number of fee rows")
    log(ref, "fact_portfolio_snapshot", "snapshot client-month has no fee row",
        "flag has_fee_record (0 = no fee); no fee rows created", int(missing.sum()),
        "fees are exactly 1 per client-month; only 0.5% of gaps are floor months",
        "client_id, snapshot_date")


def step_flag_unknown_risk():
    ref = "#6"
    banner(ref, "Flag risk_profile = 'Unknown': is_risk_profile_unknown (0/1)")
    c = df["dim_client"]
    s = df["fact_portfolio_snapshot"]
    mask = c["risk_profile"] == "Unknown"
    bench = s.groupby("client_id")["benchmark_id"].agg(lambda x: set(x))
    unk_bench = {int(x) for x in set().union(*c.loc[mask, "client_id"].map(bench))}
    bal_bench = {int(x) for x in set().union(*c.loc[c.risk_profile == "Balanced", "client_id"].map(bench))}
    print(f"    rows affected: {int(mask.sum())}; their benchmark_id(s): {unk_bench}; "
          f"Balanced clients' benchmark_id(s): {bal_bench}")
    require(mask.sum() == 75, "75 clients have 'Unknown' (decision log)")
    require(unk_bench == {3} and bal_bench == {3},
            "all Unknown clients sit on benchmark 3, the Balanced benchmark (system default)")
    before = c["risk_profile"].copy()
    c["is_risk_profile_unknown"] = mask.astype("Int64")
    ADDED_FLAGS["dim_client"].append("is_risk_profile_unknown")
    require((c["risk_profile"] == before).all(), "risk_profile values unchanged (not filled)")
    require(int(c["is_risk_profile_unknown"].sum()) == 75, "flag set on exactly 75 clients")
    log(ref, "dim_client", "risk_profile = 'Unknown'", "leave value, flag is_risk_profile_unknown",
        75, "all on benchmark 3 (Balanced default); inferring 'Balanced' would be circular")


# ---- verified "leave as is" decisions ---------------------------------------
def step_verify_before_since():
    ref = "#11"
    banner(ref, "Snapshots / fees dated before client_since -> leave (same-month convention)")
    since = df["dim_client"].set_index("client_id")["client_since"]
    for t, dc, expected in (("fact_portfolio_snapshot", "snapshot_date", 930), ("fact_fees", "fee_date", 858)):
        d = df[t]
        cs = d["client_id"].map(since)
        before = d[dc] < cs
        same_month = d.loc[before, dc].dt.to_period("M") == cs[before].dt.to_period("M")
        print(f"    {t}: {int(before.sum())} rows before client_since; same month: {same_month.mean():.0%}")
        require(before.sum() == expected, f"{t}: {expected} rows (decision log)")
        require(same_month.all(), f"{t}: every one is in the month the client joined (dated the 1st)")
        log(ref, t, f"{dc} earlier than client_since", "leave as is (verified)", int(before.sum()),
            "monthly rows are dated the 1st; all fall in the joining month")
    tx = df["fact_transactions"]
    early = tx["txn_date"] < tx["client_id"].map(since)
    require(early.sum() == 0, "0 transactions are dated before client_since")


def step_verify_cash_blanks():
    ref = "#12"
    banner(ref, "Blank security_id / shares / price in fact_transactions -> leave")
    tx = df["fact_transactions"]
    rtx = raw["fact_transactions"]
    blank_raw = rtx["security_id"] == ""
    print(f"    raw blank rows: {int(blank_raw.sum()):,}; clean blank rows: {int(tx.security_id.isna().sum()):,}")
    require(blank_raw.sum() == 101528, "101,528 blank rows in raw (decision log)")
    for col in ("security_id", "shares", "price"):
        blank = tx[col].isna()
        require((blank == tx["txn_type"].isin(CASH_TXN_TYPES)).all(),
                f"{col} is blank exactly for Deposit / Withdrawal / Advisory Fee rows")
    log(ref, "fact_transactions", "blank security_id, shares, price", "leave as is (verified)",
        int(tx.security_id.isna().sum()), "blank iff txn_type is a cash movement; not filled")


def step_verify_security_blanks():
    ref = "#13"
    banner(ref, "Blank sector / expense_ratio_bps in dim_security -> leave")
    sec = df["dim_security"]
    sb, eb = sec["sector"] == "", sec["expense_ratio_bps"].isna()
    print(f"    sector blank: {int(sb.sum())}, expense_ratio_bps blank: {int(eb.sum())}")
    require((sb == (sec.is_etf == 1)).all() and sb.sum() == 219, "sector blank for exactly the 219 ETFs")
    require((eb == (sec.is_etf == 0)).all() and eb.sum() == 281,
            "expense_ratio_bps blank for exactly the 281 individual stocks")
    log(ref, "dim_security", "blank sector (219) and expense_ratio_bps (281)", "leave as is (verified)",
        500, "sector N/A for ETFs; expense ratio N/A for single stocks")


def step_verify_txn_grain():
    ref = "#14"
    banner(ref, "Transactions sharing client + security + date -> leave")
    tx = df["fact_transactions"]
    grain = ["client_id", "advisor_id", "security_id", "txn_date"]
    rt = raw["fact_transactions"]
    rtr = rt[rt.security_id != ""]
    raw_g = int(rtr.duplicated(grain, keep=False).sum())
    trades = tx[tx.security_id.notna()]
    g = trades.duplicated(grain, keep=False)
    combos = trades[g].groupby(["client_id", "security_id", "txn_date"])["txn_type"] \
        .agg(lambda z: "/".join(sorted(z))).value_counts()
    cash = tx[tx.security_id.isna()]
    gc = cash.duplicated(["client_id", "txn_date"], keep=False)
    exact = tx.duplicated([c for c in tx.columns if c != "txn_id"])
    print(f"    trade rows sharing client+advisor+security+date: raw {raw_g}, clean {int(g.sum())}")
    print(f"    combinations: {counts(combos.head(6), already_counted=True)}")
    print(f"    (cash rows sharing client+date, not part of #14: {int(gc.sum()):,})")
    require(raw_g == 712, "712 trade rows share the grain in raw (decision log)")
    require(exact.sum() == 0, "0 exact duplicates (every column except txn_id)")
    log(ref, "fact_transactions", "trade rows share client/advisor/security/date",
        "leave as is (verified)", int(g.sum()),
        f"0 exact duplicates; mostly a Buy+Sell / Buy+Dividend on the same day; 712 in raw, "
        f"{int(g.sum())} after #3 quarantine")


def step_verify_tier():
    ref = "#15"
    banner(ref, "client_tier follows initial_aum (assigned at onboarding) -> leave")
    c = df["dim_client"]
    expected = np.select([c.initial_aum < 500_000, c.initial_aum < 5_000_000], ["Retail", "HNW"], "UHNW")
    match = c["client_tier"] == expected
    print(f"    tier matches initial_aum bands (<500K Retail, <5M HNW, else UHNW): {match.mean():.1%}")
    require(match.all(), "client_tier agrees with initial_aum for every client")
    log(ref, "dim_client", "client_tier differs from latest AUM", "leave as is (verified)", 0,
        "tier = band of initial_aum in 100% of clients; it is not re-assigned over time")


def step_verify_since_calendar():
    ref = "#16"
    banner(ref, "client_since earlier than dim_date coverage -> leave")
    lo = df["dim_date"]["date"].min()
    early = df["dim_client"]["client_since"] < lo
    print(f"    calendar starts {lo.date()}; clients joined before: {int(early.sum())}")
    require(early.sum() == 1532, "1,532 clients (decision log)")
    log(ref, "dim_client", "client_since outside dim_date range", "leave as is", int(early.sum()),
        "client attribute, not a join key to dim_date")


def step_verify_asset_class():
    ref = "#17"
    banner(ref, "asset_class differs between dim_benchmark and dim_security -> leave")
    b = sorted(df["dim_benchmark"]["asset_class"].unique())
    s = sorted(df["dim_security"]["asset_class"].unique())
    print(f"    dim_benchmark: {b}\n    dim_security:  {s}")
    require(set(b) != set(s), "domains differ as documented (index category vs security type)")
    log(ref, "dim_benchmark / dim_security", "asset_class value sets differ", "leave as is", 0,
        "different meanings; do not join on asset_class", "asset_class")


def step_verify_trading_day():
    ref = "#18"
    banner(ref, "dim_date.is_trading_day = 1 for every weekday -> leave, note")
    d = df["dim_date"]
    weekday = (d["date"].dt.dayofweek < 5).astype(int)
    print(f"    is_trading_day == weekday for {(d.is_trading_day == weekday).mean():.1%} of rows")
    require((d["is_trading_day"] == weekday).all(), "trading day == weekday (holidays not excluded)")
    log(ref, "dim_date", "is_trading_day ignores market holidays", "leave as is (noted)", 0,
        "matters only when counting trading days")


def step_verify_derived():
    ref = "#19"
    banner(ref, "Recompute derived columns from their inputs -> leave")
    f = df["fact_fees"]
    s = df["fact_portfolio_snapshot"]
    tx = df["fact_transactions"]
    d1 = (f.amount - f.aum_basis * f.bps_charged / 120000).abs()
    d2 = (s.return_ytd - (s.benchmark_return_ytd + s.active_alpha_ytd)).abs()
    tr = tx[tx.shares.notna()]
    d3 = (tr.amount - tr.shares * tr.price).abs()
    print(f"    fees amount = aum_basis * bps / 120000      max error {d1.max():.6f}")
    print(f"    return_ytd = benchmark + active_alpha       max error {d2.max():.2e}")
    print(f"    txn amount = shares * price                 max error {d3.max():.6f}")
    require(d1.max() <= MONEY_TOL, "fee amounts recompute to the cent for every row")
    require(d2.max() <= RETURN_TOL, "return_ytd recomputes (6-dp rounding) for every row")
    require(d3.max() <= MONEY_TOL, "trade amounts recompute to the cent for every row")
    for t, txt in (("fact_fees", "amount = aum_basis * bps_charged / 120000"),
                   ("fact_portfolio_snapshot", "return_ytd = benchmark_return_ytd + active_alpha_ytd"),
                   ("fact_transactions", "amount = shares * price (trades)")):
        log(ref, t, f"derived column: {txt}", "leave as is (verified)", 0, "holds for 100% of rows")


# ---- final proofs -----------------------------------------------------------
def step_integrity():
    banner("final", "Primary keys, foreign keys, and columns of the clean set")
    for t in TABLES:
        k = df[t][PK[t]]
        require(k.notna().all() and k.is_unique, f"{t}.{PK[t]} is non-null and unique")
        require(list(df[t].columns) == list(SCHEMA[t]) + ADDED_FLAGS[t],
                f"{t}: same columns as raw" + (f" + flags {ADDED_FLAGS[t]}" if ADDED_FLAGS[t] else ""))
    for ct, cc, pt, pc in FK:
        child = df[ct][cc].dropna()
        orphans = ~child.isin(df[pt][pc])
        require(not orphans.any(), f"{ct}.{cc} -> {pt}.{pc}: 0 orphans in the clean set")


def step_reconcile_rows():
    banner("final", "Prove raw rows = clean rows + quarantined rows, for every table")
    rows = []
    for t in TABLES:
        qn = sum(len(q) for _, q in quarantine[t])
        rows.append((t, len(raw[t]), len(df[t]), qn))
        require(len(raw[t]) == len(df[t]) + qn,
                f"{t}: {len(raw[t]):,} raw = {len(df[t]):,} clean + {qn:,} quarantined")
        raw_keys = set(raw[t][PK[t]])
        out_keys = set(df[t][PK[t]].map(to_text)) | {k for _, q in quarantine[t] for k in q[PK[t]]}
        require(raw_keys == out_keys, f"{t}: every raw key is in exactly one output (clean or quarantine)")
        log("final", t, "row reconciliation", "raw = clean + quarantined", len(raw[t]),
            f"raw {len(raw[t])} = clean {len(df[t])} + quarantined {qn}")
    return rows


def write_outputs():
    banner("final", "Write clean tables and audit files")
    CLEAN.mkdir(parents=True, exist_ok=True)
    AUDIT.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        p = CLEAN / f"{t}.csv"
        df[t].to_csv(p, index=False, date_format="%Y-%m-%d")
        print(f"    wrote Data/Clean/{p.name}  ({len(df[t]):,} rows)")
    for t in TABLES:
        for name, q in quarantine[t]:
            p = AUDIT / f"quarantine_{t}_{name.replace('_txn', '')}.csv"
            q.to_csv(p, index=False)
            print(f"    wrote Data/Audit/{p.name}  ({len(q):,} rows)")
    pd.DataFrame(LOG).to_csv(AUDIT / "cleaning_log.csv", index=False)
    pd.DataFrame(CHANGES, columns=["table", "key_column", "key_value", "column", "old_value",
                                   "new_value", "step", "decision_ref"]) \
        .to_csv(AUDIT / "values_changed.csv", index=False)
    print(f"    wrote Data/Audit/cleaning_log.csv  ({len(LOG)} rows)")
    print(f"    wrote Data/Audit/values_changed.csv  ({len(CHANGES):,} rows)")


def step_diff_against_raw():
    banner("final", "Re-read clean files; every changed cell must be in values_changed.csv")
    logged = {(c["table"], c["key_value"], c["column"]): (c["old_value"], c["new_value"]) for c in CHANGES}
    found = {}
    for t in TABLES:
        clean = pd.read_csv(CLEAN / f"{t}.csv", dtype=str, keep_default_na=False)
        r = raw[t].set_index(PK[t])
        cl = clean.set_index(PK[t])
        r = r.loc[cl.index]
        for col in r.columns:
            diff = r[col] != cl[col]
            for k in r.index[diff]:
                found[(t, k, col)] = (r.at[k, col], cl.at[k, col])
        n = sum(1 for key in found if key[0] == t)
        print(f"    {t}: {n:,} cells differ from raw")
    require(set(found) == set(logged), "the set of changed cells equals values_changed.csv exactly")
    require(all(found[k] == logged[k] for k in found), "every old -> new value matches the log")


def main():
    print(f"project root: {ROOT}")
    try:
        step_load()
        step_trim_whitespace()
        step_certifications()             # 5
        step_booleans()                   # 7
        step_dates()                      # 10
        step_zip()                        # 8
        step_expense_ratio()              # 9
        step_numeric_types()
        step_quarantine_post_departure()  # 3
        step_flag_aum_floor()             # 1
        step_verify_return_suspect()      # 2
        step_flag_has_fee_record()        # 4
        step_flag_unknown_risk()          # 6
        step_verify_before_since()        # 11
        step_verify_cash_blanks()         # 12
        step_verify_security_blanks()     # 13
        step_verify_txn_grain()           # 14
        step_verify_tier()                # 15
        step_verify_since_calendar()      # 16
        step_verify_asset_class()         # 17
        step_verify_trading_day()         # 18
        step_verify_derived()             # 19
        step_integrity()
        rows = step_reconcile_rows()
        write_outputs()
        step_diff_against_raw()
    except CleaningError as e:
        print(f"\n*** STOPPED: {e}\n*** No fix is trusted until this is resolved.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 78}\nDONE. Row reconciliation:")
    print(f"    {'table':26s} {'raw':>9s} {'clean':>9s} {'quarantined':>12s}")
    for t, a, b, q in rows:
        print(f"    {t:26s} {a:>9,} {b:>9,} {q:>12,}")


if __name__ == "__main__":
    main()
