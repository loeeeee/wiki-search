"""
Helper functions for QA dataset generation.
"""

from __future__ import annotations

from typing import Dict, List

from .models import Article
from .tokenizer import tokenize


def count_article_tokens(article: Article) -> int:
    """Count total tokens for an article including title and all paragraphs.
    
    Args:
        article: Article object to count tokens for
        
    Returns:
        Total number of tokens in the article
    """
    # Count tokens in title
    title_tokens = len(tokenize(article.title))
    
    # Count tokens in all paragraphs
    paragraph_tokens = sum(
        len(tokenize(paragraph)) 
        for paragraph in article.plain_text_paragraphs
    )
    
    return title_tokens + paragraph_tokens


def format_article_for_qa(article: Article) -> Dict[str, str]:
    """Format an article for QA dataset output.
    
    Args:
        article: Article object to format
        
    Returns:
        Dictionary with 'title' and 'text' fields
    """
    # Join paragraphs with newlines
    text = '\n'.join(article.plain_text_paragraphs)
    
    return {
        'title': article.title,
        'text': text
    }


def calculate_context_size(supporting_docs: List[Dict[str, str]], 
                          distractor_docs: List[Dict[str, str]]) -> int:
    """Calculate total context size in tokens.
    
    Args:
        supporting_docs: List of supporting document dictionaries
        distractor_docs: List of distractor document dictionaries
        
    Returns:
        Total token count for all documents
    """
    total_tokens = 0
    
    # Count supporting docs tokens
    for doc in supporting_docs:
        title_tokens = len(tokenize(doc['title']))
        text_tokens = len(tokenize(doc['text']))
        total_tokens += title_tokens + text_tokens
    
    # Count distractor docs tokens
    for doc in distractor_docs:
        title_tokens = len(tokenize(doc['title']))
        text_tokens = len(tokenize(doc['text']))
        total_tokens += title_tokens + text_tokens
    
    return total_tokens
