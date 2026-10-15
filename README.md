# Group 7 – Overconfidence in GenAI: Week-2 experiment kit

This kit runs the experiment we promised in Week 1 (baselines and first results), then builds the Week-2 slides from **your real numbers**.

Time needed: about 15 min to set up and 10–20 min to run (OpenAI). Cost: under US $0.20 with `gpt-4.1-mini`.

```
run_experiment.py  → asks the LLM every question and saves raw answers  (results/raw.jsonl)
analyze.py         → grades answers, applies temperature scaling, computes metrics, draws charts
make_deck.py       → builds Group7_Week2_Progress_SamanRai.pptx (12 slides + speaker notes)
```

---

## What the experiment does (so you can explain it)

- **Dataset:** TriviaQA. These are factual questions with known answers, and each answer has several accepted spellings ("aliases"). We use a random sample of 400 questions with a fixed seed, so anyone can reproduce it.
- **For each question, the model is asked three ways:**
  1. **Greedy:** temperature 0, short answer only. We also get **token log-probabilities**, which give the model's own probability for its answer.
  2. **Samples:** the same question 5 times at temperature 1.0. **Agreement** is the share of samples that match the greedy answer. This is self-consistency, and it's Stage 1 of our triage pipeline.
  3. **Verbalized:** "Answer: … Confidence: NN%". The model *says* how sure it is.
- **Temperature scaling (TS):** learns one number T so that `calibrated = sigmoid(logit(p) / T)`. It's fitted with 5-fold cross-validation: T is learned on 80% of the questions and applied to the other 20%, five times over, so we never test on data we trained on.
- **Metrics:** these are the ones from our Week-1 plan.

| Metric | Meaning | Good |
|---|---|---|
| Accuracy | % answers correct | high |
| ECE | average gap between stated confidence and real accuracy | low |
| Brier | squared error of the confidence vs. 0/1 outcome | low |
| AUROC | does confidence rank right answers above wrong ones? 0.5 = random | high |
| Wrong but ≥90% conf. | share of *wrong* answers stated with ≥90% confidence (our headline overconfidence measure) | low |
| Risk–coverage | answer only the most confident X%; what's the error rate? | low curve |

- **Triage preview:** accept an answer if at least 4 of the 5 samples agree, otherwise "escalate" it. We report how many questions get escalated and what share of all errors they contain.

---

## Step 1: Install (once)

You need Python 3.10 or newer.

```bash
cd overconfidence_kit
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Step 2: Get an API key

**Option A: OpenAI (recommended, because it gives token probabilities)**
1. Go to platform.openai.com, then API keys, then create a key. The account needs a little prepaid credit; this run costs a few cents.
2. Set the key in your terminal:
   - Windows PowerShell: `$env:OPENAI_API_KEY="sk-..."`
   - macOS/Linux: `export OPENAI_API_KEY="sk-..."`

**Option B: Google Gemini (free tier)**
1. Get a key at aistudio.google.com, then "Get API key".
2. Set it: `$env:GEMINI_API_KEY="..."` (Windows) or `export GEMINI_API_KEY="..."`.
3. Add `--provider gemini --workers 1 --sleep 4 --n 150` to the run command. The free tier is rate-limited, so the run is slower and smaller.
4. Gemini may not return token probabilities. In that case the token-probability baseline is skipped automatically and the deck adapts.

> Never paste your API key into the slides, into the code files, or into the group chat.

## Step 3: Test run (2 minutes)

```bash
python run_experiment.py --n 10 --out test_run
python analyze.py --out test_run
```

The first lines print a sanity check showing a question, the greedy answer and the verbalized answer. Make sure they look sensible (short answers, plus a `Confidence: NN%` line).

## Step 4: Full run

```bash
python run_experiment.py --n 400            # OpenAI, about 10 min
```

- You can stop it with Ctrl+C and run the same command again. Finished questions are skipped.
- If some questions fail (network or rate limits), run the same command again to retry them.

## Step 5: Analyse

```bash
python analyze.py
```

This prints a results table and writes `results/summary.json`, `results/per_question.csv` and `results/charts/*.png`.

**Do this check before presenting (10 minutes):**
1. Open `results/per_question.csv` in Excel.
2. Filter `greedy_correct = 0` and read 30 rows.
3. Count the ones that were actually right but marked wrong, for example "JFK" vs "John F. Kennedy".
4. Say this in the talk: *"We manually checked 30 answers marked wrong; X were grading mistakes, so true accuracy is slightly higher."* Professors value this kind of honesty.

## Step 6: Build the slides

```bash
python make_deck.py --date "16 October 2026"
```

1. Open `Group7_Week2_Progress_SamanRai.pptx`.
2. Read the **speaker notes** under each slide. They're written from your numbers.
3. Rehearse.

Every number on the slides comes from `summary.json`. The script never makes anything up. If you re-run the experiment, run `analyze.py` and `make_deck.py` again.

## Step 7: Share with the group

Put the whole `results/` folder (raw answers, CSV, charts) in the group drive. It's the evidence behind the slides, and it's the starting point for Week 3.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Set the OPENAI_API_KEY environment variable` | Set the key in the **same** terminal window you run the script from |
| `AuthenticationError` / `NotFoundError` | Wrong key, no credit, or the model name doesn't exist on your account. Try `--model gpt-4o-mini` |
| Lots of `429` / rate-limit retries | Use `--workers 2` or `--workers 1 --sleep 3` |
| Dataset download fails | Check your internet connection. Alternatively, use `--dataset nq_open`, or your own CSV with `--dataset csv --csv-path my.csv` (columns: `question,answer`; separate several accepted answers with `|`) |
| Empty answers from the model | Some "thinking" models spend their token budget on reasoning. Use a non-reasoning model such as `gpt-4.1-mini`, `gpt-4o-mini` or `gemini-2.5-flash-lite` |
| `strftime` error on Windows | Pass `--date "16 October 2026"` explicitly |

Note that GPT-5-family reasoning models don't allow `temperature` or `logprobs`, so use `gpt-4.1-mini` or `gpt-4o-mini` here.

---

## Likely professor questions, and good answers

- **"Why TriviaQA?"** It has factual questions with ground-truth answers and multiple accepted aliases, so grading can be automatic and reproducible. That matches our Week-1 dataset criteria. A harder set such as SimpleQA is planned next.
- **"Is 400 questions enough?"** It's enough for a first signal. With roughly N wrong answers the error bars are still wide, so we'll scale up and add a second model in Weeks 3–5.
- **"Why does temperature scaling not change AUROC much?"** TS is a monotonic rescaling. It changes *how big* the confidences are, but not *their order*. So it fixes calibration (ECE) but can't tell right from wrong any better. That's exactly why we need a stronger signal plus verification, which is our triage idea.
- **"Isn't self-consistency expensive?"** Yes: 6 generations instead of 1. That's why it's only Stage 1, and why Week 3–4 measures whether triage beats always running the expensive check.
- **"How do you know the grader is right?"** Lenient alias matching plus a manual check of 30 answers marked wrong (give your number).
- **"Why is verbalized confidence so high?"** Chat-tuned models tend to state 90–95% almost regardless. LLM-stated confidence isn't a reliable probability, which is our core problem.
# Overconfidence-in-incorrect-answers-in-Generative-Ai
