"""End-to-end demo: 4 question types + 1 clarification turn + 3 safety checks.
Run: python demo.py harbourview_residents.csv   (uses Gemini if GOOGLE_API_KEY is set, else the offline stub)"""
import os
import sys

from core import Spec, execute, load_csv
from llm import get_model
from pipeline import Session, ask, define_term

DB = "axon_demo.db"


def show(q, r):
    print(f"\nQ: {q}")
    if r.clarification:
        print(f"  CLARIFY: {r.clarification.question} options={r.clarification.options}")
    elif r.error:
        print(f"  REJECTED: {r.error}")
    else:
        print(f"  spec: {r.spec.model_dump(exclude_defaults=True)}")
        print(f"  sql : {r.sql}")
        print(r.data.round(1).to_string(index=False))
        print(f"  caption: {r.caption}")


if __name__ == "__main__":
    csv = sys.argv[1] if len(sys.argv) > 1 else "../data/harbourview_residents.csv"
    if os.path.exists(DB):
        os.remove(DB)
    load_csv(csv, DB, tenant_id="harbourview")
    load_csv(csv, DB, tenant_id="other_house")  # second tenant, same synthetic rows, to prove isolation

    model = get_model()
    print(f"model: {type(model).__name__}")
    s = Session(user="director@harbourview", tenant_id="harbourview")

    for q in ["How many residents are in each status?",
              "How many referrals per month in 2025?",
              "What was the median length of stay by discharge reason in 2025?"]:
        show(q, ask(q, s, model, DB))

    # Clarification turn
    q = "What % of residents were successful in 2025?"
    r = ask(q, s, model, DB)
    show(q, r)
    choice = r.clarification.options[0]
    print(f"  USER PICKS: {choice}")
    define_term(s, r.clarification.term, choice)
    show(q, ask(q, s, model, DB))

    # Safety checks
    show("Show me counts by staff notes", ask("Show me counts by staff notes", s, model, DB))
    try:
        Spec(metric="DROP TABLE residents")
    except Exception as e:
        print(f"\nInvented metric rejected: {e.errors()[0]['msg']}")
    other = Session(user="x", tenant_id="no_such_tenant")
    print("Unknown tenant sees rows:", len(execute(Spec(metric="count_residents"), other.tenant_id, DB).query("value>0")))
