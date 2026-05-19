# entropyFA federal-tax eval

Independent compute validation for `entropyfa compute federal-tax`.

The eval fetches embedded reference data live via `entropyfa data lookup`, recomputes federal tax from scratch in Python using that same data, and diffs the results against the CLI output. If the numbers diverge, the engine is misapplying its own data.

This separates two failure modes that are usually conflated: wrong data vs wrong computation. entropyFA's pipeline already addresses the first one. This covers the second.

## Setup

```bash
curl -fsSL https://get.entropyfa.com | sh
source ~/.zshrc
```

Python 3.10+ required, no extra packages.

## Usage

```bash
python eval_federal_tax.py                  # full report
python eval_federal_tax.py --verbose        # per-case output while running
python eval_federal_tax.py --filter se      # run cases matching substring
python eval_federal_tax.py --json           # machine-readable output
python eval_federal_tax.py --json | jq '.summary'
```

Exit code 0 = all blocking cases pass. Exit 1 = unexpected failure.

## What's covered

26 cases across:

- All bracket tiers for single filers (10% through 37%)
- All four filing statuses (single, MFJ, MFS, HOH)
- LTCG and qualified dividends at 0%, 15%, 20% rates with stacking
- NIIT triggers at the single ($200k) and MFJ ($250k) thresholds
- Self-employment tax, SE deduction, SS wage base sharing between wages and SE income
- Mixed income returns with five income types
- HSA and IRA above-the-line adjustments
- Itemized deductions and the 2026 SALT cap with phaseout
- IRA distributions and pension/SS income (no payroll tax)

## Findings from running this

**25/26 pass.** One known discrepancy is tracked rather than silently skipped:

The engine computes additional Medicare tax (0.9%) on gross SE income rather than net SE earnings. IRS Form 8959 uses Schedule SE Line 3, which is gross x 0.9235. At $250k SE income this overstates additional Medicare by about $172. The test case stays in the suite to make the delta visible.

Three undocumented behaviors were also found during calibration:

- `effective_rate` is computed as `total_income_tax / AGI`, not `/ gross_income`. This matters any time there are above-the-line adjustments (SE deduction, HSA, IRA contributions).
- `marginal_ordinary_rate` returns the first-bracket rate (10%) even when taxable income is zero. It reports the rate on the next dollar of income earned, not the next dollar of taxable income.
- `deductions.method` is required even when only SALT component fields are passed. If you include a `deductions` object without an explicit `method`, the CLI errors instead of defaulting to `"standard"`.

## Tolerances

| Field | Tolerance |
|---|---|
| Dollar amounts | $2.00 |
| Rate fields | 0.02% |

The dollar tolerance exists because the engine likely uses integer-cent arithmetic internally. Anything within $2 is a rounding artifact.

## Extending

Add a `Case` to the `CASES` list in `eval_federal_tax.py`:

```python
Case(
    name="mfj_ltcg_straddles_bracket",
    payload={"filing_status": "married_filing_jointly", "income": {"wages": 90_000, "long_term_capital_gains": 40_000}},
    focus="LTCG partially in 0% tier and partially in 15% tier for MFJ",
),
```

The runner handles the rest.
