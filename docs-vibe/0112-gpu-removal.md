# GPU Code Removal from PageRank

## User Intent

User's request:
> Since GPU offer no benefits, please remove the GPU code in the pagerank. And because now page rank is the only script that requires GPU, you also need to update project dependency.

## Context

After comprehensive testing (docs-vibe/0111-pagerank-single-threaded-implementation.md), GPU acceleration proved to be detrimental for PageRank computation:

**Test Results:**
- **Small datasets (1k)**: GPU 1.5x slower than CPU
- **Medium datasets (10k)**: GPU 1.2x slower than CPU  
- **Large datasets (100k+)**: GPU OOM failure

**Root Causes:**
1. GPU implementation converts sparse→dense matrix (18,857x memory inflation)
2. Transfer overhead dominates for small datasets
3. Storage phase (96.6% of time) cannot be GPU-accelerated
4. CPU is faster and more memory efficient

## Changes Made

### 1. Removed GPU Code from pagerank.py

**Removed:**
- PyTorch imports (`torch`, `torch.sparse`)
- GPU availability flags (`TORCH_AVAILABLE`, `GPU_AVAILABLE`)
- `compute_pagerank_gpu()` function (123 lines)

**Kept:**
- `compute_pagerank()` - CPU implementation
- `compute_pagerank_parallel()` - CPU parallel implementation
- NumPy/SciPy dependencies

### 2. Updated build_pagerank.py Command

**Removed:**
- `--use-gpu` command-line argument
- GPU availability checking code
- GPU device info display
- GPU branch in computation phase
- `compute_pagerank_gpu` import

**Result:**
- Simpler command interface
- Faster startup (no GPU detection)
- Clearer user experience (no confusing GPU option)

### 3. Updated Dependencies

**pyproject.toml:**
- Removed `torch>=2.9.0` from main dependencies
- Removed `[project.optional-dependencies] gpu` section

**shell.nix:**
- Removed `torchWithRocm` and torch ecosystem packages
- Removed duplicate Python package definitions
- Simplified to single Python environment definition
- Kept NumPy, SciPy for CPU computation

### 4. Updated Documentation

**README.md:**
- Removed `--use-gpu` from command examples
- Removed GPU performance comparison section
- Removed GPU acceleration notes
- Kept CPU performance characteristics

**docs-vibe/0111-pagerank-single-threaded-implementation.md:**
- Removed PyTorch from test environment
- Simplified performance table (removed GPU columns)
- Removed GPU findings from critical findings
- Added "GPU Code Removed" section explaining rationale
- Updated optimization recommendations

## Files Modified

### Code Files
1. `wiki_search/search_engine/pagerank.py`
   - Removed: 9 lines (imports)
   - Removed: 123 lines (GPU function)
   - Total: -132 lines

2. `wiki_search/search_engine/management/commands/build_pagerank.py`
   - Removed: GPU imports, arguments, checking code
   - Total: -36 lines

### Configuration Files
3. `pyproject.toml`
   - Removed torch dependency
   - Removed optional GPU dependencies

4. `shell.nix`
   - Removed torch packages
   - Simplified Python environment
   - Total: -42 lines

### Documentation Files
5. `README.md`
   - Removed GPU examples and notes
   - Simplified performance section

6. `docs-vibe/0111-pagerank-single-threaded-implementation.md`
   - Removed GPU test results
   - Added removal rationale
   - Simplified recommendations

7. `docs-vibe/0112-gpu-removal.md` (new)
   - This document

## Testing

Verified the changes work correctly:

```bash
python wiki_search/manage.py build_pagerank --limit 1000 --rebuild --verbose
```

**Results:**
- Command executes successfully
- No GPU-related errors or warnings
- Performance identical to previous CPU-only runs
- Clean output without GPU messages

## Benefits

1. **Simpler Codebase**:
   - 210+ lines of code removed
   - No GPU complexity to maintain
   - Easier to understand and modify

2. **Fewer Dependencies**:
   - No PyTorch requirement (large dependency)
   - Faster installation
   - Smaller deployment footprint

3. **Better Performance**:
   - CPU is actually faster for this workload
   - No transfer overhead
   - More memory efficient

4. **Clearer User Experience**:
   - No confusing GPU option
   - No "GPU not recommended" warnings
   - Straightforward command interface

## Impact

**No Breaking Changes:**
- Existing PageRank data remains valid
- CPU performance unchanged (or slightly better)
- All tests pass

**Users Should:**
- Update dependencies: `uv sync` or `pip install -e .`
- Remove `--use-gpu` from any scripts/documentation
- No need to rebuild PageRank data

## Conclusion

GPU acceleration was comprehensively tested and found to provide no benefit for PageRank computation. The code has been cleanly removed, simplifying the codebase while maintaining full functionality. CPU-only implementation is faster, more memory efficient, and easier to maintain.

The project no longer has any GPU dependencies, making it more portable and easier to deploy.

