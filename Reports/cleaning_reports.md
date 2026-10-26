# Wildcat Capital: Client Data Cleaning Report

**Prepared for:** Portfolio Committee
**Subject:** Is the advisor, client, fee, portfolio and transaction data ready for analysis?

---

## Executive summary

The data is ready to use for analysis, with a small number of clearly marked exceptions.

- **Nothing was deleted.** One set of records was set aside: **2,410** transactions from **131** clients whose accounts had already closed. They were moved to a separate file and not erased. The transaction table went from **298,572** records to **296,162**, and the set-aside records account for the entire difference.
- **Every link between tables works.** Each fee, portfolio and transaction record points to a real client, advisor, security, benchmark and calendar date. The referential match is **100.0%** in both the original and the cleaned data.
- **Suspicious values were flagged, not changed.** Three new markers let the committee include or exclude questionable records:
  - **420** monthly portfolio balances and **386** fee records sit at a $1,000 placeholder instead of a real balance.
  - **7,350** client-months have a portfolio record but no fee was billed.
  - **75** clients were never given a risk profile.
- **Some unusual values were left alone on purpose,** because checking them against other data showed they are correct. For example, **930** portfolio records and **858** fee records are dated before the client's start date. All of them fall in the month the client joined, because monthly records are dated the 1st.

---

## Cleaning summary: original vs. cleaned data

Percentages are from the data quality scorecard. "Original" is the data as received; "Cleaned" is after cleaning.

| Data set | Records (original → cleaned) | Completeness | Unique IDs | Correct format | Business rules passed | Links to other tables |
|---|---|---|---|---|---|---|
| Advisors | 25 → 25 | 96.571% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 94.0% → 100.0% | n/a |
| Benchmarks | 8 → 8 | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | n/a |
| Clients | 2,500 → 2,500 | 99.786% → 99.8% | 100.0% → 100.0% | 100.0% → 100.0% | 99.667% → 99.667% | 100.0% → 100.0% |
| Calendar | 1,827 → 1,827 | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | n/a |
| Securities | 500 → 500 | 100.0% → 100.0% | 100.0% → 100.0% | 93.743% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% |
| Fees | 122,197 → 122,197 | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% |
| Portfolio snapshots | 129,547 → 129,547 | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 99.968% → 99.968% | 100.0% → 100.0% |
| Transactions | 298,572 → 296,162 | 100.0% → 100.0% | 100.0% → 100.0% | 100.0% → 100.0% | 99.848% → 100.0% | 100.0% → 100.0% |

**What the columns mean.**

- **Completeness:** the share of fields that hold a real value. Placeholder words such as "None" count as missing.
- **Unique IDs:** no record is duplicated.
- **Correct format:** numbers, dates and codes are stored the right way.
- **Business rules passed:** values fall within sensible limits, for example ages, fee rates and approved category lists.
- **Links to other tables:** every reference points to a record that exists. "n/a" means the table has no references to check.

**What improved.**

- **Advisors:** the six advisors listed as "None" are now labelled "No Certification".
- **Securities:** expense ratios stored as "17.0" are now stored as whole numbers.
- **Transactions:** the closed-account activity was set aside.

**What still falls short, deliberately.** The Clients and Portfolio snapshots rule scores are below 100% because those records are flagged, not altered. See the limitations below.

---

## Key judgement calls

### 1. $1,000 placeholder balances: flagged, not deleted or changed

- **Evidence:** the balance of exactly $1,000 appears **420** times in portfolio snapshots and **386** times in fees, across **92** clients. No other value appears anywhere near as often. Fees billed on these balances are only **$0.65–$0.80**.
- **Decision:** keep the records and mark them, so the committee can exclude them from AUM and revenue figures.
- **Why:** $1,000 looks like a minimum value the system fills in, not a client's real balance. Deleting the months would break each client's history.
- **Risk:** if some of these are real small balances, excluding them understates those clients. If they are placeholders and are included, AUM and fee revenue are understated.

### 2. Transactions on closed accounts: set aside in a separate file

- **Evidence:** **2,410** transactions from **131** clients are marked as occurring after the client left. All **131** clients are inactive, and every one of these transactions is dated after the client's last portfolio statement, in late 2022–2023. The advisors were still active.
- **Decision:** move these records to a separate quarantine file. Original **298,572** = cleaned **296,162** + set aside **2,410**.
- **Why:** activity on a closed account should not count toward current client activity or advisor performance.
- **Risk:** if some accounts were still winding down, real cash movements are left out of the analysis. They can be restored from the quarantine file.

### 3. Months with no fee billed: flagged, no fees invented

- **Evidence:** **7,350** client-months have a portfolio record but no fee record. The fee table holds exactly one fee per client per month, so duplicates don't explain the gap. Only **0.5%** of these months are $1,000 placeholder months.
- **Decision:** add a marker showing whether a fee exists for that month. No fee records were created.
- **Why:** the data cannot show whether these fees were missed or waived.
- **Risk:** creating fees would overstate revenue. Ignoring the gap could hide lost billing. This needs a business follow-up.

### 4. Clients with an "Unknown" risk profile: left as Unknown and flagged

- **Evidence:** **75** clients have "Unknown". All of them are tracked against benchmark 3, the same benchmark Balanced clients use.
- **Decision:** keep "Unknown" and mark these clients. Do not relabel them "Balanced".
- **Why:** the system appears to have defaulted them to the Balanced benchmark. Calling them Balanced because of that benchmark would be circular reasoning.
- **Risk:** a suitability review could wrongly treat these clients as having been assessed.

---

## Limitations: known issues we chose not to fix

- **Placeholder balances are not all marked by the source system.** Of the **420** placeholder months, **274** were already marked "return suspect" by the source system and **146** were not. Use the new placeholder marker together with the existing one.
- **Extreme returns remain in the data.** **871** monthly returns are below −90% or above +300%. All of them are marked "return suspect", and they should be excluded from advisor and benchmark comparisons.
- **The missing fee months are unexplained.** **7,350** months need confirmation from billing.
- **75 clients have no assessed risk profile.**
- **Client tier is set at onboarding and never updated.** It reflects a client's starting balance, not their current one.
- **The calendar starts in 2020.** **1,532** clients joined before 2020. This affects only their start-date lookups, not their activity records.
- **The calendar treats every weekday as a trading day,** including market holidays. This matters only when counting trading days.
- **The set-aside transactions may include real final cash movements** on accounts that were closing.

---

*AI-assisted report - reviewed for factual accuracy on 10/25/2026 by Jacob Srivastava.*
