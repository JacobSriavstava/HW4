# Decisions Log #

## 1: fact_portfolion_snapshot and fact_fees
Problem: They are stuck at 1000. 
Evidence: The value 1000 appears 420 times in fact_portfolio_snapshot and 386 times in fact_fees. No other number appears even close to this amount of times. 92 clients are affected. 
Dimention Violated: Accuracy
Decision: Flag, Don't delete the rows or change the values
Verified against: 274 of the 420 rows have is_return_suspect = 1, 146 do not. The fees billed in these rows are between $0.65 - $0.80. 
Justifiication: $1,000 looks like a minimum value the system substitutes and it is not representing a real balance. 
Risk: IF these are real balances, flagging them will wrongly drop them from the analysis. If they are fake, using them will understate the fee revenue and impact the data. 

## 2: Fact_portfolio_snapshot.return_ytd has extreme values ##
Evidence: 498 rows are below -90% and 373 rows are above +300%
Dimension: Flag. Using the existing is_return_suspect column rether than adding a new one
Verify against: all 871 extreme rows alreadt a=have is_return_suspect = 1. The low returns line up with the $1,000 floow months.
Justification: These values come from bad inputs, not real market performance
RiskL Including them distrorts advisor and benchmark comparisons, Excluding real crashes would make performance look better than it was

## 3: Fact_transactions rows with is_post_departure_txn =1 ##
Evidence: 2,410 rows from 131 clients. All 131 clients have is_active = false. Every one of those transactions is dated after the clients last snapshot. This falls in late 2022-2023. 
Dimension violated: Consistency
Decision: Quarantine. Move the rows to a separate file isntead of deleting them. 
Verified againstL the clients advisor is still active, with normal trnasactions. So departure means the client left, not the advisor. 
Justification: These are transactions on closed accounts
RiskL If the accounts were really still winding down, quarantinng loses real cash flows. 

## 4: fact_portfolio_snapshot months with no matching fee ##
Evidence: 7,350 snapshot client months have no rows in fact_fees. Fees are exactly one per client per month, so this is not caused by duplicated
Violation: Completeness
Decision: Flag with has_fee_record. Don't create fee row
Verified against: only 0.5% of these months are the $1,000 floow months, so the floor does not explain the gap
Justification: Missed billing or fees were waived. Can't tell from the data
Risk: Inventing fees would overstate revenue. Ignoring the gap hides possibile billing leakage

## 5: dim_advisor.certifications = "None" ##
Evidence: 6 advisors. Normal pd.read_csv would turn this into NaN. 
Dimension violated: Validity, Completeness
Decision: Standardize to "No Certification"
verified against: These advisors have 5-23 years of experience so this looks like a real "has no certification" value, not missing data
Justification: As NaN, these advisors would look like they have unknown credentials
Risk: If some of them really were left blank, the label overstates what they know

## 6: dim_client.risk_portfolio = "Unknown"
Evidence: 75 clients. All of them are mapped to benchmark 3, the same benchmark balanced clients get
Domension violated: Completeness
Decision: Leave as is and flag it. Dont fill it with "Balanced"
Justification: It looks like the system defaulted these clients to the Balanced benchmark. Inferring "Balanced" from the benchmark would be circular. 
Risk: A suitable analysis could wrogly treat these clients as having been assessed

## 7: Mixed boolean encodings ##
Evidence: True/False in is_active and is_etf, but 0/1 in the dim_date and fact table flags
Dimension violated: Consistency
Decision: convert all of them to real booleans
Risk: Low. Check that the counts are the same after conversion

## 8: dim_client.zip_code ##
Evidence: 228 zip codes have leading zeros such as 03897
Dimension Violated: Validity, if they were converted wrong
Decision: Convert to text, never to a number
Risk: A numeric type silently turns 03897 into 3897

## 9: dim_security.expense_ratio_bps ##
Evidence: Values are stored as 17.0 
Dimension Validity (minor)
Decision: Convert to numeric

## 10: All date columns ##
Evidence: The dates are stored as text, all in YYYY-MM-DD with no parse failures
Dimension Vilated: None
Decision: convert to datetime

## 11: Snapshots and fees dated before client_since ##
Evidence: 930 snapshot rows and 858 fee rows are dated before the clients start date
Decision: Leave as is 
Verify against: 100% of them fall in the same month the client joined. Snapshots and fees are always dated on the 1st of the month, so a client who joined on the 19th gets a row dated the 1st. The transaction dated before client_since: 0.

## 12: Blank security_id, shares, and price in fact_transactions ## 
Evidence: 101,528 rows
Decision: Leave as is. Don't fill
Verify against: every blank is a deposit, withdrawl, or advisory fee row, and none are trades. Cash movements have no security. 

## 13: Blank sector and expense_ratio_bps in dim_security ##
Evidence: sector is blank for 219 securities and expense_ratio_bps for 281
Decision: Leave as is
Verified against: Sector is blank for exactly the 219 ETFs, and expense_ratio_bps is blank for exactly the 281 individual stocks. 

## 14: 712 transactions share the same client, security, and date ##
Deciision: Leave it as is
Verify against: There are o exact duplicates. Most of these are different transaction types on the same day, such as a deposit and an advisory fee

## 15: Client_tier does not always match the latest AUM ##
Decision: leave as is
Verify against:  tier lines up exactly with initial_aum. It was assigned at onboarding and is not updated

## 16: client_since dates earlier than dim_date ##
Evidence: 1,532 clients joined before 2020, and the calendar starts in 2020
Decision: Leave as is
Justification: client_since is a client attribute, not a join key in the calendar

## 17: Asset_class values differ between dim_benchmark and dim_security##
Decision: Leave as is. Don't standardize them
Justification: They describe different things: Index categories vs. security types. Don't join on this column

## 18: dim_date.is_trading_day is 1 for eevry weekday ##
Decision: Leave as is, and note it
Justificaiton: Market holidays are not excluded. That matters only if you count trading days

## 19: Calculated columns ##
Evidence: amount = shares X price, amount = aum_basics x bps / 120000 and return = banchmark + alpha hold for 100% of rows
Decision: leave as is
Justification: this shows the calculations are correct, so they don't need recomputing

#### Notes ####
I used some AI to help me identify these 19 different decisions. I first went through on my own and looked at the .csv reports and came up with my list. Then I used AI to review my list and help me add anything I was missing. I verified all the data from the AI recomended additional points. 
