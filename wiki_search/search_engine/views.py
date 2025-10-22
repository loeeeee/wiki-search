import re
import urllib.parse
from typing import List, Optional

from django.shortcuts import render, get_object_or_404
from django.http import Http404

from .models import Article, Redirect
from .search import search_hybrid


def search_view(request):
    """Handle search queries and display results."""
    query = request.GET.get('q', '').strip()
    results = []
    
    if query:
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
        'has_results': len(results) > 0
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
