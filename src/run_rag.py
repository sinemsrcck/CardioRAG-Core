"""Single RAG entry point. Offline/local retrieval by default; no SQL writes.

    python -m src.run_rag run --question "..." --archive-dir data/review/rag_runs
    python -m src.run_rag persist --archive FILE --question-type cevaplanabilir --write-sql

--use-llm and --write-sql are separate explicit operations requiring authorization.
"""
import argparse
import json
import os
from pathlib import Path


RERANKER_CHOICES = ("bge-m3", "minilm")
RELEVANCE_CHOICES = ("tiered", "legacy")


def run(question, *, archive_dir, use_llm=False, chunks=None, provenance=None,
        reranker_model=None, relevance_mode="tiered"):
    """reranker_model: None = src.reranker.DEFAULT_RERANKER (çok dilli bge-reranker-v2-m3; answer_question ile aynı)."""
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from src.database import get_connection, load_chunks_for_rag, archive_rag_result
    from src.rag import answer_question, DEFAULT_LLM, LLM_TIMEOUT_SECONDS, MAX_COMPLETION_TOKENS
    from src.reranker import DEFAULT_RERANKER
    reranker_model = reranker_model or DEFAULT_RERANKER
    if reranker_model not in RERANKER_CHOICES:
        raise ValueError("Geçersiz reranker_model.")
    if chunks is None:
        conn = get_connection()
        try:
            conn.start_transaction(readonly=True)
            chunks = load_chunks_for_rag("minilm", connection=conn)
            conn.rollback()
        finally:
            conn.close()
    result = answer_question(question, chunks, embedding_model="minilm", reranker_model=reranker_model,
                            candidate_k=20, top_k=5, use_llm=use_llm, retain_audit=True,
                            relevance_mode=relevance_mode)
    result["run_provenance"] = provenance or {"kind": "rag_pipeline",
        "mode": "llm_requested" if use_llm else "local_retrieval_only",
        "requested_model": DEFAULT_LLM if use_llm else None,
        "timeout_seconds": LLM_TIMEOUT_SECONDS, "sdk_max_retries": 0,
        "completion_budget": MAX_COMPLETION_TOKENS,
        "scientific_verification": False, "expert_approved": False}
    directory = Path(archive_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (result["query_id"] + ".json")
    archive_rag_result(result, path)
    # Never return the restricted audit payload as a public answer.
    return {k: v for k, v in result.items() if k != "audit"}, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    invoke = sub.add_parser("run")
    invoke.add_argument("--question", required=True)
    invoke.add_argument("--archive-dir", type=Path, default=Path("data/review/rag_runs"))
    invoke.add_argument("--use-llm", action="store_true", help="Explicit paid request permission required")
    invoke.add_argument("--reranker", choices=RERANKER_CHOICES, default=None,
                        help="varsayılan: bge-m3 (BAAI/bge-reranker-v2-m3, çok dilli)")
    invoke.add_argument("--relevance-mode", choices=RELEVANCE_CHOICES, default="tiered",
                        help="kanıt filtresi: tiered (varsayılan) ya da eski davranış legacy")
    persist = sub.add_parser("persist")
    persist.add_argument("--archive", type=Path, required=True)
    persist.add_argument("--question-type", choices=("cevaplanabilir", "cevaplanamaz", "celiskili"), required=True)
    persist.add_argument("--write-sql", action="store_true")
    args = parser.parse_args(argv)
    if args.operation == "run":
        result, path = run(args.question, archive_dir=args.archive_dir, use_llm=args.use_llm,
                           reranker_model=args.reranker, relevance_mode=args.relevance_mode)
        print(json.dumps({"query_id": result["query_id"], "status": result["status"],
            "answer": result["answer"], "sources": result["sources"], "archive": str(path)}, ensure_ascii=False))
    else:
        if not args.write_sql:
            parser.error("SQL persistence requires separate explicit --write-sql authorization")
        from src.rag_persistence import save_rag_bundle
        result = json.loads(args.archive.read_text(encoding="utf-8"))
        print(json.dumps(save_rag_bundle(result, question_type=args.question_type), ensure_ascii=False))


if __name__ == "__main__":
    main()
