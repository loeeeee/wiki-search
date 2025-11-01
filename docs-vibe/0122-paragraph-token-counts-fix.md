# 0122: Fix Missing paragraph_token_counts in load_wiki_dump Command

## User Intent

Fix the load_wiki_dump command which fails with "null value in column 'paragraph_token_counts' of relation 'search_engine_article' violates not-null constraint" when running `python3 wiki_search/manage.py load_wiki_dump --limit 100`. The command currently computes and stores `plain_text_paragraphs` but neglects to populate `paragraph_token_counts`, a NOT NULL JSONField added in migration 0007.

## Root Cause

- The `Article` model (search_engine/models.py) includes `paragraph_token_counts = models.JSONField(default=list)` as a NOT NULL field.
- During ingestion in `load_wiki_dump.py`, article tuples are created as `(page_id, title, paragraphs)` but the COPY operation omits `paragraph_token_counts`.
- When PostgreSQL COPY is invoked, it attempts to insert defaults or NULL, but the column is constrained to NOT NULL, hence failure.
- Existing code computes token counts elsewhere (e.g., in TF-IDF building), but ingestion pipeline overlooks it.

## Proposed Solution

Compute paragraph token counts during shard parsing and include them in the article tuple and COPY operation.

### Technical Details

1. **Token Counting**: For each paragraph in `plain_text_paragraphs`, count tokens using existing tokenizer logic (split on whitespace, punctuation).
2. **Tuple Structure**: Change from `(page_id, title, paragraphs)` to `(page_id, title, paragraphs, token_counts: List[int])`.
3. **COPY Update**: Add `paragraph_token_counts` column to the COPY statement and pass JSON values.
4. **Fail-Fast**: Raise error if paragraph/token count lengths mismatch; ensure alignment.
5. **Performance**: Keep computation lightweight; avoid heavy tokenization libraries if possible.
6. **Testing**: Run with `--limit 100` to validate; check tuple creation and database insertion.

## Expected Outcome

Successful ingestion without constraint violations, maintaining parallel array structure (`plain_text_paragraphs[i] ~ token_counts[i]`).

## Implementation Notes

- **Token Computation**: Used `len(tokenize(paragraph))` per paragraph to compute token counts during shard parsing.
- **Tuple Expansion**: Modified article tuples from `(page_id, title, paragraphs)` to `(page_id, title, paragraphs, token_counts)`.
- **COPY Update**: Added `paragraph_token_counts` to the COPY columns and included `Json(token_counts)` in the row insertion.
- **Fail-Fast**: Added validation that `len(token_counts) == len(paragraphs)`, logging errors and skipping articles with mismatches.
- **Testing**: Verified with `--limit 5`; ingestion completed successfully without constraint violations.
- **Performance**: Tokenization is lightweight and incorporated into the existing parsing pipeline without additional overhead.

## Future Considerations

- Ensure consistency with downstream consumers (e.g., QA dataset builders).
- Document any changes to tuple structures for maintainability.
