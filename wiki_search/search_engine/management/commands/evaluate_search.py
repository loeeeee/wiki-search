from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Iterable, Any

from django.core.management.base import BaseCommand, CommandError

from search_engine.models import Article
from search_engine.search import search_hybrid

logger = logging.getLogger(__name__)


def ndcg_at_k(relevances: List[int], k: int) -> float:
    import math
    rel = relevances[:k]
    dcg = 0.0
    for i, r in enumerate(rel, start=1):
        if r:
            dcg += (2**r - 1) / math.log2(i + 1)
    ideal = sorted(rel, reverse=True)
    idcg = 0.0
    for i, r in enumerate(ideal, start=1):
        if r:
            idcg += (2**r - 1) / math.log2(i + 1)
    return dcg / idcg if idcg > 0 else 0.0


def mrr_at_k(relevances: List[int], k: int) -> float:
    for i, r in enumerate(relevances[:k], start=1):
        if r:
            return 1.0 / i
    return 0.0


class Command(BaseCommand):
    help = "Offline evaluation of search quality using QA supporting titles as relevance."

    def add_arguments(self, parser):
        parser.add_argument('--qa-file', type=str, default='data/raw/hotpot_dev_fullwiki_v1.json', help='HotpotQA file')
        parser.add_argument('--limit', type=int, default=200, help='Limit number of QA entries')
        parser.add_argument('--k', type=int, default=10, help='Cutoff for metrics')
        parser.add_argument('--output', type=str, default='data/profiling/quality_results.json', help='Output JSON file')
        parser.add_argument('--verbose', action='store_true')
        # Optional single-run overrides
        parser.add_argument('--alpha', type=str, default=None, help='alpha or comma-list for grid (e.g. 0.7 or 0.7,0.85)')
        parser.add_argument('--max-candidates', type=str, default=None, help='max_candidates or comma-list for grid (e.g. 300 or 300,500)')
        parser.add_argument('--coverage-bonus', type=str, default=None, help='coverage_bonus_weight or comma-list (e.g. 0.0,0.1)')
        parser.add_argument('--min-term-match-policy', type=str, default=None, help='balanced|strict|len2_strict or comma-list')
        parser.add_argument('--strict-and-filter', type=str, default=None, help='true|false or comma-list for grid')
        parser.add_argument('--partial-title-boost', type=str, default=None, help='true|false or comma-list for grid')
        parser.add_argument('--grid', action='store_true', help='Enable grid search over comma-listed parameters')

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.DEBUG if options['verbose'] else logging.INFO)

        qa_path = Path(options['qa_file'])
        k = options['k']
        limit = options['limit']
        output_file = Path(options['output'])

        if not qa_path.exists():
            raise CommandError(f"QA file not found: {qa_path}")

        with qa_path.open('r', encoding='utf-8') as f:
            qa_data = json.load(f)

        if limit:
            qa_data = qa_data[:limit]

        def parse_list(arg: str | None, cast) -> List[Any]:
            if arg is None:
                return []
            parts = [p.strip() for p in arg.split(',')]
            if not parts:
                return []
            if cast is bool:
                def to_bool(x: str) -> bool:
                    return x.lower() in ('1', 'true', 'yes', 'y')
                return [to_bool(p) for p in parts]
            return [cast(p) for p in parts]

        def eval_params(alpha: float, max_candidates: int, coverage_bonus: float, min_term_match_policy: str, strict_and_filter: bool, partial_title_boost: bool) -> Dict[str, float | int]:
            ndcg_list: List[float] = []
            mrr_list: List[float] = []
            p_at5_list: List[float] = []
            p_at10_list: List[float] = []
            recall_at20_list: List[float] = []
            processed = 0
            for entry in qa_data:
                supporting_facts = entry.get('supporting_facts', [])
                if not supporting_facts:
                    continue
                titles: List[str] = [fact[0] for fact in supporting_facts if len(fact) >= 1]
                query = titles[0]
                relevant_titles: Set[str] = set(titles)
                results = search_hybrid(
                    query,
                    limit=max(20, k),
                    alpha=alpha,
                    max_candidates=max_candidates,
                    coverage_bonus_weight=coverage_bonus,
                    strict_and_filter=strict_and_filter,
                    min_term_match_policy=min_term_match_policy,
                    enable_partial_title_boost=partial_title_boost,
                )
                retrieved_titles = [a.title for (a, _score) in results]
                rel_vec = [1 if t in relevant_titles else 0 for t in retrieved_titles]
                ndcg_list.append(ndcg_at_k(rel_vec, k))
                mrr_list.append(mrr_at_k(rel_vec, k))
                p_at5_list.append(sum(rel_vec[:5]) / 5.0 if len(rel_vec) >= 5 else 0.0)
                p_at10_list.append(sum(rel_vec[:10]) / 10.0 if len(rel_vec) >= 10 else 0.0)
                recall_at20_list.append((sum(rel_vec[:20]) / len(relevant_titles)) if len(relevant_titles) > 0 else 0.0)
                processed += 1
            return {
                'entries': processed,
                'ndcg_at_10': sum(ndcg_list) / len(ndcg_list) if ndcg_list else 0.0,
                'mrr_at_10': sum(mrr_list) / len(mrr_list) if mrr_list else 0.0,
                'precision_at_5': sum(p_at5_list) / len(p_at5_list) if p_at5_list else 0.0,
                'precision_at_10': sum(p_at10_list) / len(p_at10_list) if p_at10_list else 0.0,
                'recall_at_20': sum(recall_at20_list) / len(recall_at20_list) if recall_at20_list else 0.0,
            }

        # Build search space
        alphas = parse_list(options.get('alpha'), float)
        max_cands = parse_list(options.get('max_candidates'), int)
        cov_bonuses = parse_list(options.get('coverage_bonus'), float)
        policies = parse_list(options.get('min_term_match_policy'), str)
        stricts = parse_list(options.get('strict_and_filter'), bool)
        partials = parse_list(options.get('partial_title_boost'), bool)

        output: Dict[str, Any] = {'k': k}

        # If grid requested and at least one list provided, do grid search
        do_grid = options.get('grid') and any([alphas, max_cands, cov_bonuses, policies, stricts, partials])
        if do_grid:
            # Provide sensible defaults when a dim is empty
            if not alphas:
                alphas = [0.85]
            if not max_cands:
                max_cands = [500]
            if not cov_bonuses:
                cov_bonuses = [0.1]
            if not policies:
                policies = ['balanced']
            if not stricts:
                stricts = [False]
            if not partials:
                partials = [False]

            grid_results: List[Dict[str, Any]] = []
            best_ndcg: Dict[str, Any] = {'metrics': {'ndcg_at_10': -1}}
            best_mrr: Dict[str, Any] = {'metrics': {'mrr_at_10': -1}}

            for a in alphas:
                for mc in max_cands:
                    for cb in cov_bonuses:
                        for pol in policies:
                            for st in stricts:
                                for ptb in partials:
                                    metrics = eval_params(a, mc, cb, pol, st, ptb)
                                    entry = {
                                        'params': {
                                            'alpha': a,
                                            'max_candidates': mc,
                                            'coverage_bonus_weight': cb,
                                            'min_term_match_policy': pol,
                                            'strict_and_filter': st,
                                            'enable_partial_title_boost': ptb,
                                        },
                                        'metrics': metrics,
                                    }
                                    grid_results.append(entry)
                                    if metrics['ndcg_at_10'] > best_ndcg['metrics']['ndcg_at_10']:
                                        best_ndcg = entry
                                    if metrics['mrr_at_10'] > best_mrr['metrics']['mrr_at_10']:
                                        best_mrr = entry

            output['entries'] = grid_results[0]['metrics']['entries'] if grid_results else 0
            output['grid_results'] = grid_results
            output['best_by_ndcg'] = best_ndcg
            output['best_by_mrr'] = best_mrr
        else:
            # Single run (defaults or overridden by single values)
            alpha = float(alphas[0]) if alphas else 0.85
            mc = int(max_cands[0]) if max_cands else 500
            cb = float(cov_bonuses[0]) if cov_bonuses else 0.1
            pol = str(policies[0]) if policies else 'balanced'
            st = bool(stricts[0]) if stricts else False
            ptb = bool(partials[0]) if partials else False
            metrics = eval_params(alpha, mc, cb, pol, st, ptb)
            output.update({
                'entries': metrics['entries'],
                'metrics': metrics,
                'params': {
                    'alpha': alpha,
                    'max_candidates': mc,
                    'coverage_bonus_weight': cb,
                    'min_term_match_policy': pol,
                    'strict_and_filter': st,
                    'enable_partial_title_boost': ptb,
                }
            })

        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(output, indent=2), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f"Quality results written to {output_file}"))


