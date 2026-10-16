"""Regression eval: NL question -> expected spec / clarification / rejection.
Run on every prompt or model change (CI). Exit code 1 if the pass rate drops below the threshold."""
import json
import os
import sys

from core import load_csv
from llm import get_model
from pipeline import Session, ask, define_term

THRESHOLD = 0.9
DB = "axon_eval.db"


def check(r, exp) -> bool:
    if exp["kind"] == "clarification":
        return r.clarification is not None and r.clarification.term == exp["term"]
    if exp["kind"] == "rejected":
        return bool(r.error)
    if r.spec is None:
        return False
    s = r.spec
    tw = [s.time_window.field, s.time_window.start, s.time_window.end] if s.time_window else None
    return (s.metric == exp["metric"] and sorted(s.dimensions) == sorted(exp.get("dimensions", []))
            and tw == exp.get("time_window"))


if __name__ == "__main__":
    csv = sys.argv[1] if len(sys.argv) > 1 else "harbourview_residents.csv"
    if os.path.exists(DB):
        os.remove(DB)
    load_csv(csv, DB, "harbourview")
    model, cases = get_model(), json.load(open("eval_cases.json"))
    passed = 0
    for c in cases:
        sess = Session(user="eval", tenant_id="harbourview")
        for term, option in c.get("define", {}).items():  # simulate the user's clarification answer
            define_term(sess, term, option)
        r = ask(c["q"], sess, model, DB)
        ok = check(r, c["expect"])
        passed += ok
        tag = " [after clarification]" if c.get("define") else ""
        print(f"{'PASS' if ok else 'FAIL'}  {c['q']}{tag}")
    rate = passed / len(cases)
    print(f"\n{type(model).__name__}: {passed}/{len(cases)} = {rate:.0%} (threshold {THRESHOLD:.0%})")
    sys.exit(0 if rate >= THRESHOLD else 1)
