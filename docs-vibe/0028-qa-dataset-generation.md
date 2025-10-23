# QA Dataset Generation for LLM Training

## User Intent

**Original Request**: "Create a QA dataset for LLM training. The resulting data is in the format of JSON. It follows this schema, with supporting_docs and distractor_docs, and context_size. The dataset you are working with is (1) @hotpot_dev_fullwiki_v1_toy.json and (2) the wikipedia search engine we just developed."

**Logical Rephrasing**: Generate a question-answering dataset for LLM training by processing HotpotQA data through the Wikipedia search engine, extracting supporting documents and finding distractor documents via TF-IDF search, producing outputs at multiple context sizes (8k, 32k, 128k tokens).

## Technical Approach

### Input Data
- **Source**: HotpotQA development dataset (`data/raw/hotpot_dev_fullwiki_v1.json`)
- **Format**: JSON with entries containing `_id`, `question`, `answer`, `supporting_facts`, `context`
- **Size**: ~135k entries in full dataset, 2 entries in toy dataset

### Processing Pipeline

1. **Supporting Documents Extraction**
   - Extract article titles from `supporting_facts` field
   - Query Wikipedia database using exact title matching
   - Retrieve full article content (title + paragraphs)
   - Count tokens using GPT tokenizer (tiktoken cl100k_base)

2. **Distractor Documents Selection**
   - Use TF-IDF search with supporting fact titles as queries
   - Exclude supporting documents from distractor results
   - Accumulate distractor documents until reaching context size limits
   - Ensure no duplicate articles in final results

3. **Context Size Filtering**
   - Calculate total tokens: supporting_docs + distractor_docs
   - Generate 3 separate outputs based on context size caps:
     - 8k tokens: `qa_dataset_8k.json`
     - 32k tokens: `qa_dataset_32k.json` 
     - 128k tokens: `qa_dataset_128k.json`
   - Skip entries where supporting docs alone exceed context cap

### Output Schema

```json
{
  "id": "string",
  "question": "string", 
  "gold_answer": "string",
  "supporting_docs": [{"title": "string", "text": "string"}, ...],
  "distractor_docs": [{"title": "string", "text": "string"}, ...],
  "context_size": int
}
```

### Token Counting Methodology

- **Tokenizer**: GPT tokenizer (tiktoken cl100k_base) for consistency with transformer models
- **Article Tokens**: `len(tokenize(title)) + sum(len(tokenize(paragraph)) for paragraph in paragraphs)`
- **Text Format**: Concatenate paragraphs with newlines for `text` field
- **Context Size**: Sum of all supporting_docs + distractor_docs token counts

### Search Strategy

- **Method**: TF-IDF optimized search (`search_by_tfidf_optimized`)
- **Queries**: Use supporting fact titles as search queries
- **Exclusion**: Remove supporting documents from distractor results
- **Ranking**: Sort by TF-IDF relevance scores
- **Deduplication**: Remove duplicate articles by title

### Edge Cases

- **Missing Articles**: Log warning and skip entire QA entry
- **Context Overflow**: Skip entries where supporting docs exceed context cap
- **No Distractors**: Include entries with empty distractor list
- **Duplicate Results**: Deduplicate by article title

### Performance Considerations

- **Multiprocessing**: Parallel processing with `ProcessPoolExecutor`; defaults to CPU count
- **Workers Flag**: Control concurrency with `--workers N`
- **Throughput**: ~5–6s/entry with 8 workers (vs ~13–15s sequential)
- **Progress Tracking**: `tqdm` progress bar over completed futures
- **Memory Management**: Streams entries; workers hold only per-entry state
- **Logging**: Comprehensive logging for debugging and monitoring

### Validation Strategy

1. **Toy Dataset Testing**: Validate on `hotpot_dev_fullwiki_v1_toy.json` first
2. **Token Accuracy**: Verify token counting matches expected values
3. **Search Quality**: Ensure distractor documents are relevant but distinct
4. **Schema Compliance**: Validate output JSON follows specified schema
5. **Context Filtering**: Verify entries are correctly categorized by context size

### Command Interface

```bash
python manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --workers 8 \
  --verbose
```

Flags:
- `--input PATH`: HotpotQA JSON input
- `--output-dir PATH`: Output directory
- `--context-sizes N...`: Token caps (e.g., 8000 32000 128000)
- `--workers N`: Number of worker processes (default: CPU count)
- `--limit N`: Optional cap on processed entries (smoke tests)

This approach ensures high-quality QA dataset generation with proper token counting, relevant distractor selection, and multiple context size variants for different LLM training scenarios.
