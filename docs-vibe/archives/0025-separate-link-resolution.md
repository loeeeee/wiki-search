# Separate Link Resolution Command

## Overview

The link resolution functionality has been extracted from `load_wiki_dump.py` into a standalone Django management command `resolve_links.py`. This separation provides better modularity and allows for independent execution of link resolution without reloading articles.

## Implementation Details

### New Command: resolve_links.py

The new command provides the following functionality:

- **Resolve from_article links**: Matches `from_page_id` to article `page_id` to set `from_article_id`
- **Resolve to_article links**: Matches `to_title` to article `title` to set `to_article_id`
- **Parallel processing**: Uses ThreadPoolExecutor for concurrent database operations
- **Progress tracking**: Includes tqdm progress bars for both resolution phases
- **Batch processing**: Configurable batch sizes for optimal performance

### Command Arguments

```bash
python manage.py resolve_links [options]

Options:
  --batch-size BATCH_SIZE    Batch size for processing (default: 5000)
  --db-workers DB_WORKERS    Number of database worker threads (default: 96)
  --help                     Show help message
```

### Usage Examples

```bash
# Run with default settings
python manage.py resolve_links

# Run with custom batch size and worker count
python manage.py resolve_links --batch-size 10000 --db-workers 48

# Run after loading articles
python manage.py load_wiki_dump --limit 1000
python manage.py resolve_links
```

## Updated load_wiki_dump.py

The main loading command now calls the separate `resolve_links` command after article ingestion:

```python
# Phase 2: Resolve links using separate command
with phase_timer("Resolve Links"):
    call_command('resolve_links', batch_size=batch_size, db_workers=db_workers)
```

### Benefits of Separation

1. **Independent execution**: Can re-run link resolution without reloading articles
2. **Modular design**: Clear separation of concerns between loading and linking
3. **Performance optimization**: Can tune link resolution parameters independently
4. **Debugging**: Easier to isolate and debug link resolution issues
5. **Flexibility**: Allows for different link resolution strategies

### Backward Compatibility

The `load_wiki_dump` command maintains full backward compatibility:
- Still resolves links automatically after article loading
- Same command-line interface and behavior
- All existing functionality preserved

### Database Operations

The link resolution process performs two main SQL operations:

1. **from_article resolution**:
   ```sql
   UPDATE search_engine_internallink AS link
   SET from_article_id = article.id
   FROM search_engine_article AS article
   WHERE link.from_page_id = article.page_id
   ```

2. **to_article resolution**:
   ```sql
   UPDATE search_engine_internallink AS link
   SET to_article_id = article.id
   FROM search_engine_article AS article
   WHERE link.to_title = article.title
   ```

### Performance Considerations

- Uses batch processing to avoid memory issues with large datasets
- Parallel database operations for improved throughput
- Progress bars provide real-time feedback
- Configurable worker counts for different system capabilities

### Error Handling

- Comprehensive logging for debugging
- Graceful handling of database connection issues
- Progress tracking even in case of errors
- Clear error messages for troubleshooting
