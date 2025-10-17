# Internal Link Extraction Implementation

## Overview

This document describes the implementation of internal link extraction in the Wikipedia dump loading pipeline. The feature extracts internal Wikipedia links from article text and stores them in the `InternalLink` model for later use in search and navigation features.

## Implementation Details

### Link Extraction Function

**File**: `wiki_search/search_engine/ingest/parser.py`

Added `extract_internal_links()` function that:
- Parses HTML anchor tags from Wikipedia article text
- Extracts internal links (those without external domains)
- URL-decodes href values (e.g., `Pierre-Joseph%20Proudhon` → `Pierre-Joseph Proudhon`)
- Extracts anchor text from within tags
- Returns list of `(target_title, anchor_text)` tuples

```python
def extract_internal_links(text_field: Iterable[Iterable[str]]) -> List[Tuple[str, str]]:
    """Extract internal Wikipedia links from article text.
    
    Args:
        text_field: List of paragraphs, each containing HTML with anchor tags
        
    Returns:
        List of (target_title, anchor_text) tuples for internal links
    """
```

### Pipeline Integration

**File**: `wiki_search/search_engine/management/commands/load_wiki_dump.py`

Modified the Wikipedia dump loading pipeline to:

1. **Extract links during article processing**: Links are extracted alongside article text parsing
2. **Batch link storage**: Links are accumulated in batches and stored after articles are created
3. **Handle unresolved links**: All links are stored with `to_article=None` initially (resolved in future pass)
4. **Track link metrics**: Added link counting to checkpoints and progress reporting

### Key Changes

1. **Worker data structure**: Updated `batch_buffer` to include link data:
   ```python
   batch_buffer: list[tuple[int, Optional[str], list[str], list[tuple[str, str]]]] = []
   # (page_id, title, paragraphs, links)
   ```

2. **Link batch processing**: Added `_flush_link_batch()` method for bulk link insertion

3. **Checkpoint tracking**: Extended checkpoint data to include `total_links_created`

4. **Progress reporting**: Updated output to show link creation counts

## Performance Considerations

- **Bulk operations**: Links are inserted using `bulk_create()` for optimal performance
- **Batch processing**: Links are processed in the same batches as articles
- **Memory efficiency**: Link data is processed incrementally without loading all links into memory
- **Error handling**: Individual link insertion fallback if bulk operations fail

## Usage

The link extraction runs automatically during the normal Wikipedia dump loading process:

```bash
python wiki_search/manage.py load_wiki_dump --limit 1000
```

Links are extracted from all paragraphs of each article and stored in the `InternalLink` model with:
- `from_article`: Reference to the source article
- `to_title`: Target article title (URL-decoded)
- `anchor_text`: Display text from the link
- `to_article`: Initially `None` (to be resolved in future pass)

## Database Schema

The `InternalLink` model includes:
- Foreign key to source article (`from_article`)
- Foreign key to target article (`to_article`, nullable)
- Target title string (`to_title`)
- Anchor text (`anchor_text`)
- Indexes on `from_article`, `to_article`, and `to_title` for efficient querying

## Performance Optimization

### Link Processing Optimization (2024)

The initial implementation caused the main process to become overloaded (100% CPU) because it was accumulating and processing thousands of links sequentially. This was optimized by:

1. **Separate Link Processing**: Workers now emit separate `link_batch` messages containing link data
2. **Parallel Link Handling**: Links are processed independently in the main loop without blocking article processing
3. **Reduced Main Process Load**: Main process no longer accumulates large link lists in memory
4. **Better Throughput**: Workers can continue processing while links are being inserted

**Key Changes**:
- Workers maintain separate `batch_buffer` (articles) and `link_buffer` (links)
- Links are emitted when buffer reaches threshold (typically 1000 links)
- Main process handles `link_batch` messages independently from `record_batch`
- Database queries for article IDs happen only when processing link batches

**Performance Impact**:
- Main process CPU usage significantly reduced
- Better parallelization across worker processes
- Improved overall pipeline throughput
- Same data integrity maintained

## Future Enhancements

1. **Link resolution**: Implement a second pass to resolve `to_article` references
2. **Duplicate handling**: Consider deduplicating links within articles
3. **Link validation**: Add validation for malformed or invalid links
4. **Further optimization**: Consider link-specific worker processes for very large datasets
