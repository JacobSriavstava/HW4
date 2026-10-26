# HW4 : Cleaning the Wildcat Capital Data #
## MIS3060 Business Intelligence with AI | Jacob Srivastava ##

## Overview ##
The following code audits, cleans, and validates 8 Wildcat Capital tables. The raw files are not modified. Clean copies and audit notes are saved separately. The validation reports are saved

## Folder Structure ##
hw4/

├── README.md

├── decision_log.md            # issues found and decisions made before cleaning

├── Python Scripts/

       ├── 01_profile.py          # Part A: audit every table

      ├── 02_clean.py            # Part C: clean all 8 tables

      └── 03_validate.py         # Part D: referential integrity, rules, scorecard

├── data/

     ├── Raw/                   # original 8 CSVs (unchanged)

      ├── Clean/                 # cleaned 8 CSVs + README_clean.md
       └── Audit/                 # cleaning_log, value_changes, quarantine files

└── Reports/

    ├── profile_raw.csv

    ├── dq_summary_raw.csv

    ├── data_quality_scorecard.csv

    └── cleaning_reports.md

## How to Run ##
From the folder:
1. Activate the virtual environment: `source .venv/bin/activate` (Mac) or `.venv\Scripts\activate` (Windows)
2. Install requirments.txt. For the python code pandas is needed. You can also just install pandas by typing 'pip install pandas' into the terminal
3. From the hw4 folder, run it order:
    - `python scripts/01_profile.py`
   - `python scripts/02_clean.py`
   - `python scripts/03_validate.py`


## Tools Used ##
The python scripts and the reports were drafted with Claude (Claude Cowork in Claude Desktop), they were ran in VS Code, and I reviwed the reports and data before each commit. 

## Claude Code review findings

| Script | What Claude Code flagged | What I did about it |
|---|---|---|
| 01_profile.py | It warned about "duplicates" in the client table that weren't really duplicates. It treated short lists of unique names as categories. It missed one math check (quantity × price) in a test file. One error message described the problem wrong. | Ran the duplicate check only on fact tables. Changed when a column counts as a category. Fixed the math check and retested: it caught all 8 test errors. Fixed the error message. |
| 02_clean.py | The duplicate count (3,695) didn't match my decision log (712), because blank cells were matching each other. The printed numbers were hard to read. I needed proof that the script stops when a fix fails. | Counted only trades, which gives 712, and fixed the note in my decision log. Cleaned up the printout. Broke one value on purpose in a test copy: the script stopped and saved nothing. |
| 03_validate.py | The comparison tables were too wide to read. Fees billed didn't match the fee transactions (0% match). | Shortened the column names. Kept the 0% result as a real finding instead of hiding it, and added a note to use fact_fees for revenue.|

These are just some, see decision_log.md for other changes and fixes I identified that needed to be made. See README_clean.md for information. see cleaning_reports.md for information. See the files in the audit folder for information as well. 


---
## Description of each part ##
### Part A ###
How to run: Run the python code, and then the reports are made in the two .csv files that get placed within the reports folder. 

Claude Cowork Prompt: Write a python code and place it within the scripts folder. Title it 01_profile.py. Make this a generic code that will work on any table and not just the tables that are within the raw data file. Report for every column the null/blank count, the distinct count, dominant value shapes, share of values that fail to parse as the expected type, whitespace problems, mixed formats, and numeric min and max. After this, then do the following checks in four groups. 1: Check for the column level: Type, allowed values, the ranges, any key uniqueness 2: Single-table rule: check for duplicates, near duplicates, values that contraditct other columns of the same row 3: Referential integrity: every foreign key must point to an existing primary key. Check for this. 4: Cross-table business rule: check for facts that disagree with dimensions, or with each other. Tag each of these findings as ERROR (Meaning wrong, or will break a join or calculation), WARN (meaning probably wrong; investigate), or INFO (Worth knowing) Save the findings in a .csv file as profile_raw.csv and dq_summary_raw.csv. Save these .csv files both within the reports folder. profile_raw.csv should be formatted as one row per column per table. This is where it reports the facts like this column x amount of emtpy cells. dq_summary_raw.csv should be one row per problem found. This should have the ERROR/WARN/INFO tags. Other things to note: read every file as text (dtype=str, keep_default_na=False). Default pd.read_csv parsing can silently change a column: it drops leading zeros, turns 45 into 45.0, turns blank cells and the word None into NaN, and hides % and $ strings. Read as text first, then convert each column on purpose.

Prompt Explained: This prompt works on any table and it is not hard coded to the specific ones being used for this assignment. It reports all of the requirments and reports them in the two required files. It took Claude a while to produce the script

### Part B ###
What I did: went through the two different reports generated from the python script in Part A. Identified where there were errors and put together a decision log of what needed to be fixed. Used AI to help me with this step to confirm the connection between different columns and tables as well as numbers. Whenever I used AI I made sure to verify the numbers with the data on the .csv file to ensure accuracy. 

### Part C ###
What I did: Wrote a python script using Claude Cowork to make the changes I identified in Part B when I was making the decision log. 

Prompt: Write a python code titled 02_clean.py and place it within the scripts folder. This script will read the 8 raw files and write a new set of 8 files that are cleaned based on terms explained in the following. The raw files are never modified and remain in the raw folder. The new 8 files that are cleaned should have the same file names, and the same columns. You can add clearly named flag columns (0/1) where a value is suspicious but cannot be corrected without investigation. Do one transformation at a time. After each: print the row affected and a before/after check, and assert the after state. The script must stop if a fix did ot do what you claimed. Reconcile instead of guessing. use one column to recompute from others or two tables to describe the same fact if needed. Nothing should be deleted silently. Rows that are removed go to a quarantine file with enough columns to trace them. Prove rows = clean rows + quarantined rows for every table. Save the cleaning_log.csv int he audit file in the data file. This has table, key columns, issue, action, rows effected, notes. Also in the audit file, place the values_changed.csv file. This has table, ley column, old value, new value. Then have one data/audit/quarantine_*.csv per quarantined set. Save the clean tables in the clean folder within the data folder. Write a README_clean.md file that has the relationships between tables, column type settings a BI tool will need, the flag columns I added, and known limitations I chose not to fix, each with a reason. Here is the information from the decision log that should be used for this script: (Here I copied and pasted the decision log. I am not going to include it here as it would make this very long and it can be found in the decision_log.md)

What the prompt does: This cleanes the files based on the decision log and creates 8 new ones in the cleaned data folder. It does not change anything about the raw files. It also creates quarantine .csv files as well as a README_clean.md file that provides important information. It also creates a cleaning log so all the changes can be tracked. It was successful in creating all the files. 

### Part D ###

Prompt to Cowork: Write a python script titled 03_validate.py that is within the scripts folder. Run it on both data/raw and data/clean. Read the files as text. Print a table comparing raw vs clean for each relationship. Start with one reusable function: check_referential_integrity(fact_df, fact_key, dim_df, dim_key). It should return match rate, orphan rows, distinct orphan keys, and null-key rate. Find every relationship between the 8 tables. There should be 10 ID-based relationships and 3 that join to dim_date. List them and run the function on each one. 

Run rule based checks for every column with a natural business bound, add a rule for valid ranges, allow code lists, or signal rules (example: prices positive, dates within dim_date’s range, transaction types from a known list). Look at the actual data to choose the columns and allowed values. Keep all rules in one tabled list at the top of the script. 

Run cross table reconciliations. Add at least two checks where one number should match another. Example: transaction amount vs. shares x price, fees in fact_fees vs fee debits in fact_transactions, or snapshot market value vs. a recomputed value. Report how many rows match and how big the differences are. 

IQR outlier flags. Pick at least two numeric columns with no natural bound (like market value or transaction amount). Flag values below Q1-1.5xIQR or above Q3+1.5xIQR. Report how many were flagged. Add a printed note explains that flagged does not mean wrong: Large values can be real, like a wealthy client. 

Save the report in the reports folder as data_quality_scorecard.csv with one row per table per stage (raw/clean) and the columns: % completeness, %key uniqueness, % type validity, %rule pass, % referential match. Print a clear summary of each sections results for raw vs clear. 

How the prompt works: The prompt runs all the needed checks listed in the requirments. It runs them for both the raw and the clean and then puts them into tables so it can be easily compared. 

How successful was it: It was very succesful. It validated the data and all of the data was accurate across the raw and the clean. See data_quality_scorecard.csv for file with tests and statistics.

### Part E ###
AI helped create the report and then I went back and verified each of the numbers. When I had AI make the report, I also had it output every number it pulled from the documents and where it got them from. This made validating the numbers easy. I went back through and verified each number and they were all correct. 

Prompt I used: Write cleaning_reports.md and place it within the reports folder. Write a 1-2 page .md file for the Wildcat Capital portfolio committee (a non technical business audience). Use ONLY numbers from these files: decision_log.md, reports/data_quality_scorecard.csv, and data/audit/cleaning_log.csv. Do not ever invent, estimate, or round any figures. If a number is not in these files, leave it out. This is the business report. Include the following sections: An executive summary, cleaning summary table (Raw vs clean, from the scorecard), at least three key judgement calls with justification and risk, a limitations section, and the footer: AI-assisted report - reviewed for factual accuracy on 10/25/2026 by Jacob Srivastava. 

After writing it list every number you used and which file it came from so I can check it for accuracy. 