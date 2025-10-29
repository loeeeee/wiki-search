import json
import re
import urllib.parse
from statistics import mean
from typing import List, Optional

from django.shortcuts import render, get_object_or_404
from django.http import Http404
from django.db import connection, models

from .models import Article, Vocabulary, InvertedIndex, PageRank, InternalLink
from .search import search_hybrid
from .tokenizer import tokenize


def search_view(request):
    """Handle search queries and display results."""
    query = request.GET.get('q', '').strip()
    results = []
    tokens = []
    
    if query:
        # Tokenize query for display
        tokens = tokenize(query)
        
        # Use hybrid search with TF-IDF + PageRank, fallback to title search
        try:
            search_results = search_hybrid(query, limit=20)
            if not search_results:  # If hybrid search returns no results, try title search
                from .search import search_by_title_exact
                articles = search_by_title_exact(query, limit=20)
                search_results = [(art, 1.0) for art in articles]
        except Exception as e:
            # Fallback to simple title search if TF-IDF not available
            from .search import search_by_title_exact
            articles = search_by_title_exact(query, limit=20)
            search_results = [(art, 1.0) for art in articles]
        
        # Format results with snippets
        for article, score in search_results:
            snippet = _extract_snippet(article.plain_text_paragraphs)
            results.append({
                'article': article,
                'score': score,
                'snippet': snippet
            })
    
    context = {
        'query': query,
        'results': results,
        'has_results': len(results) > 0,
        'tokens': tokens,  # Add tokens to context
    }
    return render(request, 'search_engine/search.html', context)


def article_detail_view(request, page_id):
    """Display full article content with clickable internal links."""
    try:
        article = get_object_or_404(Article, page_id=page_id)
    except Http404:
        # Try to find via redirect
        try:
            redirect = Redirect.objects.get(source_page_id=page_id)
            article = redirect.target
        except Redirect.DoesNotExist:
            raise Http404("Article not found")
    
    # Process article content to convert internal links
    processed_paragraphs = _process_article_links(article.plain_text_paragraphs)
    
    context = {
        'article': article,
        'paragraphs': processed_paragraphs
    }
    return render(request, 'search_engine/article_detail.html', context)


def _extract_snippet(paragraphs: List[str]) -> str:
    """Extract first 200 characters from article paragraphs."""
    if not paragraphs:
        return ""
    
    # Get first non-empty paragraph
    for paragraph in paragraphs:
        if paragraph and paragraph.strip():
            text = paragraph.strip()
            return text[:200] + "..." if len(text) > 200 else text
    
    return ""


def _process_article_links(paragraphs: List[str]) -> List[str]:
    """Convert Wikipedia internal links to app URLs."""
    processed = []
    
    for paragraph in paragraphs:
        if not paragraph:
            processed.append("")
            continue
            
        # Find all <a href="...">...</a> patterns
        def replace_link(match):
            href = match.group(1)
            link_text = match.group(2)
            
            # URL decode the title
            decoded_title = urllib.parse.unquote(href)
            
            # Try to find the target article
            target_article = _resolve_article_title(decoded_title)
            
            if target_article:
                return f'<a href="/article/{target_article.page_id}/">{link_text}</a>'
            else:
                # Fallback to plain text if article not found
                return link_text
        
        # Replace all internal links
        processed_paragraph = re.sub(
            r'<a href="([^"]+)">([^<]+)</a>',
            replace_link,
            paragraph
        )
        
        processed.append(processed_paragraph)
    
    return processed


def _resolve_article_title(title: str) -> Optional[Article]:
    """Resolve a Wikipedia title to an Article object, handling redirects."""
    # First try direct lookup
    try:
        return Article.objects.get(title=title)
    except Article.DoesNotExist:
        pass
    
    # Try case-insensitive lookup
    try:
        return Article.objects.get(title__iexact=title)
    except Article.DoesNotExist:
        pass
    
    # Try to find via redirect
    try:
        redirect = Redirect.objects.get(source_title=title)
        return redirect.target
    except Redirect.DoesNotExist:
        pass
    
    # Try case-insensitive redirect lookup
    try:
        redirect = Redirect.objects.get(source_title__iexact=title)
        return redirect.target
    except Redirect.DoesNotExist:
        pass
    
    return None


def status_view(request):
    """Display comprehensive database statistics and system information."""
    try:
        # Basic counts
        article_count = Article.objects.count()
        redirect_count = Redirect.objects.count()
        link_count = InternalLink.objects.count()
        unresolved_links = InternalLink.objects.filter(to_article__isnull=True).count()
        
        # Search index statistics
        vocabulary_count = Vocabulary.objects.count()
        tfidf_count = TFIDFIndex.objects.count()
        inverted_index_count = InvertedIndex.objects.count()
        pagerank_count = PageRank.objects.count()
        
        # Sample statistics for performance - optimized
        sample_size = 100  # Reduced sample size for faster loading
        articles_sample = Article.objects.all().only("plain_text_paragraphs")[:sample_size]
        
        # Calculate average paragraphs per article
        paragraph_counts = []
        for article in articles_sample:
            try:
                paragraph_counts.append(len(article.plain_text_paragraphs or []))
            except Exception:
                try:
                    paragraph_counts.append(len(json.loads(article.plain_text_paragraphs) or []))
                except Exception:
                    pass
        
        avg_paragraphs = mean(paragraph_counts) if paragraph_counts else 0.0
        
        # Calculate average links per article (outgoing) - optimized
        avg_outgoing_links = 0
        if link_count > 0 and article_count > 0:
            # Simple approximation: total links / total articles
            avg_outgoing_links = link_count / article_count
        
        # Calculate average links per article (incoming) - same as outgoing for simplicity
        avg_incoming_links = avg_outgoing_links
        
        # Database metadata
        db_backend = connection.vendor
        try:
            db_version = connection.get_server_version()
        except AttributeError:
            # get_server_version() not available on all backends
            db_version = "Unknown"
        
        # PageRank metadata - simplified for performance
        pagerank_stats = {}
        if pagerank_count > 0:
            try:
                # Single aggregation query for all stats
                stats = PageRank.objects.aggregate(
                    max_score=models.Max('score'),
                    min_score=models.Min('score'),
                    avg_score=models.Avg('score')
                )
                pagerank_stats = {
                    'max_score': stats['max_score'],
                    'min_score': stats['min_score'],
                    'avg_score': stats['avg_score'],
                    'last_computed': PageRank.objects.order_by('-last_computed').first().last_computed if PageRank.objects.exists() else None
                }
            except Exception:
                pagerank_stats = {}
        
        # TF-IDF statistics - simplified
        tfidf_stats = {}
        if tfidf_count > 0:
            try:
                stats = TFIDFIndex.objects.aggregate(
                    avg_norm=models.Avg('l2_norm'),
                    max_norm=models.Max('l2_norm')
                )
                tfidf_stats = {
                    'avg_l2_norm': stats['avg_norm'],
                    'max_l2_norm': stats['max_norm']
                }
            except Exception:
                tfidf_stats = {}
        
        # Vocabulary statistics - simplified
        vocab_stats = {}
        if vocabulary_count > 0:
            try:
                stats = Vocabulary.objects.aggregate(
                    avg_df=models.Avg('document_frequency'),
                    max_df=models.Max('document_frequency'),
                    avg_idf=models.Avg('idf_value')
                )
                vocab_stats = {
                    'avg_document_frequency': stats['avg_df'],
                    'max_document_frequency': stats['max_df'],
                    'avg_idf': stats['avg_idf']
                }
            except Exception:
                vocab_stats = {}
        
        context = {
            'article_count': article_count,
            'redirect_count': redirect_count,
            'link_count': link_count,
            'unresolved_links': unresolved_links,
            'vocabulary_count': vocabulary_count,
            'tfidf_count': tfidf_count,
            'inverted_index_count': inverted_index_count,
            'pagerank_count': pagerank_count,
            'avg_paragraphs': avg_paragraphs,
            'avg_outgoing_links': avg_outgoing_links,
            'avg_incoming_links': avg_incoming_links,
            'db_backend': db_backend,
            'db_version': db_version,
            'pagerank_stats': pagerank_stats,
            'tfidf_stats': tfidf_stats,
            'vocab_stats': vocab_stats,
            'sample_size': len(paragraph_counts)
        }
        
    except Exception as e:
        # Handle any database errors gracefully
        context = {
            'error': str(e),
            'article_count': 0,
            'redirect_count': 0,
            'link_count': 0,
            'unresolved_links': 0,
            'vocabulary_count': 0,
            'tfidf_count': 0,
            'inverted_index_count': 0,
            'pagerank_count': 0,
            'avg_paragraphs': 0.0,
            'avg_outgoing_links': 0.0,
            'avg_incoming_links': 0.0,
            'db_backend': 'Unknown',
            'db_version': 'Unknown',
            'pagerank_stats': {},
            'tfidf_stats': {},
            'vocab_stats': {}
        }
    
    return render(request, 'search_engine/status.html', context)
