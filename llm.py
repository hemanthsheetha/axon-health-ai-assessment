"""NL -> LLMOutput (spec or clarification). The only step that uses a model.
- GeminiSpecModel: real LLM via structured output (needs GOOGLE_API_KEY). Sees schema + glossary only, never rows.
- StubSpecModel: deterministic keyword stand-in so the demo, UI and eval run offline and reproducibly."""
import json
import os
import re

from core import FUZZY_TERMS, LLMOutput, schema_card

SYSTEM = """You translate an analytics question about a recovery home into a JSON spec.
Rules:
- Use ONLY metrics, dimensions, filter fields, operators and enum values listed in SCHEMA. Never invent fields.
- GLOSSARY maps a term to a user-approved partial spec. If the question uses that term, copy its partial spec
  exactly (metric, dimensions, filters, time_window, chart_type) and only add what the question adds.
- If the question uses a vague term that is NOT in GLOSSARY (e.g. "successful", "sober", "relapse", "active",
  "typical", "recent"), return kind="clarification" with a question and 2-3 options. Do not guess.
- Never output numbers or answers. You only produce the spec; the system runs the query.
- time_window dates are YYYY-MM-DD. "2025" means 2025-01-01 to 2025-12-31.
Return JSON matching the response schema."""


def glossary_for_question(question: str, glossary: dict) -> dict:
    q = question.lower()
    return {t: spec for t, spec in glossary.items() if re.search(rf"\b{t}", q)}


def list_gemini_models(api_key: str) -> list[str]:
    """Models this key can call for text generation (names without the 'models/' prefix)."""
    from google import genai
    client = genai.Client(api_key=api_key)
    names = [m.name.removeprefix("models/") for m in client.models.list()
             if "generateContent" in (m.supported_actions or [])]
    return sorted(n for n in names if "gemini" in n and "embedding" not in n)


class GeminiSpecModel:
    def __init__(self, models: list[str] | None = None, api_key: str | None = None):
        from google import genai
        from google.genai import types
        # hard 20 s timeout per call so an overloaded model can't hang the app
        self.client = genai.Client(api_key=api_key or os.environ["GOOGLE_API_KEY"],
                                   http_options=types.HttpOptions(timeout=20_000))
        self.good = None  # first model that answered; reused for captions
        # tried in order; falls through to the next on 503 "high demand" / 429 rate limits
        self.models = models or [m.strip() for m in
                                 os.environ.get("GEMINI_MODELS", "gemini-2.5-flash").split(",")]

    def __call__(self, question: str, glossary: dict, error: str | None = None) -> str:
        prompt = (f"{SYSTEM}\n\nSCHEMA:\n{schema_card()}\n\nGLOSSARY (user-approved definitions):\n"
                  f"{json.dumps(glossary_for_question(question, glossary))}\n\nQUESTION: {question}")
        if error:
            prompt += f"\n\nYour previous output failed validation: {error}. Fix it."
        last = None
        for m in self.models:
            try:
                resp = self.client.models.generate_content(
                    model=m, contents=prompt,
                    config={"response_mime_type": "application/json", "response_schema": LLMOutput,
                            "temperature": 0})
                self.good = m
                return resp.text  # validated by the caller, never trusted as-is
            except Exception as e:
                last = e
        raise RuntimeError(f"All Gemini models failed: {last}")

    def caption(self, label: str, rows_json: str, feedback: str | None = None) -> str:
        """One-sentence answer written from AGGREGATED result rows only (counts/rates, no records)."""
        prompt = (f"Write ONE short sentence answering a non-technical house director.\n"
                  f"Metric: {label}\nROWS (the only facts you may use): {rows_json}\n"
                  f"Rules: use only numbers that appear in ROWS, copied exactly; mention n; no advice.")
        if feedback:
            prompt += f"\nYour previous sentence was rejected: {feedback}. Rewrite it."
        try:  # one model only (the one that just worked) so retries stay fast
            return self.client.models.generate_content(
                model=self.good or self.models[0], contents=prompt,
                config={"temperature": 0}).text.strip()
        except Exception:
            return ""  # empty -> answer check fails -> template caption


class StubSpecModel:
    """Offline stand-in: a few question patterns -> specs, honouring glossary definitions exactly.
    Has no caption() method, so the pipeline uses the deterministic template caption."""

    def __call__(self, question: str, glossary: dict, error: str | None = None) -> str:
        q = question.lower()
        defined = glossary_for_question(question, glossary)
        if defined:  # user-approved definition wins
            spec = {}
            for partial in defined.values():
                spec.update(json.loads(json.dumps(partial)))
        elif "length of stay" in q or "how long" in q:
            spec = {"metric": "median_los_days", "dimensions": ["discharge_reason"]}
        elif "per month" in q or "by month" in q or "monthly" in q:
            spec = {"metric": "count_residents", "dimensions": ["referral_month"], "chart_type": "line"}
        elif "status" in q:
            spec = {"metric": "count_residents", "dimensions": ["current_status"]}
        elif "staff note" in q or "notes" in q:  # adversarial: tries to reach free text
            spec = {"metric": "count_residents", "dimensions": ["staff_note"]}
        else:
            spec = {"metric": "count_residents", "chart_type": "kpi"}
        if (m := re.search(r"\b(20\d\d)\b", q)) and "time_window" not in spec:
            y = m.group(1)
            metric = spec.get("metric", "")
            field = ("discharge_date" if ("los" in metric or "completion" in metric or "engaged" in metric)
                     else "referral_date")
            spec["time_window"] = {"field": field, "start": f"{y}-01-01", "end": f"{y}-12-31"}
        return json.dumps({"kind": "spec", "spec": spec})


def get_model():
    return GeminiSpecModel() if os.environ.get("GOOGLE_API_KEY") else StubSpecModel()
