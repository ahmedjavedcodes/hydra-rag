"""End-to-end check against the real services (Groq, Pinecone, local models).

    uv run python -m scripts.try_rag inputs/data.docx report.pdf -q "What is ...?" -q "Who ...?"
    uv run python -m scripts.try_rag --url en.wikipedia.org/wiki/Monsoon -q "When does the monsoon start?"

Creates a new session, ingests and indexes the inputs, asks the questions, prints answers with
sources, then deletes the session (files and Pinecone namespace) unless --keep is given.
"""

import argparse

from src.config import require_keys
from src.pipeline.index import Indexer
from src.pipeline.ingest import ingest_files, ingest_url
from src.pipeline.rag_chain import RagChain


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("-q", "--question", action="append", required=True)
    parser.add_argument("--keep", action="store_true", help="keep the session afterwards")
    args = parser.parse_args()

    require_keys()
    indexer, chain = Indexer(), RagChain()
    session_id = None
    if args.files:
        batch = ingest_files(args.files)
        session_id = batch.session_id
        indexer.index_batch(batch)
    for url in args.url:
        batch = ingest_url(url, session_id)
        session_id = batch.session_id
        indexer.index_batch(batch)
    if not session_id:
        parser.error("give at least one file or --url")

    try:
        for question in args.question:
            answer = chain.ask(session_id, question)
            print(f"\nQ: {question}\nA: {answer.answer}")
            for source in answer.sources:
                where = source.source_url or source.filename
                print(f"   [{source.index}] {where}, {source.location} (score {source.score}, rerank {source.rerank_score})")
            for note in answer.notes:
                print(f"   note: {note}")
    finally:
        if args.keep:
            print(f"\nsession kept: {session_id}")
        else:
            indexer.delete_session(session_id)
            print("\nsession deleted")


if __name__ == "__main__":
    main()
