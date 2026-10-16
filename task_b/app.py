"""Streamlit UI for the NL -> JSON spec -> read-only query -> chart prototype.
Run:  streamlit run app.py
Every sample question contains a vague term, so each one goes: clarify -> user picks a definition -> answer."""
import json
import os
import tempfile

import streamlit as st

from core import FUZZY_TERMS, METRICS, load_csv
from llm import GeminiSpecModel, StubSpecModel, list_gemini_models
from pipeline import AUDIT_LOG, Session, ask, chart, define_term

SAMPLE_QUESTIONS = [
    "Show a chart of referrals per month recently",                      # -> line chart
    "Chart the typical length of stay by discharge reason in 2025",     # -> bar chart
    "What % of residents were successful in 2025?",
    "How many residents are active right now?",
    "How many residents relapsed after leaving?",
    "What % of residents stayed sober after leaving?",
]
TENANT = "harbourview"

st.set_page_config(page_title="Harbourview analytics assistant", layout="centered")


@st.cache_resource
def init_db() -> str:
    csv = next(p for p in ["../data/harbourview_residents.csv", "harbourview_residents.csv"] if os.path.exists(p))
    db = os.path.join(tempfile.mkdtemp(), "axon_ui.db")
    load_csv(csv, db, TENANT)
    load_csv(csv, db, "other_house")  # second tenant in the same table: proves tenant scoping
    return db


@st.cache_data(ttl=3600, show_spinner=False)
def available_models(api_key: str) -> list[str]:
    return list_gemini_models(api_key)


def get_model(kind: str, api_key: str = "", models: tuple = ()):
    if kind == "Gemini":
        return GeminiSpecModel(models=list(models), api_key=api_key)
    return StubSpecModel()


DB = init_db()
state = st.session_state
state.setdefault("session", Session(user="director@harbourview", tenant_id=TENANT))
state.setdefault("result", None)
state.setdefault("question", "")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("Google API key", value=os.environ.get("GOOGLE_API_KEY", ""), type="password",
                            help="Used only for the spec step. Never stored or logged.")
    choices = ["Gemini", "Offline stub"] if api_key else ["Offline stub"]
    model_kind = st.radio("Spec model", choices,
                          help="Gemini sees only the schema and saved definitions, never resident records. "
                               "The offline stub is deterministic, for tests.")
    picked_models = ()
    if model_kind == "Gemini":
        try:
            names = available_models(api_key)
        except Exception as e:
            names = []
            st.error(f"Couldn't list models: {e}")
        names = [n for n in names if n.startswith("gemini-3")]  # Gemini 3.x only
        # default fallback stack: all Gemini 3 flash-lite models first, then flash
        flash = sorted([n for n in names if "flash" in n and "lite" not in n], reverse=True)
        lite = sorted([n for n in names if "flash-lite" in n], reverse=True)
        picked_models = tuple(st.multiselect(
            "Gemini models (tried in order; falls back on 503 / 429)", names,
            default=(lite + flash) or names[:1]))
        if not picked_models:
            st.warning("Pick at least one model, or switch to the offline stub.")
            model_kind = "Offline stub"
    st.caption(f"Tenant: **{TENANT}** (from login, never from the question)")
    st.caption("Data: synthetic. Staff notes are not loaded.")

    st.subheader("Saved definitions")
    if state.session.glossary:
        for term, spec in state.session.glossary.items():
            label = next(o for o, s in FUZZY_TERMS[term].items() if s == spec)
            st.markdown(f"- **{term}** → {label}")
        if st.button("Forget saved definitions"):
            state.session.glossary.clear()
            state.session.cache.clear()
            state.result = None
            st.rerun()
    else:
        st.caption("None yet. You'll be asked the first time a vague term comes up.")

model = get_model(model_kind, api_key, picked_models)

# ---------------------------------------------------------------- ask
st.title("Ask Harbourview's data")
st.caption("Natural language → JSON spec → validated read-only query → chart. Numbers come only from the query.")

picked = st.selectbox("Pick a question", SAMPLE_QUESTIONS)
custom = st.text_input("…or type your own (optional)")
question = custom.strip() or picked

if st.button("Ask", type="primary"):
    state.question = question
    try:
        with st.spinner("Thinking (each model call times out after 20 s)..."):
            state.result = ask(question, state.session, model, DB)
    except Exception as e:  # e.g. model API overloaded
        state.result = None
        st.error(f"Model call failed: {e}. Switch to the offline stub in the sidebar and retry.")

r = state.result
if r is not None:
    st.divider()
    st.markdown(f"**Question:** {state.question}")

    if r.clarification:
        st.info(f"🤔 {r.clarification.question}\n\nI won't guess what this means. Pick a definition:")
        choice = st.radio("Definition", r.clarification.options, label_visibility="collapsed")
        if st.button("Use this definition"):
            define_term(state.session, r.clarification.term, choice)
            with st.spinner("Answering..."):
                state.result = ask(state.question, state.session, model, DB)
            st.rerun()

    elif r.error:
        st.warning(r.error)

    else:
        label = METRICS[r.spec.metric]["label"]
        df = r.data
        if df.empty:
            st.write("No matching records.")
        elif not r.spec.dimensions:
            st.metric(label, f"{df['value'].iloc[0]:.1f}", help=f"Denominator n = {int(df['n'].iloc[0])}")
            st.caption(f"n = {int(df['n'].iloc[0])}")
        else:
            st.plotly_chart(chart(r), use_container_width=True, theme=None)  # keep our black/white styling
        if getattr(r, "verified", True):
            st.success(r.caption)
            src, n_try = getattr(r, "caption_source", "template"), getattr(r, "caption_attempts", 0)
            how = (f"LLM-written, passed on attempt {n_try}" if src == "llm"
                   else ("template, after the LLM failed 3 rewrites" if n_try else "template"))
            st.caption(f"✓ Answer check: every number above matches the query result ({how}).")
        else:
            st.warning(f"Answer check failed (numbers not in the result: {r.unverified}). "
                       "Showing the raw result table only.")

        with st.expander("How this was answered"):
            st.markdown("**1. JSON spec (from the model, validated against allow-lists)**")
            st.json(r.spec.model_dump(exclude_defaults=True))
            st.markdown("**2. Compiled SQL (from code, tenant filter injected, read-only)**")
            st.code(r.sql, language="sql")
            st.markdown("**3. Result rows**")
            st.dataframe(df.round(1), hide_index=True)

with st.expander("Audit log (last 5 entries: who asked what, which query ran; no result values)"):
    if os.path.exists(AUDIT_LOG):
        lines = open(AUDIT_LOG).read().splitlines()[-5:]
        for line in reversed(lines):
            e = json.loads(line)
            st.code(f"{e['user']} | {e['tenant']} | {e['outcome']} | {e['question']}\n{e['sql']}", language="text")
