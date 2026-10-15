"""Shared helpers: dataset loading, answer normalisation and grading."""
import csv
import random
import re
import string

# ---------------------------------------------------------------------------
# Answer normalisation (SQuAD / TriviaQA style)
# ---------------------------------------------------------------------------

_ARTICLES = re.compile(r"\b(a|an|the)\b")
_PUNCT = set(string.punctuation) | {"‘", "’", "“", "”", "–", "—"}


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = "".join(ch if ch not in _PUNCT else " " for ch in text)
    text = _ARTICLES.sub(" ", text)
    return " ".join(text.split())


def _contains_phrase(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` as a whole-word phrase."""
    if not needle or not haystack:
        return False
    return f" {needle} " in f" {haystack} "


def is_correct(prediction: str, gold_aliases) -> bool:
    """Lenient match: exact normalised match, or a gold alias appears inside the
    (short) prediction as a whole phrase. E.g. 'Paris, France' matches 'Paris'."""
    pred = normalize(prediction)
    if not pred:
        return False
    for alias in gold_aliases:
        a = normalize(alias)
        if not a:
            continue
        if pred == a or _contains_phrase(pred, a):
            return True
    return False


def same_answer(a: str, b: str) -> bool:
    """Do two model answers say the same thing? Used for self-consistency."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return na == nb or _contains_phrase(na, nb) or _contains_phrase(nb, na)


def clean_answer(text: str) -> str:
    """Strip 'Answer:' prefixes, trailing explanations and punctuation."""
    text = (text or "").strip()
    text = re.sub(r"^\s*(final\s+)?answer\s*[:\-]\s*", "", text, flags=re.I)
    text = text.split("\n")[0].strip()
    return text.strip(" .")


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def load_questions(name: str, n: int, seed: int = 7, csv_path: str = None, pool: int = 3000):
    """Return a list of dicts: {id, question, aliases}.

    name = 'triviaqa' | 'nq_open' | 'csv'
    We stream the first `pool` validation questions and draw a seeded random
    sample of size n, so every group member gets the same questions.
    """
    rows = []
    if name == "csv":
        if not csv_path:
            raise SystemExit("--csv-path is required with --dataset csv")
        with open(csv_path, newline="", encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                aliases = [a.strip() for a in r["answer"].split("|") if a.strip()]
                rows.append({"id": r.get("id") or f"csv-{i}", "question": r["question"].strip(), "aliases": aliases})
    else:
        from datasets import load_dataset  # imported lazily so 'csv' needs no extra deps

        if name == "triviaqa":
            ds = load_dataset("mandarjoshi/trivia_qa", "rc.nocontext", split="validation", streaming=True)
            for i, r in enumerate(ds):
                if i >= pool:
                    break
                ans = r["answer"]
                aliases = list(dict.fromkeys([ans["value"]] + list(ans.get("aliases", []))))
                rows.append({"id": r.get("question_id", f"tqa-{i}"), "question": r["question"].strip(), "aliases": aliases})
        elif name == "nq_open":
            ds = load_dataset("google-research-datasets/nq_open", split="validation", streaming=True)
            for i, r in enumerate(ds):
                if i >= pool:
                    break
                rows.append({"id": f"nq-{i}", "question": r["question"].strip().rstrip("?") + "?", "aliases": list(r["answer"])})
        else:
            raise SystemExit(f"Unknown dataset: {name}")

    rng = random.Random(seed)
    if n and n < len(rows):
        rows = rng.sample(rows, n)
    return rows


DATASET_LABELS = {
    "triviaqa": "TriviaQA (rc.nocontext, validation)",
    "nq_open": "Natural Questions – Open (validation)",
    "csv": "Custom question set",
}
