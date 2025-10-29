import cProfile
import logging
import math
import pstats
import time
from collections import Counter
from dataclasses import dataclass
from io import StringIO
from typing import Dict, List

from django.core.management.base import BaseCommand
from django.db import transaction
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


def pass2_build_tfidf(
    pass1_result: Pass1Result,
    batch_size: int,
    logger: logging.Logger
) -> None:
    """Pass 2: Build IDF values and inverted index, save to database.
    
    Uses cached TF/DF data from Pass 1 to:
    - Calculate IDF values
    - Create Vocabulary entries
    - Build inverted index entries
    - Save to database in batches
    
    Args:
        pass1_result: Results from Pass 1
        batch_size: Batch size for bulk database operations
        logger: Logger for output
    """
    logger.info("=== Pass 2: Building IDF and Inverted Index ===")
    
    # Calculate IDF values
    logger.info("Calculating IDF values...")
    idf_dict = compute_idf(pass1_result.global_df, pass1_result.total_docs)
    
    # Create Vocabulary entries
    logger.info(f"Creating {len(idf_dict)} Vocabulary entries...")
    vocabulary_entries = []
    term_to_vocab_id = {}
    
    with transaction.atomic():
        for term, idf_value in tqdm(idf_dict.items(), desc="Creating Vocabulary", unit="term"):
            vocab_entry = Vocabulary(
                term=term,
                document_frequency=pass1_result.global_df[term],
                idf_value=idf_value
            )
            vocabulary_entries.append(vocab_entry)
            
            # Bulk create in batches
            if len(vocabulary_entries) >= batch_size:
                Vocabulary.objects.bulk_create(vocabulary_entries)
                vocabulary_entries = []
        
        # Create remaining entries
        if vocabulary_entries:
            Vocabulary.objects.bulk_create(vocabulary_entries)
    
    logger.info("Vocabulary saved to database")
    
    # Build term_to_vocab_id mapping
    logger.info("Building term-to-ID mapping...")
    vocab_objects = Vocabulary.objects.all()
    for vocab in vocab_objects:
        term_to_vocab_id[vocab.term] = vocab.id
    
    logger.info(f"Mapped {len(term_to_vocab_id)} terms to vocabulary IDs")
    
    # Build inverted index
    logger.info("Building inverted index...")
    inverted_index_entries = []
    
    with transaction.atomic():
        for article_id in tqdm(pass1_result.article_ids, desc="Building Inverted Index", unit="article"):
            tf_dict = pass1_result.article_tf_map[article_id]
            
            # Compute TF-IDF vector
            tfidf_vector = compute_tfidf_vector(tf_dict, idf_dict)
            
            # Create inverted index entries for this article
            for term, tfidf_score in tfidf_vector.items():
                if term in term_to_vocab_id:
                    inverted_index_entries.append(
                        InvertedIndex(
                            term_id=term_to_vocab_id[term],
                            article_id=article_id,
                            tf_idf_score=tfidf_score
                        )
                    )
            
            # Bulk create in batches
            if len(inverted_index_entries) >= batch_size:
                InvertedIndex.objects.bulk_create(inverted_index_entries)
                inverted_index_entries = []
        
        # Create remaining entries
        if inverted_index_entries:
            InvertedIndex.objects.bulk_create(inverted_index_entries)
    
    logger.info("Inverted index saved to database")
    logger.info(f"Pass 2 complete:")
    logger.info(f"  - Vocabulary entries: {len(idf_dict)}")
    logger.info(f"  - Inverted index entries: {InvertedIndex.objects.count()}")


class Command(BaseCommand):
    help = 'Build TF-IDF index using single-process, single-threaded approach'

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
            '--batch-size',
            type=int,
            default=500,
            help='Batch size for bulk database operations (default: 500)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )

    def handle(self, *args, **options):
        # Setup logging
        log_level = logging.DEBUG if options['verbose'] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        logger = logging.getLogger(__name__)
        
        # Display configuration
        logger.info("=" * 60)
        logger.info("Single-Thread TF-IDF Builder")
        logger.info("=" * 60)
        logger.info(f"Limit: {options['limit'] or 'None (all articles)'}")
        logger.info(f"Batch size: {options['batch_size']}")
        logger.info(f"Profile: {options['profile']}")
        logger.info(f"Rebuild: {options['rebuild']}")
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
            
            # Initialize tokenizer
            logger.info("Initializing NLTK tokenizer...")
            tokenizer = NLTKTokenizer()
            
            # Pass 1: Build TF and DF
            pass1_start = time.time()
            pass1_result = pass1_build_tf_df(articles_qs, tokenizer, logger)
            pass1_time = time.time() - pass1_start
            logger.info(f"Pass 1 completed in {pass1_time:.2f}s")
            
            # Pass 2: Build IDF and inverted index
            pass2_start = time.time()
            pass2_build_tfidf(pass1_result, options['batch_size'], logger)
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
            logger.info(f"  - Target: 20 articles/second")
            
            if articles_per_second >= 20:
                logger.info("  - ✓ TARGET ACHIEVED!")
            else:
                logger.info(f"  - ✗ Target missed by {20 - articles_per_second:.2f} articles/second")
            
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

