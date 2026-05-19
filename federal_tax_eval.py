#!/usr/bin/env python3
"""
entropyFA federal-tax eval suite

Fetches embedded reference data live via `entropyfa data lookup`, recomputes
federal tax independently in Python, and diffs against `entropyfa compute federal-tax`.

Usage:
  python eval_federal_tax.py
  python eval_federal_tax.py --verbose
  python eval_federal_tax.py --json > report.json
  python eval_federal_tax.py --filter se

Requires entropyfa on PATH: https://get.entropyfa.com
Exit 0 = all blocking cases pass, 1 = any failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from math import floor
from typing import Optional


# ---------------------------------------------------------------------------
# CLI wrappers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def cli_lookup(category: str, key: str, year: int = 2026, filing_status: Optional[str] = None) -> object:
    cmd = ["entropyfa", "data", "lookup", "--category", category, "--key", key, "--year", str(year)]
    if filing_status:
        cmd += ["--filing-status", filing_status]
    resp = _run(cmd)
    if not resp.get("ok"):
        raise RuntimeError(f"lookup failed: {resp}")
    return resp["data"]["value"]


def cli_compute_federal_tax(payload: dict) -> dict:
    cmd = ["entropyfa", "compute", "federal-tax", "--json", json.dumps(payload)]
    resp = _run(cmd)
    if not resp.get("ok"):
        raise RuntimeError(f"compute failed: {resp}")
    return resp["data"]


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

@dataclass
class TaxParams:
    ordinary_brackets: list
    capital_gains_brackets: list
    standard_deduction: float
    capital_loss_limit: float
    niit_rate: float
    niit_threshold: float
    ss_rate: float
    ss_wage_base: float
    medicare_rate: float
    additional_medicare_rate: float
    additional_medicare_threshold: float
    se_ss_rate: float
    se_medicare_rate: float
    salt_cap: float
    salt_floor: float
    salt_phaseout_threshold: float
    salt_phaseout_rate: float


def _as_float(val: object, fallback: float = 0.0) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        for k in ("amount", "limit", "value"):
            if k in val:
                return float(val[k])
    return fallback


def load_params(filing_status: str, year: int = 2026) -> TaxParams:
    brackets    = cli_lookup("tax", "federal_income_tax_brackets",       year, filing_status)
    cg_brackets = cli_lookup("tax", "federal_capital_gains_brackets",    year, filing_status)
    std_ded     = cli_lookup("tax", "federal_standard_deductions",       year, filing_status)
    niit        = cli_lookup("tax", "federal_net_investment_income_tax", year, filing_status)
    payroll     = cli_lookup("tax", "federal_payroll_tax_parameters",    year, filing_status)
    cap_loss    = cli_lookup("tax", "federal_capital_loss_limit",        year, filing_status)
    salt        = cli_lookup("tax", "federal_salt_deduction_parameters", year, filing_status)

    return TaxParams(
        ordinary_brackets=brackets,
        capital_gains_brackets=cg_brackets,
        standard_deduction=_as_float(std_ded),
        capital_loss_limit=_as_float(cap_loss, 3000.0),
        niit_rate=niit["rate"],
        niit_threshold=niit["threshold"],
        ss_rate=payroll["social_security_rate"],
        ss_wage_base=payroll["social_security_wage_base"],
        medicare_rate=payroll["medicare_rate"],
        additional_medicare_rate=payroll["additional_medicare_rate"],
        additional_medicare_threshold=payroll["additional_medicare_threshold"],
        se_ss_rate=payroll["self_employment_tax_rate"],
        se_medicare_rate=payroll["self_employment_medicare_rate"],
        salt_cap=salt["cap_amount"],
        salt_floor=salt["floor_amount"],
        salt_phaseout_threshold=salt["phaseout_threshold"],
        salt_phaseout_rate=salt["phaseout_rate"],
    )


# ---------------------------------------------------------------------------
# Reference calculator
# ---------------------------------------------------------------------------

def _bracket_tax(income: float, brackets: list) -> float:
    tax = 0.0
    for b in sorted(brackets, key=lambda x: x["min"]):
        lo, hi = b["min"], b["max"]
        if income <= lo:
            break
        taxable = (min(income, hi) if hi is not None else income) - lo
        if taxable > 0:
            tax += taxable * b["rate"]
    return tax


def _marginal_rate(income: float, brackets: list) -> float:
    rate = 0.0
    for b in sorted(brackets, key=lambda x: x["min"]):
        if income > b["min"]:
            rate = b["rate"]
    return rate


def _salt_deduction(state_local_tax: float, real_property_tax: float,
                    personal_property_tax: float, agi: float, p: TaxParams) -> float:
    raw = state_local_tax + real_property_tax + personal_property_tax
    if agi > p.salt_phaseout_threshold:
        reduction = floor((agi - p.salt_phaseout_threshold) * p.salt_phaseout_rate)
        effective_cap = max(p.salt_cap - reduction, p.salt_floor)
    else:
        effective_cap = p.salt_cap
    return min(raw, effective_cap)


def compute_reference(
    *,
    filing_status: str,
    wages: float = 0.0,
    self_employment_income: float = 0.0,
    taxable_interest: float = 0.0,
    ordinary_dividends: float = 0.0,
    qualified_dividends: float = 0.0,
    short_term_capital_gains: float = 0.0,
    long_term_capital_gains: float = 0.0,
    taxable_ira_distributions: float = 0.0,
    taxable_pensions: float = 0.0,
    taxable_social_security: float = 0.0,
    other_income: float = 0.0,
    hsa_deduction: float = 0.0,
    ira_deduction: float = 0.0,
    student_loan_interest: float = 0.0,
    other_adjustments: float = 0.0,
    deduction_method: str = "standard",
    itemized_amount: Optional[float] = None,
    state_local_income_or_sales_tax: float = 0.0,
    real_property_tax: float = 0.0,
    personal_property_tax: float = 0.0,
    other_itemized_deductions: float = 0.0,
    params: Optional[TaxParams] = None,
    year: int = 2026,
) -> dict:
    p = params or load_params(filing_status, year)

    # SE tax - wages consume SS wage base first, SE gets the remainder
    se_net = self_employment_income * 0.9235
    wages_ss = min(wages, p.ss_wage_base)
    se_ss_base = max(0.0, min(se_net, p.ss_wage_base - wages_ss))
    se_ss_tax = se_ss_base * p.se_ss_rate
    se_medicare_tax = se_net * p.se_medicare_rate

    # Additional Medicare on SE net above threshold, after accounting for wages
    combined = wages + se_net
    se_addl_medicare = max(
        0.0,
        (combined - p.additional_medicare_threshold)
        - max(0.0, wages - p.additional_medicare_threshold),
    ) * p.additional_medicare_rate

    # Only regular SE tax (SS + 2.9% Medicare) is deductible per Schedule SE.
    # The 0.9% additional Medicare (Form 8959) is not.
    se_regular_tax = se_ss_tax + se_medicare_tax
    total_se_tax = se_regular_tax + se_addl_medicare
    se_deduction = se_regular_tax / 2.0

    # Use max(ordinary, qualified) to avoid double-counting dividends
    dividend_income = max(ordinary_dividends, qualified_dividends)
    gross_income = (
        wages + self_employment_income + taxable_interest + dividend_income
        + short_term_capital_gains + long_term_capital_gains
        + taxable_ira_distributions + taxable_pensions
        + taxable_social_security + other_income
    )

    agi = gross_income - (hsa_deduction + ira_deduction + student_loan_interest
                          + other_adjustments + se_deduction)

    if deduction_method == "itemized" and itemized_amount is not None:
        deduction = itemized_amount
    elif deduction_method == "itemized":
        salt = _salt_deduction(state_local_income_or_sales_tax, real_property_tax,
                               personal_property_tax, agi, p)
        deduction = salt + other_itemized_deductions
    else:
        deduction = p.standard_deduction

    taxable_income = max(0.0, agi - deduction)

    # LTCG + qualified dividends sit on top of ordinary income in the bracket stack
    preferential = min(max(0.0, qualified_dividends + long_term_capital_gains), taxable_income)
    ordinary_taxable = max(0.0, taxable_income - preferential)

    ordinary_tax = _bracket_tax(ordinary_taxable, p.ordinary_brackets)
    cg_tax = (
        _bracket_tax(ordinary_taxable + preferential, p.capital_gains_brackets)
        - _bracket_tax(ordinary_taxable, p.capital_gains_brackets)
    )

    net_investment_income = (qualified_dividends + long_term_capital_gains
                             + short_term_capital_gains + taxable_interest)
    niit = 0.0
    if agi > p.niit_threshold and net_investment_income > 0:
        niit = max(0.0, min(net_investment_income, agi - p.niit_threshold) * p.niit_rate)

    total_income_tax = ordinary_tax + cg_tax + niit

    w2_ss = wages_ss * p.ss_rate
    w2_med = wages * p.medicare_rate
    w2_addl = max(0.0, wages - p.additional_medicare_threshold) * p.additional_medicare_rate
    total_payroll = w2_ss + w2_med + w2_addl + total_se_tax
    total_tax = total_income_tax + total_payroll

    # CLI computes effective_rate as total_income_tax / AGI, not gross income.
    # Confirmed empirically - not in the schema docs.
    effective_rate = total_income_tax / agi if agi > 0 else 0.0

    # CLI returns the first-bracket rate (10%) even when taxable income is 0.
    # Epsilon makes _marginal_rate return the bracket the next dollar falls in.
    marginal_ordinary = _marginal_rate(max(ordinary_taxable, 1e-6), p.ordinary_brackets)

    return {
        "gross_income":           round(gross_income, 2),
        "agi":                    round(agi, 2),
        "taxable_income":         round(taxable_income, 2),
        "ordinary_income_tax":    round(ordinary_tax, 2),
        "capital_gains_tax":      round(cg_tax, 2),
        "niit":                   round(niit, 2),
        "total_income_tax":       round(total_income_tax, 2),
        "payroll_tax_total":      round(total_payroll, 2),
        "total_tax":              round(total_tax, 2),
        "effective_rate":         round(effective_rate, 4),
        "marginal_ordinary_rate": marginal_ordinary,
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@dataclass
class Case:
    name: str
    payload: dict
    focus: str
    known_discrepancy: bool = False


CASES: list[Case] = [
    # Bracket coverage
    Case(
        name="single_wages_below_standard_deduction",
        payload={"filing_status": "single", "income": {"wages": 10_000}},
        focus="Wages absorbed by standard deduction, zero income tax, payroll only",
    ),
    Case(
        name="single_wages_12pct_bracket",
        payload={"filing_status": "single", "income": {"wages": 35_000}},
        focus="12% marginal bracket with standard deduction",
    ),
    Case(
        name="single_wages_24pct_bracket",
        payload={"filing_status": "single", "income": {"wages": 150_000}},
        focus="24% marginal - matches the docs quick-start example",
    ),
    Case(
        name="single_wages_32pct_bracket",
        payload={"filing_status": "single", "income": {"wages": 220_000}},
        focus="32% marginal, additional Medicare kicks in at $200k",
    ),
    Case(
        name="single_wages_35pct_bracket",
        payload={"filing_status": "single", "income": {"wages": 400_000}},
        focus="35% marginal bracket",
    ),
    Case(
        name="single_wages_37pct_bracket",
        payload={"filing_status": "single", "income": {"wages": 700_000}},
        focus="37% top bracket",
    ),

    # Filing status variants
    Case(
        name="mfj_wages_mid_income",
        payload={"filing_status": "married_filing_jointly", "income": {"wages": 120_000}},
        focus="MFJ wider brackets and larger standard deduction vs single",
    ),
    Case(
        name="mfj_wages_high_income",
        payload={"filing_status": "married_filing_jointly", "income": {"wages": 600_000}},
        focus="MFJ high income, additional Medicare threshold is $250k",
    ),
    Case(
        name="mfs_wages",
        payload={"filing_status": "married_filing_separately", "income": {"wages": 100_000}},
        focus="MFS narrower brackets than MFJ",
    ),
    Case(
        name="hoh_wages",
        payload={"filing_status": "head_of_household", "income": {"wages": 75_000}},
        focus="HOH bracket table is distinct from single and MFJ",
    ),

    # Capital gains and qualified dividends
    Case(
        name="single_ltcg_zero_rate",
        payload={"filing_status": "single", "income": {"wages": 30_000, "long_term_capital_gains": 15_000, "qualified_dividends": 2_000}},
        focus="LTCG stacked on low ordinary income, 0% CG rate applies",
    ),
    Case(
        name="single_ltcg_15pct_rate",
        payload={"filing_status": "single", "income": {"wages": 100_000, "long_term_capital_gains": 50_000, "qualified_dividends": 5_000}},
        focus="15% CG rate via stacking rule",
    ),
    Case(
        name="single_ltcg_20pct_rate",
        payload={"filing_status": "single", "income": {"wages": 500_000, "long_term_capital_gains": 100_000}},
        focus="20% CG rate plus NIIT on high-income filer",
    ),
    Case(
        name="single_stcg_ordinary_treatment",
        payload={"filing_status": "single", "income": {"wages": 80_000, "short_term_capital_gains": 20_000}},
        focus="STCG taxed as ordinary income",
    ),

    # NIIT
    Case(
        name="single_niit_just_above_threshold",
        payload={"filing_status": "single", "income": {"wages": 198_000, "qualified_dividends": 10_000}},
        focus="AGI just over $200k single NIIT threshold",
    ),
    Case(
        name="mfj_niit_just_above_threshold",
        payload={"filing_status": "married_filing_jointly", "income": {"wages": 248_000, "qualified_dividends": 10_000, "long_term_capital_gains": 5_000}},
        focus="AGI just over $250k MFJ NIIT threshold",
    ),

    # Self-employment
    Case(
        name="single_se_only_mid_income",
        payload={"filing_status": "single", "income": {"self_employment_income": 100_000}},
        focus="SE tax, SE deduction reducing AGI",
    ),
    # The engine computes additional Medicare on gross SE income rather than net
    # earnings (gross x 0.9235). IRS Form 8959 uses Schedule SE Line 3 which is
    # net earnings. At $250k SE income this overstates additional Medicare by ~$172.
    Case(
        name="single_se_above_ss_wage_base__known_engine_discrepancy",
        payload={"filing_status": "single", "income": {"self_employment_income": 250_000}},
        focus="[KNOWN BUG] Engine uses gross SE income for additional Medicare threshold. IRS Form 8959 requires net (x 0.9235). Delta ~$172 at $250k.",
        known_discrepancy=True,
    ),
    Case(
        name="single_wages_and_se_shared_ss_base",
        payload={"filing_status": "single", "income": {"wages": 120_000, "self_employment_income": 80_000}},
        focus="Wages consume SS wage base first, SE SS capped at remainder",
    ),

    # Mixed income
    Case(
        name="single_mixed_five_income_types",
        payload={"filing_status": "single", "income": {
            "wages": 80_000,
            "self_employment_income": 30_000,
            "long_term_capital_gains": 15_000,
            "qualified_dividends": 3_000,
            "taxable_ira_distributions": 20_000,
        }},
        focus="Five income types together - stacking, SE deduction, IRA as ordinary",
    ),

    # Adjustments
    Case(
        name="single_hsa_ira_adjustments",
        payload={
            "filing_status": "single",
            "income": {"wages": 90_000},
            "adjustments": {"hsa_deduction": 4_300, "ira_deduction": 7_000},
        },
        focus="HSA and IRA deductions reduce AGI",
    ),

    # Itemized deductions and SALT
    Case(
        name="single_itemized_precomputed",
        payload={
            "filing_status": "single",
            "income": {"wages": 200_000},
            "deductions": {"method": "itemized", "itemized_amount": 45_000},
        },
        focus="Pre-computed itemized deduction larger than standard",
    ),
    Case(
        name="single_salt_below_cap",
        payload={
            "filing_status": "single",
            "income": {"wages": 150_000},
            "deductions": {
                "method": "standard",
                "state_local_income_or_sales_tax": 12_000,
                "real_property_tax": 8_000,
            },
        },
        focus="SALT components below 2026 cap",
    ),
    Case(
        name="single_salt_above_phaseout_threshold",
        payload={
            "filing_status": "single",
            "income": {"wages": 600_000},
            "deductions": {
                "method": "standard",
                "state_local_income_or_sales_tax": 30_000,
                "real_property_tax": 20_000,
            },
        },
        focus="AGI over $505k single threshold triggers SALT phaseout",
    ),

    # IRA and pension distributions
    Case(
        name="single_ira_distribution_only",
        payload={"filing_status": "single", "income": {"taxable_ira_distributions": 80_000}},
        focus="IRA distributions are ordinary income with no payroll tax",
    ),
    Case(
        name="mfj_pension_plus_ss",
        payload={"filing_status": "married_filing_jointly", "income": {
            "taxable_pensions": 60_000,
            "taxable_social_security": 18_000,
        }},
        focus="Pension and SS income have no payroll tax",
    ),
]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

DOLLAR_TOL = 2.00
RATE_TOL = 0.0002

COMPARE_FIELDS = [
    ("gross_income",           "gross_income",           DOLLAR_TOL, "$"),
    ("agi",                    "agi",                    DOLLAR_TOL, "$"),
    ("taxable_income",         "taxable_income",         DOLLAR_TOL, "$"),
    ("total_income_tax",       "total_income_tax",       DOLLAR_TOL, "$"),
    ("total_tax",              "total_tax",              DOLLAR_TOL, "$"),
    ("effective_rate",         "effective_rate",         RATE_TOL,   "%"),
    ("marginal_ordinary_rate", "marginal_ordinary_rate", RATE_TOL,   "%"),
]


@dataclass
class Diff:
    field: str
    cli_val: float
    ref_val: float
    delta: float
    unit: str


@dataclass
class EvalResult:
    case: Case
    passed: bool
    blocking: bool
    cli: dict
    ref: dict
    diffs: list[Diff]
    error: Optional[str] = None


def _extract(data: dict, key: str) -> Optional[float]:
    val = data.get(key)
    if isinstance(val, dict):
        val = val.get("total")
    return float(val) if val is not None else None


def compare(cli: dict, ref: dict) -> list[Diff]:
    diffs = []
    for ref_key, cli_key, tol, unit in COMPARE_FIELDS:
        r = ref.get(ref_key)
        c = _extract(cli, cli_key)
        if r is None or c is None:
            continue
        delta = abs(float(r) - float(c))
        if delta > tol:
            diffs.append(Diff(cli_key, c, float(r), delta, unit))
    return diffs


def _input_to_kwargs(payload: dict) -> dict:
    income      = payload.get("income", {})
    adjustments = payload.get("adjustments", {})
    deductions  = payload.get("deductions", {})
    return dict(
        filing_status                   = payload["filing_status"],
        wages                           = income.get("wages", 0.0),
        self_employment_income          = income.get("self_employment_income", 0.0),
        taxable_interest                = income.get("taxable_interest", 0.0),
        ordinary_dividends              = income.get("ordinary_dividends", 0.0),
        qualified_dividends             = income.get("qualified_dividends", 0.0),
        short_term_capital_gains        = income.get("short_term_capital_gains", 0.0),
        long_term_capital_gains         = income.get("long_term_capital_gains", 0.0),
        taxable_ira_distributions       = income.get("taxable_ira_distributions", 0.0),
        taxable_pensions                = income.get("taxable_pensions", 0.0),
        taxable_social_security         = income.get("taxable_social_security", 0.0),
        other_income                    = income.get("other_income", 0.0),
        hsa_deduction                   = adjustments.get("hsa_deduction", 0.0),
        ira_deduction                   = adjustments.get("ira_deduction", 0.0),
        student_loan_interest           = adjustments.get("student_loan_interest", 0.0),
        other_adjustments               = adjustments.get("other_adjustments", 0.0),
        deduction_method                = deductions.get("method", "standard"),
        itemized_amount                 = deductions.get("itemized_amount"),
        state_local_income_or_sales_tax = deductions.get("state_local_income_or_sales_tax", 0.0),
        real_property_tax               = deductions.get("real_property_tax", 0.0),
        personal_property_tax           = deductions.get("personal_property_tax", 0.0),
        other_itemized_deductions       = deductions.get("other_itemized_deductions", 0.0),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_eval(cases: list[Case], verbose: bool = False) -> list[EvalResult]:
    results = []
    params_cache: dict[str, TaxParams] = {}

    for case in cases:
        fs = case.payload["filing_status"]

        if fs not in params_cache:
            try:
                params_cache[fs] = load_params(fs)
            except Exception as exc:
                results.append(EvalResult(case, False, True, {}, {}, [], error=f"params load failed: {exc}"))
                if verbose:
                    print(f"  ERROR  {case.name}: {exc}", file=sys.stderr)
                continue

        try:
            cli_out = cli_compute_federal_tax(case.payload)
        except Exception as exc:
            results.append(EvalResult(case, False, True, {}, {}, [], error=f"CLI error: {exc}"))
            if verbose:
                print(f"  ERROR  {case.name}: {exc}", file=sys.stderr)
            continue

        try:
            kwargs = _input_to_kwargs(case.payload)
            kwargs["params"] = params_cache[fs]
            ref_out = compute_reference(**kwargs)
        except Exception as exc:
            results.append(EvalResult(case, False, True, cli_out, {}, [], error=f"reference error: {exc}"))
            if verbose:
                print(f"  ERROR  {case.name}: {exc}", file=sys.stderr)
            continue

        diffs = compare(cli_out, ref_out)
        passed = len(diffs) == 0
        blocking = not passed and not case.known_discrepancy
        results.append(EvalResult(case, passed, blocking, cli_out, ref_out, diffs))

        if verbose:
            icon = "v" if passed else "x"
            print(f"  {icon}  {case.name}", file=sys.stderr)
            for d in diffs:
                print(f"     {d.field}: CLI={d.cli_val}, ref={d.ref_val}, delta={d.delta:.4f} {d.unit}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _bar(passed: int, total: int, width: int = 40) -> str:
    filled = round(width * passed / total) if total else 0
    return "#" * filled + "." * (width - filled)


def print_report(results: list[EvalResult]) -> None:
    total      = len(results)
    passed     = sum(1 for r in results if r.passed)
    discrepant = sum(1 for r in results if not r.passed and r.case.known_discrepancy)
    errors     = sum(1 for r in results if r.error)
    failed     = total - passed - discrepant

    W = 74
    print("\n" + "-" * W)
    print("  entropyFA Federal Tax Eval Suite")
    print("-" * W)
    print(f"\n  [{_bar(passed, total)}]  {passed}/{total} passed  ({discrepant} known {'discrepancy' if discrepant == 1 else 'discrepancies'} tracked)\n")
    if errors:
        print(f"  !  {errors} case(s) could not run (see below)\n")

    for r in results:
        if r.error:
            print(f"  x ERROR   {r.case.name}")
            print(f"            {r.error}")
        elif r.passed:
            cli = r.cli
            print(f"  v PASS    {r.case.name}")
            print(f"            total_tax={cli.get('total_tax')}  agi={cli.get('agi')}  marginal={cli.get('marginal_ordinary_rate')}")
        elif r.case.known_discrepancy:
            print(f"  ~ KNOWN   {r.case.name}")
            print(f"            {r.case.focus}")
            for d in r.diffs:
                print(f"            -> {d.field}: CLI={d.cli_val}  ref={d.ref_val}  delta={d.delta:.4f}")
        else:
            print(f"  x FAIL    {r.case.name}")
            print(f"            {r.case.focus}")
            for d in r.diffs:
                print(f"            -> {d.field}: CLI={d.cli_val}  ref={d.ref_val}  delta={d.delta:.4f}")
        print()

    print("-" * W)
    pct = 100 * passed / total if total else 0
    print(f"  Result: {passed}/{total} passed ({pct:.0f}%)")
    if discrepant:
        print(f"  Known engine discrepancies: {discrepant} (tracked, not blocking)")
    if failed or errors:
        print(f"  Unexpected failures: {failed}  |  Errors: {errors}")
    print("-" * W + "\n")


def build_json_report(results: list[EvalResult]) -> dict:
    return {
        "summary": {
            "total":  len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed and not r.error),
            "errors": sum(1 for r in results if r.error),
        },
        "tolerances": {"dollar": DOLLAR_TOL, "rate": RATE_TOL},
        "cases": [
            {
                "name":       r.case.name,
                "focus":      r.case.focus,
                "passed":     r.passed,
                "error":      r.error,
                "diffs":      [{"field": d.field, "cli": d.cli_val, "ref": d.ref_val, "delta": d.delta} for d in r.diffs],
                "cli_output": r.cli,
                "ref_output": r.ref,
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="entropyFA federal-tax eval suite")
    parser.add_argument("--json", action="store_true", help="Output JSON report to stdout")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each case as it runs")
    parser.add_argument("--filter", "-f", metavar="SUBSTR", help="Only run cases whose name contains SUBSTR")
    args = parser.parse_args()

    cases = CASES
    if args.filter:
        cases = [c for c in CASES if args.filter in c.name]
        if not cases:
            print(f"No cases match: {args.filter!r}", file=sys.stderr)
            sys.exit(1)

    print(f"Running {len(cases)} case(s)...", file=sys.stderr)
    results = run_eval(cases, verbose=args.verbose)

    if args.json:
        print(json.dumps(build_json_report(results), indent=2))
    else:
        print_report(results)

    blocking = sum(1 for r in results if r.blocking or r.error)
    sys.exit(0 if blocking == 0 else 1)


if __name__ == "__main__":
    main()
