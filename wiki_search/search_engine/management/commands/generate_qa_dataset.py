"""
Django management command to generate QA dataset from HotpotQA data.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from tqdm import tqdm

from search_engine.models import Article, TFIDFIndex, InvertedIndex
from search_engine.search import search_hybrid
from search_engine.qa_helpers import (
    count_article_tokens, 
    format_article_for_qa, 
    calculate_context_size
)

logger = logging.getLogger(__name__)


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
                distractor_docs=self.distractor_docs[:self.context_sizes[context_size][1]],
                context_size=self.context_sizes[context_size][0])
                for context_size in self.context_sizes
        }

def process_qa_entry_worker(entry_data: Dict) -> _QAEntry:
    """Worker function to process a single QA entry.
    
    This function runs in a separate process and handles:
    - Extracting supporting documents
    - Finding distractor documents
    - Calculating context size
    - Returning processed entry or None if skipped
    """
    try:
        from django.db import connection
        from search_engine.models import Article
        from search_engine.search import search_hybrid
        from search_engine.qa_helpers import (
            count_article_tokens, 
            format_article_for_qa, 
            calculate_context_size
        )
        
        # Ensure database connection is established in worker thread
        connection.ensure_connection()
        
        # Extract basic fields
        qa_id = entry_data.get('_id', '')
        question = entry_data.get('question', '')
        answer = entry_data.get('answer', '')
        supporting_facts = entry_data.get('supporting_facts', [])

        # Get supporting documents
        supporting_docs = []
        missing_articles = []

        for fact in supporting_facts:
            if len(fact) >= 1:
                title = fact[0]
                try:
                    article = Article.objects.get(title__iexact=title)
                    supporting_docs.append(format_article_for_qa(article))
                except Article.DoesNotExist:
                    missing_articles.append(title)

        if missing_articles:
            return {
                'status': 'skipped_missing_articles',
                'id': qa_id,
                'missing_articles': missing_articles
            }

        # Check if supporting docs exceed context limit (use 8k as minimum)
        supporting_tokens = sum(
            count_article_tokens(Article.objects.get(title=doc['title']))
            for doc in supporting_docs
        )

        # Get distractor documents
        distractor_docs = []
        supporting_titles = {doc['title'] for doc in supporting_docs}
        
        # Use supporting fact titles as search queries
        search_results = [search_hybrid(fact, limit=20) for fact in supporting_facts]
        cnt_distractor_tokens: int = 0

        while supporting_tokens + cnt_distractor_tokens <= 128000:
            for result_index in range(len(search_results)):
                result: Tuple[Article, float] = search_results[result_index].pop(0)
                cnt_distractor_tokens += count_article_tokens(result[0])

                    
        #             for article, score in search_results:
        #                 # Skip if it's already a supporting doc
        #                 if article.title in supporting_titles:
        #                     continue
                        
        #                 # Skip if already in distractors
        #                 if any(doc['title'] == article.title for doc in distractor_docs):
        #                     continue
                        
        #                 distractor_docs.append(format_article_for_qa(article))
        #                 logger.debug(f"Added distractor: {article.title} (score: {score:.4f})")
                        
        #                 # Check if we've reached a reasonable context limit
        #                 current_context_size = calculate_context_size(supporting_docs, distractor_docs)
        #                 if current_context_size >= 128000:  # Max context size
        #                     break
                    
        #             # Check if we should break outer loop
        #             current_context_size = calculate_context_size(supporting_docs, distractor_docs)
        #             if current_context_size >= 128000:
        #                 break
                        
        #         except Exception as e:
        #             logger.error(f"Error searching for distractors with query '{query}': {e}", exc_info=True)

        # Create QA entry
        qa_entry = {
            'id': qa_id,
            'question': question,
            'gold_answer': answer,
            'supporting_docs': supporting_docs,
            'distractor_docs': distractor_docs,
            'context_size': calculate_context_size(supporting_docs, distractor_docs)
        }

        return {
            'status': 'success',
            'entry': qa_entry
        }

    except Exception as e:
        return {
            'status': 'error',
            'id': entry_data.get('_id', 'unknown'),
            'error': str(e)
        }


class Command(BaseCommand):
    help = "Generate QA dataset from HotpotQA data with supporting and distractor documents"

    def add_arguments(self, parser):
        parser.add_argument(
            '--input',
            type=str,
            default='data/raw/hotpot_dev_fullwiki_v1.json',
            help='Path to input HotpotQA JSON file'
        )
        parser.add_argument(
            '--output-dir',
            type=str,
            default='data/processed',
            help='Directory to save output JSON files'
        )
        parser.add_argument(
            '--context-sizes',
            nargs='+',
            type=int,
            default=[8000, 32000, 128000],
            help='Context size limits in tokens (default: 8000 32000 128000)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Limit number of QA entries to process (for testing)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=cpu_count(),
            help=f'Number of worker threads (default: {cpu_count()})'
        )
        parser.add_argument(
            '--debug',
            action='store_true',
            help='Enable debug logging for troubleshooting'
        )

    def handle(self, *args, **options):
        # Configure logging
        log_level = logging.DEBUG if (options['verbose'] or options['debug']) else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler('generate_qa_dataset.log')
            ]
        )

        input_path = Path(options['input'])
        output_dir = Path(options['output_dir'])
        context_sizes = options['context_sizes']
        limit = options.get('limit')
        workers = options['workers']

        # Validate input file
        if not input_path.exists():
            raise CommandError(f"Input file not found: {input_path}")

        # Validate TF-IDF index exists
        tfidf_count = TFIDFIndex.objects.count()
        inverted_count = InvertedIndex.objects.count()
        
        if tfidf_count == 0:
            raise CommandError("TF-IDF index is empty. Please run 'python manage.py build_tfidf_index' first.")
        
        if inverted_count == 0:
            raise CommandError("Inverted index is empty. Please run 'python manage.py build_tfidf_index' first.")
        
        self.stdout.write(f"TF-IDF index validation: {tfidf_count} articles indexed, {inverted_count} inverted index entries")

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"Loading HotpotQA data from: {input_path}")
        
        # Load HotpotQA data
        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                qa_data = json.load(f)
        except Exception as e:
            raise CommandError(f"Failed to load input file: {e}")

        if limit:
            qa_data = qa_data[:limit]
            self.stdout.write(f"Limited to {limit} entries for testing")

        self.stdout.write(f"Processing {len(qa_data)} QA entries with {workers} workers...")

        # Process entries with multiprocessing
        results = self.process_qa_entries_parallel(qa_data, context_sizes, workers)

        # Generate output files
        self.generate_output_files(results, output_dir, context_sizes)

        self.stdout.write(self.style.SUCCESS("QA dataset generation completed!"))

    def process_qa_entries_parallel(self, qa_data: List[Dict], context_sizes: List[int], workers: int) -> Dict[int, List[Dict]]:
        """Process QA entries in parallel using multiprocessing."""
        results = {size: [] for size in context_sizes}
        
        stats = {
            'total': len(qa_data),
            'processed': 0,
            'skipped_missing_articles': 0,
            'skipped_context_overflow': 0,
            'errors': 0
        }

        # Process entries in parallel
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Submit all tasks
            future_to_entry = {
                executor.submit(process_qa_entry_worker, entry): entry 
                for entry in qa_data
            }
            
            # Process completed tasks with progress bar
            for future in tqdm(as_completed(future_to_entry), 
                             total=len(qa_data), 
                             desc="Processing QA entries"):
                try:
                    result = future.result()
                    
                    if result['status'] == 'success':
                        qa_entry = result['entry']
                        stats['processed'] += 1
                        
                        # Add to appropriate context size buckets
                        for context_size in context_sizes:
                            if qa_entry['context_size'] <= context_size:
                                results[context_size].append(qa_entry)
                                
                    elif result['status'] == 'skipped_missing_articles':
                        stats['skipped_missing_articles'] += 1
                        logger.warning(f"Skipping {result['id']}: Missing articles {result['missing_articles']}")
                        
                    elif result['status'] == 'skipped_context_overflow':
                        stats['skipped_context_overflow'] += 1
                        logger.warning(f"Skipping {result['id']}: Supporting docs exceed context limit ({result['supporting_tokens']} tokens)")
                        
                    elif result['status'] == 'error':
                        stats['errors'] += 1
                        logger.error(f"Error processing entry {result['id']}: {result['error']}")
                        
                except Exception as e:
                    stats['errors'] += 1
                    logger.error(f"Unexpected error processing entry: {e}")

        # Log statistics
        self.stdout.write(f"\nProcessing Statistics:")
        self.stdout.write(f"  Total entries: {stats['total']}")
        self.stdout.write(f"  Successfully processed: {stats['processed']}")
        self.stdout.write(f"  Skipped (missing articles): {stats['skipped_missing_articles']}")
        self.stdout.write(f"  Skipped (context overflow): {stats['skipped_context_overflow']}")
        self.stdout.write(f"  Errors: {stats['errors']}")

        return results


    def generate_output_files(self, results: Dict[int, List[Dict]], output_dir: Path, context_sizes: List[int]):
        """Generate output JSON files for each context size."""
        for context_size in context_sizes:
            output_file = output_dir / f"qa_dataset_{context_size}.json"
            
            self.stdout.write(f"Writing {len(results[context_size])} entries to {output_file}")
            
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(results[context_size], f, indent=2, ensure_ascii=False)
                
                self.stdout.write(f"  Context size: {context_size} tokens")
                self.stdout.write(f"  Entries: {len(results[context_size])}")
                
            except Exception as e:
                raise CommandError(f"Failed to write output file {output_file}: {e}")
