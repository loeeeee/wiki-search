# Status Page Implementation

## Overview

Implemented a comprehensive database status page accessible at `/status/` that displays real-time statistics about the Wikipedia search engine's database and search indexes.

## Implementation Details

### View Function (`views.py`)

Added `status_view` function that efficiently collects database statistics:

**Basic Statistics:**
- Article count, redirects, internal links, unresolved links
- Search index counts (Vocabulary, TFIDFIndex, InvertedIndex, PageRank)

**Performance-Optimized Sampling:**
- Uses 1000-article samples for expensive calculations
- Calculates average paragraphs per article from JSON field
- Computes average outgoing/incoming links per article
- Handles JSON parsing errors gracefully

**Advanced Statistics:**
- PageRank score aggregations (min/max/average)
- TF-IDF vector statistics (L2 norm aggregations)
- Vocabulary statistics (document frequency, IDF values)
- Database metadata (backend type, version)

**Error Handling:**
- Graceful fallback for missing indexes
- Exception handling for database errors
- Returns zero values when indexes unavailable

### URL Configuration (`urls.py`)

Added route: `path('status/', views.status_view, name='status')`

### Template (`status.html`)

**Design Features:**
- Extends `base.html` for consistency
- Responsive grid layout for statistics cards
- Auto-refresh every 30 seconds via JavaScript
- Manual refresh button for immediate updates

**Statistics Display:**
- Organized into logical sections (Basic, Search Indexes, Content, PageRank, TF-IDF, Vocabulary)
- Number formatting with appropriate decimal places
- Conditional display of advanced statistics (only when data available)
- System information with database backend details

**User Experience:**
- Clean, card-based layout
- Mobile-responsive design
- Real-time timestamp display
- Error message display for database issues

### Styling

CSS included directly in template for simplicity:
- Statistics cards with clean borders and shadows
- Responsive grid layout
- Number formatting with monospace font
- Color-coded information sections
- Mobile-optimized responsive design

## Query Optimizations

**Efficient Counting:**
- Uses `.count()` for basic statistics
- Avoids loading full objects for counts

**Sampling Strategy:**
- 1000-article samples for expensive calculations
- `only()` method to limit field selection
- Batch processing for link calculations

**Aggregation Queries:**
- Uses Django's `aggregate()` for min/max/average calculations
- Single queries for multiple statistics
- Proper error handling for missing data

## Database Statistics Displayed

### Basic Statistics
- Articles, redirects, internal links, unresolved links
- Search index entry counts

### Content Analysis
- Average paragraphs per article (sampled)
- Average outgoing/incoming links per article
- Sample size information

### Search Index Details
- PageRank score statistics (when available)
- TF-IDF vector L2 norm statistics
- Vocabulary document frequency and IDF statistics

### System Information
- Database backend (PostgreSQL/SQLite)
- Database version
- Last updated timestamp
- Auto-refresh status

## Performance Considerations

**Sampling Approach:**
- Uses 1000-article samples for expensive calculations
- Balances accuracy with performance
- Handles large datasets efficiently

**Query Optimization:**
- Efficient counting queries
- Minimal data loading with `only()`
- Aggregation queries for statistics

**Error Resilience:**
- Graceful handling of missing indexes
- Fallback values for failed calculations
- Database error reporting

## Usage

Access the status page at `http://localhost:8000/status/` to monitor:
- Database health and statistics
- Search index completeness
- Content analysis metrics
- System performance indicators

The page auto-refreshes every 30 seconds and provides manual refresh capability for real-time monitoring.
