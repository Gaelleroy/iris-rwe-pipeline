"""Normalization primitives.

Kept separate from the transform job because validation needs them too - the
QC layer must parse dates the same way the ETL does, or it will report problems
the pipeline does not actually have (and miss ones it does).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d", "%d/%m/%Y")


def parse_dates_mixed(s: pd.Series, formats=ACCEPTED_DATE_FORMATS) -> pd.Series:
    """Parse a column where different source sites used different formats.

    Deliberately does NOT fall back to pandas' inference. Inference silently
    reads 03/04/2024 as March 4 or April 3 depending on the rest of the column,
    which is how a whole site's dates end up quietly wrong. An explicit format
    list means anything unrecognised becomes NaT and gets counted by the
    unparseable_dates rule instead of guessed at.
    """
    raw = s.astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    remaining = raw.replace({"": np.nan, "nan": np.nan, "None": np.nan, "NaT": np.nan})
    for fmt in formats:
        todo = out.isna() & remaining.notna()
        if not todo.any():
            break
        parsed = pd.to_datetime(remaining[todo], format=fmt, errors="coerce")
        out.loc[todo] = parsed
    return out


_CODE_RE = re.compile(r"[^A-Za-z0-9]")


def normalize_icd10(s: pd.Series) -> pd.Series:
    """Standardize ICD-10 codes to uppercase dotted form.

    E11-311, e11.311, E11311, ' E11.311 ' all collapse to E11.311. Without this
    a cohort query with `WHERE diagnosis_code = 'E11.311'` silently loses every
    patient from the sites that export lowercase.
    """
    cleaned = s.astype(str).str.strip().str.upper()
    stripped = cleaned.str.replace(_CODE_RE, "", regex=True)
    dotted = stripped.where(
        stripped.str.len() <= 3,
        stripped.str.slice(0, 3) + "." + stripped.str.slice(3),
    )
    return dotted.mask(dotted.isin(["", "NAN", "NONE"]))


def normalize_sex(s: pd.Series) -> pd.Series:
    m = s.astype(str).str.strip().str.upper()
    mapped = m.map({
        "M": "M", "MALE": "M", "1": "M",
        "F": "F", "FEMALE": "F", "2": "F",
    })
    return mapped  # anything else -> NaN, explicitly missing rather than guessed


_SNELLEN_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def snellen_to_logmar(s: pd.Series) -> pd.Series:
    """Convert Snellen fractions to logMAR.

    logMAR = log10(denominator / numerator). This conversion matters because
    Snellen is an ordinal string, not a number: you cannot average '20/40' and
    '20/80', and the difference between 20/20 and 20/40 is not the same visual
    change as between 20/200 and 20/400. Everything downstream - the outcome
    definition, the regression, the KM curve - operates on logMAR.

    Returns NaN for unparseable or non-physiologic values rather than
    substituting a number, so the QC layer can count them.
    """
    def one(v):
        if v is None:
            return np.nan
        m = _SNELLEN_RE.match(str(v))
        if not m:
            return np.nan
        num, den = float(m.group(1)), float(m.group(2))
        if num <= 0 or den <= 0:
            return np.nan
        return float(np.log10(den / num))

    return s.map(one)


def logmar_to_etdrs_letters(logmar: pd.Series) -> pd.Series:
    """Approximate ETDRS letter score. 0.02 logMAR ~ 1 letter."""
    return (85 - (logmar / 0.02)).round(1)
