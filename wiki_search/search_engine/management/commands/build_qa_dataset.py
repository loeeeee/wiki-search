"""Django management command to build QA datasets for LLM training."""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, Vocabulary
from search_engine.qa_helpers import calculate_context_size, format_article_for_qa
from search_engine.search import search_hybrid
from search_engine.tokenizer import tokenize_gpt
from search_engine.utils.profiler import ProfileManager, phase_timer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridParams:
    """Parameters forwarded to hybrid search."""
    limit: int
    max_candidates: int
    alpha: float
    coverage_bonus: float
    min_term_match_policy: str
    strict_and_filter: bool
    enable_partial_title_boost: bool


@dataclass(frozen=True)
class ThroughputTarget:
    """Throughput guardrail configuration."""
    threshold: float = 20.0
    minimum_samples: int = 20


@dataclass
class QADatasetEntry:
    """Serializable QA dataset record."""
    id: str
    question: str
    gold_answer: str
    supporting_docs: List[Dict[str, str]]
    distractor_docs: List[Dict[str, str]]
    context_size: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "gold_answer": self.gold_answer,
            "supporting_docs": self.supporting_docs,
            "distractor_docs": self.distractor_docs,
            "context_size": self.context_size,
        }


@dataclass
class DistractorOutcome:
    """Result of distractor selection for a QA item."""
    docs: List[Dict[str, str]]
    doc_token_counts: List[int]
    total_tokens: int
    queries_executed: List[str]
    fallback_queries_used: List[str]
    fallback_invocations: int
    search_time: float
    selection_time: float


@dataclass
class EvaluationSample:
    """Evaluation payload for manual inspection."""
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
    queries_executed: List[str]
    fallback_queries: List[str]
    fallback_invocations: int
    context_tokens_by_cap: Dict[int, int]
    distractor_counts_by_cap: Dict[int, int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArticleCache:
    """Cache for articles and their token counts."""
    by_title: Dict[str, Article] = field(default_factory=dict)
    by_id: Dict[int, Article] = field(default_factory=dict)
    token_by_id: Dict[int, int] = field(default_factory=dict)

    def attach_article(self, article: Article) -> None:
        key = article.title.lower()
        self.by_title[key] = article
        self.by_id[article.id] = article
        if article.id not in self.token_by_id:
            self.token_by_id[article.id] = compute_article_tokens(article)

    def attach_articles(self, articles: Iterable[Article]) -> None:
        for article in articles:
            self.attach_article(article)

    def get_by_title(self, title: str) -> Optional[Article]:
        return self.by_title.get(title.lower())

    def ensure_by_ids(self, ids: Sequence[int]) -> None:
        missing_ids = [article_id for article_id in ids if article_id not in self.by_id]
        if not missing_ids:
            return
        for article in Article.objects.filter(id__in=missing_ids):
            self.attach_article(article)

    def tokens_for_article(self, article: Article) -> int:
        if article.id not in self.token_by_id:
            self.token_by_id[article.id] = compute_article_tokens(article)
        self.attach_article(article)
        return self.token_by_id[article.id]

    def tokens_for_doc(self, doc: Dict[str, str]) -> int:
        article = self.get_by_title(doc["title"])
        if article:
            return self.tokens_for_article(article)

        text_tokens = len(tokenize_gpt(doc.get("text", "")))
        title_tokens = len(tokenize_gpt(doc.get("title", "")))
        return title_tokens + text_tokens

    def format_article(self, article: Article) -> Dict[str, str]:
        self.attach_article(article)
        return format_article_for_qa(article)


def compute_article_tokens(article: Article) -> int:
    """Compute total GPT token count (title + body) for the provided article."""
    title_tokens = len(tokenize_gpt(article.title))

    paragraph_counts = getattr(article, "paragraph_token_counts", None)
    if paragraph_counts:
        try:
            body_tokens = int(sum(paragraph_counts))
        except (TypeError, ValueError):
            body_tokens = 0
    else:
        body_tokens = 0

    if body_tokens == 0:
        paragraphs = getattr(article, "plain_text_paragraphs", []) or []
        if paragraphs:
            serialized = "\n\n".join(paragraphs)
            body_tokens = len(tokenize_gpt(serialized))

    return title_tokens + body_tokens


class Command(BaseCommand):
    help = "Build QA datasets with supporting and distractor documents using hybrid search."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._search_cache: Dict[Tuple[str, HybridParams], List[Tuple[int, float]]] = {}

    def add_arguments(self, parser) -> None:
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
            help="Directory where dataset JSON files will be written",
        )
        parser.add_argument(
            "--context-sizes",
            nargs="+",
            type=int,
            default=[8000, 32000, 128000],
            help="Context size caps in GPT tokens (default: 8000 32000 128000)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit the number of QA entries processed (default: all)",
        )
        parser.add_argument(
            "--min-distractor-tokens",
            type=int,
            default=64,
            help="Minimum token count required for a distractor article",
        )
        parser.add_argument(
            "--max-fallback-queries",
            type=int,
            default=2,
            help="Maximum number of fallback hybrid searches per QA entry",
        )
        parser.add_argument(
            "--evaluation-sample",
            type=int,
            default=12,
            help="Number of QA entries captured for manual evaluation report",
        )
        parser.add_argument(
            "--evaluation-report",
            type=str,
            default="data/profiling/qa_dataset_evaluation.json",
            help="Path to manual evaluation report JSON (default: data/profiling/qa_dataset_evaluation.json)",
        )
        parser.add_argument(
            "--no-evaluation-report",
            action="store_true",
            help="Disable manual evaluation report generation",
        )
        parser.add_argument(
            "--profile",
            action="store_true",
            help="Enable cProfile instrumentation",
        )
        parser.add_argument(
            "--profile-name",
            type=str,
            default="build_qa_dataset",
            help="Base name for generated profiling artifacts",
        )
        parser.add_argument(
            "--search-limit",
            type=int,
            default=60,
            help="Hybrid search limit for primary queries",
        )
        parser.add_argument(
            "--max-candidates",
            type=int,
            default=2000,
            help="Maximum inverted-index candidates per hybrid search",
        )
        parser.add_argument(
            "--alpha",
            type=float,
            default=0.85,
            help="Hybrid search TF-IDF weighting factor",
        )
        parser.add_argument(
            "--coverage-bonus",
            type=float,
            default=0.15,
            help="Coverage bonus forwarded to hybrid search",
        )
        parser.add_argument(
            "--min-term-match-policy",
            choices=["balanced", "strict", "len2_strict"],
            default="balanced",
            help="Hybrid search minimum term coverage policy",
        )
        parser.add_argument(
            "--strict-and-filter",
            action="store_true",
            help="Enable strict AND filtering for short queries",
        )
        parser.add_argument(
            "--disable-partial-title-boost",
            action="store_true",
            help="Disable partial title boost in hybrid search",
        )
        parser.add_argument(
            "--fallback-limit",
            type=int,
            default=None,
            help="Override fallback hybrid search result cap (default: 2x search-limit)",
        )
        parser.add_argument(
            "--fallback-max-candidates",
            type=int,
            default=None,
            help="Override fallback candidate cap (default: 4x max-candidates)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging output",
        )
        parser.add_argument(
            "--debug",
            action="store_true",
            help="Enable debug logging output",
        )

    def handle(self, *args, **options) -> None:
        log_level = logging.DEBUG if (options["debug"] or options["verbose"]) else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler("build_qa_dataset.log"),
            ],
        )

        input_path = Path(options["input"])
        output_dir = Path(options["output_dir"])
        if not input_path.exists():
            raise CommandError(f"Input file not found: {input_path}")

        self._validate_search_index()

        context_sizes = sorted(set(options["context_sizes"]))
        if not context_sizes:
            raise CommandError("At least one context size must be provided")

        max_context_tokens = max(context_sizes)
        limit = options.get("limit")

        evaluation_path: Optional[Path]
        if options["no_evaluation_report"]:
            evaluation_path = None
        else:
            evaluation_path = Path(options["evaluation_report"])
            evaluation_path.parent.mkdir(parents=True, exist_ok=True)

        output_dir.mkdir(parents=True, exist_ok=True)

        primary_params = HybridParams(
            limit=options["search_limit"],
            max_candidates=options["max_candidates"],
            alpha=options["alpha"],
            coverage_bonus=options["coverage_bonus"],
            min_term_match_policy=options["min_term_match_policy"],
            strict_and_filter=options["strict_and_filter"],
            enable_partial_title_boost=not options["disable_partial_title_boost"],
        )
        fallback_params = HybridParams(
            limit=options["fallback_limit"] or options["search_limit"] * 2,
            max_candidates=options["fallback_max_candidates"] or options["max_candidates"] * 4,
            alpha=options["alpha"],
            coverage_bonus=max(options["coverage_bonus"] / 2, 0.0),
            min_term_match_policy="balanced",
            strict_and_filter=False,
            enable_partial_title_boost=True,
        )

        throughput_target = ThroughputTarget()

        self.stdout.write(f"Loading HotpotQA data from: {input_path}")
        with input_path.open("r", encoding="utf-8") as fh:
            qa_data: List[Dict[str, Any]] = json.load(fh)

        if limit is not None and limit > 0:
            qa_data = qa_data[:limit]
            self.stdout.write(f"Processing limited sample of {len(qa_data)} entries")

        article_cache = ArticleCache()
        with phase_timer("preprocessing"):
            titles = self._collect_supporting_titles(qa_data)
            self.stdout.write(f"Collected {len(titles)} unique supporting titles")

            supporting_articles = Article.objects.filter(title__in=titles)
            article_cache.attach_articles(supporting_articles)
            fetched = {article.title for article in supporting_articles}
            missing = titles - fetched
            if missing:
                logger.warning("Missing %d supporting articles (sample: %s)", len(missing), list(missing)[:10])

        profiler = ProfileManager(options["profile_name"], enabled=options["profile"])
        timestamp = time.strftime("%Y%m%d_%H%M%S")

        if profiler.enabled:
            profiler.start()

        build_start = time.perf_counter()
        results_by_context, stats, timing_stats, evaluation_samples = self._process_entries(
            qa_data=qa_data,
            context_sizes=context_sizes,
            max_context_tokens=max_context_tokens,
            article_cache=article_cache,
            primary_params=primary_params,
            fallback_params=fallback_params,
            min_distractor_tokens=max(0, options["min_distractor_tokens"]),
            max_fallback_queries=max(0, options["max_fallback_queries"]),
            evaluation_sample_size=max(0, options["evaluation_sample"]),
        )
        processing_duration = time.perf_counter() - build_start

        if profiler.enabled:
            profiler.stop()
            profile_file, summary_file = profiler.save(timestamp=timestamp)
            self.stdout.write(f"Profile data saved to: {profile_file}")
            self.stdout.write(f"Profile summary saved to: {summary_file}")

        self._print_stats(stats, timing_stats, processing_duration)
        self._check_throughput(stats, processing_duration, throughput_target)

        self._write_outputs(results_by_context, output_dir)
        self._write_evaluation_report(
            evaluation_path=evaluation_path,
            evaluation_samples=evaluation_samples,
            context_sizes=context_sizes,
            primary_params=primary_params,
            fallback_params=fallback_params,
            min_distractor_tokens=max(0, options["min_distractor_tokens"]),
        )

        self.stdout.write(self.style.SUCCESS("QA dataset build completed."))

    def _validate_search_index(self) -> None:
        vocab_count = Vocabulary.objects.count()
        inverted_count = InvertedIndex.objects.count()

        if vocab_count == 0:
            raise CommandError("Vocabulary is empty. Please build the TF-IDF index first.")
        if inverted_count == 0:
            raise CommandError("Inverted index is empty. Please build the TF-IDF index first.")

        self.stdout.write(
            f"Search index validation succeeded ({vocab_count} vocabulary terms, {inverted_count} inverted entries)"
        )

    def _collect_supporting_titles(self, qa_data: List[Dict[str, Any]]) -> set[str]:
        titles: set[str] = set()
        for entry in qa_data:
            for fact in entry.get("supporting_facts", []):
                if fact:
                    titles.add(fact[0])
        return titles

    def _process_entries(
        self,
        qa_data: List[Dict[str, Any]],
        context_sizes: List[int],
        max_context_tokens: int,
        article_cache: ArticleCache,
        primary_params: HybridParams,
        fallback_params: HybridParams,
        min_distractor_tokens: int,
        max_fallback_queries: int,
        evaluation_sample_size: int,
    ) -> Tuple[Dict[int, List[QADatasetEntry]], Dict[str, int], Dict[str, List[float]], List[EvaluationSample]]:
        results: Dict[int, List[QADatasetEntry]] = {size: [] for size in context_sizes}
        stats = {
            "total": len(qa_data),
            "processed": 0,
            "skipped_missing_articles": 0,
            "skipped_context_overflow": 0,
            "errors": 0,
        }
        timing_stats: Dict[str, List[float]] = {"entry_total": [], "search": [], "selection": [], "context_finalize": []}
        evaluation_samples: List[EvaluationSample] = []

        for entry in tqdm(qa_data, desc="Building QA dataset"):
            entry_start = time.perf_counter()
            try:
                qa_id = entry.get("_id") or entry.get("id") or ""
                question = entry.get("question", "")
                answer = entry.get("answer", "")
                supporting_facts = entry.get("supporting_facts", [])

                supporting_titles = self._deduplicate_titles(supporting_facts)
                supporting_docs: List[Dict[str, str]] = []
                missing_titles: List[str] = []

                supporting_tokens = 0
                for title in supporting_titles:
                    article = article_cache.get_by_title(title)
                    if not article:
                        missing_titles.append(title)
                        continue
                    supporting_docs.append(article_cache.format_article(article))
                    supporting_tokens += article_cache.tokens_for_article(article)

                if missing_titles:
                    stats["skipped_missing_articles"] += 1
                    logger.warning("Skipping %s due to missing articles: %s", qa_id, missing_titles)
                    continue

                if supporting_tokens > max_context_tokens:
                    stats["skipped_context_overflow"] += 1
                    logger.info(
                        "Skipping %s: supporting docs exceed max context (%d > %d)",
                        qa_id,
                        supporting_tokens,
                        max_context_tokens,
                    )
                    continue

                selection_start = time.perf_counter()
                outcome = self._select_distractors(
                    qa_id=qa_id,
                    question=question,
                    answer=answer,
                    supporting_titles=supporting_titles,
                    supporting_tokens=supporting_tokens,
                    article_cache=article_cache,
                    max_context_tokens=max_context_tokens,
                    primary_params=primary_params,
                    fallback_params=fallback_params,
                    max_fallback_queries=max_fallback_queries,
                    min_distractor_tokens=min_distractor_tokens,
                )
                selection_duration = time.perf_counter() - selection_start
                timing_stats["search"].append(outcome.search_time)
                timing_stats["selection"].append(selection_duration)

                context_finalize_start = time.perf_counter()
                context_tokens_by_cap, distractor_counts_by_cap = {}, {}
                for context_size in context_sizes:
                    distractors_for_cap, cap_tokens = self._slice_distractors_for_cap(
                        supporting_tokens=supporting_tokens,
                        context_size=context_size,
                        outcome=outcome,
                    )
                    entry_record = QADatasetEntry(
                        id=qa_id,
                        question=question,
                        gold_answer=answer,
                        supporting_docs=supporting_docs,
                        distractor_docs=distractors_for_cap,
                        context_size=cap_tokens,
                    )
                    results[context_size].append(entry_record)
                    context_tokens_by_cap[context_size] = cap_tokens
                    distractor_counts_by_cap[context_size] = len(distractors_for_cap)
                timing_stats["context_finalize"].append(time.perf_counter() - context_finalize_start)

                stats["processed"] += 1

                if len(evaluation_samples) < evaluation_sample_size:
                    max_cap = max(context_sizes)
                    evaluation_samples.append(
                        EvaluationSample(
                            qa_id=qa_id,
                            question=question,
                            gold_answer=answer,
                            supporting_titles=supporting_titles,
                            distractor_titles=[doc["title"] for doc in outcome.docs],
                            supporting_tokens=supporting_tokens,
                            distractor_tokens=sum(outcome.doc_token_counts),
                            max_context_size=max_cap,
                            max_context_tokens_used=context_tokens_by_cap.get(max_cap, supporting_tokens),
                            unfilled_tokens=max(
                                0,
                                max_cap - context_tokens_by_cap.get(max_cap, supporting_tokens),
                            ),
                            queries_executed=outcome.queries_executed,
                            fallback_queries=outcome.fallback_queries_used,
                            fallback_invocations=outcome.fallback_invocations,
                            context_tokens_by_cap=context_tokens_by_cap,
                            distractor_counts_by_cap=distractor_counts_by_cap,
                        )
                    )
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("Error processing QA entry %s: %s", entry.get("_id", "unknown"), exc)
            finally:
                timing_stats["entry_total"].append(time.perf_counter() - entry_start)

        return results, stats, timing_stats, evaluation_samples

    def _deduplicate_titles(self, supporting_facts: Iterable[Sequence[str]]) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for fact in supporting_facts:
            if not fact:
                continue
            title = fact[0]
            lowered = title.lower()
            if lowered not in seen:
                seen.add(lowered)
                ordered.append(title)
        return ordered

    def _select_distractors(
        self,
        qa_id: str,
        question: str,
        answer: str,
        supporting_titles: Sequence[str],
        supporting_tokens: int,
        article_cache: ArticleCache,
        max_context_tokens: int,
        primary_params: HybridParams,
        fallback_params: HybridParams,
        max_fallback_queries: int,
        min_distractor_tokens: int,
    ) -> DistractorOutcome:
        selection_start = time.perf_counter()

        supporting_title_keys = {title.lower() for title in supporting_titles}
        seen_titles = set(supporting_title_keys)

        queued_queries: List[Tuple[str, bool]] = [(title, False) for title in supporting_titles if title]
        seen_queries = {title for title, _ in queued_queries}

        fallback_candidates: List[str] = []
        if question:
            fallback_candidates.append(question)
        if answer and answer.lower() not in question.lower():
            fallback_candidates.append(f"{question} {answer}".strip())
        combined_titles = " ".join(sorted(set(supporting_titles)))[:256]
        if combined_titles:
            fallback_candidates.append(combined_titles)

        fallback_queries_used: List[str] = []
        fallback_invocations = 0

        docs: List[Dict[str, str]] = []
        doc_token_counts: List[int] = []
        queries_executed: List[str] = []
        total_distractor_tokens = 0
        search_time = 0.0

        idx = 0
        while idx < len(queued_queries) and supporting_tokens + total_distractor_tokens < max_context_tokens:
            query, is_fallback = queued_queries[idx]
            idx += 1

            params = fallback_params if is_fallback else primary_params
            search_begin = time.perf_counter()
            results = self._cached_search(query, params, article_cache)
            search_time += time.perf_counter() - search_begin
            queries_executed.append(query)

            for article, _score in results:
                if article.title.lower() in seen_titles:
                    continue

                article_tokens = article_cache.tokens_for_article(article)
                if article_tokens < min_distractor_tokens:
                    continue

                projected = supporting_tokens + total_distractor_tokens + article_tokens
                if projected > max_context_tokens:
                    continue

                docs.append(article_cache.format_article(article))
                doc_token_counts.append(article_tokens)
                total_distractor_tokens += article_tokens
                seen_titles.add(article.title.lower())

                if supporting_tokens + total_distractor_tokens >= max_context_tokens:
                    break

            if supporting_tokens + total_distractor_tokens >= max_context_tokens:
                break

            if idx == len(queued_queries):
                while fallback_candidates and fallback_invocations < max_fallback_queries:
                    fallback_query = fallback_candidates.pop(0)
                    if not fallback_query or fallback_query in seen_queries:
                        continue
                    queued_queries.append((fallback_query, True))
                    seen_queries.add(fallback_query)
                    fallback_queries_used.append(fallback_query)
                    fallback_invocations += 1
                    break
                if idx == len(queued_queries):
                    break

        selection_time = time.perf_counter() - selection_start
        return DistractorOutcome(
            docs=docs,
            doc_token_counts=doc_token_counts,
            total_tokens=total_distractor_tokens,
            queries_executed=queries_executed,
            fallback_queries_used=fallback_queries_used,
            fallback_invocations=fallback_invocations,
            search_time=search_time,
            selection_time=selection_time,
        )

    def _cached_search(
        self,
        query: str,
        params: HybridParams,
        article_cache: ArticleCache,
    ) -> List[Tuple[Article, float]]:
        key = (query, params)
        cached = self._search_cache.get(key)
        if cached is None:
            results = search_hybrid(
                query,
                limit=params.limit,
                alpha=params.alpha,
                max_candidates=params.max_candidates,
                coverage_bonus_weight=params.coverage_bonus,
                strict_and_filter=params.strict_and_filter,
                min_term_match_policy=params.min_term_match_policy,
                enable_partial_title_boost=params.enable_partial_title_boost,
            )
            article_cache.attach_articles(article for article, _ in results)
            cached = [(article.id, score) for article, score in results]
            self._search_cache[key] = cached

        article_ids = [article_id for article_id, _ in cached]
        article_cache.ensure_by_ids(article_ids)

        resolved: List[Tuple[Article, float]] = []
        for article_id, score in cached:
            article = article_cache.by_id.get(article_id)
            if article is not None:
                resolved.append((article, score))
        return resolved

    def _slice_distractors_for_cap(
        self,
        supporting_tokens: int,
        context_size: int,
        outcome: DistractorOutcome,
    ) -> Tuple[List[Dict[str, str]], int]:
        tokens_used = supporting_tokens
        selected_docs: List[Dict[str, str]] = []
        for doc, doc_tokens in zip(outcome.docs, outcome.doc_token_counts):
            if tokens_used + doc_tokens > context_size:
                break
            selected_docs.append(doc)
            tokens_used += doc_tokens
        return list(selected_docs), tokens_used

    def _print_stats(self, stats: Dict[str, int], timing_stats: Dict[str, List[float]], processing_duration: float) -> None:
        self.stdout.write("\nProcessing Summary:")
        self.stdout.write(f"  Total entries: {stats['total']}")
        self.stdout.write(f"  Processed: {stats['processed']}")
        self.stdout.write(f"  Skipped (missing articles): {stats['skipped_missing_articles']}")
        self.stdout.write(f"  Skipped (context overflow): {stats['skipped_context_overflow']}")
        self.stdout.write(f"  Errors: {stats['errors']}")
        self.stdout.write(f"  Total processing time: {processing_duration:.2f}s")

        self.stdout.write("\nTiming Statistics (seconds):")
        for label, samples in timing_stats.items():
            if not samples:
                continue
            total = sum(samples)
            mean = statistics.mean(samples)
            median = statistics.median(samples)
            p95 = statistics.quantiles(samples, n=100, method="inclusive")[94] if len(samples) >= 5 else max(samples)
            self.stdout.write(
                f"  {label}: count={len(samples)} total={total:.2f}s mean={mean:.4f}s "
                f"median={median:.4f}s p95={p95:.4f}s"
            )

    def _check_throughput(
        self,
        stats: Dict[str, int],
        processing_duration: float,
        throughput_target: ThroughputTarget,
    ) -> None:
        processed = stats.get("processed", 0)
        throughput = processed / processing_duration if processing_duration > 0 else 0.0
        self.stdout.write(f"\nThroughput: {throughput:.2f} entries/sec")

        if processed >= throughput_target.minimum_samples and throughput < throughput_target.threshold:
            warning = (
                f"Throughput below target: {throughput:.2f} < {throughput_target.threshold:.2f} entries/sec "
                f"over {processed} processed entries."
            )
            logger.warning(warning)
            self.stdout.write(self.style.WARNING(warning))

    def _write_outputs(self, results_by_context: Dict[int, List[QADatasetEntry]], output_dir: Path) -> None:
        for context_size, entries in results_by_context.items():
            output_path = output_dir / f"qa_dataset_{context_size}.json"
            serializable = [entry.to_dict() for entry in entries]
            io_start = time.perf_counter()
            with output_path.open("w", encoding="utf-8") as fh:
                json.dump(serializable, fh, ensure_ascii=False, indent=2)
            self.stdout.write(
                f"Wrote {len(entries)} entries to {output_path} ({time.perf_counter() - io_start:.2f}s)"
            )

    def _write_evaluation_report(
        self,
        evaluation_path: Optional[Path],
        evaluation_samples: List[EvaluationSample],
        context_sizes: List[int],
        primary_params: HybridParams,
        fallback_params: HybridParams,
        min_distractor_tokens: int,
    ) -> None:
        if evaluation_path is None:
            self.stdout.write("Evaluation report generation disabled.")
            return

        if not evaluation_samples:
            self.stdout.write("Evaluation report skipped (no samples captured).")
            return

        max_cap = max(context_sizes)
        fill_ratios = [
            sample.max_context_tokens_used / max_cap if max_cap > 0 else 0.0
            for sample in evaluation_samples
        ]
        average_fill_ratio = statistics.mean(fill_ratios) if fill_ratios else 0.0
        avg_fallbacks = statistics.mean(sample.fallback_invocations for sample in evaluation_samples)
        avg_distractors = statistics.mean(
            sample.distractor_counts_by_cap.get(max_cap, 0) for sample in evaluation_samples
        )

        payload = {
            "metadata": {
                "sample_size": len(evaluation_samples),
                "average_fill_ratio": average_fill_ratio,
                "average_fallback_invocations": avg_fallbacks,
                "average_distractor_count_at_max": avg_distractors,
                "min_distractor_tokens": min_distractor_tokens,
                "primary_params": asdict(primary_params),
                "fallback_params": asdict(fallback_params),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "samples": [sample.to_dict() for sample in evaluation_samples],
        }

        with evaluation_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        self.stdout.write(self.style.SUCCESS(f"Evaluation report saved to {evaluation_path}"))
