#!/usr/bin/env python
"""Simple test script for TF-IDF functionality"""

import os
import sys
import django

# Setup Django
sys.path.append('/home/loe/Projects/wiki-search')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
django.setup()

from search_engine.models import Article
from search_engine.tokenizer import tokenize
from collections import Counter

def test_basic_functionality():
    """Test basic tokenization and document frequency computation"""
    print("Testing basic TF-IDF functionality...")
    
    # Get a few articles
    articles = Article.objects.only('id', 'plain_text_paragraphs')[:5]
    print(f"Found {len(articles)} articles")
    
    # Test tokenization
    doc_freq = Counter()
    for article in articles:
        print(f"Processing article {article.id} with {len(article.plain_text_paragraphs)} paragraphs")
        seen_terms = set()
        for para in article.plain_text_paragraphs:
            tokens = tokenize(para)
            seen_terms.update(tokens)
        doc_freq.update(seen_terms)
        print(f"  Found {len(seen_terms)} unique terms")
    
    print(f"Total unique terms across all articles: {len(doc_freq)}")
    print("Top 10 terms:", list(doc_freq.most_common(10)))
    print("Test completed successfully!")

if __name__ == "__main__":
    test_basic_functionality()
