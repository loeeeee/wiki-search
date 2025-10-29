# Fix Duplicate Title Constraint Issue

## Overview

Fixed a critical loading issue in the Wikipedia dump processing pipeline where duplicate article titles caused `UniqueViolation` errors during database insertion.

## Problem Description

The `load_wiki_dump` command was failing with the following error:

```
psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "search_engine_article_title_key"
DETAIL: Key (title)=(Harry Diamond) already exists.
```

### Root Cause Analysis

1. **Database Schema Issue**: The `Article` model had a `unique=True` constraint on the `title` field
2. **Wikipedia Data Reality**: Wikipedia dumps contain legitimate duplicate titles (e.g., disambiguation pages, namespace variations, redirects)
3. **Incomplete Deduplication**: The existing deduplication logic only tracked `page_id`, not `title`

## Solution Implemented

### 1. Model Schema Change

**File**: `wiki_search/search_engine/models.py`

Removed the `unique=True` constraint from the `title` field:

```python
# Before:
title = models.CharField(max_length=512, unique=True, db_index=True)

# After:
title = models.CharField(max_length=512, db_index=True)
```

**Rationale**: 
- `page_id` remains unique (Wikipedia's true identifier)
- `title` keeps its index for search performance
- Allows legitimate duplicate titles as they exist in Wikipedia

### 2. Database Migration

**Generated Migration**: `0006_alter_article_title.py`

```python
operations = [
    migrations.AlterField(
        model_name='article',
        name='title',
        field=models.CharField(db_index=True, max_length=512),
    ),
]
```

**Applied Successfully**: Migration dropped the unique constraint on the `title` field.

### 3. Verification

The fix was verified by running the complete `load_wiki_dump` command:

```bash
uv run python wiki_search/manage.py load_wiki_dump
```

**Result**: 
- No more `UniqueViolation` errors
- Command progresses through all phases successfully
- Articles with duplicate titles are now accepted

## Technical Details

### Why This Approach

1. **Simpler**: No complex deduplication logic needed
2. **Correct**: Matches Wikipedia's actual data structure
3. **Performant**: No overhead from tracking seen titles
4. **Clean**: `page_id` is the true identifier

### Database Impact

- **Before**: Unique constraint on both `page_id` and `title`
- **After**: Unique constraint only on `page_id`
- **Index**: `title` field retains its index for search queries

## Files Modified

1. `wiki_search/search_engine/models.py` - Removed `unique=True` from title field
2. `wiki_search/search_engine/migrations/0006_alter_article_title.py` - Generated migration

## Testing

The fix was validated by:
1. Running the migration successfully
2. Executing the full `load_wiki_dump` command without errors
3. Confirming all phases complete (Article ingestion, Link resolution)

## Impact

- **Loading Pipeline**: Now handles Wikipedia's duplicate titles correctly
- **Search Performance**: Unchanged (title index preserved)
- **Data Integrity**: Maintained through `page_id` uniqueness
- **User Experience**: Wikipedia dump loading now works reliably

## Lessons Learned

1. **Data Model Design**: Consider the actual data structure of source systems
2. **Wikipedia Specifics**: Wikipedia has legitimate duplicate titles that should be preserved
3. **Constraint Strategy**: Use unique constraints only for true identifiers (`page_id`), not descriptive fields (`title`)

## Future Considerations

- Monitor for any search-related issues with duplicate titles
- Consider adding title-based filtering if needed for specific use cases
- Ensure search algorithms handle duplicate titles appropriately
