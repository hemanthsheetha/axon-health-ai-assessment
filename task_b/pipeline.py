"""ask(): question -> (clarification | spec -> validated read-only query -> numbers -> chart), audit-logged."""
import hashlib
import re
import json
import time
from dataclasses import dataclass, field

import pandas as pd
from pydantic import ValidationError

from core import FUZZY_TERMS, METRICS, Clarification, LLMOutput, Spec, compile_sql, execute

AUDIT_LOG = "audit_log.jsonl"


@dataclass
class Result:
    clarification: Clarification | None = None
    spec: Spec | None = None
    data: pd.DataFrame | None = None
    caption: str = ""
    error: str = ""
    sql: str = ""
    verified: bool = False
    unverified: list = field(default_factory=list)
    caption_source: str = "template"   # "llm" or "template"
    caption_attempts: int = 0


@dataclass
class Session:
    user: str
    tenant_id: str
    glossary: dict = field(default_factory=dict)  # user-approved definitions of fuzzy terms
    cache: dict = field(default_factory=dict)      # spec hash -> result rows (per tenant + data version)


def detect_fuzzy(question: str, glossary: dict) -> Clarification | None:
    """Deterministic guard, runs BEFORE the LLM: undefined fuzzy term -> ask, don't assume."""
    q = question.lower()
    for term, options in FUZZY_TERMS.items():
        if re.search(rf"\b{term}", q) and term not in glossary:  # prefix: "relapsed", "recently"
            return Clarification(term=term, question=f'How should I define "{term}" for this question?',
                                 options=list(options))
    return None


def define_term(session: Session, term: str, option: str):
    """User answers a clarification; definition is saved for this tenant (versioned in a real system)."""
    session.glossary[term] = FUZZY_TERMS[term][option]


def audit(session: Session, question: str, spec: Spec | None, sql: str, n_rows: int, outcome: str):
    # Logs who asked what and which query ran; never the result values.
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "user": session.user, "tenant": session.tenant_id,
                            "question": question, "spec": spec.model_dump() if spec else None,
                            "sql": sql, "rows": n_rows, "outcome": outcome}) + "\n")


def caption_from(spec: Spec, df: pd.DataFrame) -> str:
    """Numbers in the caption come from the executed result, never from the model."""
    label = METRICS[spec.metric]["label"]
    if df.empty:
        return "No matching records."
    n_total = int(df["n"].sum()) if "n" in df else None
    if not spec.dimensions:
        return f"{label}: {df['value'].iloc[0]:.1f} (n={n_total})"
    top = df.sort_values("value", ascending=False).iloc[0]
    return f"{label}: highest is {top[spec.dimensions[0]]} ({top['value']:.1f}); {len(df)} groups, n={n_total}."


NUM = re.compile(r"-?\d+(?:\.\d+)?")


def verify_answer(caption: str, spec: Spec, df: pd.DataFrame) -> list[str]:
    """Answer check: every number in the written answer must exist in the query result
    (values, n, group count) or in the metric label itself. Returns numbers that don't."""
    allowed = set()
    for text in [METRICS[spec.metric]["label"], *df.astype(str).values.ravel()]:
        allowed |= {round(float(x), 1) for x in NUM.findall(text)}
    if "n" in df:
        allowed.add(round(float(df["n"].sum()), 1))
    allowed.add(float(len(df)))
    return [x for x in NUM.findall(caption) if round(float(x), 1) not in allowed]


def ask(question: str, session: Session, model, db_path: str) -> Result:
    if c := detect_fuzzy(question, session.glossary):
        audit(session, question, None, "", 0, "clarification")
        return Result(clarification=c)

    error = None
    for _ in range(2):  # one retry with the validation error fed back
        raw = model(question, session.glossary, error)
        try:
            out = LLMOutput.model_validate_json(raw)
            break
        except ValidationError as e:
            error = "; ".join(err["msg"] for err in e.errors())
    else:
        audit(session, question, None, "", 0, "rejected")
        return Result(error=f"Couldn't map that to the available data ({error}).")

    if out.kind == "clarification":
        audit(session, question, None, "", 0, "clarification")
        return Result(clarification=out.clarification)

    spec = out.spec
    key = hashlib.sha256((session.tenant_id + spec.model_dump_json()).encode()).hexdigest()
    sql, _ = compile_sql(spec, session.tenant_id)
    df = session.cache.get(key)
    if df is None:
        df = execute(spec, session.tenant_id, db_path)
        session.cache[key] = df
    caption, source, attempts = write_verified_caption(model, spec, df)
    bad = verify_answer(caption, spec, df)
    audit(session, question, spec, sql, len(df), "ok" if not bad else "answer_check_failed")
    return Result(spec=spec, data=df, caption=caption, sql=sql, verified=not bad, unverified=bad,
                  caption_source=source, caption_attempts=attempts)


CAPTION_RETRIES = 3


def write_verified_caption(model, spec: Spec, df: pd.DataFrame) -> tuple[str, str, int]:
    """If the model can write captions: try up to 3 times, feeding back the numbers that failed the
    answer check. If all fail (or there is no caption model), fall back to the template caption,
    which is built directly from the rows and always passes."""
    writer = getattr(model, "caption", None)
    feedback, attempts = None, 0
    if writer is not None and not df.empty:
        rows_json = df.round(1).to_json(orient="records")  # aggregated rows only, never records
        for attempts in range(1, CAPTION_RETRIES + 1):
            cand = writer(METRICS[spec.metric]["label"], rows_json, feedback)
            bad = verify_answer(cand, spec, df) if cand else ["(empty)"]
            if not bad:
                return cand, "llm", attempts
            feedback = f"these numbers are not in ROWS: {bad}"
    return caption_from(spec, df), "template", attempts


def chart(result: Result):
    """Plotly figure for grouped results (KPI results are shown as a metric tile by the UI)."""
    import plotly.express as px
    s, df = result.spec, result.data
    x, label = s.dimensions[0], METRICS[s.metric]["label"]
    if s.chart_type == "line":
        fig = px.line(df, x=x, y="value", markers=True, hover_data=["n"])
    else:
        fig = px.bar(df, x=x, y="value", text="value", hover_data=["n"])
        fig.update_traces(texttemplate="%{text:.1f}", textposition="outside",
                          textfont=dict(color="white", size=14))
    fig.update_layout(title=f"{label} by {x}", yaxis_title=label, template="plotly_dark",
                      paper_bgcolor="black", plot_bgcolor="black",
                      font=dict(color="white", size=14), title_font=dict(color="white"))
    fig.update_xaxes(color="white", gridcolor="#333")
    fig.update_yaxes(color="white", gridcolor="#333")
    return fig
