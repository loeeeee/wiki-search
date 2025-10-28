from __future__ import annotations

import cProfile
import json
import logging
import os
import pstats
import queue
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from multiprocessing import Process, Queue
from typing import Dict, List, Tuple
import multiprocessing

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, TFIDFIndex, Vocabulary
from search_engine.search import compute_idf, vector_l2_norm
from search_engine.tokenizer import tokenize
from .tfidf_workers import _compute_doc_freq_batch, _build_tfidf_batch_gpu, _build_tfidf_batch_cpu_fallback

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


def producer_pass1(article_queue: Queue, batch_size: int, limit: int, num_consumers: int) -> None:
    """Producer thread for Pass 1: fetch articles from database and put in queue."""
    try:
        logger.info(f"Producer Pass 1 starting with limit={limit}")
        qs = Article.objects.only("id", "plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]
        
        articles = qs.iterator(chunk_size=batch_size)
        count = 0
        
        for article in articles:
            article_queue.put((article.id, article.plain_text_paragraphs))
            count += 1
            if count % 100 == 0:
                logger.info(f"Producer Pass 1: put {count} articles in queue")
        
        logger.info(f"Producer Pass 1: finished putting {count} articles")
        # Signal end of data to all consumers
        for _ in range(num_consumers):
            article_queue.put(None)
        logger.info(f"Producer Pass 1: sent {num_consumers} end signals")
        
    except Exception as e:
        logger.error(f"Producer Pass 1 error: {e}")
        # Send end signals to all consumers even on error
        for _ in range(num_consumers):
            article_queue.put(None)


def producer_pass2(article_queue: Queue, batch_size: int, limit: int, num_consumers: int) -> None:
    """Producer thread for Pass 2: fetch articles from database and put in queue."""
    try:
        qs = Article.objects.only("id", "plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]
        
        articles = qs.iterator(chunk_size=batch_size)
        
        for article in articles:
            article_queue.put((article.id, article.plain_text_paragraphs))
        
        # Signal end of data to all consumers
        for _ in range(num_consumers):
            article_queue.put(None)
        
    except Exception as e:
        logger.error(f"Producer Pass 2 error: {e}")
        # Send end signals to all consumers even on error
        for _ in range(num_consumers):
            article_queue.put(None)


def consumer_pass1(article_queue: Queue, result_queue: Queue) -> None:
    """Consumer process for Pass 1: tokenize articles and compute document frequency."""
    try:
        logger.info("Consumer Pass 1 starting")
        batch = []
        batch_size = 100  # Process articles in small batches
        processed_count = 0
        
        while True:
            logger.info("Consumer Pass 1: waiting for item from queue")
            item = article_queue.get()
            logger.info(f"Consumer Pass 1: got item {item}")
            if item is None:  # End signal
                logger.info("Consumer Pass 1: received end signal")
                break
            
            batch.append(item)
            processed_count += 1
            
            if len(batch) >= batch_size:
                # Process batch
                logger.info(f"Consumer Pass 1: processing batch of {len(batch)} articles")
                doc_freq = _compute_doc_freq_batch(batch)
                result_queue.put(doc_freq)
                batch = []
        
        # Process remaining items
        if batch:
            logger.info(f"Consumer Pass 1: processing final batch of {len(batch)} articles")
            doc_freq = _compute_doc_freq_batch(batch)
            result_queue.put(doc_freq)
        
        logger.info(f"Consumer Pass 1: processed {processed_count} articles total")
        # Signal completion
        result_queue.put(None)
        logger.info("Consumer Pass 1: sent completion signal")
        
    except Exception as e:
        logger.error(f"Consumer Pass 1 error: {e}")
        result_queue.put(None)


def gpu_batch_processor(
    article_batch: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float],
    device,
    result_queue: Queue
) -> None:
    """GPU batch processor for Pass 2: process articles on GPU."""
    try:
        tfidf_tuples, inverted_tuples = _build_tfidf_batch_gpu(
            article_batch, term_to_id, term_to_idf, device
        )
        result_queue.put((tfidf_tuples, inverted_tuples))
        
    except Exception as e:
        logger.error(f"GPU batch processor error: {e}")
        result_queue.put(([], []))


class Command(BaseCommand):
    help = "Build TF-IDF index and inverted index over Article.plain_text_paragraphs using GPU acceleration with producer-consumer architecture"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rebuild", action="store_true", help="Clear existing index before building")
        parser.add_argument("--batch-size", type=int, default=500, help="Articles per database batch")
        parser.add_argument("--limit", type=int, default=0, help="Limit number of articles (for testing)")
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2), help="Number of CPU consumer processes")
        parser.add_argument("--db-workers", type=int, default=96, help="Number of database writer threads (default: 96)")
        parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
        parser.add_argument("--profile", action="store_true", help="Enable detailed profiling with cProfile")
        parser.add_argument("--use-gpu", action="store_true", default=True, help="Use GPU acceleration (default: True)")
        parser.add_argument("--gpu-batch-size", type=int, default=10000, help="Articles per GPU batch (default: 10000)")
        parser.add_argument("--test-mode", action="store_true", help="Test mode - bypass GPU requirements for development testing")

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
        use_gpu = options.get("use_gpu", True)  # Default to True
        gpu_batch_size = options.get("gpu_batch_size", 10000)
        test_mode = options.get("test_mode", False)
        
        # Initialize profilers
        profiler_pass1 = None
        profiler_vocab = None
        profiler_pass2 = None
        
        start_time = time.perf_counter()
        
        # GPU validation - fail fast if not available (unless in test mode)
        if use_gpu and not test_mode:
            try:
                import torch
                if not torch.cuda.is_available():
                    raise RuntimeError("GPU acceleration requested but no GPU available")
                
                gpu_name = torch.cuda.get_device_name(0)
                gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                self.stdout.write(f"GPU acceleration enabled: {gpu_name} ({gpu_memory:.1f}GB VRAM)")
                logger.info(f"GPU acceleration enabled: {gpu_name} ({gpu_memory:.1f}GB VRAM)")
                
                device = torch.device('cuda')
                
            except ImportError:
                raise RuntimeError("GPU acceleration requested but PyTorch not available")
            except RuntimeError as e:
                raise RuntimeError(f"GPU acceleration failed: {e}")
        elif test_mode:
            self.stdout.write(self.style.WARNING("Test mode enabled - bypassing GPU requirements"))
            logger.warning("Test mode enabled - bypassing GPU requirements")
            device = None  # Will be handled in GPU functions
        else:
            raise RuntimeError("GPU acceleration is required - CPU fallback not supported")
        
        # Configure multiprocessing context for GPU compatibility
        mp_context = multiprocessing.get_context('spawn')
        
        if rebuild:
            self.stdout.write("Clearing existing indexes...")
            InvertedIndex.objects.all().delete()
            TFIDFIndex.objects.all().delete()
            Vocabulary.objects.all().delete()
        
        # Get total articles count
        qs = Article.objects.only("id", "plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]
        
        total_articles = qs.count()
        if total_articles == 0:
            self.stdout.write(self.style.WARNING("No articles found to process"))
            return
        
        # Limit workers for small datasets to avoid too many consumers
        workers = min(workers, max(1, total_articles // 100))
        
        self.stdout.write(f"Processing {total_articles} articles with {workers} CPU workers, {db_workers} database workers")
        self.stdout.write(f"GPU batch size: {gpu_batch_size} articles")
        
        # Pass 1: Document frequency computation with producer-consumer
        self.stdout.write("Pass 1: Computing document frequencies...")
        pass1_start = time.perf_counter()
        
        if enable_profiling:
            profiler_pass1 = cProfile.Profile()
            profiler_pass1.enable()
        
        # Create queues for producer-consumer
        article_queue = Queue()  # Buffer for articles (unbounded)
        result_queue = Queue()  # Results from consumers
        
        # Start producer thread
        producer_thread = threading.Thread(
            target=producer_pass1,
            args=(article_queue, batch_size, limit, workers)
        )
        producer_thread.start()
        
        # Start consumer processes
        consumer_processes = []
        for _ in range(workers):
            process = Process(
                target=consumer_pass1,
                args=(article_queue, result_queue)
            )
            process.start()
            consumer_processes.append(process)
        
        # Collect results
        global_df = Counter()
        completed_consumers = 0
        
        with tqdm(total=total_articles, desc="Pass 1 - Doc Freq") as pbar:
            while completed_consumers < workers:
                result = result_queue.get()
                if result is None:
                    completed_consumers += 1
                else:
                    global_df.update(result)
                    pbar.update(len(result))
        
        # Cleanup
        producer_thread.join()
        for process in consumer_processes:
            process.join()
        
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
        
        total_docs = total_articles
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
        
        # Build maps for GPU processing
        term_to_id = {v.term: v.id for v in Vocabulary.objects.only("id", "term")}
        term_to_idf = {v.term: float(v.idf_value) for v in Vocabulary.objects.only("term", "idf_value")}
        
        # Pass 2: GPU-accelerated TF-IDF computation with producer-consumer
        self.stdout.write("Pass 2: Building TF-IDF vectors and inverted index...")
        pass2_start = time.perf_counter()
        
        if enable_profiling:
            profiler_pass2 = cProfile.Profile()
            profiler_pass2.enable()
        
        # Create queues for Pass 2
        article_queue_pass2 = Queue()  # Unbounded queue
        gpu_result_queue = Queue()
        
        # Start producer thread for Pass 2
        producer_thread_pass2 = threading.Thread(
            target=producer_pass2,
            args=(article_queue_pass2, batch_size, limit, 1)
        )
        producer_thread_pass2.start()
        
        # GPU batch processing
        tfidf_buffer = []
        inverted_buffer = []
        db_futures = []
        
        # Large flush thresholds for PostgreSQL efficiency
        TFIDF_FLUSH_THRESHOLD = 20000
        INVERTED_FLUSH_THRESHOLD = 500000
        
        with ThreadPoolExecutor(max_workers=db_workers) as db_executor:
            # Process articles in GPU batches
            current_batch = []
            
            with tqdm(total=total_articles, desc="Pass 2 - TF-IDF") as pbar:
                while True:
                    item = article_queue_pass2.get()
                    if item is None:  # End signal
                        break
                    
                    current_batch.append(item)
                    
                    if len(current_batch) >= gpu_batch_size:
                        # Process GPU batch
                        if test_mode:
                            # In test mode, use CPU fallback for GPU functions
                            tfidf_tuples, inverted_tuples = _build_tfidf_batch_cpu_fallback(
                                current_batch, term_to_id, term_to_idf
                            )
                        else:
                            tfidf_tuples, inverted_tuples = _build_tfidf_batch_gpu(
                                current_batch, term_to_id, term_to_idf, device
                            )
                        
                        tfidf_buffer.extend(tfidf_tuples)
                        inverted_buffer.extend(inverted_tuples)
                        pbar.update(len(current_batch))
                        
                        # Async database flush when threshold reached
                        if len(tfidf_buffer) >= TFIDF_FLUSH_THRESHOLD:
                            db_future = db_executor.submit(flush_tfidf_sync, tfidf_buffer[:])
                            db_futures.append(('tfidf', db_future))
                            tfidf_buffer.clear()
                        
                        if len(inverted_buffer) >= INVERTED_FLUSH_THRESHOLD:
                            db_future = db_executor.submit(flush_inverted_sync, inverted_buffer[:])
                            db_futures.append(('inverted', db_future))
                            inverted_buffer.clear()
                        
                        current_batch = []
                
                # Process remaining articles
                if current_batch:
                    if test_mode:
                        # In test mode, use CPU fallback for GPU functions
                        tfidf_tuples, inverted_tuples = _build_tfidf_batch_cpu_fallback(
                            current_batch, term_to_id, term_to_idf
                        )
                    else:
                        tfidf_tuples, inverted_tuples = _build_tfidf_batch_gpu(
                            current_batch, term_to_id, term_to_idf, device
                        )
                    
                    tfidf_buffer.extend(tfidf_tuples)
                    inverted_buffer.extend(inverted_tuples)
                    pbar.update(len(current_batch))
            
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
        
        # Cleanup
        producer_thread_pass2.join()
        
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
        self.stdout.write(f"  - CPU workers used: {workers}")
        self.stdout.write(f"  - GPU batch size: {gpu_batch_size}")
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
                f"GPU-accelerated TF-IDF indexing complete. "
                f"Processed {total_articles} articles in {total_time:.2f}s using GPU acceleration."
            )
        )