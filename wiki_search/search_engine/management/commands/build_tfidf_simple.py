import cProfile
import concurrent.futures
import io
import logging
import math
import os
import pstats
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO
from typing import Dict, List, Tuple

from django.core.management.base import BaseCommand
from django.db import connection, connections, transaction
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, Vocabulary
from search_engine.tokenizer import NLTKTokenizer


@dataclass
class Pass1Result:
    """Data structure to pass results from Pass 1 to Pass 2."""
    article_tf_map: Dict[int, Dict[str, int]]  # article_id -> {term: count}
    global_df: Dict[str, int]  # term -> num_docs_containing_term
    total_docs: int
    article_ids: List[int]  # Preserve order for iteration


def tokenize_article(paragraphs: List[str], tokenizer: NLTKTokenizer) -> Dict[str, int]:
    """Tokenize article paragraphs and count term frequencies.
    
    Args:
        paragraphs: List of paragraph text strings
        tokenizer: NLTKTokenizer instance
        
    Returns:
        Dictionary mapping terms to their frequencies in the article
    """
    # Join all paragraphs into single text
    full_text = ' '.join(paragraphs)
    
    # Tokenize using NLTK
    tokens = tokenizer.tokenize(full_text)
    
    # Count term frequencies
    tf_dict = Counter(tokens)
    
    return dict(tf_dict)


def tokenize_article_batch(article_batch: List[tuple]) -> List[tuple]:
    """Worker function to tokenize a batch of articles in parallel.
    
    This function is designed to be used with ProcessPoolExecutor.
    Each worker process initializes its own NLTKTokenizer instance.
    
    Args:
        article_batch: List of (article_id, paragraphs) tuples
        
    Returns:
        List of (article_id, tf_dict) tuples where tf_dict is {term: count}
    """
    # Initialize tokenizer in worker process (avoids pickling issues)
    tokenizer = NLTKTokenizer()
    
    results = []
    for article_id, paragraphs in article_batch:
        tf_dict = tokenize_article(paragraphs, tokenizer)
        results.append((article_id, tf_dict))
    
    return results


def compute_idf(df_dict: Dict[str, int], total_docs: int) -> Dict[str, float]:
    """Calculate IDF values for all terms.
    
    IDF = log(N / df) where N is total documents and df is document frequency.
    
    Args:
        df_dict: Dictionary mapping terms to document frequency
        total_docs: Total number of documents
        
    Returns:
        Dictionary mapping terms to IDF values
    """
    idf_dict = {}
    for term, df in df_dict.items():
        idf_dict[term] = math.log(total_docs / df)
    return idf_dict


def compute_tfidf_vector(tf_dict: Dict[str, int], idf_dict: Dict[str, float]) -> Dict[str, float]:
    """Compute TF-IDF vector for an article.
    
    TF-IDF = TF * IDF
    
    Args:
        tf_dict: Term frequency dictionary for the article
        idf_dict: IDF values for all terms
        
    Returns:
        Dictionary mapping terms to TF-IDF scores
    """
    tfidf_vector = {}
    for term, tf in tf_dict.items():
        if term in idf_dict:
            tfidf_vector[term] = tf * idf_dict[term]
    return tfidf_vector


def pass1_build_tf_df(
    articles_qs,
    tokenizer: NLTKTokenizer,
    logger: logging.Logger
) -> Pass1Result:
    """Pass 1: Build term frequency and document frequency structures.
    
    Iterates through all articles, tokenizes them, and builds:
    - Per-article TF maps
    - Global DF map
    
    Args:
        articles_qs: Django QuerySet of Article objects
        tokenizer: NLTKTokenizer instance
        logger: Logger for output
        
    Returns:
        Pass1Result with cached TF and DF data
    """
    logger.info("=== Pass 1: Building TF and DF structures ===")
    
    article_tf_map = {}
    global_df = {}
    article_ids = []
    
    # Progress bar for Pass 1
    articles = list(articles_qs.values_list('id', 'plain_text_paragraphs'))
    
    for article_id, paragraphs in tqdm(articles, desc="Pass 1: TF/DF", unit="article"):
        # Tokenize article
        tf_dict = tokenize_article(paragraphs, tokenizer)
        
        # Store TF for this article
        article_tf_map[article_id] = tf_dict
        article_ids.append(article_id)
        
        # Update global DF
        for term in tf_dict.keys():
            global_df[term] = global_df.get(term, 0) + 1
    
    total_docs = len(articles)
    
    logger.info(f"Pass 1 complete:")
    logger.info(f"  - Processed {total_docs} articles")
    logger.info(f"  - Unique terms: {len(global_df)}")
    logger.info(f"  - Avg terms per article: {sum(len(tf) for tf in article_tf_map.values()) / total_docs:.1f}")
    
    return Pass1Result(
        article_tf_map=article_tf_map,
        global_df=global_df,
        total_docs=total_docs,
        article_ids=article_ids
    )


def pass1_build_tf_df_parallel(
    articles_qs,
    batch_size_per_worker: int,
    cpu_workers: int,
    logger: logging.Logger
) -> Pass1Result:
    """Pass 1: Build TF and DF using multiprocess parallelism.
    
    Uses ProcessPoolExecutor to parallelize tokenization across CPU cores.
    Database reads use iterator for memory efficiency.
    
    Args:
        articles_qs: Django QuerySet of Article objects
        batch_size_per_worker: Number of articles per worker batch
        cpu_workers: Number of CPU worker processes
        logger: Logger for output
        
    Returns:
        Pass1Result with cached TF and DF data
    """
    logger.info("=== Pass 1: Building TF and DF (Parallel) ===")
    logger.info(f"CPU workers: {cpu_workers}")
    logger.info(f"Batch size per worker: {batch_size_per_worker}")
    
    article_tf_map = {}
    global_df = {}
    article_ids = []
    
    # Get total count for progress bar
    total_articles = articles_qs.count()
    logger.info(f"Total articles to process: {total_articles}")
    
    # Collect batches from iterator
    logger.info("Collecting articles from database iterator...")
    current_batch = []
    batches = []
    
    for article_id, paragraphs in articles_qs.values_list('id', 'plain_text_paragraphs').iterator(chunk_size=100):
        current_batch.append((article_id, paragraphs))
        
        if len(current_batch) >= batch_size_per_worker:
            batches.append(current_batch)
            current_batch = []
    
    # Add remaining articles
    if current_batch:
        batches.append(current_batch)
    
    logger.info(f"Created {len(batches)} batches for parallel processing")
    
    # Process batches in parallel
    with ProcessPoolExecutor(max_workers=cpu_workers) as executor:
        # Submit all batches
        future_to_batch = {
            executor.submit(tokenize_article_batch, batch): batch 
            for batch in batches
        }
        
        # Process results as they complete
        with tqdm(total=total_articles, desc="Pass 1: TF/DF (Parallel)", unit="article") as pbar:
            for future in concurrent.futures.as_completed(future_to_batch):
                batch_results = future.result()
                
                # Aggregate results
                for article_id, tf_dict in batch_results:
                    article_tf_map[article_id] = tf_dict
                    article_ids.append(article_id)
                    
                    # Update global DF
                    for term in tf_dict.keys():
                        global_df[term] = global_df.get(term, 0) + 1
                    
                    pbar.update(1)
    
    total_docs = len(article_ids)
    
    logger.info(f"Pass 1 complete:")
    logger.info(f"  - Processed {total_docs} articles")
    logger.info(f"  - Unique terms: {len(global_df)}")
    logger.info(f"  - Avg terms per article: {sum(len(tf) for tf in article_tf_map.values()) / total_docs:.1f}")
    
    return Pass1Result(
        article_tf_map=article_tf_map,
        global_df=global_df,
        total_docs=total_docs,
        article_ids=article_ids
    )


def create_vocabulary_raw_sql(
    idf_dict: Dict[str, float],
    df_dict: Dict[str, int],
    logger: logging.Logger
) -> Dict[str, int]:
    """Create Vocabulary entries using PostgreSQL COPY for maximum speed.
    
    Args:
        idf_dict: Term to IDF value mapping
        df_dict: Term to document frequency mapping
        logger: Logger for output
        
    Returns:
        Dictionary mapping terms to vocabulary IDs
    """
    logger.info("Creating Vocabulary using raw SQL COPY...")
    
    # Prepare CSV data in memory
    csv_buffer = io.StringIO()
    for term, idf_value in idf_dict.items():
        df = df_dict[term]
        # Escape term for CSV (handle quotes and newlines)
        escaped_term = term.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        csv_buffer.write(f"{escaped_term}\t{df}\t{idf_value}\n")
    
    csv_buffer.seek(0)
    
    # Use PostgreSQL COPY for bulk insert (psycopg3 API)
    table_name = Vocabulary._meta.db_table
    # Get the underlying psycopg connection
    with connection.connection.cursor() as cursor:
        # Use psycopg3 copy() method
        with cursor.copy(f"COPY {table_name} (term, document_frequency, idf_value) FROM STDIN") as copy:
            for line in csv_buffer:
                copy.write(line)
    
    logger.info(f"Vocabulary entries created via COPY")
    
    # Build term-to-ID mapping by querying back
    logger.info("Building term-to-ID mapping...")
    term_to_vocab_id = {}
    vocab_objects = Vocabulary.objects.all()
    for vocab in vocab_objects:
        term_to_vocab_id[vocab.term] = vocab.id
    
    logger.info(f"Mapped {len(term_to_vocab_id)} terms to vocabulary IDs")
    return term_to_vocab_id


def create_inverted_index_raw_sql(
    article_tf_map: Dict[int, Dict[str, int]],
    article_ids: List[int],
    term_to_vocab_id: Dict[str, int],
    idf_dict: Dict[str, float],
    logger: logging.Logger
) -> int:
    """Create InvertedIndex entries using PostgreSQL COPY for maximum speed.
    
    Args:
        article_tf_map: Article ID to term frequency mapping
        article_ids: List of article IDs to process
        term_to_vocab_id: Term to vocabulary ID mapping
        idf_dict: Term to IDF value mapping
        logger: Logger for output
        
    Returns:
        Number of inverted index entries created
    """
    logger.info("Building inverted index using raw SQL COPY...")
    
    # Prepare CSV data in memory
    csv_buffer = io.StringIO()
    entry_count = 0
    
    for article_id in tqdm(article_ids, desc="Preparing Inverted Index", unit="article"):
        tf_dict = article_tf_map[article_id]
        
        for term, tf in tf_dict.items():
            if term in term_to_vocab_id and term in idf_dict:
                tfidf_score = tf * idf_dict[term]
                csv_buffer.write(f"{term_to_vocab_id[term]}\t{article_id}\t{tfidf_score}\n")
                entry_count += 1
    
    csv_buffer.seek(0)
    
    # Use PostgreSQL COPY for bulk insert (psycopg3 API)
    logger.info(f"Inserting {entry_count} entries via COPY...")
    table_name = InvertedIndex._meta.db_table
    # Get the underlying psycopg connection
    with connection.connection.cursor() as cursor:
        # Use psycopg3 copy() method
        with cursor.copy(f"COPY {table_name} (term_id, article_id, tf_idf_score) FROM STDIN") as copy:
            for line in csv_buffer:
                copy.write(line)
    
    logger.info("Inverted index created via COPY")
    return entry_count


# Process-local storage for vocabulary worker data
_vocabulary_worker_data = None


def init_vocabulary_worker(
    global_df: Dict[str, int],
    idf_dict: Dict[str, float]
) -> None:
    """Initialize worker process with shared data for vocabulary CSV building.
    
    This function is called once per worker process to load the dictionaries
    into process-local storage, avoiding repeated serialization.
    
    Args:
        global_df: Term to document frequency mapping
        idf_dict: Term to IDF value mapping
    """
    global _vocabulary_worker_data
    _vocabulary_worker_data = {
        'global_df': global_df,
        'idf_dict': idf_dict
    }


def create_vocabulary_csv_batch(terms_list: List[str]) -> str:
    """Create CSV buffer for a batch of vocabulary terms.
    
    Worker function for ProcessPoolExecutor to build CSV buffers in parallel.
    Uses process-local data initialized by init_vocabulary_worker().
    
    Args:
        terms_list: List of terms
        
    Returns:
        CSV buffer as string
    """
    # Access process-local data
    global _vocabulary_worker_data
    global_df = _vocabulary_worker_data['global_df']
    idf_dict = _vocabulary_worker_data['idf_dict']
    
    csv_buffer = io.StringIO()
    for term in terms_list:
        df = global_df[term]
        idf_value = idf_dict[term]
        # Escape term for CSV (handle quotes and newlines)
        escaped_term = term.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        csv_buffer.write(f"{escaped_term}\t{df}\t{idf_value}\n")
    
    return csv_buffer.getvalue()


def write_vocabulary_batch_sql(csv_data: str) -> int:
    """Write vocabulary batch to database via PostgreSQL COPY.
    
    Worker function for ThreadPoolExecutor to write CSV buffers concurrently.
    Each thread gets its own database connection.
    
    Args:
        csv_data: CSV buffer string
        
    Returns:
        Number of entries written
    """
    table_name = Vocabulary._meta.db_table
    entry_count = csv_data.count('\n')
    
    # Get connection for this thread
    conn = connections['default']
    conn.ensure_connection()
    
    # Use PostgreSQL COPY for bulk insert (psycopg3 API)
    with conn.connection.cursor() as cursor:
        with cursor.copy(f"COPY {table_name} (term, document_frequency, idf_value) FROM STDIN") as copy:
            for line in csv_data.splitlines(keepends=True):
                copy.write(line)
    
    return entry_count


# Process-local storage for inverted index worker data
_inverted_index_worker_data = None


def init_inverted_index_worker(
    article_tf_map: Dict[int, Dict[str, int]],
    term_to_vocab_id: Dict[str, int],
    idf_dict: Dict[str, float]
) -> None:
    """Initialize worker process with shared data for inverted index CSV building.
    
    This function is called once per worker process to load the large
    dictionaries into process-local storage, avoiding repeated serialization.
    
    Args:
        article_tf_map: Article ID to term frequency mapping
        term_to_vocab_id: Term to vocabulary ID mapping
        idf_dict: Term to IDF value mapping
    """
    global _inverted_index_worker_data
    _inverted_index_worker_data = {
        'article_tf_map': article_tf_map,
        'term_to_vocab_id': term_to_vocab_id,
        'idf_dict': idf_dict
    }


def create_inverted_index_csv_batch(article_batch: List[int]) -> str:
    """Create CSV buffer for a batch of inverted index entries.
    
    Worker function for ProcessPoolExecutor to build CSV buffers in parallel.
    Uses process-local data initialized by init_inverted_index_worker().
    
    Args:
        article_batch: List of article IDs
        
    Returns:
        CSV buffer as string
    """
    # Access process-local data
    global _inverted_index_worker_data
    article_tf_map = _inverted_index_worker_data['article_tf_map']
    term_to_vocab_id = _inverted_index_worker_data['term_to_vocab_id']
    idf_dict = _inverted_index_worker_data['idf_dict']
    
    csv_buffer = io.StringIO()
    
    for article_id in article_batch:
        tf_dict = article_tf_map[article_id]
        
        for term, tf in tf_dict.items():
            if term in term_to_vocab_id and term in idf_dict:
                tfidf_score = tf * idf_dict[term]
                csv_buffer.write(f"{term_to_vocab_id[term]}\t{article_id}\t{tfidf_score}\n")
    
    return csv_buffer.getvalue()


def write_inverted_index_batch_sql(csv_data: str) -> int:
    """Write inverted index batch to database via PostgreSQL COPY.
    
    Worker function for ThreadPoolExecutor to write CSV buffers concurrently.
    Each thread gets its own database connection.
    
    Args:
        csv_data: CSV buffer string
        
    Returns:
        Number of entries written
    """
    table_name = InvertedIndex._meta.db_table
    entry_count = csv_data.count('\n')
    
    # Get connection for this thread
    conn = connections['default']
    conn.ensure_connection()
    
    # Use PostgreSQL COPY for bulk insert (psycopg3 API)
    with conn.connection.cursor() as cursor:
        with cursor.copy(f"COPY {table_name} (term_id, article_id, tf_idf_score) FROM STDIN") as copy:
            for line in csv_data.splitlines(keepends=True):
                copy.write(line)
    
    return entry_count


def pass2_build_tfidf(
    pass1_result: Pass1Result,
    logger: logging.Logger
) -> None:
    """Pass 2: Build IDF values and inverted index, save to database.
    
    Uses cached TF/DF data from Pass 1 to:
    - Calculate IDF values
    - Create Vocabulary entries using PostgreSQL COPY
    - Build inverted index entries using PostgreSQL COPY
    
    Args:
        pass1_result: Results from Pass 1
        logger: Logger for output
    """
    logger.info("=== Pass 2: Building IDF and Inverted Index ===")
    
    # Calculate IDF values
    logger.info("Calculating IDF values...")
    idf_dict = compute_idf(pass1_result.global_df, pass1_result.total_docs)
    
    # Use raw SQL COPY for maximum performance
    logger.info("Using PostgreSQL COPY for database writes")
    
    # Create Vocabulary via COPY
    term_to_vocab_id = create_vocabulary_raw_sql(
        idf_dict, 
        pass1_result.global_df, 
        logger
    )
    
    # Create InvertedIndex via COPY
    entry_count = create_inverted_index_raw_sql(
        pass1_result.article_tf_map,
        pass1_result.article_ids,
        term_to_vocab_id,
        idf_dict,
        logger
    )
    
    logger.info(f"Pass 2 complete:")
    logger.info(f"  - Vocabulary entries: {len(idf_dict)}")
    logger.info(f"  - Inverted index entries: {entry_count}")


def pass2_build_tfidf_concurrent(
    pass1_result: Pass1Result,
    batch_size: int,
    csv_workers: int,
    db_workers: int,
    logger: logging.Logger
) -> None:
    """Pass 2: Build IDF and inverted index using concurrent batch processing.
    
    Uses ProcessPoolExecutor for CPU-bound CSV building and ThreadPoolExecutor
    for I/O-bound database writes in a producer-consumer pipeline.
    
    Args:
        pass1_result: Results from Pass 1
        batch_size: Articles per batch for inverted index
        csv_workers: Number of worker processes for CSV building
        db_workers: Number of worker threads for database writes
        logger: Logger for output
    """
    logger.info("=== Pass 2: Building IDF and Inverted Index (Concurrent) ===")
    logger.info(f"CSV workers (processes): {csv_workers}")
    logger.info(f"DB workers (threads): {db_workers}")
    logger.info(f"Batch size: {batch_size} articles")
    
    # Calculate IDF values
    logger.info("Calculating IDF values...")
    idf_start = time.time()
    idf_dict = compute_idf(pass1_result.global_df, pass1_result.total_docs)
    logger.info(f"IDF calculation completed in {time.time() - idf_start:.2f}s")
    
    # === Vocabulary Building (Concurrent) ===
    logger.info("Building Vocabulary (concurrent)...")
    vocab_start = time.time()
    
    # Prepare terms in batches (only send term strings, not df/idf data)
    terms_list = list(idf_dict.keys())
    vocab_batch_size = 10000  # Terms per batch
    terms_batches = [terms_list[i:i + vocab_batch_size] 
                     for i in range(0, len(terms_list), vocab_batch_size)]
    
    logger.info(f"Split {len(terms_list)} terms into {len(terms_batches)} batches")
    
    # Build CSV buffers in parallel (CPU-bound)
    # Use initializer to load shared data once per worker process
    csv_buffers = []
    with ProcessPoolExecutor(
        max_workers=csv_workers,
        initializer=init_vocabulary_worker,
        initargs=(pass1_result.global_df, idf_dict)
    ) as csv_executor:
        futures = [csv_executor.submit(create_vocabulary_csv_batch, batch) 
                   for batch in terms_batches]
        
        for future in tqdm(concurrent.futures.as_completed(futures), 
                          total=len(futures), 
                          desc="Building Vocab CSV", 
                          unit="batch"):
            csv_buffers.append(future.result())
    
    csv_time = time.time() - vocab_start
    logger.info(f"Vocabulary CSV building completed in {csv_time:.2f}s")
    
    # Write to database concurrently (I/O-bound)
    write_start = time.time()
    total_vocab_entries = 0
    with ThreadPoolExecutor(max_workers=db_workers) as db_executor:
        futures = [db_executor.submit(write_vocabulary_batch_sql, csv_buffer) 
                   for csv_buffer in csv_buffers]
        
        for future in tqdm(concurrent.futures.as_completed(futures), 
                          total=len(futures), 
                          desc="Writing Vocab", 
                          unit="batch"):
            total_vocab_entries += future.result()
    
    write_time = time.time() - write_start
    vocab_total_time = time.time() - vocab_start
    logger.info(f"Vocabulary write completed in {write_time:.2f}s")
    logger.info(f"Total vocabulary time: {vocab_total_time:.2f}s ({total_vocab_entries} entries)")
    
    # Query back term-to-ID mapping
    logger.info("Building term-to-ID mapping...")
    mapping_start = time.time()
    term_to_vocab_id = {}
    vocab_objects = Vocabulary.objects.all()
    for vocab in vocab_objects:
        term_to_vocab_id[vocab.term] = vocab.id
    logger.info(f"Mapped {len(term_to_vocab_id)} terms in {time.time() - mapping_start:.2f}s")
    
    # === Inverted Index Building (Concurrent with Pipeline) ===
    logger.info("Building Inverted Index (concurrent with pipeline)...")
    index_start = time.time()
    
    # Split article IDs into batches
    article_batches = [pass1_result.article_ids[i:i + batch_size] 
                       for i in range(0, len(pass1_result.article_ids), batch_size)]
    
    logger.info(f"Split {len(pass1_result.article_ids)} articles into {len(article_batches)} batches")
    
    # Pipeline CSV building and DB writes for better parallelism
    # Start DB writes as soon as CSV buffers are ready
    total_index_entries = 0
    csv_build_time = 0
    db_write_time = 0
    
    with ProcessPoolExecutor(
        max_workers=csv_workers,
        initializer=init_inverted_index_worker,
        initargs=(pass1_result.article_tf_map, term_to_vocab_id, idf_dict)
    ) as csv_executor, ThreadPoolExecutor(max_workers=db_workers) as db_executor:
        
        # Submit all CSV building tasks
        csv_futures = [
            csv_executor.submit(create_inverted_index_csv_batch, batch)
            for batch in article_batches
        ]
        
        # As CSV buffers complete, immediately submit them for DB writes
        db_futures = []
        csv_start = time.time()
        with tqdm(total=len(article_batches), desc="Building & Writing Index", unit="batch") as pbar:
            for csv_future in concurrent.futures.as_completed(csv_futures):
                csv_buffer = csv_future.result()
                # Immediately submit to DB writer
                db_future = db_executor.submit(write_inverted_index_batch_sql, csv_buffer)
                db_futures.append(db_future)
                pbar.update(1)
        
        csv_build_time = time.time() - csv_start
        
        # Wait for all DB writes to complete
        db_start = time.time()
        for db_future in concurrent.futures.as_completed(db_futures):
            total_index_entries += db_future.result()
        db_write_time = time.time() - db_start
    
    index_total_time = time.time() - index_start
    logger.info(f"Inverted index CSV building completed in {csv_build_time:.2f}s")
    logger.info(f"Inverted index DB writes completed in {db_write_time:.2f}s")
    logger.info(f"Total inverted index time: {index_total_time:.2f}s ({total_index_entries} entries)")
    
    logger.info(f"Pass 2 complete:")
    logger.info(f"  - Vocabulary entries: {total_vocab_entries}")
    logger.info(f"  - Inverted index entries: {total_index_entries}")


class Command(BaseCommand):
    help = 'Build TF-IDF index using multiprocess parallel approach with concurrent Pass 2 (200+ articles/sec)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit number of articles to process (default: all)'
        )
        parser.add_argument(
            '--profile',
            action='store_true',
            help='Enable cProfile profiling'
        )
        parser.add_argument(
            '--rebuild',
            action='store_true',
            help='Clear existing Vocabulary and InvertedIndex before building'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )
        parser.add_argument(
            '--cpu-workers',
            type=int,
            default=None,
            help='Number of CPU worker processes for Pass 1 (default: all available cores)'
        )
        parser.add_argument(
            '--batch-size-per-worker',
            type=int,
            default=400,
            help='Number of articles per worker batch in Pass 1 (default: 50)'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=400,
            help='Articles per batch for Pass 2 inverted index (default: 400, optimized for 200+ articles/sec)'
        )
        parser.add_argument(
            '--csv-workers',
            type=int,
            default=12,
            help='Number of worker processes for CSV building in Pass 2 (default: 12)'
        )
        parser.add_argument(
            '--db-workers',
            type=int,
            default=12,
            help='Number of worker threads for database writes in Pass 2 (default: 12)'
        )

    def handle(self, *args, **options):
        # Setup logging
        log_level = logging.DEBUG if options['verbose'] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        logger = logging.getLogger(__name__)
        
        # Determine CPU workers
        cpu_workers = options['cpu_workers'] or os.cpu_count()
        
        # Display configuration
        logger.info("=" * 60)
        logger.info("Multi-Process TF-IDF Builder (Concurrent Pass 2)")
        logger.info("=" * 60)
        logger.info(f"Limit: {options['limit'] or 'None (all articles)'}")
        logger.info(f"Profile: {options['profile']}")
        logger.info(f"Rebuild: {options['rebuild']}")
        logger.info(f"CPU Workers (Pass 1): {cpu_workers}")
        logger.info(f"Batch Size Per Worker (Pass 1): {options['batch_size_per_worker']}")
        logger.info(f"Pass 2 Batch Size: {options['batch_size']}")
        logger.info(f"Pass 2 CSV Workers (processes): {options['csv_workers']}")
        logger.info(f"Pass 2 DB Workers (threads): {options['db_workers']}")
        logger.info("=" * 60)
        
        # Clear existing data if rebuild
        if options['rebuild']:
            logger.info("Clearing existing Vocabulary and InvertedIndex...")
            InvertedIndex.objects.all().delete()
            Vocabulary.objects.all().delete()
            logger.info("Cleared existing data")
        
        # Main processing function
        def build_index():
            start_time = time.time()
            
            # Get articles queryset
            articles_qs = Article.objects.all()
            if options['limit']:
                articles_qs = articles_qs[:options['limit']]
            
            article_count = articles_qs.count()
            logger.info(f"Processing {article_count} articles")
            
            # Pass 1: Build TF and DF (multiprocess)
            pass1_start = time.time()
            pass1_result = pass1_build_tf_df_parallel(
                articles_qs, 
                options['batch_size_per_worker'],
                cpu_workers,
                logger
            )
            pass1_time = time.time() - pass1_start
            logger.info(f"Pass 1 completed in {pass1_time:.2f}s")
            
            # Pass 2: Build IDF and inverted index (concurrent)
            pass2_start = time.time()
            pass2_build_tfidf_concurrent(
                pass1_result,
                options['batch_size'],
                options['csv_workers'],
                options['db_workers'],
                logger
            )
            pass2_time = time.time() - pass2_start
            logger.info(f"Pass 2 completed in {pass2_time:.2f}s")
            
            # Final statistics
            total_time = time.time() - start_time
            articles_per_second = article_count / total_time if total_time > 0 else 0
            
            logger.info("=" * 60)
            logger.info("Final Statistics:")
            logger.info(f"  - Total articles processed: {article_count}")
            logger.info(f"  - Total time: {total_time:.2f}s")
            logger.info(f"  - Pass 1 time: {pass1_time:.2f}s ({pass1_time/total_time*100:.1f}%)")
            logger.info(f"  - Pass 2 time: {pass2_time:.2f}s ({pass2_time/total_time*100:.1f}%)")
            logger.info(f"  - Articles per second: {articles_per_second:.2f}")
            logger.info(f"  - Target: 200 articles/second")
            
            if articles_per_second >= 200:
                logger.info(f"  - TARGET ACHIEVED!")
            else:
                logger.info(f"  - Target missed by {200 - articles_per_second:.2f} articles/second")
            
            logger.info("=" * 60)
        
        # Run with or without profiling
        if options['profile']:
            logger.info("Running with cProfile profiling...")
            profiler = cProfile.Profile()
            profiler.enable()
            
            build_index()
            
            profiler.disable()
            
            # Save profiling results
            s = StringIO()
            stats = pstats.Stats(profiler, stream=s)
            stats.sort_stats('cumulative')
            stats.print_stats(30)  # Top 30 functions
            
            profile_output = s.getvalue()
            
            # Save to file
            profile_file = '/home/loe/Projects/wiki-search/data/build_tfidf_simple_profile.log'
            with open(profile_file, 'w') as f:
                f.write(profile_output)
            
            logger.info(f"Profile saved to {profile_file}")
            logger.info("\nTop bottlenecks:")
            logger.info(profile_output.split('\n')[0:40])  # Print first 40 lines
        else:
            build_index()
        
        logger.info("Build complete!")

