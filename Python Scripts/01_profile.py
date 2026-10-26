#!/usr/bin/env python3
"""
01_profile.py -- generic table profiler + data-quality (DQ) checker
====================================================================

Point it at a folder of delimited files (CSV / TSV / TXT). It reads every file
as TEXT (dtype=str, keep_default_na=False), works out what each column is
supposed to be, converts each column on purpose, and writes two reports:

    Reports/profile_<label>.csv      one row per column per table (the facts)
    Reports/dq_summary_<label>.csv   one row per problem found, tagged
                                     ERROR / WARN / INFO

Severity
    ERROR  wrong, or will break a join or a calculation
    WARN   probably wrong -- investigate
    INFO   worth knowing

Check groups (dq_summary.check_group)
    1 Column-level      type, allowed values, ranges, key uniqueness
    2 Single-table      duplicates, near duplicates, values that contradict
                        other columns in the same row
    3 Referential       every foreign key points to an existing primary key
    4 Cross-table       facts that disagree with dimensions or with each other

Nothing about any particular dataset is hard-coded. Primary keys, foreign
keys, calendar tables, derived-column formulas (c = a*b*k, c = a-b ...),
"blank when category = X" rules, etc. are all inferred from column names and
from the data itself. Inference can be overridden with --pk / --fk.

Usage (run from the project folder, or from anywhere -- paths default to the
folder above this script):
    python3 "Python Scripts/01_profile.py"
    python3 "Python Scripts/01_profile.py" --input Data/Clean --label clean
    python3 "Python Scripts/01_profile.py" --pk dim_client=client_id \
            --fk fact_fees.client_id=dim_client.client_id
"""
from __future__ import annotations

import argparse
import csv
import difflib
import itertools
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# --------------------------------------------------------------------------
# Settings / vocabulary (generic -- tune here, not in the checks)
# --------------------------------------------------------------------------
PLACEHOLDER_TOKENS = {
    "none", "null", "nan", "n/a", "na", "#n/a", "nil", "-", "--", "?",
    "unknown", "missing", "undefined", "(blank)", "blank", "not available",
    "tbd", "#value!", "#ref!", "#div/0!",
}
BOOL_MAP = {"true": True, "t": True, "yes": True, "y": True, "1": True,
            "false": False, "f": False, "no": False, "n": False, "0": False}
BOOL_NAME = re.compile(r"^(is|has|was|can|should|flag)_|_flag$|^flag$", re.I)
KEY_NAME = re.compile(r"(^id$|_id$|_key$|^key$|_sk$|_pk$|_fk$)", re.I)
CODE_NAME = re.compile(r"(zip|postal|phone|fax|ssn|cusip|isin|sedol|_code$|^code$)", re.I)
AGE_NAME = re.compile(r"(^|_)age($|_)", re.I)
RETURN_NAME = re.compile(r"return", re.I)
PCT_NAME = re.compile(r"(pct|percent|_rate$|^rate$|ratio|weight)", re.I)
NONNEG_NAME = re.compile(
    r"(amount|amt|price|shares|qty|quantity|aum|balance|bps|fee|cost|"
    r"count|years|age|target|basis|volume|units|salary)", re.I)
START_NAME = re.compile(r"(since|start|open|begin|hire|join|inception|onboard|"
                        r"effective|created|enrol|signup|sign_up|registered)", re.I)
END_NAME = re.compile(r"(end_date|_end$|close|depart|terminat|expir|exit|until|"
                      r"left_on|churn)", re.I)
ACTIVE_NAME = re.compile(r"^(is_)?active$", re.I)
IDENTITY_NAME = re.compile(r"(name|email|phone|ticker|zip|postal|dob|birth|"
                           r"address|ssn)", re.I)
DATE_PART_NAMES = {
    "year", "yr", "calendar_year", "month", "month_num", "month_number",
    "quarter", "qtr", "day", "day_of_month", "dom", "day_of_year", "doy",
    "month_name", "day_of_week", "weekday", "day_name", "dow", "week",
    "week_of_year", "week_num", "iso_week", "is_month_end", "is_month_start",
    "is_quarter_end", "is_quarter_start", "is_year_end", "is_year_start",
    "is_weekend", "is_weekday", "is_trading_day", "is_business_day",
}
GENERIC_TOKENS = {"id", "name", "type", "date", "code", "value", "amount",
                  "year", "month", "is", "key", "num", "no", "flag", "status",
                  "pct", "first", "last", "total", "of", "the"}
DATE_FORMATS = [
    ("%Y-%m-%d", "YYYY-MM-DD"), ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%m/%d/%Y", "MM/DD/YYYY"), ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"), ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m/%d/%y", "MM/DD/YY"), ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%b %d, %Y", "Mon DD, YYYY"), ("%B %d, %Y", "Month DD, YYYY"),
    ("%d %b %Y", "DD Mon YYYY"), ("%d-%b-%Y", "DD-Mon-YYYY"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "ISO datetime"),
    ("%m/%d/%Y %H:%M", "MM/DD/YYYY HH:MM"),
]
COMPACT_DATE = ("%Y%m%d", "YYYYMMDD")       # only tried when name says "date"
INT_RE = re.compile(r"^[+-]?\d+$")
NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
INT_DOT0_RE = re.compile(r"^[+-]?\d+\.0+$")
THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
NONPRINT_RE = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f ​﻿]"
TYPE_THRESHOLD = 0.90        # share of non-blank values that must fit a type
SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
GROUP_NAME = {1: "1 Column-level", 2: "2 Single-table",
              3: "3 Referential integrity", 4: "4 Cross-table business rule"}
TODAY = pd.Timestamp.today().normalize()


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------
@dataclass
class Col:
    table: str
    name: str
    pos: int
    raw: pd.Series
    stripped: pd.Series = None
    missing: pd.Series = None          # empty | whitespace-only | placeholder
    empty: pd.Series = None
    ws_only: pd.Series = None
    placeholder: pd.Series = None
    itype: str = "text"                # empty/boolean/integer/code/decimal/date/categorical/text
    role: str = "attribute"            # pk / fk / attribute
    references: str = ""
    num: pd.Series = None              # float view (NaN where not numeric)
    dt: pd.Series = None               # datetime view
    bl: pd.Series = None               # boolean view
    fail: pd.Series = None             # non-missing values that fail strict parse
    fmt_counts: dict = field(default_factory=dict)
    special_fmt: dict = field(default_factory=dict)
    decimals: int = 0
    nunique: int = 0

    @property
    def lname(self):
        return self.name.lower()

    @property
    def present(self):
        return ~self.missing


@dataclass
class Table:
    name: str
    path: Path
    df: pd.DataFrame
    cols: dict = field(default_factory=dict)
    pk: str | None = None
    fks: dict = field(default_factory=dict)       # col -> (table, pk col)
    bad_lines: list = field(default_factory=list)
    blank_lines: int = 0
    header_issues: list = field(default_factory=list)
    pk_inferred_how: str = ""

    @property
    def n(self):
        return len(self.df)

    def date_cols(self):
        return [c for c in self.cols.values() if c.itype == "date"]

    def row_labels(self, mask, k=5):
        """Short example identifiers for rows in mask (PK value or file line)."""
        idx = mask[mask].index[:k] if hasattr(mask, "index") else mask[:k]
        if self.pk:
            return ", ".join(f"{self.pk}={self.df.at[i, self.pk]}" for i in idx)
        return ", ".join(f"line {i + 2}" for i in idx)


FINDINGS: list[dict] = []


def add(sev, group, check, table, column, rows, total, detail, examples=""):
    FINDINGS.append({
        "severity": sev,
        "check_group": GROUP_NAME[group],
        "check": check,
        "table": table,
        "column": column,
        "affected_rows": int(rows) if rows is not None else "",
        "affected_pct": round(100.0 * rows / total, 3) if rows and total else (0.0 if rows == 0 else ""),
        "detail": detail,
        "examples": examples,
    })


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def fmt_examples(values, k=5):
    out = []
    for v in list(values)[:k]:
        out.append(repr(v) if isinstance(v, str) else str(v))
    return ", ".join(out)


def lenient_number(v: str):
    t = v.strip()
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    for ch in "$€£¥, %":
        t = t.replace(ch, "")
    if NUM_RE.match(t):
        x = float(t)
        return -x if neg else x
    return np.nan


def number_format(v: str) -> str:
    t = v.strip()
    if INT_RE.match(t):
        return "integer"
    if INT_DOT0_RE.match(t):
        return "integer-with-.0"
    if NUM_RE.match(t):
        return "scientific" if "e" in t.lower() else "decimal"
    if t.startswith("(") and t.endswith(")"):
        return "accounting-negative ( )"
    if any(s in t for s in "$€£¥"):
        return "currency-symbol"
    if t.endswith("%"):
        return "percent-sign"
    if THOUSANDS_RE.match(t):
        return "thousands-separator"
    return "unparseable"


PLAIN_NUMBER_FMTS = {"integer", "integer-with-.0", "decimal", "scientific"}


def value_shape(v: str) -> str:
    v = v[:40]
    v = re.sub(r"\d+", lambda m: "9" * len(m.group()) if len(m.group()) <= 4 else "9+", v)
    v = re.sub(r"[A-Z]+", "A", v)
    v = re.sub(r"[a-z]+", "a", v)
    v = re.sub(r"\s+", " ", v)
    return v


def norm_text(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def tokens(name: str) -> set:
    return set(t for t in re.split(r"[_\W]+", name.lower()) if t)


def related_names(a: str, b: str) -> bool:
    """Same column name, or one name's tokens contained in the other's
    (account_type ~ primary_account_type) -- ignoring purely generic tokens."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    ta, tb = tokens(a), tokens(b)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not small or not small <= big:
        return False
    return bool(small - GENERIC_TOKENS)


def base_name(table: str) -> str:
    b = re.sub(r"^(dim|fact|fct|dw|stg|tbl|lkp|ref|d|f)_", "", table.lower())
    return b


def singular(w: str) -> str:
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("ses"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def nice_constant(k: float) -> float:
    cands = [1, 0.01, 0.0001, 0.001, 100, 1000, 10000, 1 / 12, 1 / 1200,
             1 / 120000, 12, 1 / 365, 1 / 252, 1e6, 1e-6, -1]
    for c in cands:
        if abs(k / c - 1) < 0.002:
            return c
    return k


def const_str(k: float) -> str:
    if k == 1:
        return ""
    if abs(k) < 1 and abs(round(1 / k) - 1 / k) < 1e-6:
        return f" / {int(round(1 / k))}"
    return f" * {k:g}"


def mode_pct(s: pd.Series) -> str:
    vc = s.value_counts(normalize=True)
    return "; ".join(f"{k} ({v:.1%})" for k, v in vc.head(3).items())


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def detect_encoding(path: Path) -> str:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            while fh.read(1 << 20):
                pass
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "latin-1"


def detect_delimiter(path: Path, enc: str) -> str:
    if path.suffix.lower() in (".tsv", ".tab"):
        return "\t"
    with open(path, encoding=enc, newline="") as fh:
        sample = fh.read(65536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
    except csv.Error:
        return ","


def read_table(path: Path) -> Table:
    enc = detect_encoding(path)
    sep = detect_delimiter(path, enc)
    bad, blank = [], 0
    with open(path, encoding=enc, newline="") as fh:
        rdr = csv.reader(fh, delimiter=sep)
        header = next(rdr, [])
        for i, row in enumerate(rdr, start=2):
            if not row or all(not x.strip() for x in row):
                blank += 1
                continue
            if len(row) != len(header):
                bad.append(i)
    # Read EVERYTHING as text. No NA conversion: '', 'None', 'NA' stay strings.
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False,
                     sep=sep, encoding=enc, on_bad_lines="skip",
                     skip_blank_lines=True)
    df = df.fillna("")          # short rows (too few fields) -> ''
    issues = []
    for h in header:
        if h != h.strip():
            issues.append(f"header {h!r} has leading/trailing whitespace")
        if not h.strip():
            issues.append("empty column header")
    dup_h = [h for h in set(header) if header.count(h) > 1]
    if dup_h:
        issues.append(f"duplicate column headers: {dup_h}")
    df.columns = [str(c).strip() for c in df.columns]
    t = Table(name=path.stem, path=path, df=df, bad_lines=bad,
              blank_lines=blank, header_issues=issues)
    return t


# --------------------------------------------------------------------------
# Column analysis: infer type, then convert on purpose
# --------------------------------------------------------------------------
def parse_dates_unique(uniq: pd.Series, allow_compact: bool):
    out = pd.Series(pd.NaT, index=uniq.index, dtype="datetime64[ns]")
    lab = pd.Series("", index=uniq.index, dtype=object)
    remaining = pd.Series(True, index=uniq.index)
    fmts = DATE_FORMATS + ([COMPACT_DATE] if allow_compact else [])
    for fmt, label in fmts:
        if not remaining.any():
            break
        p = pd.to_datetime(uniq[remaining], format=fmt, errors="coerce")
        ok = p.notna()
        idx = ok[ok].index
        out.loc[idx] = p.loc[idx]
        lab.loc[idx] = label
        remaining.loc[idx] = False
    return out, lab


def analyze_column(t: Table, name: str, pos: int) -> Col:
    s = t.df[name].astype(str)
    c = Col(table=t.name, name=name, pos=pos, raw=s)
    c.stripped = s.str.strip()
    c.empty = s.eq("")
    c.ws_only = ~c.empty & c.stripped.eq("")
    c.placeholder = c.stripped.ne("") & c.stripped.str.lower().isin(PLACEHOLDER_TOKENS)
    c.missing = c.empty | c.ws_only | c.placeholder

    vc = c.stripped[c.present].value_counts()
    n = int(vc.sum())
    c.nunique = len(vc)
    if n == 0:
        c.itype = "empty"
        c.fail = pd.Series(False, index=s.index)
        return c
    uniq = pd.Series(vc.index, index=vc.index)

    def share(mask):
        return float(vc[mask.values].sum()) / n

    low = uniq.str.lower()
    is_bool_tok = low.isin(BOOL_MAP.keys())
    is_int = uniq.str.match(INT_RE)
    is_num = uniq.str.match(NUM_RE)
    len_num = uniq.map(lenient_number)
    only_01 = set(low[is_bool_tok]) <= {"0", "1"}

    itype = None
    if share(is_bool_tok) >= TYPE_THRESHOLD and c.nunique <= 6 and \
            (BOOL_NAME.search(name) or not only_01):
        itype = "boolean"
    elif share(is_int) >= TYPE_THRESHOLD:
        lead0 = uniq.str.match(r"^0\d") & is_int
        itype = "code" if (lead0.any() or CODE_NAME.search(name)) else "integer"
    elif share(is_num) >= TYPE_THRESHOLD or share(len_num.notna()) >= TYPE_THRESHOLD:
        itype = "decimal"
    else:
        samp = uniq if len(uniq) <= 5000 else uniq.sample(5000, random_state=0)
        p, _ = parse_dates_unique(samp, "date" in name.lower())
        if share(p.reindex(uniq.index).notna()) >= TYPE_THRESHOLD * (len(samp) / len(uniq)) and \
                p.notna().mean() >= TYPE_THRESHOLD:
            itype = "date"
    if itype is None:
        if (c.nunique <= 12 and c.nunique < n) or (c.nunique <= 60 and c.nunique <= 0.2 * n):
            itype = "categorical"
        else:
            itype = "text"
    c.itype = itype

    # ---- convert on purpose -------------------------------------------------
    present = c.present
    if itype in ("integer", "decimal", "code"):
        numd = dict(zip(uniq.index, len_num.values))
        c.num = c.stripped.map(numd).astype(float)
        c.num[~present] = np.nan
        fmts = uniq.map(number_format)
        c.fmt_counts = vc.groupby(fmts.values).sum().sort_values(ascending=False).to_dict()
        if itype == "integer":
            strict_ok = is_int
        elif itype == "code":
            strict_ok = uniq.str.match(r"^\d+$")
        else:
            strict_ok = is_num
        okd = dict(zip(uniq.index, strict_ok.values))
        c.fail = present & ~c.stripped.map(okd).fillna(False).astype(bool)
        dec = uniq[is_num].str.extract(r"\.(\d+)$")[0].str.len()
        c.decimals = int(min(dec.max(), 8)) if dec.notna().any() else 0
        c.special_fmt = {k: v for k, v in c.fmt_counts.items()
                         if k not in PLAIN_NUMBER_FMTS and k != "unparseable"}
        if itype == "integer" and "integer-with-.0" in c.fmt_counts:
            c.special_fmt["integer-with-.0"] = c.fmt_counts["integer-with-.0"]
    elif itype == "boolean":
        c.bl = c.stripped.str.lower().map(BOOL_MAP).astype("boolean")
        c.bl[~present] = pd.NA
        c.fail = present & c.bl.isna()
        c.fmt_counts = vc[is_bool_tok.values].to_dict()       # raw tokens used
        c.num = c.bl.astype("Float64").astype(float)
    elif itype == "date":
        p, lab = parse_dates_unique(uniq, "date" in name.lower())
        c.dt = pd.to_datetime(c.stripped.map(dict(zip(uniq.index, p.values))), errors="coerce")
        c.dt[~present] = pd.NaT
        c.fail = present & c.dt.isna()
        lab = lab.where(lab != "", "unparseable")
        c.fmt_counts = vc.groupby(lab.values).sum().sort_values(ascending=False).to_dict()
        c.special_fmt = dict(list(c.fmt_counts.items())[1:])   # anything but dominant
    else:
        c.fail = pd.Series(False, index=s.index)
    return c


# --------------------------------------------------------------------------
# Key inference
# --------------------------------------------------------------------------
def uniqueness(c: Col) -> float:
    n = len(c.raw)
    return c.stripped[c.present].nunique() / n if n else 0


def infer_pk(t: Table, override: dict):
    if t.name in override:
        t.pk = override[t.name]
        t.pk_inferred_how = "--pk override"
        return
    names = list(t.cols)
    keyish = [c for c in names if KEY_NAME.search(c)]
    b = base_name(t.name)
    toks = b.split("_")
    wanted = {"id", f"{b}_id", f"{singular(b)}_id", f"{b}_key", f"{singular(b)}_key"}
    for i in range(1, len(toks)):
        tail = "_".join(toks[i:])
        wanted |= {f"{tail}_id", f"{singular(tail)}_id"}
    for c in keyish:
        if c.lower() in wanted and uniqueness(t.cols[c]) >= 0.5:
            t.pk, t.pk_inferred_how = c, "name matches table"
            return
    if names and KEY_NAME.search(names[0]) and uniqueness(t.cols[names[0]]) >= 0.95:
        t.pk, t.pk_inferred_how = names[0], "first column, key-named, ~unique"
        return
    for c in keyish:
        if uniqueness(t.cols[c]) >= 0.99:
            t.pk, t.pk_inferred_how = c, "key-named and unique"
            return
    for c in names:
        col = t.cols[c]
        if col.itype in ("integer", "code") and uniqueness(col) == 1.0:
            t.pk, t.pk_inferred_how = c, "fully unique integer column"
            return


def infer_fks(tables: dict, override: list):
    by_pk = {}
    for t in tables.values():
        if t.pk:
            by_pk.setdefault(t.pk.lower(), []).append(t.name)
    for t in tables.values():
        for cname, col in t.cols.items():
            if cname == t.pk or not KEY_NAME.search(cname):
                continue
            target = None
            lc = cname.lower()
            cands = [x for x in by_pk.get(lc, []) if x != t.name]
            if not cands:
                for pkname, tns in by_pk.items():
                    if pkname != "id" and lc.endswith("_" + pkname):
                        cands = [x for x in tns if x != t.name]
                        if cands:
                            break
            if not cands:
                for d in tables.values():
                    if d.name != t.name and d.pk and d.pk.lower() == "id":
                        b = base_name(d.name)
                        if lc in (f"{b}_id", f"{singular(b)}_id"):
                            cands = [d.name]
                            break
            if cands:
                target = cands[0]
                t.fks[cname] = (target, tables[target].pk)
    for spec in override:
        (ct, cc), (pt, pc) = spec
        if ct in tables and pt in tables:
            tables[ct].fks[cc] = (pt, pc)
    for t in tables.values():
        if t.pk:
            t.cols[t.pk].role = "pk"
        for cname, (pt, pc) in t.fks.items():
            t.cols[cname].role = "fk"
            t.cols[cname].references = f"{pt}.{pc}"


# --------------------------------------------------------------------------
# PROFILE (one row per column per table)
# --------------------------------------------------------------------------
def profile_rows(t: Table) -> list:
    rows = []
    n = t.n
    for c in t.cols.values():
        vc = c.stripped[c.present].value_counts()
        shapes = pd.Series(vc.index, index=vc.index).map(value_shape)
        shape_counts = vc.groupby(shapes.values).sum().sort_values(ascending=False)
        tot = shape_counts.sum() or 1
        lt_ws = int((c.present & (c.raw != c.stripped)).sum())
        multi_ws = int(c.stripped.str.contains(r"\s{2,}", regex=True).sum())
        nonprint = int(c.raw.str.contains(NONPRINT_RE, regex=True).sum())
        lens = c.stripped[c.present].str.len()
        fail_n = int(c.fail.sum()) if c.fail is not None else 0
        fail_ex = fmt_examples(c.stripped[c.fail].unique()) if fail_n else ""
        # mixed formats
        mixed, fmt_detail = "N", ""
        if c.fmt_counts:
            fsum = sum(c.fmt_counts.values()) or 1
            fmt_detail = "; ".join(f"{k}: {v} ({v / fsum:.1%})" for k, v in c.fmt_counts.items())
        if c.itype in ("integer", "decimal", "code"):
            mixed = "Y" if c.special_fmt else "N"
        elif c.itype == "date":
            mixed = "Y" if len(c.fmt_counts) > 1 else "N"
        elif c.itype == "boolean":
            mixed = "Y" if len(c.fmt_counts) > 2 else "N"
        elif c.itype in ("categorical", "text") and len(vc) <= 200000:
            normed = pd.Series(vc.index).map(norm_text)
            coll = normed.duplicated(keep=False).sum()
            if coll:
                mixed = "Y"
                fmt_detail = f"{int(coll)} distinct raw values collapse to the same value after case/space/punctuation normalisation"
        num = c.num[c.present] if c.num is not None and c.itype in ("integer", "decimal") else None
        row = {
            "table": t.name,
            "column": c.name,
            "position": c.pos,
            "inferred_type": c.itype,
            "key_role": c.role,
            "references": c.references,
            "row_count": n,
            "null_empty_count": int(c.empty.sum()),
            "whitespace_only_count": int(c.ws_only.sum()),
            "placeholder_token_count": int(c.placeholder.sum()),
            "placeholder_tokens_seen": fmt_examples(c.stripped[c.placeholder].unique()),
            "null_blank_total": int(c.missing.sum()),
            "null_blank_pct": round(100 * c.missing.mean(), 3) if n else 0,
            "distinct_count": int(len(vc)),
            "distinct_pct_of_nonblank": round(100 * len(vc) / max(int(c.present.sum()), 1), 3),
            "is_unique": "Y" if len(vc) == int(c.present.sum()) and len(vc) > 0 else "N",
            "dominant_shapes": "; ".join(f"{k} ({v / tot:.1%})" for k, v in shape_counts.head(3).items()),
            "shape_count": int(len(shape_counts)),
            "top_values": "; ".join(f"{k} ({v})" for k, v in vc.head(5).items()),
            "parse_fail_count": fail_n,
            "parse_fail_pct": round(100 * fail_n / max(int(c.present.sum()), 1), 3),
            "parse_fail_examples": fail_ex,
            "leading_trailing_ws_count": lt_ws,
            "internal_multi_space_count": multi_ws,
            "nonprinting_char_count": nonprint,
            "mixed_formats": mixed,
            "format_breakdown": fmt_detail,
            "min_length": int(lens.min()) if len(lens) else "",
            "max_length": int(lens.max()) if len(lens) else "",
            "leading_zero_count": int(c.stripped.str.match(r"^0\d").sum()) if c.itype in ("integer", "code") else "",
            "numeric_min": float(num.min()) if num is not None and num.notna().any() else "",
            "numeric_max": float(num.max()) if num is not None and num.notna().any() else "",
            "numeric_mean": round(float(num.mean()), 6) if num is not None and num.notna().any() else "",
            "numeric_median": float(num.median()) if num is not None and num.notna().any() else "",
            "negative_count": int((num < 0).sum()) if num is not None else "",
            "zero_count": int((num == 0).sum()) if num is not None else "",
            "date_min": c.dt.min().date().isoformat() if c.dt is not None and c.dt.notna().any() else "",
            "date_max": c.dt.max().date().isoformat() if c.dt is not None and c.dt.notna().any() else "",
            "true_count": int(c.bl.fillna(False).sum()) if c.bl is not None else "",
        }
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# GROUP 1 -- column-level checks
# --------------------------------------------------------------------------
def check_file_structure(t: Table):
    for issue in t.header_issues:
        add("WARN", 1, "header_problem", t.name, "", None, t.n, issue)
    if t.bad_lines:
        add("ERROR", 1, "ragged_rows", t.name, "", len(t.bad_lines), t.n + len(t.bad_lines),
            "rows whose field count differs from the header (too many fields are "
            "dropped by the reader; too few are padded with blanks)",
            "lines " + fmt_examples(t.bad_lines))
    if t.blank_lines:
        add("INFO", 1, "blank_lines", t.name, "", t.blank_lines, t.n,
            "completely blank lines in the file (skipped)")


def check_keys(t: Table):
    if not t.pk:
        add("WARN", 1, "no_primary_key", t.name, "", None, t.n,
            "no column could be identified as a unique row identifier")
        return
    c = t.cols[t.pk]
    add("INFO", 1, "primary_key_inferred", t.name, t.pk, None, t.n,
        f"primary key = {t.pk} ({t.pk_inferred_how})")
    if c.missing.any():
        add("ERROR", 1, "pk_blank", t.name, t.pk, c.missing.sum(), t.n,
            "primary key is blank / placeholder", t.row_labels(c.missing))
    dup = c.present & c.stripped.duplicated(keep=False)
    if dup.any():
        vals = c.stripped[dup].value_counts()
        add("ERROR", 1, "pk_not_unique", t.name, t.pk, dup.sum(), t.n,
            f"{len(vals)} key values appear more than once ({int(dup.sum())} rows)",
            fmt_examples([f"{k} x{v}" for k, v in vals.head(5).items()]))
    if c.itype not in ("integer", "code"):
        add("INFO", 1, "pk_not_integer", t.name, t.pk, None, t.n,
            f"primary key inferred type is {c.itype}")


def check_column(t: Table, c: Col):
    n = t.n
    is_key = c.role in ("pk", "fk")
    if c.itype == "empty":
        add("WARN", 1, "column_entirely_blank", t.name, c.name, n, n,
            "every value is blank / placeholder")
        return
    present_n = int(c.present.sum())

    # ---- type --------------------------------------------------------------
    if c.fail is not None and c.fail.any():
        bad = c.stripped[c.fail]
        add("ERROR", 1, "type_parse_failure", t.name, c.name, len(bad), n,
            f"{len(bad)} of {present_n} non-blank values ({len(bad) / present_n:.2%}) "
            f"do not parse as the expected type '{c.itype}'",
            fmt_examples(bad.value_counts().index))
    if c.itype in ("integer", "decimal", "code") and c.special_fmt:
        detail = "; ".join(f"{k}: {v}" for k, v in c.special_fmt.items())
        hidden = any(k in c.special_fmt for k in ("currency-symbol", "percent-sign",
                                                  "thousands-separator", "accounting-negative ( )"))
        add("ERROR" if hidden else "WARN", 1, "mixed_number_formats", t.name, c.name,
            sum(c.special_fmt.values()), n,
            ("numbers stored with $ / % / , / ( ) -- must be stripped before conversion; "
             if hidden else "integer column contains values written as 'N.0'; ") + detail)
    if c.itype == "date" and len(c.fmt_counts) > 1:
        add("ERROR", 1, "mixed_date_formats", t.name, c.name,
            sum(list(c.fmt_counts.values())[1:]), n,
            "more than one date format in the column (ambiguous parsing): " +
            "; ".join(f"{k}: {v}" for k, v in c.fmt_counts.items()))
    if c.itype == "boolean" and len(c.fmt_counts) > 2:
        add("WARN", 1, "mixed_boolean_tokens", t.name, c.name, present_n, n,
            "boolean column uses several spellings: " +
            "; ".join(f"{k}: {v}" for k, v in c.fmt_counts.items()))
    if c.itype == "decimal" and c.num is not None:
        x = c.num[c.present].dropna()
        if len(x) and (x == x.round()).all() and c.fmt_counts.get("integer-with-.0"):
            add("INFO", 1, "integral_values_stored_as_decimal", t.name, c.name, len(x), n,
                "every value is a whole number written as 'N.0' (float artefact) -- "
                "can be stored as integer")
    if c.itype == "code":
        lz = int(c.stripped.str.match(r"^0\d").sum())
        add("INFO", 1, "numeric_code_keep_as_text", t.name, c.name, lz, n,
            f"digit-only code column; {lz} values have leading zeros that a numeric "
            "conversion would silently drop -- keep as text")

    # ---- whitespace / hidden characters ------------------------------------
    lt = c.present & (c.raw != c.stripped)
    if lt.any():
        add("ERROR" if is_key else "WARN", 1, "leading_trailing_whitespace", t.name, c.name,
            lt.sum(), n, "values with leading/trailing spaces" +
            (" -- will break joins on this key" if is_key else ""),
            fmt_examples(c.raw[lt].unique()))
    if c.ws_only.any():
        add("WARN", 1, "whitespace_only_cells", t.name, c.name, c.ws_only.sum(), n,
            "cells containing only spaces (look filled, are blank)")
    ms = c.stripped.str.contains(r"\s{2,}", regex=True)
    if ms.any():
        add("WARN", 1, "internal_multiple_spaces", t.name, c.name, ms.sum(), n,
            "values with 2+ consecutive internal spaces", fmt_examples(c.raw[ms].unique()))
    npc = c.raw.str.contains(NONPRINT_RE, regex=True)
    if npc.any():
        add("WARN", 1, "nonprinting_characters", t.name, c.name, npc.sum(), n,
            "non-breaking space / zero-width / control characters present",
            fmt_examples(c.raw[npc].unique()))

    # ---- placeholders / blanks / constants ---------------------------------
    if c.placeholder.any():
        toks = c.stripped[c.placeholder].value_counts()
        add("WARN", 1, "placeholder_tokens", t.name, c.name, c.placeholder.sum(), n,
            "literal null-like tokens stored as text (default pd.read_csv would turn "
            "'None'/'NA'/'NULL' into NaN). Decide: real value or missing? " +
            "; ".join(f"{k!r}: {v}" for k, v in toks.items()))
    blank_share = float((c.empty | c.ws_only).mean())
    if 0 < blank_share and not is_key:
        add("INFO", 1, "blank_values", t.name, c.name, int((c.empty | c.ws_only).sum()), n,
            f"{blank_share:.1%} of cells are blank")
    if c.nunique == 1 and n > 1:
        add("INFO", 1, "constant_column", t.name, c.name, present_n, n,
            f"only one distinct value: {c.stripped[c.present].iloc[0]!r}")

    # ---- allowed values (categorical domains) ------------------------------
    if c.itype in ("categorical", "boolean") or (c.itype == "text" and c.nunique <= 200):
        vc = c.stripped[c.present].value_counts()
        normed = pd.Series(vc.index, index=vc.index).map(norm_text)
        groups = normed.groupby(normed.values).apply(lambda g: list(g.index))
        variants = [g for g in groups if len(g) > 1]
        if variants:
            rows = sum(int(vc[g].sum() - vc[g].max()) for g in variants)
            add("WARN", 1, "case_or_spelling_variants", t.name, c.name, rows, n,
                f"{len(variants)} values have case/space/punctuation variants -- they will "
                "split group-bys and fail equality joins",
                "; ".join(" | ".join(repr(x) for x in g) for g in variants[:5]))
        if c.itype == "categorical" and present_n >= 100 and len(vc) >= 3:
            common = vc[vc / present_n >= 0.01]
            rare = vc[(vc <= max(2, 0.002 * present_n)) & (vc / vc.max() < 0.01)]
            if len(rare) and rare.sum() / present_n < 0.02 and len(common):
                for val, cnt in rare.items():
                    m = difflib.get_close_matches(str(val), [str(x) for x in common.index], n=1, cutoff=0.8)
                    add("WARN", 1, "rare_category_value", t.name, c.name, cnt, n,
                        f"value {val!r} occurs only {cnt}x" +
                        (f" -- looks like a typo of {m[0]!r}" if m else ""),
                        t.row_labels(c.stripped.eq(val)))
        if c.itype == "categorical":
            add("INFO", 1, "allowed_values_observed", t.name, c.name, present_n, n,
                f"{len(vc)} distinct values: " + "; ".join(f"{k} ({v})" for k, v in vc.head(15).items()))

    # ---- ranges -------------------------------------------------------------
    if c.itype in ("integer", "decimal") and c.role == "attribute" and c.num is not None:
        check_numeric_range(t, c)
    if c.itype == "date":
        d = c.dt.dropna()
        old = c.dt < pd.Timestamp("1900-01-01")
        fut = c.dt > TODAY
        if old.any():
            add("ERROR", 1, "date_out_of_range", t.name, c.name, old.sum(), n,
                "dates before 1900", t.row_labels(old))
        if fut.any():
            far = c.dt > TODAY + pd.DateOffset(years=5)
            add("WARN" if not far.any() else "ERROR", 1, "future_dates", t.name, c.name, fut.sum(), n,
                f"dates after today ({TODAY.date()}); max = {d.max().date()}", t.row_labels(fut))
        if len(d):
            add("INFO", 1, "date_range", t.name, c.name, len(d), n,
                f"{d.min().date()} to {d.max().date()} ({d.nunique()} distinct dates)")


def check_numeric_range(t: Table, c: Col):
    n = t.n
    x = c.num.where(c.present)
    xv = x.dropna()
    if len(xv) < 5 or c.lname in DATE_PART_NAMES:
        return
    nm = c.lname
    if AGE_NAME.search(nm):
        bad = (x < 0) | (x > 120)
        if bad.any():
            add("ERROR", 1, "range_impossible", t.name, c.name, bad.sum(), n,
                "age outside 0-120", t.row_labels(bad))
        minor = (x >= 0) & (x < 18)
        if minor.any() and (xv >= 18).mean() >= 0.95:
            add("WARN", 1, "range_suspicious", t.name, c.name, minor.sum(), n,
                "age under 18", t.row_labels(minor))
    if RETURN_NAME.search(nm):
        bad = x < -1
        if bad.any():
            add("ERROR", 1, "range_impossible", t.name, c.name, bad.sum(), n,
                "return below -100% (cannot lose more than everything)", t.row_labels(bad))
        severe = (x >= -1) & (x <= -0.9)
        if severe.any():
            add("WARN", 1, "range_suspicious", t.name, c.name, severe.sum(), n,
                "return between -100% and -90% (near-total loss)", t.row_labels(severe))
        huge = x > 3
        if huge.any():
            add("WARN", 1, "range_suspicious", t.name, c.name, huge.sum(), n,
                "return above +300%", t.row_labels(huge))
    if PCT_NAME.search(nm):
        if xv.between(0, 1).mean() >= 0.99:
            bad = x.notna() & ~x.between(0, 1)
            if bad.any():
                add("WARN", 1, "range_outside_0_1", t.name, c.name, bad.sum(), n,
                    "share/percentage outside [0, 1] while the rest of the column is in [0, 1]",
                    t.row_labels(bad))
        elif xv.between(0, 100).mean() >= 0.99 and xv.max() > 1:
            bad = x.notna() & ~x.between(0, 100)
            if bad.any():
                add("WARN", 1, "range_outside_0_100", t.name, c.name, bad.sum(), n,
                    "percentage outside [0, 100]", t.row_labels(bad))
    if NONNEG_NAME.search(nm) and not RETURN_NAME.search(nm):
        neg = x < 0
        if neg.any() and (xv >= 0).mean() >= 0.99:
            add("WARN", 1, "unexpected_negative", t.name, c.name, neg.sum(), n,
                "negative values in a column that is otherwise non-negative", t.row_labels(neg))
    # spike: one value repeated far more than any other in a continuous column
    if c.itype == "decimal" and c.nunique > 0.5 * len(xv):
        vc = c.stripped[c.present].value_counts()
        top, cnt = vc.index[0], int(vc.iloc[0])
        second = int(vc.iloc[1]) if len(vc) > 1 else 0
        if cnt >= 10 and cnt >= 0.001 * len(xv) and cnt >= 5 * max(second, 1):
            add("WARN", 1, "suspicious_repeated_value", t.name, c.name, cnt, n,
                f"value {top!r} appears {cnt}x in an otherwise near-unique column "
                f"(next most common appears {second}x) -- default, floor, or placeholder?",
                t.row_labels(c.stripped.eq(top)))
    # statistical outliers (IQR fence, k=3; log scale for wide positive ranges)
    if c.nunique > 10:
        use_log = xv.min() > 0 and xv.max() / xv.min() > 1000
        z = np.log10(x) if use_log else x
        zv = z.dropna()
        q1, q3 = zv.quantile([0.25, 0.75])
        iqr = q3 - q1
        if iqr > 0:
            out = (z < q1 - 3 * iqr) | (z > q3 + 3 * iqr)
            if out.any():
                lo, hi = (10 ** (q1 - 3 * iqr), 10 ** (q3 + 3 * iqr)) if use_log else (q1 - 3 * iqr, q3 + 3 * iqr)
                add("INFO", 1, "statistical_outliers", t.name, c.name, out.sum(), n,
                    f"values outside the 3xIQR fence [{lo:,.4g}, {hi:,.4g}]"
                    + (" (log scale)" if use_log else ""), t.row_labels(out))


# --------------------------------------------------------------------------
# GROUP 2 -- single-table rules
# --------------------------------------------------------------------------
def check_duplicates(t: Table, is_fact: bool = True):
    df = t.df
    n = t.n
    exact = df.duplicated(keep="first")
    if exact.any():
        add("ERROR", 2, "exact_duplicate_rows", t.name, "(all columns)", exact.sum(), n,
            "rows identical in every column (incl. the key) -- double counting", t.row_labels(exact))
    others = [c for c in df.columns if c != t.pk]
    if t.pk and others:
        dup = df.duplicated(subset=others, keep="first") & ~exact
        if dup.any():
            add("WARN", 2, "duplicate_rows_different_key", t.name, "(all but " + t.pk + ")",
                dup.sum(), n, "rows identical in every column except the primary key",
                t.row_labels(dup))
    else:
        dup = exact
    # near duplicates: identical after normalising case/space/punctuation and
    # numeric formatting ("45" == "45.0")
    if others:
        norm = pd.DataFrame(index=df.index)
        for cn in others:
            c = t.cols[cn]
            if c.itype in ("integer", "decimal") and c.num is not None:
                norm[cn] = c.num.round(max(c.decimals, 0)).astype(str)
            elif c.itype == "date":
                norm[cn] = c.dt.astype(str)
            else:
                norm[cn] = c.stripped.str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
        near = norm.duplicated(keep="first") & ~dup & ~exact
        if near.any():
            add("WARN", 2, "near_duplicate_rows", t.name, "(all but key)", near.sum(), n,
                "rows that become identical after normalising case, spacing, punctuation "
                "and number formatting", t.row_labels(near))
    # identity near-duplicates (e.g. same person under two IDs)
    ident = [cn for cn in others if IDENTITY_NAME.search(cn) and t.cols[cn].itype in ("text", "categorical", "code")]
    if t.pk and len(ident) >= 2:
        key = pd.Series("", index=df.index)
        for cn in ident:
            key = key + "|" + t.cols[cn].stripped.str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
        d = key.duplicated(keep=False) & ~t.cols[ident[0]].missing
        if d.any():
            add("WARN", 2, "possible_same_entity_twice", t.name, ", ".join(ident), d.sum(), n,
                f"different {t.pk} values share the same {', '.join(ident)} (normalised)",
                t.row_labels(d))
    # grain duplicates: same foreign keys + same date
    dcols = [c.name for c in t.date_cols()]
    if is_fact and t.fks and dcols:
        grain = list(t.fks) + dcols[:1]
        present = pd.concat([t.cols[g].present for g in grain], axis=1).all(axis=1)
        g = df[grain].duplicated(keep=False) & present
        if g.any():
            rate = g.mean()
            add("WARN" if rate < 0.05 else "INFO", 2, "duplicate_grain", t.name, ", ".join(grain),
                g.sum(), n,
                f"{rate:.2%} of rows share the same ({', '.join(grain)}) combination"
                + (" -- looks like accidental duplicates" if rate < 0.05 else
                   " -- this is not the table's grain (multiple rows per key/date is normal here)"),
                t.row_labels(g))


def _expected_bool(present, values, expected, t, c, label, one_sided=False):
    v = values.astype("boolean")
    mask = present & v.notna()
    if one_sided:
        bad = mask & v.fillna(False).astype(bool) & ~expected
    else:
        bad = mask & (v.fillna(False).astype(bool) != expected)
    return bad


def check_date_parts(t: Table):
    dcols = t.date_cols()
    if not dcols:
        return
    d = next((c for c in dcols if c.lname in ("date", "full_date", "calendar_date", "dt")), dcols[0])
    dt = d.dt
    ok = dt.notna()
    names = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for c in t.cols.values():
        if c is d or c.lname not in DATE_PART_NAMES:
            continue
        nm = c.lname
        pres = ok & c.present
        bad = None
        rule = ""
        if nm in ("year", "yr", "calendar_year"):
            bad, rule = pres & (c.num != dt.dt.year), "year"
        elif nm in ("month", "month_num", "month_number"):
            bad, rule = pres & (c.num != dt.dt.month), "month"
        elif nm in ("quarter", "qtr"):
            bad, rule = pres & (c.num != dt.dt.quarter), "quarter"
        elif nm in ("day", "day_of_month", "dom"):
            bad, rule = pres & (c.num != dt.dt.day), "day of month"
        elif nm in ("day_of_year", "doy"):
            bad, rule = pres & (c.num != dt.dt.dayofyear), "day of year"
        elif nm == "month_name":
            full = dt.dt.month.map(lambda m: names[int(m) - 1] if pd.notna(m) else "")
            v = c.stripped.str.lower()
            bad, rule = pres & (v != full) & (v != full.str[:3]), "month name"
        elif nm in ("day_of_week", "weekday", "day_name", "dow"):
            if c.itype in ("integer", "decimal"):
                convs = {"0=Mon": dt.dt.dayofweek, "1=Mon": dt.dt.dayofweek + 1,
                         "0=Sun": (dt.dt.dayofweek + 1) % 7, "1=Sun": (dt.dt.dayofweek + 1) % 7 + 1}
                scores = {k: int((pres & (c.num != v)).sum()) for k, v in convs.items()}
                best = min(scores, key=scores.get)
                bad, rule = pres & (c.num != convs[best]), f"day-of-week number ({best})"
            else:
                full = dt.dt.dayofweek.map(lambda w: days[int(w)] if pd.notna(w) else "")
                v = c.stripped.str.lower()
                bad, rule = pres & (v != full) & (v != full.str[:3]), "day name"
        elif nm in ("week", "week_of_year", "week_num", "iso_week"):
            convs = {"ISO": dt.dt.isocalendar().week.astype(float),
                     "%U (Sun-start)": pd.to_numeric(dt.dt.strftime("%U"), errors="coerce"),
                     "%W (Mon-start)": pd.to_numeric(dt.dt.strftime("%W"), errors="coerce")}
            convs["%U+1"] = convs["%U (Sun-start)"] + 1
            convs["%W+1"] = convs["%W (Mon-start)"] + 1
            scores = {k: int((pres & (c.num != v)).sum()) for k, v in convs.items()}
            best = min(scores, key=scores.get)
            bad, rule = pres & (c.num != convs[best]), f"week number (best convention: {best})"
        elif c.bl is not None or c.itype in ("boolean", "integer"):
            vals = c.bl if c.bl is not None else c.num.map({1.0: True, 0.0: False})
            exp = {"is_month_end": dt.dt.is_month_end, "is_month_start": dt.dt.is_month_start,
                   "is_quarter_end": dt.dt.is_quarter_end, "is_quarter_start": dt.dt.is_quarter_start,
                   "is_year_end": dt.dt.is_year_end, "is_year_start": dt.dt.is_year_start,
                   "is_weekend": dt.dt.dayofweek >= 5}.get(nm)
            if exp is not None:
                bad, rule = _expected_bool(pres, vals, exp.fillna(False), t, c, nm), nm
            elif nm in ("is_weekday", "is_trading_day", "is_business_day"):
                bad = _expected_bool(pres, vals, (dt.dt.dayofweek < 5).fillna(False), t, c, nm, one_sided=True)
                rule = f"{nm} on a weekend"
                if not bad.any() and nm != "is_weekday":
                    vb = vals.astype("boolean").fillna(False).astype(bool)
                    if (vb == (dt.dt.dayofweek < 5)).all():
                        add("INFO", 2, "calendar_flag_equals_weekday", t.name, c.name, None, t.n,
                            f"{nm} is 1 for every weekday -- market holidays are not excluded")
        if bad is not None:
            bad = bad.fillna(False).astype(bool)
            if bad.any():
                add("ERROR", 2, "contradicts_date_column", t.name, f"{c.name} vs {d.name}",
                    bad.sum(), t.n, f"{rule} does not match {d.name} in the same row",
                    t.row_labels(bad))


def discover_formulas(t: Table):
    """Find c = k*a*b or c = a +/- b relationships that hold for >=90% of rows,
    then report the rows that break them."""
    cand = [c for c in t.cols.values()
            if c.itype in ("integer", "decimal") and c.role == "attribute"
            and c.lname not in DATE_PART_NAMES and c.nunique > 3]
    if len(cand) < 3:
        return
    cand = cand[:12]
    X = pd.DataFrame({c.name: c.num.where(c.present) for c in cand})
    S = X.sample(min(len(X), 20000), random_state=0)
    seen = set()

    def tol(cname, vals):
        dec = t.cols[cname].decimals
        return np.maximum(0.005 * vals.abs(), 3 * 10.0 ** (-dec))

    for c in cand:
        cn = c.name
        for a, b in itertools.combinations([x.name for x in cand if x.name != cn], 2):
            key = frozenset((a, b, cn))
            if key in seen:
                continue
            sub = S[[a, b, cn]].dropna()
            if len(sub) < 50:
                continue
            found = None
            prod = sub[a] * sub[b]
            nz = prod.abs() > 1e-12
            if nz.mean() > 0.5:
                k = (sub[cn][nz] / prod[nz]).median()
                if np.isfinite(k) and k != 0:
                    k = nice_constant(k)
                    if ((sub[cn] - k * prod).abs() <= tol(cn, sub[cn])).mean() >= 0.9:
                        found = ("product", k, f"{cn} = {a} * {b}{const_str(k)}")
            if not found:
                for op, f, txt in (("+", lambda p, q: p + q, f"{cn} = {a} + {b}"),
                                   ("-", lambda p, q: p - q, f"{cn} = {a} - {b}"),
                                   ("r-", lambda p, q: q - p, f"{cn} = {b} - {a}")):
                    if ((sub[cn] - f(sub[a], sub[b])).abs() <= tol(cn, sub[cn])).mean() >= 0.9:
                        found = (op, f, txt)
                        break
            if not found:
                continue
            seen.add(key)
            full = X[[a, b, cn]].dropna()
            if found[0] == "product":
                pred = found[1] * full[a] * full[b]
            else:
                pred = found[1](full[a], full[b])
            viol_idx = full.index[((full[cn] - pred).abs() > tol(cn, full[cn])).values]
            holds = 1 - len(viol_idx) / len(full)
            mask = pd.Series(False, index=t.df.index)
            mask.loc[viol_idx] = True
            if len(viol_idx):
                add("ERROR" if holds >= 0.98 else "WARN", 2, "derived_value_mismatch", t.name,
                    f"{a}, {b}, {cn}", len(viol_idx), t.n,
                    f"inferred rule '{found[2]}' holds for {holds:.2%} of rows; "
                    f"{len(viol_idx)} rows break it", t.row_labels(mask))
            else:
                add("INFO", 2, "derived_rule_holds", t.name, f"{a}, {b}, {cn}", 0, t.n,
                    f"inferred rule '{found[2]}' holds for every row")


def check_conditional_blanks(t: Table):
    """X is blank exactly when category column C takes certain values."""
    n = t.n
    min_grp = 20 if n >= 500 else 3
    cats = [c for c in t.cols.values() if c.itype in ("categorical", "boolean") and 2 <= c.nunique <= 30]
    for x in t.cols.values():
        rate = x.missing.mean()
        if not (0.001 <= rate <= 0.999) or x.role == "pk":
            continue
        best = None
        for c in cats:
            if c is x:
                continue
            g = x.missing.groupby(c.stripped.where(c.present, "<blank>"))
            stats = pd.DataFrame({"rate": g.mean(), "n": g.size()})
            big = stats[stats.n >= min_grp]
            if len(big) < 2:
                continue
            blank_cats = big.index[big.rate >= 0.98].tolist()
            fill_cats = big.index[big.rate <= 0.02].tolist()
            if len(blank_cats) + len(fill_cats) != len(big) or not blank_cats or not fill_cats:
                continue
            viol = (c.stripped.isin(blank_cats) & x.present) | (c.stripped.isin(fill_cats) & x.missing)
            if best is None or viol.sum() < best[1].sum():
                best = (c, viol, blank_cats)
        if best:
            c, viol, blank_cats = best
            rule = f"{x.name} is blank when {c.name} in {blank_cats}, filled otherwise"
            if viol.any():
                add("WARN", 2, "conditional_blank_violation", t.name, f"{x.name} vs {c.name}",
                    viol.sum(), n, f"inferred rule '{rule}' is broken by {int(viol.sum())} rows",
                    t.row_labels(viol))
            else:
                add("INFO", 2, "conditional_blank_rule_holds", t.name, f"{x.name} vs {c.name}",
                    int(x.missing.sum()), n, f"blanks are explained: {rule} (all rows comply)")
        elif x.missing.any() and x.role == "fk":
            add("INFO", 2, "fk_blank_unexplained", t.name, x.name, x.missing.sum(), n,
                "foreign key is blank and no category column explains when it should be")


def check_entity_consistency(t: Table):
    """Attributes that are (almost) constant per entity, but vary for a few."""
    for k in t.fks:
        kc = t.cols[k]
        nk = kc.stripped[kc.present].nunique()
        if nk == 0 or t.n / nk < 2:
            continue
        for c in t.cols.values():
            if c.name in (k, t.pk) or c.lname in DATE_PART_NAMES or c.itype in ("boolean", "date", "text", "empty"):
                continue
            if c.itype in ("integer", "decimal") and c.role != "fk":
                continue
            m = kc.present & c.present
            per = c.stripped[m].groupby(kc.stripped[m]).nunique()
            if len(per) == 0 or per.max() <= 1:
                continue
            single = (per == 1).mean()
            if single >= 0.95:
                multi = per[per > 1].index
                rows = m & kc.stripped.isin(multi)
                add("WARN", 2, "attribute_changes_within_entity", t.name, f"{c.name} per {k}",
                    rows.sum(), t.n,
                    f"{c.name} is constant for {single:.1%} of {k} values but varies for "
                    f"{len(multi)} of them (re-assignment or data error?)",
                    fmt_examples(list(multi[:5])))


def check_row_date_order(t: Table):
    dc = t.date_cols()
    for a in dc:
        for b in dc:
            if a is b or not (START_NAME.search(a.lname) and END_NAME.search(b.lname)):
                continue
            bad = a.dt.notna() & b.dt.notna() & (b.dt < a.dt)
            if bad.any():
                add("ERROR", 2, "end_before_start", t.name, f"{a.name}, {b.name}", bad.sum(), t.n,
                    f"{b.name} is earlier than {a.name}", t.row_labels(bad))


def check_source_flags(t: Table):
    for c in t.cols.values():
        if c.itype == "boolean" and c.lname not in DATE_PART_NAMES and c.bl is not None:
            share = float(c.bl.fillna(False).mean())
            if 0 < share <= 0.05:
                add("INFO", 2, "rows_flagged_by_source", t.name, c.name, int(c.bl.fillna(False).sum()), t.n,
                    f"{share:.2%} of rows have {c.name} = True -- the source itself marks these as special")


def check_calendar(t: Table) -> bool:
    """Is this a calendar/date dimension? If so check for gaps."""
    for c in t.date_cols():
        if c.missing.mean() < 0.01 and not c.dt.dropna().duplicated().any() and t.n >= 28 \
                and any(x in t.cols for x in ("year", "month", "quarter", "day_of_week", "month_name")):
            d = c.dt.dropna()
            full = pd.date_range(d.min(), d.max(), freq="D")
            missing = full.difference(pd.DatetimeIndex(d))
            if len(missing) and len(missing) < 0.1 * len(full):
                add("WARN", 2, "calendar_gaps", t.name, c.name, len(missing), len(full),
                    f"{len(missing)} days missing between {d.min().date()} and {d.max().date()}",
                    fmt_examples([str(x.date()) for x in missing[:5]]))
            return True
    return False


# --------------------------------------------------------------------------
# GROUP 3 -- referential integrity
# --------------------------------------------------------------------------
def norm_key(v: str) -> str:
    v = v.strip()
    v = re.sub(r"\.0+$", "", v)
    return v.lstrip("0") or "0"


def check_referential(tables: dict):
    for t in tables.values():
        for k, (pt, pc) in t.fks.items():
            child = t.cols[k]
            parent = tables[pt].cols[pc]
            pkeys = set(parent.stripped[parent.present])
            vals = child.stripped.where(child.present)
            orphan = child.present & ~vals.isin(pkeys)
            if orphan.any():
                ov = child.stripped[orphan]
                pnorm = {norm_key(x) for x in pkeys}
                fixable = ov.map(norm_key).isin(pnorm)
                real = orphan.copy()
                real[orphan] = ~fixable.values
                if fixable.any():
                    add("ERROR", 3, "fk_format_mismatch", t.name, k, int(fixable.sum()), t.n,
                        f"values match {pt}.{pc} only after removing leading zeros / '.0' -- "
                        "the join will silently drop them", fmt_examples(ov[fixable.values].unique()))
                if real.any():
                    miss = child.stripped[real].value_counts()
                    add("ERROR", 3, "orphan_foreign_key", t.name, k, int(real.sum()), t.n,
                        f"{int(real.sum())} rows reference {len(miss)} {k} values that do not exist "
                        f"in {pt}.{pc}", fmt_examples([f"{a} x{b}" for a, b in miss.head(5).items()]))
            else:
                add("INFO", 3, "fk_ok", t.name, k, 0, t.n,
                    f"every non-blank {k} exists in {pt}.{pc}")
            if child.missing.any():
                add("INFO", 3, "fk_blank", t.name, k, child.missing.sum(), t.n,
                    f"{int(child.missing.sum())} rows have no {k} (cannot join to {pt}; "
                    "see conditional-blank checks for whether that is expected)")
            if child.itype != parent.itype and {child.itype, parent.itype} - {"integer", "code"}:
                add("WARN", 3, "fk_type_mismatch", t.name, k, None, t.n,
                    f"{t.name}.{k} looks like {child.itype} but {pt}.{pc} looks like {parent.itype}")
            used = set(vals.dropna())
            unused = pkeys - used
            if unused:
                add("INFO", 3, "unreferenced_parent_rows", pt, pc, len(unused), tables[pt].n,
                    f"{len(unused)} of {len(pkeys)} {pt} rows are never referenced by {t.name}.{k}",
                    fmt_examples(sorted(unused, key=lambda z: (len(z), z))[:5]))


# --------------------------------------------------------------------------
# GROUP 4 -- cross-table business rules
# --------------------------------------------------------------------------
def compatible(a: Col, b: Col) -> bool:
    numeric = {"integer", "decimal"}
    if a.itype in numeric and b.itype in numeric:
        return True
    keyish = {"integer", "code"}
    if a.itype in keyish and b.itype in keyish:
        return True
    return a.itype == b.itype and a.itype in ("categorical", "boolean", "code", "date", "text")


def values_agree(a: Col, b_vals: pd.Series, b: Col, mask):
    if a.itype in ("integer", "decimal") and b.itype in ("integer", "decimal"):
        bv = pd.to_numeric(b_vals, errors="coerce")
        tolv = np.maximum(0.005 * bv.abs(), 0.01)
        return (a.num - bv).abs() <= tolv
    if a.itype == "boolean":
        return a.stripped.str.lower().map(BOOL_MAP) == b_vals.str.lower().map(BOOL_MAP)
    return a.stripped.map(norm_text) == b_vals.map(norm_text)


def check_fact_vs_dim(tables: dict):
    """For every FK edge, columns that exist on both sides (same/related
    name) should agree for the referenced row."""
    for t in tables.values():
        for k, (pt, pc) in t.fks.items():
            d = tables[pt]
            dk = d.cols[pc]
            lookup_ok = dk.present & ~dk.stripped.duplicated(keep=False)
            for c in t.cols.values():
                if c.name in (k, t.pk) or c.lname in DATE_PART_NAMES or c.itype == "empty":
                    continue
                for dc in d.cols.values():
                    if dc.name == pc or dc.itype == "empty" or not related_names(c.name, dc.name):
                        continue
                    if not compatible(c, dc):
                        continue
                    mapper = pd.Series(dc.stripped[lookup_ok].values, index=dk.stripped[lookup_ok].values)
                    mpres = pd.Series(dc.present[lookup_ok].values, index=dk.stripped[lookup_ok].values)
                    other = t.cols[k].stripped.map(mapper)
                    other_present = t.cols[k].stripped.map(mpres).fillna(False).astype(bool)
                    both = c.present & other_present & other.notna()
                    if both.sum() < 20:
                        continue
                    agree = values_agree(c, other.fillna(""), dc, both) & both
                    rate = agree.sum() / both.sum()
                    dis = both & ~agree
                    if rate >= 0.5 and dis.any():
                        add("WARN", 4, "fact_disagrees_with_dimension", t.name,
                            f"{t.name}.{c.name} vs {pt}.{dc.name} (via {k})", dis.sum(), t.n,
                            f"{c.name} differs from the referenced {pt} row's {dc.name} in "
                            f"{int(dis.sum())} rows (agrees in {rate:.2%})", t.row_labels(dis))
                    elif rate == 1:
                        add("INFO", 4, "fact_matches_dimension", t.name,
                            f"{t.name}.{c.name} vs {pt}.{dc.name} (via {k})", 0, t.n,
                            f"{c.name} always equals {pt}.{dc.name} -- redundant copy")
            # facts dated before the entity's start date / after its end date
            fdates = t.date_cols()
            if not fdates:
                continue
            fd = fdates[0]
            for dc in d.date_cols():
                is_start, is_end = START_NAME.search(dc.lname), END_NAME.search(dc.lname)
                if not (is_start or is_end):
                    continue
                mapper = pd.Series(dc.dt[lookup_ok].values, index=dk.stripped[lookup_ok].values)
                ref = pd.to_datetime(t.cols[k].stripped.map(mapper), errors="coerce")
                if is_start:
                    bad = fd.dt.notna() & ref.notna() & (fd.dt < ref)
                    txt = f"{fd.name} is before the {pt}.{dc.name} date"
                else:
                    bad = fd.dt.notna() & ref.notna() & (fd.dt > ref)
                    txt = f"{fd.name} is after the {pt}.{dc.name} date"
                if bad.any():
                    ents = t.cols[k].stripped[bad].nunique()
                    add("WARN", 4, "fact_outside_entity_lifetime", t.name,
                        f"{t.name}.{fd.name} vs {pt}.{dc.name}", bad.sum(), t.n,
                        f"{txt} ({ents} distinct {k} values)", t.row_labels(bad))
            # activity for inactive entities
            for dc in d.cols.values():
                if ACTIVE_NAME.match(dc.name) and dc.bl is not None:
                    inactive = set(dk.stripped[(dc.bl == False).fillna(False).astype(bool)])  # noqa: E712
                    if not inactive:
                        continue
                    last = fd.dt.max()
                    if pd.isna(last):
                        continue
                    recent = fd.dt >= (last - pd.DateOffset(months=1))
                    hit = recent & t.cols[k].stripped.isin(inactive)
                    if hit.any():
                        add("WARN", 4, "activity_for_inactive_entity", t.name,
                            f"{t.name}.{k} vs {pt}.{dc.name}", hit.sum(), t.n,
                            f"{t.cols[k].stripped[hit].nunique()} {pt} rows flagged inactive still "
                            f"have {t.name} rows in the last month of data (after {(last - pd.DateOffset(months=1)).date()})",
                            t.row_labels(hit))


def check_calendar_coverage(tables: dict, calendars: list, facts: list):
    for cal in calendars:
        cc = next(c for c in cal.date_cols() if not c.dt.dropna().duplicated().any())
        cal_dates = set(cc.dt.dropna().dt.normalize())
        lo, hi = min(cal_dates), max(cal_dates)
        for t in tables.values():
            if t is cal:
                continue
            for c in t.date_cols():
                d = c.dt.dt.normalize()
                miss = d.notna() & ~d.isin(cal_dates)
                if not miss.any():
                    continue
                sev = "ERROR" if t.name in facts else "INFO"
                add(sev, 4, "date_not_in_calendar", t.name, f"{c.name} vs {cal.name}.{cc.name}",
                    miss.sum(), t.n,
                    f"dates with no row in {cal.name} ({lo.date()} to {hi.date()})"
                    + (" -- the join to the calendar will drop them" if sev == "ERROR" else
                       " -- dimension attribute, not a join key, but note the calendar does not cover it"),
                    t.row_labels(miss))


def check_fact_vs_fact(tables: dict, facts: list):
    ft = [tables[f] for f in facts if tables[f].date_cols()]
    spans = {}
    for t in ft:
        d = t.date_cols()[0].dt
        spans[t.name] = (d.min(), d.max())
    if len(spans) > 1:
        los = [v[0] for v in spans.values()]
        his = [v[1] for v in spans.values()]
        if (max(los) - min(los)).days > 31 or (max(his) - min(his)).days > 31:
            add("WARN", 4, "fact_date_spans_differ", "(multiple)", "", None, None,
                "fact tables cover different periods: " +
                "; ".join(f"{k}: {a.date()} to {b.date()}" for k, (a, b) in spans.items()))
        else:
            add("INFO", 4, "fact_date_spans", "(multiple)", "", None, None,
                "fact tables cover the same period: " +
                "; ".join(f"{k}: {a.date()} to {b.date()}" for k, (a, b) in spans.items()))
    for t1, t2 in itertools.permutations(ft, 2):
        shared = {k: v for k, v in t1.fks.items() if k in t2.fks and t2.fks[k] == v}
        if not shared:
            continue
        # join on the most granular shared FK + calendar month
        ent = max(shared, key=lambda k: t1.cols[k].nunique)
        d1, d2 = t1.date_cols()[0], t2.date_cols()[0]
        k1 = t1.cols[ent].stripped + "|" + d1.dt.dt.strftime("%Y-%m")
        k2 = t2.cols[ent].stripped + "|" + d2.dt.dt.strftime("%Y-%m")
        uniq2 = ~k2.duplicated(keep=False)
        if uniq2.mean() < 0.95:
            continue                           # t2 is not a lookup at entity-month grain
        idx2 = pd.Series(k2[uniq2].index, index=k2[uniq2].values)
        match = k1.map(idx2)
        has = match.notna() & d1.dt.notna() & t1.cols[ent].present
        rate = has.mean()
        if 0.9 <= rate < 1:
            nomatch = ~has & d1.dt.notna() & t1.cols[ent].present
            add("WARN", 4, "fact_missing_counterpart", t1.name, f"{ent} + month vs {t2.name}",
                nomatch.sum(), t1.n,
                f"{rate:.2%} of {t1.name} rows have a {t2.name} row for the same {ent} and month; "
                f"{int(nomatch.sum())} do not", t1.row_labels(nomatch))
        if rate < 0.5:
            continue
        pos2 = match[has].astype(int)
        for c1 in t1.cols.values():
            if c1.name in (ent, t1.pk, d1.name) or c1.lname in DATE_PART_NAMES or c1.itype == "empty":
                continue
            for c2 in t2.cols.values():
                if c2.name in (ent, t2.pk, d2.name) or c2.lname in DATE_PART_NAMES:
                    continue
                if not related_names(c1.name, c2.name) or not compatible(c1, c2):
                    continue
                if t1.name > t2.name and c1.name == c2.name:
                    continue                    # same-name pair: report once
                other = pd.Series("", index=t1.df.index)
                other[has] = c2.stripped.loc[pos2.values].values
                opres = pd.Series(False, index=t1.df.index)
                opres[has] = c2.present.loc[pos2.values].values
                both = has & c1.present & opres
                if both.sum() < 20:
                    continue
                agree = values_agree(c1, other, c2, both) & both
                r = agree.sum() / both.sum()
                dis = both & ~agree
                if r >= 0.5 and dis.any():
                    add("WARN", 4, "facts_disagree", t1.name,
                        f"{t1.name}.{c1.name} vs {t2.name}.{c2.name} (same {ent} + month)",
                        dis.sum(), t1.n,
                        f"values disagree in {int(dis.sum())} matched rows (agree in {r:.2%})",
                        t1.row_labels(dis))
                elif r == 1:
                    add("INFO", 4, "facts_agree", t1.name,
                        f"{t1.name}.{c1.name} vs {t2.name}.{c2.name} (same {ent} + month)", 0, t1.n,
                        "values agree in every matched row")


def check_domains_across_tables(tables: dict):
    cats = [(t, c) for t in tables.values() for c in t.cols.values() if c.itype == "categorical"]
    done = set()
    for (t1, c1), (t2, c2) in itertools.combinations(cats, 2):
        if t1 is t2 or not related_names(c1.name, c2.name):
            continue
        key = (t1.name, c1.name, t2.name, c2.name)
        if key in done:
            continue
        done.add(key)
        s1 = set(c1.stripped[c1.present])
        s2 = set(c2.stripped[c2.present])
        if s1 == s2:
            continue
        only1, only2 = s1 - s2, s2 - s1
        n1 = {norm_text(x) for x in s1}
        n2 = {norm_text(x) for x in s2}
        sev = "WARN" if (n1 == n2 or (only1 and only2 and len(s1 & s2) > 0)) else "INFO"
        add(sev, 4, "domain_mismatch_across_tables", f"{t1.name} / {t2.name}",
            f"{c1.name} / {c2.name}", None, None,
            f"related columns use different value sets. only in {t1.name}: {sorted(only1)[:8]}; "
            f"only in {t2.name}: {sorted(only2)[:8]}")
    styles = {}
    for t in tables.values():
        for c in t.cols.values():
            if c.itype == "boolean":
                styles.setdefault("/".join(sorted(c.fmt_counts)), []).append(f"{t.name}.{c.name}")
    if len(styles) > 1:
        add("INFO", 4, "inconsistent_boolean_encoding", "(multiple)", "", None, None,
            "boolean columns are encoded differently across tables: " +
            "; ".join(f"[{k}] {', '.join(v[:4])}{'...' if len(v) > 4 else ''}" for k, v in styles.items()))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def parse_args():
    here = Path(__file__).resolve().parent
    root = here.parent
    ap = argparse.ArgumentParser(description="Generic profiler + data-quality checker")
    ap.add_argument("--input", default=str(root / "Data" / "Raw"),
                    help="folder of .csv/.tsv/.txt files (default: ../Data/Raw)")
    ap.add_argument("--output", default=str(root / "Reports"),
                    help="folder for the reports (default: ../Reports)")
    ap.add_argument("--label", default=None,
                    help="suffix for report names (default: input folder name, e.g. 'raw')")
    ap.add_argument("--pk", action="append", default=[], metavar="TABLE=COLUMN",
                    help="override primary key inference (repeatable)")
    ap.add_argument("--fk", action="append", default=[], metavar="TABLE.COL=PARENT.COL",
                    help="declare a foreign key (repeatable)")
    return ap.parse_args()


def main():
    args = parse_args()
    in_dir, out_dir = Path(args.input), Path(args.output)
    if not in_dir.is_absolute():
        in_dir = Path.cwd() / in_dir
    label = (args.label or in_dir.name).lower().replace(" ", "_")
    files = sorted(p for p in in_dir.iterdir()
                   if p.suffix.lower() in (".csv", ".tsv", ".txt", ".tab") and not p.name.startswith("."))
    if not files:
        sys.exit(f"No delimited files found in {in_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    pk_override = dict(x.split("=", 1) for x in args.pk)
    fk_override = []
    for x in args.fk:
        left, right = x.split("=", 1)
        fk_override.append((tuple(left.split(".", 1)), tuple(right.split(".", 1))))

    tables = {}
    for p in files:
        print(f"reading {p.name} ...", flush=True)
        t = read_table(p)
        for i, cn in enumerate(t.df.columns, start=1):
            t.cols[cn] = analyze_column(t, cn, i)
        tables[t.name] = t
    for t in tables.values():
        infer_pk(t, pk_override)
    infer_fks(tables, fk_override)

    referenced = {pt for t in tables.values() for (pt, _) in t.fks.values()}
    facts = [t.name for t in tables.values() if t.fks and t.name not in referenced]

    profile = []
    calendars = []
    for t in tables.values():
        print(f"checking {t.name} ...", flush=True)
        profile += profile_rows(t)
        # group 1
        check_file_structure(t)
        check_keys(t)
        for c in t.cols.values():
            check_column(t, c)
        # group 2
        check_duplicates(t, is_fact=t.name in facts)
        check_date_parts(t)
        discover_formulas(t)
        check_conditional_blanks(t)
        check_entity_consistency(t)
        check_row_date_order(t)
        check_source_flags(t)
        if check_calendar(t):
            calendars.append(t)
    # group 3
    check_referential(tables)
    # group 4
    print("cross-table checks ...", flush=True)
    check_fact_vs_dim(tables)
    check_calendar_coverage(tables, calendars, facts)
    check_fact_vs_fact(tables, facts)
    check_domains_across_tables(tables)

    prof_df = pd.DataFrame(profile)
    dq = pd.DataFrame(FINDINGS)
    if len(dq):
        dq["_s"] = dq.severity.map(SEV_ORDER)
        dq = dq.sort_values(["_s", "check_group", "table", "column"], kind="stable").drop(columns="_s")
        dq.insert(0, "finding_id", range(1, len(dq) + 1))
    prof_path = out_dir / f"profile_{label}.csv"
    dq_path = out_dir / f"dq_summary_{label}.csv"
    prof_df.to_csv(prof_path, index=False)
    dq.to_csv(dq_path, index=False)

    print("\nTables:")
    for t in tables.values():
        fk = ", ".join(f"{k}->{v[0]}" for k, v in t.fks.items()) or "-"
        print(f"  {t.name:28s} rows={t.n:>8,}  pk={t.pk or '-':14s} fks: {fk}")
    print(f"\nfact tables: {facts}   calendar tables: {[c.name for c in calendars]}")
    if len(dq):
        print("\nFindings by severity / group:")
        print(dq.groupby(["severity", "check_group"]).size().to_string())
    print(f"\nwrote {prof_path}  ({len(prof_df)} rows)")
    print(f"wrote {dq_path}  ({len(dq)} rows)")


if __name__ == "__main__":
    main()
