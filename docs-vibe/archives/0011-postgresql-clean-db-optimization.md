# PostgreSQL Database Cleanup Optimization

## Overview

Optimized the `clean_db.py` management command for PostgreSQL performance by replacing SQLite-specific code with PostgreSQL's native `TRUNCATE CASCADE` operations.

## Problem

The original implementation was designed for SQLite and performed poorly on PostgreSQL:

- Used slow `DELETE` statements with row-by-row processing
- Applied SQLite-specific PRAGMA optimizations that don't work on PostgreSQL
- Missing deletion of newer tables (InvertedIndex, PageRank)
- Required foreign key constraint checks for each deletion
- Used slow `COUNT(*)` queries for progress tracking

## Solution

Replaced the complex SQLite-centric implementation with PostgreSQL-optimized `TRUNCATE CASCADE`:

### Key Changes

1. **Removed SQLite-specific code**:
   - Eliminated 150+ lines of PRAGMA logic
   - Removed `--no-fast-pragmas` and `--drop-recreate` arguments
   - Removed SQLite vendor checks

2. **Added missing model imports**:
   - Added `InvertedIndex` and `PageRank` to imports

3. **Implemented PostgreSQL TRUNCATE CASCADE**:
   ```python
   TRUNCATE TABLE 
     search_engine_internallink,
     search_engine_redirect, 
     search_engine_tfidfindex,
     search_engine_invertedindex,
     search_engine_pagerank,
     search_engine_vocabulary,
     search_engine_article
   CASCADE RESTART IDENTITY
   ```

4. **Simplified VACUUM logic**:
   - Removed SQLite VACUUM, kept only PostgreSQL `VACUUM ANALYZE`

## Performance Improvements

- **Before**: Minutes for large databases (row-by-row DELETE)
- **After**: Seconds (single TRUNCATE CASCADE operation)
- **Eliminated**: All PRAGMA overhead, slow COUNT(*) queries, sequential DELETEs

## Technical Details

### TRUNCATE CASCADE Benefits

- Deletes all rows instantly without row-by-row processing
- Handles foreign key constraints automatically via CASCADE
- Resets auto-increment sequences via RESTART IDENTITY
- Bypasses PostgreSQL's row-level locking overhead

### Table Order

Tables are listed in dependency order (child tables first):
1. InternalLink
2. Redirect  
3. TFIDFIndex
4. InvertedIndex
5. PageRank
6. Vocabulary
7. Article

### Command Arguments

Simplified to PostgreSQL-only arguments:
- `--yes`: Run non-interactively
- `--no-progress`: Skip progress bars and COUNT(*) queries

## Usage

```bash
# Interactive mode
python manage.py clean_db

# Non-interactive mode
python manage.py clean_db --yes

# Skip progress bars
python manage.py clean_db --yes --no-progress
```

## Implementation

The optimized implementation:
- Uses single `TRUNCATE CASCADE RESTART IDENTITY` statement
- Only computes counts when progress bars are enabled
- Maintains proper error handling and logging
- Follows project coding guidelines (typing, logging, tqdm)

## Files Modified

- `wiki_search/search_engine/management/commands/clean_db.py`: Complete rewrite for PostgreSQL optimization
