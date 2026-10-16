"""Semantic layer + JSON spec + deterministic compiler.
The LLM only ever fills a Spec. It never writes SQL and never sees rows."""
import json
import sqlite3
import time
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------- semantic layer
# Every metric has ONE definition and its own denominator; it is the only thing the model may name.
METRICS = {
    "count_residents": {
        "label": "Residents", "sql": "COUNT(*)", "base": None,
        "desc": "Number of residents (rows) matching the filters."},
    "median_los_days": {
        "label": "Median length of stay (days)", "sql": None,
        "base": "discharge_date IS NOT NULL AND admission_date IS NOT NULL",
        "desc": "Median days from admission to discharge, finished stays only."},
    "avg_los_days": {
        "label": "Average length of stay (days)",
        "sql": "AVG(julianday(discharge_date) - julianday(admission_date))",
        "base": "discharge_date IS NOT NULL AND admission_date IS NOT NULL",
        "desc": "Mean days from admission to discharge, finished stays only."},
    "completion_rate_pct": {
        "label": "Completed the program (%)",
        "sql": "100.0 * SUM(current_status IN ('alumni','aftercare')) / COUNT(*)",
        "base": "discharge_date IS NOT NULL",
        "desc": "% of finished stays that completed the program (alumni/aftercare). Denominator: all discharged."},
    "engaged_6m_pct": {
        "label": "Engaged in recovery at 6 months, worst case (%)",
        "sql": "100.0 * SUM(followup_6m = 'engaged_in_recovery') / COUNT(*)",
        "base": "followup_6m IS NOT NULL AND followup_6m != 'not_yet_due'",
        "desc": "% engaged in recovery at 6 months. Denominator: everyone whose 6m follow-up is due, "
                "INCLUDING lost to follow-up (worst case, no survivorship bias)."},
    "engaged_6m_reached_pct": {
        "label": "Engaged in recovery at 6 months, best case: of those reached (%)",
        "sql": "100.0 * SUM(followup_6m = 'engaged_in_recovery') / COUNT(*)",
        "base": "followup_6m IN ('engaged_in_recovery', 'relapse_reported')",
        "desc": "% engaged in recovery among residents actually reached at 6 months (best case; "
                "excludes lost to follow-up, so it is optimistic)."},
}
DIMENSIONS = {  # name -> SQL expression
    "current_status": "current_status",
    "discharge_reason": "COALESCE(discharge_reason, 'not recorded')",
    "fee_waived": "fee_waived",
    "referral_year": "strftime('%Y', referral_date)",
    "referral_month": "strftime('%Y-%m', referral_date)",
}
FILTER_FIELDS = {"current_status", "discharge_reason", "fee_waived", "followup_6m", "followup_12m"}
OPS = {"eq": "=", "neq": "!=", "in": "IN"}
DATE_FIELDS = {"referral_date", "admission_date", "discharge_date"}

PHASES = ["phase_1", "phase_2", "phase_3"]
Y2025 = lambda field: {"field": field, "start": "2025-01-01", "end": "2025-12-31"}

# Fuzzy terms are never interpreted by the model. Each option is an exact, user-approved definition
# expressed as a partial spec; the chosen one is stored in the tenant glossary and reused.
FUZZY_TERMS = {
    "successful": {
        "Completed the program (alumni or aftercare)":
            {"metric": "completion_rate_pct", "chart_type": "kpi"},
        "Engaged in recovery at the 6-month follow-up (lost to follow-up counted as not engaged)":
            {"metric": "engaged_6m_pct", "chart_type": "kpi"},
    },
    "active": {
        "Currently in the program (phase 1-3)":
            {"metric": "count_residents", "dimensions": ["current_status"],
             "filters": [{"field": "current_status", "op": "in", "values": PHASES}]},
        "Admitted at any point in 2025":
            {"metric": "count_residents", "dimensions": ["current_status"],
             "time_window": Y2025("admission_date")},
    },
    "relapse": {
        "Relapse reported at the 6-month follow-up":
            {"metric": "count_residents", "dimensions": ["discharge_reason"],
             "filters": [{"field": "followup_6m", "op": "eq", "values": ["relapse_reported"]}]},
        "Relapse reported at the 12-month follow-up":
            {"metric": "count_residents", "dimensions": ["discharge_reason"],
             "filters": [{"field": "followup_12m", "op": "eq", "values": ["relapse_reported"]}]},
    },
    "typical": {
        "Median (robust to a few very long stays)":
            {"metric": "median_los_days", "dimensions": ["discharge_reason"]},
        "Average (mean)":
            {"metric": "avg_los_days", "dimensions": ["discharge_reason"]},
    },
    "recent": {
        "Last 3 months of 2025 (Oct-Dec)":
            {"metric": "count_residents", "dimensions": ["referral_month"], "chart_type": "line",
             "time_window": {"field": "referral_date", "start": "2025-10-01", "end": "2025-12-31"}},
        "All of 2025":
            {"metric": "count_residents", "dimensions": ["referral_month"], "chart_type": "line",
             "time_window": Y2025("referral_date")},
    },
    "sober": {
        "Worst case: engaged in recovery at 6 months, out of everyone due (lost = not sober)":
            {"metric": "engaged_6m_pct", "chart_type": "kpi"},
        "Best case: engaged in recovery at 6 months, out of those we reached":
            {"metric": "engaged_6m_reached_pct", "chart_type": "kpi"},
    },
}

# ---------------------------------------------------------------- JSON spec
class Filter(BaseModel):
    field: str
    op: Literal["eq", "neq", "in"]
    values: list[str]

    @field_validator("field")
    @classmethod
    def allowed(cls, v):
        if v not in FILTER_FIELDS:
            raise ValueError(f"filter field '{v}' is not allowed")
        return v


class TimeWindow(BaseModel):
    field: Literal["referral_date", "admission_date", "discharge_date"]
    start: str  # YYYY-MM-DD
    end: str


class Spec(BaseModel):
    metric: str
    dimensions: list[str] = []
    filters: list[Filter] = []
    time_window: Optional[TimeWindow] = None
    chart_type: Literal["bar", "line", "kpi", "table"] = "bar"

    @field_validator("metric")
    @classmethod
    def metric_ok(cls, v):
        if v not in METRICS:
            raise ValueError(f"metric '{v}' is not defined")
        return v

    @field_validator("dimensions")
    @classmethod
    def dims_ok(cls, v):
        bad = [d for d in v if d not in DIMENSIONS]
        if bad or len(v) > 2:
            raise ValueError(f"dimensions not allowed or more than 2: {v}")
        return v


class Clarification(BaseModel):
    term: str
    question: str
    options: list[str]


class LLMOutput(BaseModel):
    """What the model returns: exactly one of spec / clarification."""
    kind: Literal["spec", "clarification"]
    spec: Optional[Spec] = None
    clarification: Optional[Clarification] = None

    @model_validator(mode="after")
    def one_of(self):
        if (self.kind == "spec") != (self.spec is not None):
            raise ValueError("kind=spec requires a spec")
        if (self.kind == "clarification") != (self.clarification is not None):
            raise ValueError("kind=clarification requires a clarification")
        return self


def schema_card() -> str:
    """Everything the LLM is allowed to know: schema and definitions, no records."""
    return json.dumps({
        "metrics": {k: v["desc"] for k, v in METRICS.items()},
        "dimensions": list(DIMENSIONS),
        "filter_fields": sorted(FILTER_FIELDS),
        "filter_ops": list(OPS),
        "time_window_fields": sorted(DATE_FIELDS),
        "enum_values": {
            "current_status": ["phase_1", "phase_2", "phase_3", "aftercare", "alumni", "discharged", "not_admitted"],
            "discharge_reason": ["completed", "self_discharge", "administrative", "transfer", "other"],
            "followup_6m": ["engaged_in_recovery", "relapse_reported", "unknown", "lost_to_follow_up", "not_yet_due"],
            "fee_waived": ["true", "false"],
        },
    }, indent=1)

# ---------------------------------------------------------------- compiler (code, not LLM)
MAX_ROWS = 1000
TIMEOUT_S = 2.0


def compile_sql(spec: Spec, tenant_id: str) -> tuple[str, list]:
    m = METRICS[spec.metric]
    dims = [(DIMENSIONS[d], d) for d in spec.dimensions]
    select = [f"{expr} AS {name}" for expr, name in dims]
    if m["sql"] is None:  # median: fetch values, aggregate in pandas (SQLite has no MEDIAN)
        select.append("julianday(discharge_date) - julianday(admission_date) AS los_days")
    else:
        select.append(f"{m['sql']} AS value")
        select.append("COUNT(*) AS n")  # denominator shown next to every number

    where, params = ["tenant_id = ?"], [tenant_id]  # tenant scope always injected by code
    if m["base"]:
        where.append(f"({m['base']})")
    for f in spec.filters:
        vals = [{"true": "1", "false": "0"}.get(v.lower(), v) if f.field == "fee_waived" else v for v in f.values]
        if f.op == "in":
            where.append(f"{f.field} IN ({','.join('?' * len(vals))})")
            params += vals
        else:
            where.append(f"{f.field} {OPS[f.op]} ?")
            params.append(vals[0])
    if spec.time_window:
        where.append(f"{spec.time_window.field} BETWEEN ? AND ?")
        params += [spec.time_window.start, spec.time_window.end]

    sql = f"SELECT {', '.join(select)} FROM residents WHERE {' AND '.join(where)}"
    if dims and m["sql"] is not None:
        sql += " GROUP BY " + ", ".join(expr for expr, _ in dims)
    return sql + f" LIMIT {MAX_ROWS}", params


def execute(spec: Spec, tenant_id: str, db_path: str) -> pd.DataFrame:
    sql, params = compile_sql(spec, tenant_id)
    if not sql.upper().startswith("SELECT"):
        raise PermissionError("only SELECT allowed")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # read-only at the connection level
    con.execute("PRAGMA query_only = ON")
    deadline = time.time() + TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10_000)  # query timeout
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    if spec.metric == "median_los_days":
        if spec.dimensions:
            df = df.groupby(spec.dimensions)["los_days"].agg(value="median", n="count").reset_index()
        else:
            df = pd.DataFrame({"value": [df["los_days"].median()], "n": [len(df)]})
    return df


def load_csv(csv_path: str, db_path: str, tenant_id: str):
    """Load synthetic CSV for one tenant. Free-text staff notes never enter the query store."""
    df = pd.read_csv(csv_path).drop(columns=["staff_note"])
    df["tenant_id"] = tenant_id
    with sqlite3.connect(db_path) as con:
        df.to_sql("residents", con, if_exists="append", index=False)
