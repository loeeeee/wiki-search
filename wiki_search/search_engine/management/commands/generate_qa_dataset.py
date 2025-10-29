"""
Django management command to generate QA dataset from HotpotQA data.
Single-threaded implementation with profiling support.
"""

from __future__ import annotations

import json
import logging
import time
import cProfile
import pstats
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass, asdict
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from search_engine.models import Article, InvertedIndex, Vocabulary
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
            default=100,
            help='Limit number of QA entries to process (default: 100)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )
        parser.add_argument(
            '--debug',
            action='store_true',
            help='Enable debug logging for troubleshooting'
        )
        parser.add_argument(
            '--profile',
            action='store_true',
            help='Enable cProfile profiling and save results'
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
        enable_profiling = options['profile']

        # Validate input file
        if not input_path.exists():
            raise CommandError(f"Input file not found: {input_path}")

        # Validate search indexes exist
        inverted_count = InvertedIndex.objects.count()
        vocab_count = Vocabulary.objects.count()
        
        if vocab_count == 0:
            raise CommandError("Vocabulary is empty. Please run 'python manage.py build_tfidf_simple' first.")
        
        if inverted_count == 0:
            raise CommandError("Inverted index is empty. Please run 'python manage.py build_tfidf_simple' first.")
        
        self.stdout.write(f"Search index validation: {vocab_count} vocabulary terms, {inverted_count} inverted index entries")

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

        self.stdout.write(f"Processing {len(qa_data)} QA entries (single-threaded)...")

        # Process entries with optional profiling
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
            
            results, timing_stats = self.process_qa_entries(qa_data, context_sizes)
            
            profiler.disable()
            
            # Save profile stats
            profile_file = 'qa_dataset_generation.prof'
            profiler.dump_stats(profile_file)
            self.stdout.write(f"\nProfile saved to: {profile_file}")
            
            # Print profile statistics
            self.stdout.write("\nTop 30 time-consuming functions:")
            stats = pstats.Stats(profiler, stream=self.stdout)
            stats.sort_stats('cumulative')
            stats.print_stats(30)
        else:
            results, timing_stats = self.process_qa_entries(qa_data, context_sizes)

        # Print timing statistics
        self._print_timing_stats(timing_stats)

        # Generate output files
        self.generate_output_files(results, output_dir, context_sizes)

        self.stdout.write(self.style.SUCCESS("\nQA dataset generation completed!"))

    def process_qa_entries(self, qa_data: List[Dict], context_sizes: List[int]) -> Tuple[Dict[int, List[Dict]], Dict]:
        """Process QA entries in single-threaded manner with timing instrumentation."""
        results = {size: [] for size in context_sizes}
        
        stats = {
            'total': len(qa_data),
            'processed': 0,
            'skipped_missing_articles': 0,
            'skipped_context_overflow': 0,
            'errors': 0
        }
        
        timing_stats = defaultdict(list)
        
        # Process entries with progress bar
        for entry_data in tqdm(qa_data, desc="Processing QA entries"):
            entry_start = time.perf_counter()
            
            try:
                # Extract basic fields
                qa_id = entry_data.get('_id', '')
                question = entry_data.get('question', '')
                answer = entry_data.get('answer', '')
                supporting_facts = entry_data.get('supporting_facts', [])

                # Get supporting documents
                supporting_docs = []
                missing_articles = []
                
                lookup_start = time.perf_counter()
                for fact in supporting_facts:
                    if len(fact) >= 1:
                        title = fact[0]
                        try:
                            article = Article.objects.get(title__iexact=title)
                            supporting_docs.append(format_article_for_qa(article))
                        except Article.DoesNotExist:
                            missing_articles.append(title)
                timing_stats['article_lookup'].append(time.perf_counter() - lookup_start)

                if missing_articles:
                    stats['skipped_missing_articles'] += 1
                    logger.warning(f"Skipping {qa_id}: Missing articles {missing_articles}")
                    continue

                # Count supporting document tokens
                token_start = time.perf_counter()
                supporting_tokens = sum(
                    count_article_tokens(Article.objects.get(title=doc['title']))
                    for doc in supporting_docs
                )
                timing_stats['token_counting'].append(time.perf_counter() - token_start)

                # Get distractor documents using round-robin selection
                distractor_docs = []
                supporting_titles = {doc['title'] for doc in supporting_docs}
                
                # Use supporting fact titles as search queries
                search_start = time.perf_counter()
                search_results = [search_hybrid(fact[0], limit=20) for fact in supporting_facts if len(fact) > 0]
                timing_stats['search_operations'].append(time.perf_counter() - search_start)
                
                # Round-robin through search results to collect distractors up to 128k limit
                current_distractor_tokens = 0
                max_context_tokens = 128000
                search_result_indices = [0] * len(search_results)
                
                while supporting_tokens + current_distractor_tokens < max_context_tokens:
                    found_article = False
                    
                    for result_index in range(len(search_results)):
                        if search_result_indices[result_index] < len(search_results[result_index]):
                            article, score = search_results[result_index][search_result_indices[result_index]]
                            search_result_indices[result_index] += 1
                            
                            # Skip if it's already a supporting doc
                            if article.title in supporting_titles:
                                continue
                                
                            # Skip if already in distractors
                            if any(doc['title'] == article.title for doc in distractor_docs):
                                continue
                            
                            # Check if adding this article would exceed the limit
                            try:
                                article_tokens = count_article_tokens(article)
                            except Exception as e:
                                logger.error(f"Error counting tokens for article {article.title}: {e}")
                                continue
                            
                            if supporting_tokens + current_distractor_tokens + article_tokens > max_context_tokens:
                                break
                            
                            # Add the distractor
                            distractor_docs.append(format_article_for_qa(article))
                            current_distractor_tokens += article_tokens
                            found_article = True
                            logger.debug(f"Added distractor: {article.title} (score: {score:.4f}, tokens: {article_tokens})")
                            break
                    
                    # If no more articles available from any search result, break
                    if not found_article:
                        break

                # Calculate context size mapping for different target sizes
                context_sizes_map = {}
                target_sizes = [8000, 32000, 128000]
                
                for target_size in target_sizes:
                    # Calculate how many distractor docs fit within this target size
                    distractor_tokens_used = 0
                    num_distractor_docs = 0
                    
                    for doc in distractor_docs:
                        try:
                            doc_tokens = count_article_tokens(Article.objects.get(title=doc['title']))
                        except Exception as e:
                            logger.error(f"Error counting tokens for distractor doc {doc['title']}: {e}")
                            continue
                        
                        if supporting_tokens + distractor_tokens_used + doc_tokens <= target_size:
                            distractor_tokens_used += doc_tokens
                            num_distractor_docs += 1
                        else:
                            break
                    
                    # Store the actual context size and number of distractor docs
                    actual_context_size = supporting_tokens + distractor_tokens_used
                    context_sizes_map[target_size] = (actual_context_size, num_distractor_docs)

                # Create _QAEntry dataclass
                qa_entry = _QAEntry(
                    id=qa_id,
                    question=question,
                    gold_answer=answer,
                    supporting_docs=supporting_docs,
                    distractor_docs=distractor_docs,
                    context_sizes=context_sizes_map
                )

                # Add to results
                stats['processed'] += 1
                
                # Get all context size variants
                context_entries = qa_entry.get_all_context_sizes()
                
                # Add to appropriate context size buckets
                for context_size in context_sizes:
                    if context_size in context_entries:
                        entry_dict = asdict(context_entries[context_size])
                        results[context_size].append(entry_dict)

            except Exception as e:
                stats['errors'] += 1
                logger.error(f"Error processing entry {entry_data.get('_id', 'unknown')}: {e}")
                if logger.isEnabledFor(logging.DEBUG):
                    import traceback
                    logger.debug(traceback.format_exc())
            
            timing_stats['entry_total'].append(time.perf_counter() - entry_start)

        # Log statistics
        self.stdout.write(f"\nProcessing Statistics:")
        self.stdout.write(f"  Total entries: {stats['total']}")
        self.stdout.write(f"  Successfully processed: {stats['processed']}")
        self.stdout.write(f"  Skipped (missing articles): {stats['skipped_missing_articles']}")
        self.stdout.write(f"  Skipped (context overflow): {stats['skipped_context_overflow']}")
        self.stdout.write(f"  Errors: {stats['errors']}")

        return results, timing_stats

    def _print_timing_stats(self, timing_stats: Dict[str, List[float]]):
        """Print timing statistics summary."""
        self.stdout.write(f"\nTiming Statistics:")
        
        for operation, times in timing_stats.items():
            if times:
                total = sum(times)
                avg = total / len(times)
                min_time = min(times)
                max_time = max(times)
                
                self.stdout.write(f"  {operation}:")
                self.stdout.write(f"    Total: {total:.2f}s")
                self.stdout.write(f"    Average: {avg*1000:.2f}ms")
                self.stdout.write(f"    Min: {min_time*1000:.2f}ms")
                self.stdout.write(f"    Max: {max_time*1000:.2f}ms")

    def generate_output_files(self, results: Dict[int, List[Dict]], output_dir: Path, context_sizes: List[int]):
        """Generate output JSON files for each context size."""
        for context_size in context_sizes:
            output_file = output_dir / f"qa_dataset_{context_size}.json"
            
            self.stdout.write(f"\nWriting {len(results[context_size])} entries to {output_file}")
            
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(results[context_size], f, indent=2, ensure_ascii=False)
                
                self.stdout.write(f"  Context size: {context_size} tokens")
                self.stdout.write(f"  Entries: {len(results[context_size])}")
                
            except Exception as e:
                raise CommandError(f"Failed to write output file {output_file}: {e}")
