
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/rag_evaluation_questions_v1.json"
OUTPUT = ROOT / "evaluation/results/rag60_bge_m3"
ARCHIVES = OUTPUT / "archives"

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def load_questions():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) != 60:
        raise ValueError("Beklenen 60 soruluk 'questions' listesi bulunamadı.")
    return questions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--use-llm", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.limit <= 60:
        parser.error("--limit 1 ile 60 arasında olmalı")

    questions = load_questions()

    from src.database import (
        get_connection,
        load_chunks_for_rag,
        archive_rag_result,
    )
    from src.rag import answer_question

    conn = get_connection()
    try:
        conn.start_transaction(readonly=True)
        chunks = load_chunks_for_rag("minilm", connection=conn)
        conn.rollback()
    finally:
        conn.close()

    ARCHIVES.mkdir(parents=True, exist_ok=True)
    summary_path = OUTPUT / "summary.jsonl"

    for index, item in enumerate(questions[:args.limit], start=1):
        question_id = item["question_id"]
        path = ARCHIVES / f"{question_id}.json"

        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                existing.get("evaluation_model") == "bge-m3"
                and existing.get("llm_requested") == args.use_llm
                and existing.get("question") == item["question"]
            ):
                print(f"[{index}/60] SKIP {question_id}", flush=True)
                continue
            raise RuntimeError(f"Mevcut arşiv uyuşmuyor: {path}")

        print(f"[{index}/60] START {question_id}", flush=True)

        try:
            result = answer_question(
                item["question"],
                chunks,
                embedding_model="minilm",
                reranker_model="bge-m3",
                candidate_k=20,
                top_k=5,
                use_llm=args.use_llm,
                retain_audit=True,
            )

            result["evaluation_question_id"] = question_id
            result["evaluation_model"] = "bge-m3"
            result["llm_requested"] = args.use_llm
            result["expected_behavior"] = item.get("expected_behavior")
            result["run_provenance"] = {
                "kind": "rag60_evaluation",
                "embedding_model": "minilm",
                "reranker_model": "bge-m3",
                "llm_requested": args.use_llm,
                "scientific_verification": False,
                "expert_approved": False,
            }

            with path.open("x", encoding="utf-8") as f:
                 json.dump(result, f, ensure_ascii=False, indent=2, default=str)

            status = result.get("status", "unknown")
            with summary_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "question_id": question_id,
                    "status": status,
                    "archive": str(path),
                    "source_count": len(result.get("sources") or []),
                    "citation_valid": (result.get("citation_check") or {}).get("valid"),
                    "claims_verified": result.get("claims_verified"),
                }, ensure_ascii=False) + "\n")

            print(f"[{index}/60] {question_id}: {status}", flush=True)

        except Exception as exc:
            print(f"[{index}/60] ERROR {question_id}: {exc}", flush=True)
            raise


if __name__ == "__main__":
    main()
