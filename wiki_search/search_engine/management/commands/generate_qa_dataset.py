"""
Django management command to generate QA dataset from HotpotQA data.
Single-threaded implementation with profiling support.
"""

from __future__ import annotations

import cProfile
import io
import json
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pstats
from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, Vocabulary
from search_engine.qa_helpers import calculate_context_size, format_article_for_qa
from search_engine.search import search_hybrid
from search_engine.tokenizer import tokenize_gpt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchConfig:
    """Configuration used for hybrid search invocation."""
    limit: int
    max_candidates: int
    alpha: float
    coverage_bonus: float
    min_term_match_policy: str
    strict_and_filter: bool
    partial_title_boost: bool


@dataclass
class QAEntry:
    id: str
    question: str
    gold_answer: str
    supporting_docs: List[Dict]
    distractor_docs: List[Dict]
    context_size: int


@dataclass
class _QAEntry:
    id: str
    question: str
    gold_answer: str
    supporting_docs: List[Dict]
    distractor_docs: List[Dict]
    context_sizes: Dict[int, Tuple[int, int]]

    def get_all_context_sizes(self) -> Dict[int, QAEntry]:
        return {
            context_size: QAEntry(
                id=self.id,
                question=self.question,
                gold_answer=self.gold_answer,
                supporting_docs=self.supporting_docs,
                distractor_docs=self.distractor_docs[: self.context_sizes[context_size][1]],
                context_size=self.context_sizes[context_size][0],
            )
            for context_size in self.context_sizes
        }


@dataclass
class DistractorSelectionResult:
    """Encapsulate distractor selection outcome and metrics."""
    distractor_docs: List[Dict[str, Any]]
    distractor_tokens: int
    search_queries: List[str]
    fallback_queries: List[str]
    fallback_invocations: int
    search_time: float
    selection_time: float


@dataclass
class EvaluationSample:
    """Structure captured for manual evaluation reporting."""
    qa_id: str
    question: str
    gold_answer: str
    supporting_titles: List[str]
    distractor_titles: List[str]
    supporting_tokens: int
    distractor_tokens: int
    max_context_size: int
    max_context_tokens_used: int
    unfilled_tokens: int
    search_queries: List[str]
    fallback_queries: List[str]
    fallback_invocations: int
    context_tokens_by_target: Dict[int, int]
    distractor_counts_by_target: Dict[int, int]


class Command(BaseCommand):
    help = "Generate QA dataset from HotpotQA data with supporting and distractor documents"

    def add_arguments(self, parser):
        parser.add_argument(
            "--input",
            type=str,
            default="data/raw/hotpot_dev_fullwiki_v1.json",
            help="Path to input HotpotQA JSON file",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default="data/processed",
            help="Directory to save output JSON files",
        )
        parser.add_argument(
            "--context-sizes",
            nargs="+",
            type=int,
            default=[8000, 32000, 128000],
            help="Context size limits in tokens (default: 8000 32000 128000)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of QA entries to process (default: 100)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging",
        )
        parser.add_argument(
            "--debug",
            action="store_true",
            help="Enable debug logging for troubleshooting",
        )
        parser.add_argument(
            "--profile",
            action="store_true",
            help="Enable cProfile profiling and save results",
        )
        parser.add_argument(
            "--search-limit",
            type=int,
            default=60,
            help="Hybrid search result cap per primary query (default: 60)",
        )
        parser.add_argument(
            "--max-candidates",
            type=int,
            default=2000,
            help="Total inverted-index candidates to consider per primary query (default: 2000)",
        )
        parser.add_argument(
            "--fallback-limit",
            type=int,
            default=None,
            help="Override fallback hybrid search result cap (default: 2x --search-limit)",
        )
        parser.add_argument(
            "--fallback-max-candidates",
            type=int,
            default=None,
            help="Override fallback inverted-index candidate cap (default: 4x --max-candidates)",
        )
        parser.add_argument(
            "--max-fallback-queries",
            type=int,
            default=2,
            help="Maximum number of fallback hybrid search executions per QA entry (default: 2)",
        )
        parser.add_argument(
            "--alpha",
            type=float,
            default=0.85,
            help="Hybrid search TF-IDF weighting factor alpha (default: 0.85)",
        )
        parser.add_argument(
            "--coverage-bonus",
            type=float,
            default=0.15,
            help="Coverage bonus weight forwarded to hybrid search (default: 0.15)",
        )
        parser.add_argument(
            "--min-term-match-policy",
            type=str,
            default="balanced",
            choices=["balanced", "strict", "len2_strict"],
            help="Hybrid search min-term-match policy (default: balanced)",
        )
        parser.add_argument(
            "--strict-and-filter",
            action="store_true",
            help="Enforce strict AND filtering for short queries",
        )
        parser.add_argument(
            "--disable-partial-title-boost",
            action="store_true",
            help="Disable partial title boost in hybrid search",
        )
        parser.add_argument(
            "--min-distractor-tokens",
            type=int,
            default=64,
            help="Minimum token count required for a distractor article (default: 64)",
        )
        parser.add_argument(
            "--evaluation-report",
            type=str,
            default="data/profiling/qa_distractor_report.json",
            help="Path for manual evaluation report output (default: data/profiling/qa_distractor_report.json)",
        )
        parser.add_argument(
            "--no-evaluation-report",
            action="store_true",
            help="Disable evaluation report generation",
        )
        parser.add_argument(
            "--evaluation-sample",
            type=int,
            default=12,
            help="Maximum number of QA entries to capture for manual evaluation (default: 12)",
        )
        parser.add_argument(
            "--profile-output",
            type=str,
            default="data/profiling/qa_dataset_generation.prof",
            help="Output path for raw cProfile statistics",
        )
        parser.add_argument(
            "--profile-summary",
            type=str,
            default="data/profiling/qa_dataset_generation_summary.txt",
            help="Output path for textual profile summary",
        )

    def handle(self, *args, **options):
        log_level = logging.DEBUG if (options["verbose"] or options["debug"]) else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler("generate_qa_dataset.log"),
            ],
        )

        input_path = Path(options["input"])
        output_dir = Path(options["output_dir"])
        context_sizes = options["context_sizes"]
        limit = options.get("limit")
        enable_profiling = options["profile"]

        search_limit = options["search_limit"]
        max_candidates = options["max_candidates"]
        alpha = options["alpha"]
        coverage_bonus = options["coverage_bonus"]
        min_term_match_policy = options["min_term_match_policy"]
        strict_and_filter = options["strict_and_filter"]
        partial_title_boost = not options["disable_partial_title_boost"]
        min_distractor_tokens = max(0, options["min_distractor_tokens"])
        evaluation_sample_size = max(0, options["evaluation_sample"])
        fallback_limit = options["fallback_limit"] if options["fallback_limit"] is not None else search_limit * 2
        fallback_max_candidates = (
            options["fallback_max_candidates"] if options["fallback_max_candidates"] is not None else max_candidates * 4
        )
        max_fallback_queries = max(0, options["max_fallback_queries"])

        evaluation_report_path: Optional[Path]
        if options["no_evaluation_report"]:
            evaluation_report_path = None
        else:
            evaluation_report_path = Path(options["evaluation_report"])
            evaluation_report_path.parent.mkdir(parents=True, exist_ok=True)

        profile_output_path = Path(options["profile_output"])
        profile_output_path.parent.mkdir(parents=True, exist_ok=True)
        profile_summary_path = Path(options["profile_summary"])
        profile_summary_path.parent.mkdir(parents=True, exist_ok=True)

        search_config = SearchConfig(
            limit=search_limit,
            max_candidates=max_candidates,
            alpha=alpha,
            coverage_bonus=coverage_bonus,
            min_term_match_policy=min_term_match_policy,
            strict_and_filter=strict_and_filter,
            partial_title_boost=partial_title_boost,
        )
        fallback_config = SearchConfig(
            limit=fallback_limit,
            max_candidates=max(fallback_max_candidates, fallback_limit * 5),
            alpha=alpha,
            coverage_bonus=max(coverage_bonus / 2, 0.0),
            min_term_match_policy="balanced",
            strict_and_filter=False,
            partial_title_boost=True,
        )

        if not input_path.exists():
            raise CommandError(f"Input file not found: {input_path}")

        inverted_count = InvertedIndex.objects.count()
        vocab_count = Vocabulary.objects.count()

        if vocab_count == 0:
            raise CommandError("Vocabulary is empty. Please run 'python manage.py build_tfidf_simple' first.")

        if inverted_count == 0:
            raise CommandError("Inverted index is empty. Please run 'python manage.py build_tfidf_simple' first.")

        self.stdout.write(f"Search index validation: {vocab_count} vocabulary terms, {inverted_count} inverted index entries")

        output_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"Loading HotpotQA data from: {input_path}")

        try:
            with open(input_path, "r", encoding="utf-8") as f:
                qa_data = json.load(f)
        except Exception as exc:
            raise CommandError(f"Failed to load input file: {exc}") from exc

        if limit:
            qa_data = qa_data[:limit]
            self.stdout.write(f"Limited to {limit} entries for testing")

        self.stdout.write(f"Processing {len(qa_data)} QA entries (single-threaded)...")

        start_preprocessing = time.perf_counter()
        titles = self.collect_article_titles(qa_data)
        article_cache = self.batch_fetch_articles(titles)
        token_cache = self.precompute_token_counts(article_cache)
        preprocessing_time = time.perf_counter() - start_preprocessing
        self.stdout.write(f"Pre-processing completed in {preprocessing_time:.2f}s")

        timing_stats: Dict[str, List[float]] = defaultdict(list)
        evaluation_samples: List[EvaluationSample]

        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()

            processing_start = time.perf_counter()
            results, timing_stats, stats, evaluation_samples = self.process_qa_entries(
                qa_data=qa_data,
                context_sizes=context_sizes,
                article_cache=article_cache,
                token_cache=token_cache,
                search_config=search_config,
                fallback_config=fallback_config,
                min_distractor_tokens=min_distractor_tokens,
                evaluation_sample_size=evaluation_sample_size,
                max_fallback_queries=max_fallback_queries,
            )
            processing_time = time.perf_counter() - processing_start

            profiler.disable()
            profiler.dump_stats(profile_output_path)
            self.stdout.write(f"\nProfile saved to: {profile_output_path}")

            self.stdout.write("\nTop 30 time-consuming functions:")
            profile_stats = pstats.Stats(profiler, stream=self.stdout)
            profile_stats.sort_stats("cumulative")
            profile_stats.print_stats(30)

            summary_stream = io.StringIO()
            summary_stats = pstats.Stats(profiler, stream=summary_stream)
            summary_stats.sort_stats("cumulative")
            summary_stats.print_stats(50)
            profile_summary_path.write_text(summary_stream.getvalue(), encoding="utf-8")
            self.stdout.write(f"Profile summary written to: {profile_summary_path}")
        else:
            processing_start = time.perf_counter()
            results, timing_stats, stats, evaluation_samples = self.process_qa_entries(
                qa_data=qa_data,
                context_sizes=context_sizes,
                article_cache=article_cache,
                token_cache=token_cache,
                search_config=search_config,
                fallback_config=fallback_config,
                min_distractor_tokens=min_distractor_tokens,
                evaluation_sample_size=evaluation_sample_size,
                max_fallback_queries=max_fallback_queries,
            )
            processing_time = time.perf_counter() - processing_start

        timing_stats.setdefault("preprocessing", []).append(preprocessing_time)
        self._print_timing_stats(timing_stats)

        processed = stats.get("processed", 0)
        throughput = processed / processing_time if processing_time > 0 else 0.0
        self.stdout.write(f"\nProcessing time (entries loop): {processing_time:.2f}s")
        self.stdout.write(f"Throughput: {throughput:.2f} entries/sec")

        min_required_throughput = 5.0
        throughput_threshold_count = 20
        if processed >= throughput_threshold_count and throughput < min_required_throughput:
            warning_message = (
                f"Throughput below target: {throughput:.2f} < {min_required_throughput:.2f} "
                f"entries/sec over {processed} processed entries."
            )
            logger.warning(warning_message)
            self.stdout.write(self.style.WARNING(warning_message))

        if limit:
            remaining = max(0, len(qa_data) - processed)
            eta_s = (remaining / throughput) if throughput > 0 else float("inf")
            if eta_s != float("inf"):
                self.stdout.write(f"ETA for remaining {remaining} entries: {eta_s / 60:.2f} min")

        self.generate_output_files(results, output_dir, context_sizes)

        self._write_evaluation_report(
            report_path=evaluation_report_path,
            evaluation_samples=evaluation_samples,
            search_config=search_config,
            fallback_config=fallback_config,
            min_distractor_tokens=min_distractor_tokens,
        )

        self.stdout.write(self.style.SUCCESS("\nQA dataset generation completed!"))

    def collect_article_titles(self, qa_data: List[Dict]) -> Set[str]:
        """Collect all unique article titles needed from QA data."""
        titles = set()
        for entry in qa_data:
            supporting_facts = entry.get("supporting_facts", [])
            for fact in supporting_facts:
                if len(fact) >= 1:
                    titles.add(fact[0])

        self.stdout.write(f"Collected {len(titles)} unique article titles from QA data")
        return titles

    def batch_fetch_articles(self, titles: Set[str]) -> Dict[str, Article]:
        """Batch fetch all articles and build case-insensitive lookup dict."""
        self.stdout.write("Fetching articles in batch...")
        articles = Article.objects.filter(title__in=titles)
        article_cache = {article.title.lower(): article for article in articles}

        fetched_titles = {article.title for article in articles}
        missing_titles = titles - fetched_titles

        if missing_titles:
            logger.warning(f"Missing {len(missing_titles)} articles from database: {list(missing_titles)[:10]}")

        self.stdout.write(f"Fetched {len(article_cache)} articles successfully")
        return article_cache

    def precompute_token_counts(self, article_cache: Dict[str, Article]) -> Dict[int, int]:
        """Pre-compute token counts for all cached articles."""
        self.stdout.write("Pre-computing token counts...")
        token_cache: Dict[int, int] = {}

        for article in tqdm(article_cache.values(), desc="Computing token counts"):
            total_tokens = self._compute_article_token_total(article)
            token_cache[article.id] = total_tokens
            article_cache[article.title.lower()] = article

        self.stdout.write(f"Pre-computed token counts for {len(token_cache)} articles")
        return token_cache

    def process_qa_entries(
        self,
        qa_data: List[Dict],
        context_sizes: List[int],
        article_cache: Dict[str, Article],
        token_cache: Dict[int, int],
        search_config: SearchConfig,
        fallback_config: SearchConfig,
        min_distractor_tokens: int,
        evaluation_sample_size: int,
        max_fallback_queries: int,
    ) -> Tuple[Dict[int, List[Dict]], Dict[str, List[float]], Dict[str, int], List[EvaluationSample]]:
        """Process QA entries in single-threaded manner with timing instrumentation."""
        results: Dict[int, List[Dict[str, Any]]] = {size: [] for size in context_sizes}
        stats = {
            "total": len(qa_data),
            "processed": 0,
            "skipped_missing_articles": 0,
            "skipped_context_overflow": 0,
            "errors": 0,
        }
        timing_stats: Dict[str, List[float]] = defaultdict(list)
        evaluation_samples: List[EvaluationSample] = []

        target_sizes = sorted(set(context_sizes))
        max_context_tokens = max(target_sizes) if target_sizes else 0

        for entry_data in tqdm(qa_data, desc="Processing QA entries"):
            entry_start = time.perf_counter()

            try:
                qa_id = entry_data.get("_id", "")
                question = entry_data.get("question", "")
                answer = entry_data.get("answer", "")
                supporting_facts = entry_data.get("supporting_facts", [])

                supporting_docs: List[Dict[str, Any]] = []
                missing_articles: List[str] = []

                raw_titles: List[str] = []
                seen_titles_lower: Set[str] = set()
                for fact in supporting_facts:
                    if len(fact) >= 1:
                        title = fact[0]
                        raw_titles.append(title)
                        title_lower = title.lower()
                        if title_lower not in seen_titles_lower:
                            seen_titles_lower.add(title_lower)

                if logger.isEnabledFor(logging.DEBUG) and len(seen_titles_lower) < len(raw_titles):
                    logger.debug(f"QA {qa_id}: deduped supporting titles from {len(raw_titles)} to {len(seen_titles_lower)}")

                for title_lower in seen_titles_lower:
                    article = article_cache.get(title_lower)
                    if article:
                        supporting_docs.append(format_article_for_qa(article))
                    else:
                        missing_title = next((t for t in raw_titles if t.lower() == title_lower), title_lower)
                        missing_articles.append(missing_title)

                if missing_articles:
                    stats["skipped_missing_articles"] += 1
                    logger.warning(f"Skipping {qa_id}: Missing articles {missing_articles}")
                    continue

                supporting_tokens = sum(
                    token_cache.get(article_cache[doc["title"].lower()].id, 0) for doc in supporting_docs
                )

                selection_result = self._select_distractors(
                    qa_id=qa_id,
                    question=question,
                    answer=answer,
                    supporting_facts=supporting_facts,
                    supporting_docs=supporting_docs,
                    supporting_tokens=supporting_tokens,
                    article_cache=article_cache,
                    token_cache=token_cache,
                    max_context_tokens=max_context_tokens,
                    search_config=search_config,
                    fallback_config=fallback_config,
                    min_distractor_tokens=min_distractor_tokens,
                    max_fallback_queries=max_fallback_queries,
                )
                timing_stats["search_operations"].append(selection_result.search_time)
                timing_stats["distractor_selection"].append(selection_result.selection_time)

                distractor_docs = selection_result.distractor_docs
                distractor_tokens = selection_result.distractor_tokens

                if supporting_tokens > max_context_tokens:
                    stats["skipped_context_overflow"] += 1
                    logger.info(
                        "Skipping %s: supporting docs exceed max context (%d > %d)",
                        qa_id,
                        supporting_tokens,
                        max_context_tokens,
                    )
                    continue

                estimation_start = time.perf_counter()
                context_sizes_map = self._estimate_context_sizes(
                    supporting_tokens=supporting_tokens,
                    distractor_docs=distractor_docs,
                    target_sizes=target_sizes,
                    article_cache=article_cache,
                    token_cache=token_cache,
                )
                timing_stats["context_finalize_estimation"].append(time.perf_counter() - estimation_start)

                qa_entry = _QAEntry(
                    id=qa_id,
                    question=question,
                    gold_answer=answer,
                    supporting_docs=supporting_docs,
                    distractor_docs=distractor_docs,
                    context_sizes=context_sizes_map,
                )
                stats["processed"] += 1

                context_entries = qa_entry.get_all_context_sizes()

                exact_start = time.perf_counter()
                for context_size in context_sizes:
                    if context_size not in context_entries:
                        continue
                    entry_dict = asdict(context_entries[context_size])
                    supporting = entry_dict["supporting_docs"]
                    distractors = entry_dict["distractor_docs"]

                    max_fit_distractors: List[Dict[str, Any]] = []
                    for end_index in range(len(distractors) + 1):
                        candidate_distractors = distractors[:end_index]
                        token_count = calculate_context_size(
                            supporting_docs=supporting,
                            distractor_docs=candidate_distractors,
                            token_lookup=lambda doc: token_cache.get(
                                article_cache.get(doc["title"].lower(), Article()).id  # type: ignore[arg-type]
                                if article_cache.get(doc["title"].lower()) else 0,
                                0,
                            ),
                        )
                        if token_count <= context_size:
                            max_fit_distractors = candidate_distractors
                        else:
                            break

                    base_token_count = calculate_context_size(
                        supporting_docs=supporting,
                        distractor_docs=[],
                        token_lookup=lambda doc: token_cache.get(
                            article_cache.get(doc["title"].lower(), Article()).id  # type: ignore[arg-type]
                            if article_cache.get(doc["title"].lower()) else 0,
                            0,
                        ),
                    )
                    if base_token_count > context_size:
                        logger.info(
                            "Skipping %s: supporting docs exceed context_size cap %d [tokens=%d]",
                            qa_id,
                            context_size,
                            base_token_count,
                        )
                        continue

                    entry_dict_capped = dict(entry_dict)
                    entry_dict_capped["distractor_docs"] = max_fit_distractors
                    entry_dict_capped["context_size"] = calculate_context_size(
                        supporting_docs=supporting,
                        distractor_docs=max_fit_distractors,
                        token_lookup=lambda doc: token_cache.get(
                            article_cache.get(doc["title"].lower(), Article()).id  # type: ignore[arg-type]
                            if article_cache.get(doc["title"].lower()) else 0,
                            0,
                        ),
                    )
                    results[context_size].append(entry_dict_capped)
                timing_stats["context_finalize_exact"].append(time.perf_counter() - exact_start)

                if len(evaluation_samples) < evaluation_sample_size:
                    context_tokens_by_target = {size: ctx[0] for size, ctx in context_sizes_map.items()}
                    distractor_counts_by_target = {size: ctx[1] for size, ctx in context_sizes_map.items()}
                    max_context_size = max_context_tokens
                    max_context_tokens_used = context_tokens_by_target.get(max_context_size, supporting_tokens)
                    evaluation_samples.append(
                        EvaluationSample(
                            qa_id=qa_id,
                            question=question,
                            gold_answer=answer,
                            supporting_titles=[doc["title"] for doc in supporting_docs],
                            distractor_titles=[doc["title"] for doc in distractor_docs],
                            supporting_tokens=supporting_tokens,
                            distractor_tokens=distractor_tokens,
                            max_context_size=max_context_size,
                            max_context_tokens_used=max_context_tokens_used,
                            unfilled_tokens=max(0, max_context_size - max_context_tokens_used),
                            search_queries=selection_result.search_queries,
                            fallback_queries=selection_result.fallback_queries,
                            fallback_invocations=selection_result.fallback_invocations,
                            context_tokens_by_target=context_tokens_by_target,
                            distractor_counts_by_target=distractor_counts_by_target,
                        )
                    )

            except Exception as exc:
                stats["errors"] += 1
                logger.error("Error processing entry %s: %s", entry_data.get("_id", "unknown"), exc)
                if logger.isEnabledFor(logging.DEBUG):
                    import traceback

                    logger.debug(traceback.format_exc())

            timing_stats["entry_total"].append(time.perf_counter() - entry_start)

        self.stdout.write("\nProcessing Statistics:")
        self.stdout.write(f"  Total entries: {stats['total']}")
        self.stdout.write(f"  Successfully processed: {stats['processed']}")
        self.stdout.write(f"  Skipped (missing articles): {stats['skipped_missing_articles']}")
        self.stdout.write(f"  Skipped (context overflow): {stats['skipped_context_overflow']}")
        self.stdout.write(f"  Errors: {stats['errors']}")

        return results, timing_stats, stats, evaluation_samples

    def _select_distractors(
        self,
        qa_id: str,
        question: str,
        answer: str,
        supporting_facts: List[Any],
        supporting_docs: List[Dict[str, Any]],
        supporting_tokens: int,
        article_cache: Dict[str, Article],
        token_cache: Dict[int, int],
        max_context_tokens: int,
        search_config: SearchConfig,
        fallback_config: SearchConfig,
        min_distractor_tokens: int,
        max_fallback_queries: int,
    ) -> DistractorSelectionResult:
        """Select distractor documents aiming to fill the context budget."""
        supporting_titles = {doc["title"] for doc in supporting_docs}

        search_queries: List[str] = []
        search_results: List[List[Tuple[Article, float]]] = []

        search_start = time.perf_counter()
        for fact in supporting_facts:
            if len(fact) == 0:
                continue
            title = fact[0]
            if not title:
                continue
            if title in search_queries:
                continue
            search_queries.append(title)
            search_results.append(list(self._cached_search(title, search_config)))
        search_time = time.perf_counter() - search_start

        fallback_candidates: List[str] = []
        seen_queries = set(search_queries)
        fallback_invocations = 0

        if question:
            fallback_candidates.append(question)
            seen_queries.add(question)

        if answer and answer.lower() not in question.lower():
            fallback_candidates.append(f"{question} {answer}")
            seen_queries.add(f"{question} {answer}")

        if supporting_titles:
            joined_titles = " ".join(sorted(list(supporting_titles))[:3])
            if joined_titles not in seen_queries:
                fallback_candidates.append(joined_titles)
                seen_queries.add(joined_titles)

        search_result_indices = [0] * len(search_results)
        distractor_docs: List[Dict[str, Any]] = []
        distractor_titles: Set[str] = set()
        fallback_queries_used: List[str] = []
        current_distractor_tokens = 0

        selection_start = time.perf_counter()
        while supporting_tokens + current_distractor_tokens < max_context_tokens:
            candidate_added = False

            for result_index, results in enumerate(search_results):
                while search_result_indices[result_index] < len(results):
                    article, score = results[search_result_indices[result_index]]
                    search_result_indices[result_index] += 1

                    if article.title in supporting_titles or article.title in distractor_titles:
                        continue

                    article_tokens = token_cache.get(article.id)
                    if article_tokens is None:
                        article_tokens = self._count_and_cache_article_tokens(article, token_cache, article_cache)

                    if article_tokens < min_distractor_tokens:
                        logger.debug(
                            "Skipping distractor %s for %s due to min token threshold (%d < %d)",
                            article.title,
                            qa_id,
                            article_tokens,
                            min_distractor_tokens,
                        )
                        continue

                    if supporting_tokens + current_distractor_tokens + article_tokens > max_context_tokens:
                        continue

                    distractor_docs.append(format_article_for_qa(article))
                    distractor_titles.add(article.title)
                    current_distractor_tokens += article_tokens
                    candidate_added = True
                    logger.debug(
                        "Added distractor: %s (score: %.4f, tokens: %d)",
                        article.title,
                        score,
                        article_tokens,
                    )
                    break

                if candidate_added:
                    break

            if candidate_added:
                continue

            if fallback_invocations >= max_fallback_queries or not fallback_candidates:
                break

            next_results_added = False
            while fallback_candidates and fallback_invocations < max_fallback_queries:
                fallback_query = fallback_candidates.pop(0)
                fallback_results = list(self._cached_search(fallback_query, fallback_config))
                fallback_invocations += 1
                if not fallback_results:
                    continue
                fallback_queries_used.append(fallback_query)
                search_queries.append(fallback_query)
                search_results.append(fallback_results)
                search_result_indices.append(0)
                next_results_added = True
                break

            if not next_results_added:
                break

        selection_time = time.perf_counter() - selection_start
        return DistractorSelectionResult(
            distractor_docs=distractor_docs,
            distractor_tokens=current_distractor_tokens,
            search_queries=search_queries,
            fallback_queries=fallback_queries_used,
            fallback_invocations=fallback_invocations,
            search_time=search_time,
            selection_time=selection_time,
        )

    def _estimate_context_sizes(
        self,
        supporting_tokens: int,
        distractor_docs: List[Dict[str, Any]],
        target_sizes: List[int],
        article_cache: Dict[str, Article],
        token_cache: Dict[int, int],
    ) -> Dict[int, Tuple[int, int]]:
        """Estimate context budgets for each target size."""
        context_sizes_map: Dict[int, Tuple[int, int]] = {}
        for target_size in target_sizes:
            distractor_tokens_used = 0
            distractor_count = 0
            for doc in distractor_docs:
                article = article_cache.get(doc["title"].lower())
                if article is None:
                    continue
                doc_tokens = token_cache.get(article.id)
                if doc_tokens is None:
                    doc_tokens = self._count_and_cache_article_tokens(article, token_cache, article_cache)
                if supporting_tokens + distractor_tokens_used + doc_tokens <= target_size:
                    distractor_tokens_used += doc_tokens
                    distractor_count += 1
                else:
                    break
            actual_context_size = supporting_tokens + distractor_tokens_used
            context_sizes_map[target_size] = (actual_context_size, distractor_count)
        return context_sizes_map

    def _count_and_cache_article_tokens(
        self,
        article: Article,
        token_cache: Dict[int, int],
        article_cache: Dict[str, Article],
    ) -> int:
        """Compute total tokens for an article and persist in caches."""
        total_tokens = self._compute_article_token_total(article)
        token_cache[article.id] = total_tokens
        article_cache[article.title.lower()] = article
        return total_tokens

    def _get_article_body_tokens(self, article: Article) -> int:
        """Estimate body token count using cached paragraph token counts when available."""
        paragraph_counts = getattr(article, "paragraph_token_counts", None)
        if paragraph_counts:
            try:
                return int(sum(paragraph_counts))
            except (TypeError, ValueError):
                logger.debug("Invalid paragraph_token_counts for article %s; falling back to tokenization", article.id)

        paragraphs = getattr(article, "plain_text_paragraphs", [])
        if not paragraphs:
            return 0

        serialized_text = "\n\n".join(paragraphs)
        return len(tokenize_gpt(serialized_text))

    def _compute_article_token_total(self, article: Article) -> int:
        """Compute total token count (title + body) for an article."""
        body_tokens = self._get_article_body_tokens(article)
        title_tokens = len(tokenize_gpt(article.title))
        return body_tokens + title_tokens

    def _print_timing_stats(self, timing_stats: Dict[str, List[float]]) -> None:
        """Print timing statistics summary with descriptive metrics."""
        self.stdout.write("\nTiming Statistics:")
        for operation, times in timing_stats.items():
            if not times:
                continue
            total = sum(times)
            mean_ms = statistics.mean(times) * 1000
            median_ms = statistics.median(times) * 1000
            if len(times) >= 2:
                percentile_95_ms = statistics.quantiles(times, n=100, method="inclusive")[94] * 1000
            else:
                percentile_95_ms = times[0] * 1000
            min_ms = min(times) * 1000
            max_ms = max(times) * 1000

            self.stdout.write(f"  {operation}:")
            self.stdout.write(f"    Count: {len(times)}")
            self.stdout.write(f"    Total: {total:.2f}s")
            self.stdout.write(f"    Average: {mean_ms:.2f}ms")
            self.stdout.write(f"    Median: {median_ms:.2f}ms")
            self.stdout.write(f"    P95: {percentile_95_ms:.2f}ms")
            self.stdout.write(f"    Min: {min_ms:.2f}ms")
            self.stdout.write(f"    Max: {max_ms:.2f}ms")

    def _write_evaluation_report(
        self,
        report_path: Optional[Path],
        evaluation_samples: List[EvaluationSample],
        search_config: SearchConfig,
        fallback_config: SearchConfig,
        min_distractor_tokens: int,
    ) -> None:
        """Persist manual evaluation report if requested."""
        if report_path is None:
            self.stdout.write("Evaluation report generation disabled.")
            return

        if not evaluation_samples:
            self.stdout.write("Evaluation report skipped (no samples collected).")
            return

        average_fill_ratio = statistics.mean(
            sample.max_context_tokens_used / sample.max_context_size
            if sample.max_context_size > 0 else 0.0
            for sample in evaluation_samples
        )
        average_distractor_count = statistics.mean(
            sample.distractor_counts_by_target.get(sample.max_context_size, 0)
            for sample in evaluation_samples
        )
        average_fallback_invocations = statistics.mean(
            sample.fallback_invocations for sample in evaluation_samples
        )

        payload = {
            "metadata": {
                "sample_size": len(evaluation_samples),
                "average_fill_ratio": average_fill_ratio,
                "average_distractor_count_at_max": average_distractor_count,
                "average_fallback_invocations": average_fallback_invocations,
                "min_distractor_tokens": min_distractor_tokens,
                "search_config": asdict(search_config),
                "fallback_config": asdict(fallback_config),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            "samples": [asdict(sample) for sample in evaluation_samples],
        }
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Evaluation report written to: {report_path}"))

    def generate_output_files(self, results: Dict[int, List[Dict]], output_dir: Path, context_sizes: List[int]):
        """Generate output JSON files for each context size."""
        for context_size in context_sizes:
            output_file = output_dir / f"qa_dataset_{context_size}.json"
            self.stdout.write(f"\nWriting {len(results[context_size])} entries to {output_file}")

            try:
                io_start = time.perf_counter()
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(results[context_size], f, indent=2, ensure_ascii=False)

                self.stdout.write(f"  Context size: {context_size} tokens")
                self.stdout.write(f"  Entries: {len(results[context_size])}")
                self.stdout.write(f"  Write time: {time.perf_counter() - io_start:.2f}s")
            except Exception as exc:
                raise CommandError(f"Failed to write output file {output_file}: {exc}") from exc

    @staticmethod
    @lru_cache(maxsize=4096)
    def _cached_search(query: str, config: SearchConfig) -> Tuple[Tuple[Article, float], ...]:
        """Shared cached wrapper around search_hybrid to reduce repeated DB hits."""
        return tuple(
            search_hybrid(
                query,
                limit=config.limit,
                alpha=config.alpha,
                max_candidates=config.max_candidates,
                coverage_bonus_weight=config.coverage_bonus,
                strict_and_filter=config.strict_and_filter,
                min_term_match_policy=config.min_term_match_policy,
                enable_partial_title_boost=config.partial_title_boost,
            )
        )
