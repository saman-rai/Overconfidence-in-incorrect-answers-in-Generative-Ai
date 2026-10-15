"""STEP 2 - Collect data: ask the LLM every question in three ways and save raw outputs.

For each question we make three kinds of API call:
  1. GREEDY      temperature 0, answer only, with token log-probabilities (if the API supports them)
  2. SAMPLES     K answers at temperature 1.0 (for self-consistency / agreement)
  3. VERBALIZED  temperature 0, answer + "Confidence: NN%"

Everything is appended to results/raw.jsonl one line per question, so the run can be
stopped (Ctrl+C) and restarted at any time - finished questions are skipped.

Examples:
  python run_experiment.py --provider openai --n 300
  python run_experiment.py --provider gemini --n 100 --workers 1 --sleep 4
"""
import argparse
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import clean_answer, load_questions

PROVIDERS = {
    "openai": {"base_url": None, "key_env": "OPENAI_API_KEY", "model": "gpt-4.1-mini"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY", "model": "gemini-3.5-flash-lite"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "llama-3.1-8b-instant"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "model": "openai/gpt-4.1-mini"},
}

SYSTEM_ANSWER = (
    "You are a question-answering assistant. Reply with ONLY the short answer "
    "(a name, number, date or a few words). No explanation, no full sentence. "
    "If you are unsure, still give your single best guess."
)
SYSTEM_VERBAL = (
    "You are a question-answering assistant. Give your single best short answer "
    "(a name, number, date or a few words), then state how confident you are that "
    "the answer is correct, as a percentage from 0 to 100. Use exactly this format:\n"
    "Answer: <short answer>\nConfidence: <number>%"
)

_write_lock = threading.Lock()
_caps = {"logprobs": True, "n": True}  # downgraded automatically if the API refuses
_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def make_client(provider: str):
    from openai import OpenAI

    cfg = PROVIDERS[provider]
    key = os.environ.get(cfg["key_env"])
    if not key:
        sys.exit(f"Set the {cfg['key_env']} environment variable first (see README step 1).")
    return OpenAI(api_key=key, base_url=cfg["base_url"], max_retries=6, timeout=60)


def _track(resp):
    with _write_lock:
        _usage["calls"] += 1
        u = getattr(resp, "usage", None)
        if u:
            _usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
            _usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0


def chat(client, model, system, user, temperature, max_tokens, n=1, logprobs=False, sleep=0.0):
    kwargs = dict(model=model, temperature=temperature, max_tokens=max_tokens,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if n > 1:
        kwargs["n"] = n
    if logprobs:
        kwargs["logprobs"] = True
    for attempt in range(6):
        try:
            resp = client.chat.completions.create(**kwargs)
            _track(resp)
            if sleep:
                time.sleep(sleep)
            return resp
        except Exception as e:  # noqa: BLE001 - we want to classify any API error
            import openai

            if isinstance(e, (openai.BadRequestError, openai.UnprocessableEntityError)):
                # 400/422 = the request itself is invalid; retrying won't help.
                if logprobs:
                    raise _Unsupported("logprobs") from e
                if n > 1:
                    raise _Unsupported("n") from e
                raise
            if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)):
                sys.exit(f"API refused the request ({type(e).__name__}): {e}\n"
                         "Check your API key, billing, and the --model name.")
            if attempt == 5:
                raise
            wait = min(60, 2 ** attempt * 2)
            print(f"  ! API error ({type(e).__name__}): {str(e)[:120]} - retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)


class _Unsupported(Exception):
    pass


def greedy_call(client, model, q, sleep):
    user = f"Question: {q}"
    if _caps["logprobs"]:
        try:
            resp = chat(client, model, SYSTEM_ANSWER, user, 0.0, 24, logprobs=True, sleep=sleep)
            ch = resp.choices[0]
            text = ch.message.content or ""
            lps = []
            if getattr(ch, "logprobs", None) and getattr(ch.logprobs, "content", None):
                lps = [t.logprob for t in ch.logprobs.content]
            return text, lps
        except _Unsupported:
            print("  i This model/API does not return logprobs - token-probability baseline will be skipped.")
            _caps["logprobs"] = False
    resp = chat(client, model, SYSTEM_ANSWER, user, 0.0, 24, sleep=sleep)
    return resp.choices[0].message.content or "", []


def sample_calls(client, model, q, k, sleep):
    user = f"Question: {q}"
    if _caps["n"]:
        try:
            resp = chat(client, model, SYSTEM_ANSWER, user, 1.0, 24, n=k, sleep=sleep)
            outs = [c.message.content or "" for c in resp.choices]
            if len(outs) == k:
                return outs
            _caps["n"] = False  # API silently ignored n
        except _Unsupported:
            _caps["n"] = False
        print("  i API does not support n>1 - sampling one answer per call instead.")
    return [chat(client, model, SYSTEM_ANSWER, user, 1.0, 24, sleep=sleep).choices[0].message.content or "" for _ in range(k)]


_CONF_RE = re.compile(r"confidence\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*%?", re.I)
_ANS_RE = re.compile(r"answer\s*[:\-]\s*(.+)", re.I)


def parse_verbal(text):
    ans_m = _ANS_RE.search(text or "")
    conf_m = _CONF_RE.search(text or "")
    answer = clean_answer(ans_m.group(1)) if ans_m else clean_answer(text)
    conf = None
    if conf_m:
        conf = max(0.0, min(100.0, float(conf_m.group(1)))) / 100.0
    return answer, conf


def run_one(client, model, item, k, sleep):
    q = item["question"]
    t0 = time.time()
    g_text, g_lps = greedy_call(client, model, q, sleep)
    samples = sample_calls(client, model, q, k, sleep)
    v_text = chat(client, model, SYSTEM_VERBAL, f"Question: {q}", 0.0, 48, sleep=sleep).choices[0].message.content or ""
    v_answer, v_conf = parse_verbal(v_text)
    return {
        "id": item["id"],
        "question": q,
        "aliases": item["aliases"],
        "greedy_raw": g_text,
        "greedy_answer": clean_answer(g_text),
        "greedy_logprobs": g_lps,
        "greedy_seq_prob": math.exp(sum(g_lps)) if g_lps else None,
        "samples": [clean_answer(s) for s in samples],
        "verbal_raw": v_text,
        "verbal_answer": v_answer,
        "verbal_conf": v_conf,
        "seconds": round(time.time() - t0, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=PROVIDERS, default="openai")
    ap.add_argument("--model", help="override the provider's default model")
    ap.add_argument("--dataset", choices=["triviaqa", "nq_open", "csv"], default="triviaqa")
    ap.add_argument("--csv-path", help="CSV with columns question,answer (aliases separated by |)")
    ap.add_argument("--n", type=int, default=400, help="number of questions")
    ap.add_argument("--k", type=int, default=5, help="samples per question for self-consistency")
    ap.add_argument("--workers", type=int, default=6, help="parallel requests (use 1 on free tiers)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to wait after each call (free-tier rate limits)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    model = args.model or PROVIDERS[args.provider]["model"]
    os.makedirs(args.out, exist_ok=True)
    raw_path = os.path.join(args.out, "raw.jsonl")
    meta_path = os.path.join(args.out, "run_meta.json")

    print(f"Loading {args.n} questions from {args.dataset} ...")
    items = load_questions(args.dataset, args.n, seed=args.seed, csv_path=args.csv_path)

    done = set()
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:  # noqa: BLE001
                    pass
    todo = [it for it in items if it["id"] not in done]
    print(f"Model: {model} via {args.provider} | {len(done)} already done, {len(todo)} to go | "
          f"3 calls/question (+{args.k - 1} more if the API can't return {args.k} samples at once)")

    client = make_client(args.provider)
    # Probe once, single-threaded, so capability fallbacks are settled before going parallel.
    t_start = time.time()
    errors = 0
    if todo:
        first = todo.pop(0)
        rec = run_one(client, model, first, args.k, args.sleep)
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  sanity check -> Q: {first['question'][:70]}\n     greedy: {rec['greedy_answer']!r}  verbal: {rec['verbal_raw']!r}")

    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        tqdm = None
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool, open(raw_path, "a", encoding="utf-8") as f:
        futs = {pool.submit(run_one, client, model, it, args.k, args.sleep): it for it in todo}
        bar = tqdm(total=len(futs), unit="q") if tqdm else None
        for fut in as_completed(futs):
            try:
                rec = fut.result()
                with _write_lock:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  ! question {futs[fut]['id']} failed: {str(e)[:150]} (re-run the script to retry it)", file=sys.stderr)
            if bar:
                bar.update(1)
        if bar:
            bar.close()

    # merge run metadata (keeps totals across restarts)
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    meta.update({
        "provider": args.provider, "model": model, "dataset": args.dataset, "n_requested": args.n,
        "k": args.k, "seed": args.seed, "logprobs_available": _caps["logprobs"],
        "n_param_supported": _caps["n"],
        "api_calls": meta.get("api_calls", 0) + _usage["calls"],
        "prompt_tokens": meta.get("prompt_tokens", 0) + _usage["prompt_tokens"],
        "completion_tokens": meta.get("completion_tokens", 0) + _usage["completion_tokens"],
        "wall_seconds": round(meta.get("wall_seconds", 0) + time.time() - t_start, 1),
        "finished_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"\nDone. {errors} failed questions. API calls this run: {_usage['calls']}, "
          f"tokens: {_usage['prompt_tokens'] + _usage['completion_tokens']:,}. Raw data -> {raw_path}")
    if errors:
        print("Re-run the same command to retry the failed questions.")


if __name__ == "__main__":
    main()
