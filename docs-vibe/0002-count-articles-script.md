# Article Counting Django Management Command

## Overview

A Django management command to count the number of articles in the Wikipedia dump. The command processes compressed bz2 files containing JSON lines, where each line represents a Wikipedia article.

## Command Location

- **File**: `wiki_search/search_engine/management/commands/count_articles.py`
- **Command**: `python wiki_search/manage.py count_articles`

## Features

- **Full Count**: Process all 15,517 bz2 files for exact article count
- **Estimation**: Use 1% sampling for quick estimates
- **Custom Sampling**: Sample a specific number of files
- **Progress Tracking**: Uses tqdm for progress bars
- **Detailed Statistics**: Shows min/max/average articles per file
- **Error Handling**: Gracefully handles corrupted files
- **Logging**: Comprehensive logging to console and file

## Usage

### Quick Estimate (Recommended)
```bash
python wiki_search/manage.py count_articles --estimate
```
- Samples 1% of files (155 files)
- Provides estimated total count
- Takes about 20 seconds

### Custom Sample Size
```bash
python wiki_search/manage.py count_articles --sample 100
```
- Samples exactly 100 files
- Shows actual count for sampled files

### Full Count (Time Intensive)
```bash
python wiki_search/manage.py count_articles
```
- Processes all 15,517 files
- Provides exact total count
- Takes several hours to complete

### Custom Directory
```bash
python wiki_search/manage.py count_articles --processed-dir /path/to/processed/dump
```

### Verbose Output
```bash
python wiki_search/manage.py count_articles --estimate --verbose
```
- Shows detailed progress for each file
- Includes debug-level logging

## Results

Based on the 1% sample estimation:
- **Estimated Total Articles**: ~5,357,970 articles
- **Average Articles per File**: ~345 articles
- **File Size Range**: 37-1,061 articles per file
- **Total Files**: 15,517 bz2 files

## Technical Details

### Data Format
Each bz2 file contains JSON lines with the following structure:
```json
{
  "id": "12",
  "url": "https://en.wikipedia.org/wiki?curid=12",
  "title": "Anarchism",
  "text": [["Anarchism"], ["Article content..."]]
}
```

### Performance
- **Processing Speed**: ~5-7 files per second (faster than standalone script)
- **Memory Usage**: Low (streaming processing)
- **Error Recovery**: Continues processing if individual files fail

### Logging
- **Console Output**: Real-time progress and results with Django styling
- **Log File**: `count_articles.log` with detailed information
- **Log Level**: INFO (configurable with --verbose flag)

## Implementation Notes

- **Django Integration**: Follows Django management command conventions
- **Uses `bz2` module** for decompression
- **Implements buffered reading** for performance
- **Handles JSON parsing errors** gracefully
- **Uses standard `json` library** (no external dependencies)
- **Includes comprehensive error handling** and logging
- **Django styling**: Uses `self.style.SUCCESS()` for colored output
- **Command arguments**: Full argparse integration with Django's argument system
