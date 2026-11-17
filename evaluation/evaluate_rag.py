
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from src.rag import answer_question
from evaluation.evidence_threshold import analyze_thresholds

OUTPUT_DIR = Path("evaluation/results")


def evaluate_questions(
    questions,
    chunks,
    use_llm=False,
    embedding_model="minilm",
    reranker_model="bge-m3",
):
    """
    RAG sistemini test eder.
    chunks:
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"rag_results_{timestamp}.csv"

    results = []
    threshold_results = []

    for index, item in enumerate(questions, start=1):
        question = item["question"]
        start = time.perf_counter()

        try:
            result = answer_question(
                question=question,
                chunks=chunks,
                embedding_model=embedding_model,
                reranker_model=reranker_model,
                use_llm=use_llm,
            )

            record = {
                "id": item.get("id", index),
                "category": item.get("category", ""),
                "question": question,
                "expected_behavior": item.get(
                    "expected_behavior", ""
                ),
                "answer": result["answer"],
                "status": result["status"],
                "response_time_seconds": result[
                    "response_time_seconds"
                ],
                "source_count": result["selected_count"],
                "unique_article_count": len({
                    source["document_id"]
                    for source in result["sources"]
                }),
                "citation_valid": (
                    result["citation_check"]["valid"]
                    if result["citation_check"]
                    else None
                ),
                "sources_json": json.dumps(
                    result["sources"],
                    ensure_ascii=False,
                ),
                "evidence_check_json": json.dumps(
                    result.get("evidence_check"),
                    ensure_ascii=False,
                ),
                
                "top_rerank_score": result.get("top_rerank_score"),

                "error": "",
            }
            threshold_results.append({
                "question": question,
                "expected_behavior": item.get("expected_behavior", ""),
                "top_rerank_score": result.get("top_rerank_score"),
            })

        except Exception as exc:
            record = {
                "id": item.get("id", index),
                "category": item.get("category", ""),
                "question": question,
                "expected_behavior": item.get(
                    "expected_behavior", ""
                ),
                "answer": "",
                "status": "error",
                "response_time_seconds": round(
                    time.perf_counter() - start, 3
                ),
                "source_count": 0,
                "unique_article_count": 0,
                "citation_valid": None,
                "top_rerank_score": None,
                "sources_json": "[]",
                "evidence_check_json": "null",
                "error": str(exc),
            }

        results.append(record)
        print(
            f"[{index}/{len(questions)}] "
            f"{record['status']} | {question[:60]}"
        )

    if results:
        with output_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=list(results[0].keys()),
            )
            writer.writeheader()
            writer.writerows(results)

    threshold_analysis = analyze_thresholds(threshold_results)

    print("\n========== EŞİK ANALİZİ ==========")
    print(json.dumps(
        threshold_analysis,
        ensure_ascii=False,
        indent=2,
    ))

    print(f"Sonuç dosyası: {output_path}")
    return results


def load_questions(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))
