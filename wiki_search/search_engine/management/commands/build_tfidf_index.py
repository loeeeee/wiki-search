from __future__ import annotations

import cProfile
import json
import logging
import os
import pstats
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, TFIDFIndex, Vocabulary
from search_engine.search import compute_idf, compute_tf, vector_l2_norm
from search_engine.tokenizer import tokenize

logger = logging.getLogger(__name__)


def save_profile_stats(profiler: cProfile.Profile, phase_name: str) -> str:
    """Save cProfile statistics to file and log top functions."""
    base_dir = settings.BASE_DIR.parent / "data" / "profiles"
    base_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    profile_path = base_dir / f"{phase_name}_{timestamp}.prof"
    
    profiler.dump_stats(str(profile_path))
    
    # Log top functions by cumulative time
    stats = pstats.Stats(profiler)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    
    logger.info("Top 20 functions by cumulative time for %s:", phase_name)
    stats.print_stats(20)
    
    return str(profile_path)


def _compute_doc_freq_batch(article_tuples: List[Tuple[int, List[str]]]) -> Counter:
    """Worker: tokenize article paragraphs, return local df Counter.
    
    Input: lightweight tuples (article_id, paragraphs)
    Output: Counter of unique terms seen across batch
    """
    doc_freq = Counter()
    for article_id, paragraphs in article_tuples:
        seen_terms = set()
        for para in paragraphs:
            seen_terms.update(tokenize(para))
        doc_freq.update(seen_terms)
    return doc_freq


def _build_tfidf_batch(
    article_tuples: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float]
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],  # (article_id, tfidf_vec, l2_norm, token_counts)
    List[Tuple[int, int, float]]  # (term_id, article_id, tfidf_score) for InvertedIndex
]:
    """Worker: compute TF-IDF vectors, inverted index tuples, and token counts.
    
    Returns lightweight tuples to minimize serialization overhead.
    """
    tfidf_tuples = []
    inverted_tuples = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        tf = compute_tf(tokens)
        vec = {}
        for term, tf_val in tf.items():
            term_id = term_to_id.get(term)
            idf_val = term_to_idf.get(term)
            if term_id is None or idf_val is None:
                continue
            tfidf_score = tf_val * idf_val
            vec[term_id] = tfidf_score
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        l2_norm = vector_l2_norm(vec.values()) if vec else 0.0
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
    
    return tfidf_tuples, inverted_tuples


def flush_tfidf_sync(tfidf_tuples: List[Tuple[int, Dict[int, float], float, List[int]]]) -> int:
    """Synchronous TF-IDF flush using PostgreSQL COPY for high throughput."""
    if not tfidf_tuples:
        return 0
    
    # Get articles for the tuples
    article_ids = [tup[0] for tup in tfidf_tuples]
    articles = {a.id: a for a in Article.objects.filter(id__in=article_ids)}
    
    # Prepare data for COPY
    tfidf_data = []
    for article_id, vec, l2_norm, token_counts in tfidf_tuples:
        if article_id in articles:
            # Convert vector dict to JSON string for COPY
            vector_json = json.dumps({str(k): float(v) for k, v in vec.items()})
            token_counts_json = json.dumps(token_counts)
            tfidf_data.append((article_id, vector_json, float(l2_norm), token_counts_json))
    
    if tfidf_data:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Use COPY for bulk insert
                with cursor.copy(
                    "COPY search_engine_tfidfindex (article_id, tfidf_vector, l2_norm) FROM STDIN"
                ) as copy:
                    for article_id, vector_json, l2_norm, token_counts_json in tfidf_data:
                        copy.write_row((article_id, vector_json, l2_norm))
            
            # Update paragraph_token_counts for articles
            for article_id, vec, l2_norm, token_counts_json in tfidf_data:
                Article.objects.filter(id=article_id).update(
                    paragraph_token_counts=json.loads(token_counts_json)
                )
    
    return len(tfidf_data)


def flush_inverted_sync(inverted_tuples: List[Tuple[int, int, float]]) -> int:
    """Synchronous inverted index flush using PostgreSQL COPY for high throughput."""
    if not inverted_tuples:
        return 0
    
    # Get vocabulary terms and articles for the tuples
    term_ids = list(set(tup[0] for tup in inverted_tuples))
    article_ids = list(set(tup[1] for tup in inverted_tuples))
    
    vocab_map = {v.id: v for v in Vocabulary.objects.filter(id__in=term_ids)}
    article_map = {a.id: a for a in Article.objects.filter(id__in=article_ids)}
    
    # Prepare data for COPY
    inverted_data = []
    for term_id, article_id, tfidf_score in inverted_tuples:
        if term_id in vocab_map and article_id in article_map:
            inverted_data.append((term_id, article_id, float(tfidf_score)))
    
    if inverted_data:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Use COPY for bulk insert
                with cursor.copy(
                    "COPY search_engine_invertedindex (term_id, article_id, tf_idf_score) FROM STDIN"
                ) as copy:
                    for term_id, article_id, tfidf_score in inverted_data:
                        copy.write_row((term_id, article_id, tfidf_score))
    
    return len(inverted_data)


class Command(BaseCommand):
    help = "Build TF-IDF index and inverted index over Article.plain_text_paragraphs using all CPU cores"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rebuild", action="store_true", help="Clear existing index before building")
        parser.add_argument("--batch-size", type=int, default=500, help="Articles per worker batch")
        parser.add_argument("--limit", type=int, default=0, help="Limit number of articles (for testing)")
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
        parser.add_argument("--db-workers", type=int, default=96, help="Number of database writer threads (default: 96)")
        parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
        parser.add_argument("--profile", action="store_true", help="Enable detailed profiling with cProfile")

    def handle(self, *args, **options):
        # Setup logging
        if options["verbose"]:
            logging.basicConfig(level=logging.INFO, 
                             format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        
        batch_size = options["batch_size"]
        limit = options["limit"]
        rebuild = options["rebuild"]
        workers = options["workers"]
        db_workers = options["db_workers"]
        enable_profiling = options.get("profile", False)
        
        # Initialize profilers
        profiler_pass1 = None
        profiler_vocab = None
        profiler_pass2 = None
        
        start_time = time.perf_counter()
        
        if rebuild:
            self.stdout.write("Clearing existing indexes...")
            InvertedIndex.objects.all().delete()
            TFIDFIndex.objects.all().delete()
            Vocabulary.objects.all().delete()
        
        # Get articles to process
        qs = Article.objects.only("id", "plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]
        
        total_articles = qs.count()
        if total_articles == 0:
            self.stdout.write(self.style.WARNING("No articles found to process"))
            return
        
        self.stdout.write(f"Processing {total_articles} articles with {workers} workers, {db_workers} database workers")
        
        # Convert to lightweight tuples for worker processing
        self.stdout.write("Loading article data...")
        article_tuples = [(a.id, a.plain_text_paragraphs) for a in qs]
        
        # Split into worker batches
        worker_batches = [article_tuples[i:i+batch_size] 
                         for i in range(0, len(article_tuples), batch_size)]
        
        self.stdout.write(f"Split into {len(worker_batches)} batches of ~{batch_size} articles each")
        
        # Pass 1: Parallel document frequency computation
        self.stdout.write("Pass 1: Computing document frequencies...")
        pass1_start = time.perf_counter()
        
        if enable_profiling:
            profiler_pass1 = cProfile.Profile()
            profiler_pass1.enable()
        
        with ProcessPoolExecutor(max_workers=workers) as executor:
            # Submit all batches for parallel processing
            futures = [executor.submit(_compute_doc_freq_batch, batch) 
                      for batch in worker_batches]
            
            # Aggregate results with progress bar
            global_df = Counter()
            for future in tqdm(as_completed(futures), total=len(futures), desc="Pass 1 - Doc Freq"):
                global_df.update(future.result())
        
        if enable_profiling and profiler_pass1 is not None:
            profiler_pass1.disable()
            save_profile_stats(profiler_pass1, "pass1_doc_freq")
        
        pass1_time = time.perf_counter() - pass1_start
        self.stdout.write(f"Pass 1 complete in {pass1_time:.2f}s - found {len(global_df)} unique terms")
        
        # Build vocabulary (single-threaded, fast)
        self.stdout.write("Building vocabulary...")
        vocab_start = time.perf_counter()
        
        if enable_profiling:
            profiler_vocab = cProfile.Profile()
            profiler_vocab.enable()
        
        total_docs = len(article_tuples)
        vocab_data = []
        for term, df in global_df.items():
            vocab_data.append((
                term, 
                int(df), 
                compute_idf(total_docs, int(df))
            ))
        
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Use COPY for vocabulary insertion
                with cursor.copy(
                    "COPY search_engine_vocabulary (term, document_frequency, idf_value) FROM STDIN"
                ) as copy:
                    for term, df, idf in vocab_data:
                        copy.write_row((term, df, idf))
        
        if enable_profiling and profiler_vocab is not None:
            profiler_vocab.disable()
            save_profile_stats(profiler_vocab, "vocabulary_build")
        
        vocab_time = time.perf_counter() - vocab_start
        self.stdout.write(f"Vocabulary built in {vocab_time:.2f}s - {len(vocab_data)} terms")
        
        # Build maps for workers
        term_to_id = {v.term: v.id for v in Vocabulary.objects.only("id", "term")}
        term_to_idf = {v.term: float(v.idf_value) for v in Vocabulary.objects.only("term", "idf_value")}
        
        # Pass 2: Parallel TF-IDF + inverted index with async DB writes
        self.stdout.write("Pass 2: Building TF-IDF vectors and inverted index...")
        pass2_start = time.perf_counter()
        
        if enable_profiling:
            profiler_pass2 = cProfile.Profile()
            profiler_pass2.enable()
        
        with ThreadPoolExecutor(max_workers=db_workers) as db_executor, \
             ProcessPoolExecutor(max_workers=workers) as process_executor:
            
            # Submit batches for parallel TF-IDF computation
            futures = [
                process_executor.submit(_build_tfidf_batch, batch, term_to_id, term_to_idf)
                for batch in worker_batches
            ]
            
            tfidf_buffer = []
            inverted_buffer = []
            db_futures = []
            
            # Large flush thresholds for PostgreSQL efficiency
            TFIDF_FLUSH_THRESHOLD = 20000
            INVERTED_FLUSH_THRESHOLD = 500000  # Inverted index is much larger
            
            for future in tqdm(as_completed(futures), total=len(futures), desc="Pass 2 - TF-IDF"):
                tfidf_tuples, inverted_tuples = future.result()
                tfidf_buffer.extend(tfidf_tuples)
                inverted_buffer.extend(inverted_tuples)
                
                # Async database flush when threshold reached
                if len(tfidf_buffer) >= TFIDF_FLUSH_THRESHOLD:
                    db_future = db_executor.submit(flush_tfidf_sync, tfidf_buffer[:])
                    db_futures.append(('tfidf', db_future))
                    tfidf_buffer.clear()
                
                if len(inverted_buffer) >= INVERTED_FLUSH_THRESHOLD:
                    db_future = db_executor.submit(flush_inverted_sync, inverted_buffer[:])
                    db_futures.append(('inverted', db_future))
                    inverted_buffer.clear()
            
            # Wait for all DB writes to complete
            self.stdout.write("Waiting for database writes to complete...")
            tfidf_created = 0
            inverted_created = 0
            
            for write_type, db_future in db_futures:
                result = db_future.result()
                if write_type == 'tfidf':
                    tfidf_created += result
                else:
                    inverted_created += result
            
            # Final flush
            if tfidf_buffer:
                tfidf_created += flush_tfidf_sync(tfidf_buffer)
            if inverted_buffer:
                inverted_created += flush_inverted_sync(inverted_buffer)
        
        if enable_profiling and profiler_pass2 is not None:
            profiler_pass2.disable()
            save_profile_stats(profiler_pass2, "pass2_tfidf")
        
        pass2_time = time.perf_counter() - pass2_start
        total_time = time.perf_counter() - start_time
        
        # Display results
        self.stdout.write(self.style.SUCCESS(
            f"TF-IDF index build complete in {total_time:.2f} seconds"
        ))
        self.stdout.write(f"  - Pass 1 (doc freq): {pass1_time:.2f}s")
        self.stdout.write(f"  - Vocabulary build: {vocab_time:.2f}s")
        self.stdout.write(f"  - Pass 2 (TF-IDF): {pass2_time:.2f}s")
        self.stdout.write(f"  - Articles processed: {total_articles}")
        self.stdout.write(f"  - TF-IDF vectors created: {tfidf_created}")
        self.stdout.write(f"  - Inverted index entries: {inverted_created}")
        self.stdout.write(f"  - Workers used: {workers}")
        self.stdout.write(f"  - Throughput: {total_articles/total_time:.1f} articles/second")
        
        # Show some statistics
        vocab_count = Vocabulary.objects.count()
        tfidf_count = TFIDFIndex.objects.count()
        inverted_count = InvertedIndex.objects.count()
        
        self.stdout.write(f"\nDatabase statistics:")
        self.stdout.write(f"  - Vocabulary terms: {vocab_count}")
        self.stdout.write(f"  - TF-IDF vectors: {tfidf_count}")
        self.stdout.write(f"  - Inverted index entries: {inverted_count}")
        
        if inverted_count > 0:
            avg_terms_per_article = inverted_count / tfidf_count if tfidf_count > 0 else 0
            self.stdout.write(f"  - Avg terms per article: {avg_terms_per_article:.1f}")
        
        self.stdout.write(
            self.style.SUCCESS(
                f"Optimized TF-IDF indexing complete. "
                f"Processed {total_articles} articles in {total_time:.2f}s using {workers} workers."
            )
        )