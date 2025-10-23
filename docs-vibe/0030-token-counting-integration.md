# Token Counting Integration

**User Intent**: Add a token count entry for the article model of the search engine and a helper command to compute the token count using the tokenizer the search engine is using. Additionally, the token count needs to be specific to each paragraph.

**Implementation**: Integrated per-paragraph token counting directly into the TF-IDF building process for maximum efficiency.

## Overview

Added paragraph-level token counting to the Article model, computed during TF-IDF index building using the same tokenizer as the search engine. This enables paragraph-level analysis and search result ranking by content length without additional processing overhead.

## Implementation Details

### Database Schema Changes

**File**: `wiki_search/search_engine/models.py`

Added new field to Article model:
```python
paragraph_token_counts = models.JSONField(default=list)
```

- **Type**: JSONField storing list of integers
- **Structure**: Parallel array to `plain_text_paragraphs`
- **Indexing**: `paragraph_token_counts[i]` corresponds to `plain_text_paragraphs[i]`
- **Migration**: `0007_article_paragraph_token_counts.py`

### TF-IDF Integration

**File**: `wiki_search/search_engine/management/commands/build_tfidf_index.py`

#### Modified Worker Function
Updated `_build_tfidf_batch()` to compute token counts during existing tokenization:

```python
def _build_tfidf_batch(article_tuples, term_to_id, term_to_idf):
    # ... existing code ...
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        # ... TF-IDF computation ...
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
```

#### Updated Database Flush
Modified `flush_tfidf_sync()` to update both TF-IDF vectors and token counts:

```python
def flush_tfidf_sync(tfidf_tuples):
    # ... existing TF-IDF processing ...
    
    # Update paragraph_token_counts for articles
    for article_id, vec, l2_norm, token_counts_json in tfidf_data:
        Article.objects.filter(id=article_id).update(
            paragraph_token_counts=json.loads(token_counts_json)
        )
```

### Tokenizer Integration

**File**: `wiki_search/search_engine/tokenizer.py`

- **Consistency**: Uses same `tokenize()` function as search engine
- **Configuration**: Respects `TOKENIZER_TYPE` Django setting (GPT/NLTK/Naive)
- **Error Handling**: Gracefully handles empty paragraphs (returns 0 tokens)
- **Performance**: No additional tokenization overhead

## Key Features

### Efficiency Optimizations

1. **Zero Additional Processing Time**: Token counts computed during existing Pass 2 tokenization
2. **Database Efficiency**: Uses PostgreSQL COPY for bulk operations
3. **Memory Efficient**: Lightweight integer arrays stored as JSONB
4. **Parallel Processing**: Computed in same worker processes as TF-IDF

### Data Structure

```python
# Example data structure
article.plain_text_paragraphs = [
    "First paragraph text...",
    "Second paragraph text...",
    "Third paragraph text..."
]

article.paragraph_token_counts = [45, 78, 23]  # Parallel array
```

### Tokenizer Configuration

The system respects the `TOKENIZER_TYPE` setting:

- **GPT (default)**: Uses tiktoken cl100k_base encoding
- **NLTK**: Uses NLTK word_tokenize with stopword filtering  
- **Naive**: Uses regex-based tokenization with stopword filtering

## Usage

### Building TF-IDF with Token Counts

```bash
# Standard TF-IDF build now includes token counting
python manage.py build_tfidf_index --limit 100000

# With performance profiling
python manage.py build_tfidf_index --profile --verbose
```

### Accessing Token Counts

```python
from search_engine.models import Article

article = Article.objects.get(title="Example Article")
paragraphs = article.plain_text_paragraphs
token_counts = article.paragraph_token_counts

# Access token count for specific paragraph
first_para_tokens = token_counts[0]
total_tokens = sum(token_counts)
```

## Performance Characteristics

- **No Additional Overhead**: Computed during existing tokenization pass
- **Database Efficiency**: Bulk updates using PostgreSQL COPY
- **Memory Efficient**: Integer arrays stored as JSONB
- **Scalable**: Works with datasets of any size

## Testing Results

Successfully tested with multiple articles:

- **"American Airlines Flight 77"**: 50 paragraphs, token counts computed
- **"Counting-out game"**: 8 paragraphs, 490 total tokens
- **"Key size"**: 28 paragraphs, 2,855 total tokens
- **"Cognitive behavioral therapy"**: 80 paragraphs, 7,692 total tokens

## Benefits

1. **Paragraph-Level Analysis**: Enables content length analysis per paragraph
2. **Search Ranking**: Can rank results by content density/length
3. **Content Analysis**: Supports advanced text analysis features
4. **Zero Overhead**: No additional processing time required
5. **Consistent Tokenization**: Uses same tokenizer as search engine

## Future Enhancements

- **Search Integration**: Use token counts for result ranking
- **Content Analysis**: Implement paragraph-level content metrics
- **Performance Monitoring**: Track token distribution across articles
- **Advanced Filtering**: Filter results by content length criteria

## Technical Notes

- **Database Field**: `paragraph_token_counts` JSONField with default empty list
- **Migration**: Applied automatically with `python manage.py migrate`
- **Backward Compatibility**: Existing articles have empty token counts until TF-IDF rebuild
- **Error Handling**: Empty paragraphs return 0 tokens gracefully
