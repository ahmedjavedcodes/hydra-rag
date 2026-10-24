"""Calibrates MIN_SCORE (spec 02, AC10) on a labelled question set. No LLM calls, only retrieval.

Usage (from the repo root, after the documents are ingested and indexed into one session):

    uv run python -m scripts.calibrate_threshold questions.json

questions.json:
    {
      "session_id": "<32-hex session id>",
      "questions": [
        {"question": "What was North region revenue?", "expected_file": "report.pdf"},
        {"question": "Who won the 1998 World Cup?", "expected_file": null}
      ]
    }

`expected_file: null` marks a question the documents cannot answer. For each threshold the script
prints the hit rate (answerable questions whose best passing match comes from the expected file)
and the false-answer rate (unanswerable questions that still get a match above the threshold).
Pick the threshold with a good hit rate and a low false-answer rate, set MIN_SCORE in .env, and
keep the printed table with the change.
"""

import json
import sys

from src.config import rag_settings
from src.embeddings.embedder import default_embedder
from src.embeddings.vector_store import default_vector_store

THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(9)]  # 0.50 .. 0.90


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    session_id, questions = data["session_id"], data["questions"]
    embedder, store = default_embedder(), default_vector_store()

    scored = []
    for item in questions:
        matches = store.query(session_id, embedder.embed_query(item["question"]), rag_settings.top_k)
        scored.append((item, matches))

    answerable = [s for s in scored if s[0].get("expected_file")]
    unanswerable = [s for s in scored if not s[0].get("expected_file")]
    print(f"{len(answerable)} answerable, {len(unanswerable)} unanswerable questions\n")
    print("threshold  hit_rate  false_answer_rate")
    for t in THRESHOLDS:
        hits = 0
        for item, matches in answerable:
            passing = [m for m in matches if m.score >= t]
            if passing and passing[0].metadata.get("filename") == item["expected_file"]:
                hits += 1
        false = sum(1 for _, matches in unanswerable if any(m.score >= t for m in matches))
        hit_rate = hits / len(answerable) if answerable else 0.0
        false_rate = false / len(unanswerable) if unanswerable else 0.0
        print(f"{t:9.2f}  {hit_rate:8.2f}  {false_rate:17.2f}")
    print(f"\ncurrent MIN_SCORE = {rag_settings.min_score}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
