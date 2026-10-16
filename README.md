# Axon Health: AI/ML Engineer take-home (Harbourview House)

Synthetic data only (`data/harbourview_residents.csv`, ~200 fictional residents). No real or de-identified client data is used.

## Repo layout
```
├── data/harbourview_residents.csv     # provided synthetic dataset
├── task_a/task_a_dashboard.ipynb      # Task A: FY2025 descriptive dashboard (funnel, time in stage, occupancy, LOS, outcomes)
├── task_b/                            # Task B: NL → JSON spec → read-only query → chart prototype
│   ├── README.md                      #   design write-up (spec, safety, clarification, guardrails, eval, PHI)
│   ├── app.py                         #   Streamlit UI
│   ├── core.py                        #   semantic layer, JSON spec (Pydantic), compiler, read-only executor
│   ├── llm.py                         #   Gemini spec/caption model + offline deterministic stub
│   ├── pipeline.py                    #   ask(): clarify → spec → validate → compile → run → answer check → audit
│   ├── demo.py / run_eval.py          #   CLI demo and regression eval (eval_cases.json)
├── docs/TASK_C.md                     # Task C written answers (C1 notes→structure, C2 accuracy, C3 predictive idea)
├── docs/axon_system_design.png        # prototype flow diagram (.drawio source alongside)
└── requirements.txt
```

## Setup
```bash
conda create -n axon python=3.11 -y
conda activate axon
pip install -r requirements.txt
```
(I'd normally use `uv` with a lockfile; `requirements.txt` keeps it simple here.)

Optional, for the live LLM path: get a Gemini API key at https://aistudio.google.com/apikey and paste it in the Streamlit sidebar (or set `GOOGLE_API_KEY`). Without a key, everything runs on the offline stub.

## Run
```bash
# Task A
jupyter notebook task_a/task_a_dashboard.ipynb      # run all cells

# Task B (from the task_b folder)
cd task_b
streamlit run app.py      # UI: sample questions → clarification → chart/answer
python demo.py            # CLI walkthrough + safety checks
python run_eval.py        # regression eval (exits 1 below 90% pass rate)
```

## Key assumptions (Task A)
- Fiscal year = Jan–Dec; reporting year **FY2025**, cut off at 2025-12-31.
- Ongoing stays are **censored**: measured to year end and shown separately, never averaged with completed stays.
- Follow-up rates use everyone whose follow-up is due as the denominator; lost to follow-up is its own category; best and worst cases are labelled.
- Excluded and documented: intake-before-referral records (2) from timing; discharged-with-no-date records (3) from occupancy and length of stay.

## Task C
Written answers are in [docs/TASK_C.md](docs/TASK_C.md).

## AI assistance disclosure
I used Claude (Anthropic) as a coding assistant. It reviewed my Task A notebook and corrected calculation logic (occupancy, censoring, follow-up denominators), and wrote most of the Task B prototype code and Streamlit UI from a design we worked out together. I chose the fiscal year, metric definitions and chart types, reviewed and ran all code, and verified the numbers against the data.
