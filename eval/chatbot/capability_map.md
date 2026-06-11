# UCSC Brain Explorer Chatbot - Capability Map

## 1. Pipeline Overview

### User Journey
1. **User uploads**: Single-cell RNA-seq dataset as h5ad file (counts or normalized expression)
2. **UCE Embedding step**: 
   - Input: h5ad with gene expression matrix
   - Process: Universal Cell Embedding (UCE) foundation model encodes each cell into 1280-dimensional embedding
   - Output: h5ad with X_uce in obsm (observed multidimensional data), preserves original obs metadata
3. **Mapping/Projection step**:
   - Input: UCE embeddings + user's cell metadata
   - Process: 
     - UMAP projection using parametric UMAP model trained on reference
     - KNN (k=30 default) search in reference embedding space
     - Distance-weighted voting to predict cell types from nearest neighbors
   - Output: Arrow IPC file with:
     - x, y coordinates (UMAP projection)
     - prediction_by_<col>_top1/top2 (predicted cell type labels)
     - prediction_by_<col>_top1/top2_score (confidence scores, 0-1 range)
     - Selected user metadata columns (cardinality 2-100, not ending in _term_id)
     - Binned continuous columns as categories
4. **Chatbot access**: User can ask questions about results, reference data, cell types, marker genes

### Data Transformations

**UCE Input Processing**:
- Species detection from gene names (auto or specified)
- If Ensembl IDs: remapped to Hugo symbols using species-specific mapping tables
- If log-normalized: raw counts promoted from adata.raw.X
- Multi-GPU compatible using accelerate launch

**Projection Output**:
- **Metadata selection** (_select_meta_columns):
  - Skips: Unnamed columns, old_index, barcodes, is_primary_data, sizeFactor, prob_compromised, sum, detected, filtered_cell_count, n_genes
  - Includes categorical (2-100 unique values) and numeric columns
  - Forces "cluster" to categorical
  - Cell-label columns (containing: label, celltype, cell_type, annotation, harmoniz, region) are always categorical
  - Numeric columns with >20 unique values: binned into ~10 smart bins with nice thresholds
  - Boolean columns preserved as boolean
  - All columns converted to PyArrow categorical or typed arrays

## 2. Tool Inventory

| Tool Name | Purpose | Input Parameters | Data Source | Output |
|-----------|---------|-----------------|-------------|--------|
| **compare_columns** | Statistical association between two categorical columns (chi-squared, Cramér's V) | col_a, col_b, filters (optional), reference_a/b (optional), transpose (bool), open (bool) | Arrow projection file(s) on S3 | Chi-squared stat, p-value, Cramér's V (0-1), association strength label (negligible/weak/moderate/strong), dot plot matrix |
| **top_expressed_genes** | Rank genes by expression in a cell subset | subset (predicate), n_genes (int, default 20), reference (optional) | User h5ad + Arrow projection | Ranked gene list with expression values (log1p CPM) |
| **differential_expression** | Wilcoxon rank-sum test comparing gene expression between two cell groups | group_a (predicate), group_b (predicate), n_genes (default 20), reference (optional) | User h5ad + Arrow projection | Top DE genes per group, p-values, log-fold-change |
| **get_marker_genes** | Look up known marker genes for cell types in reference atlas | cell_types (list), annotation_column (optional), reference (optional) | Marker genes JSONL file (per reference) + metadata | Gene lists per cell type, grouped by publication/dataset |

### Tool-Specific Details

**compare_columns**:
- Filters use DNF (disjunctive normal form): [[A, B], [C]] = (A AND B) OR C
- Filter operators: lt, le, gt, ge, eq, ne, is_null, is_not_null
- Categorical values must match exactly (case-sensitive)
- Returns: contingency table, dot plot data (rows, cols, matrix, row_totals), top 12 pairings
- Can compare across two different reference projections (s3_uri_a vs s3_uri_b)

**top_expressed_genes & differential_expression**:
- Subset predicates: filters define which cells to include
- Expression values: log1p-normalized counts per 10k (log1p CPM)
- Subset with <10 cells: error
- Subset with 10-49 cells: warning included in response
- Calls remote gene expression service (HTTP POST to GENE_EXPRESSION_HOST)
- Returns cell counts (n_cells_a, n_cells_b) for context

**get_marker_genes**:
- Searches marker_genes/<reference_uuid>.marker_genes.jsonl file (JSONL format)
- Each line: {dataset_id, annotation_column, cell_type, genes: [list]}
- Publication labels injected from reference metadata (dataset_id → publication label join)
- Can return same cell type from multiple datasets/publications with different gene lists
- Returns availability list if requested cell types not found

## 3. System Prompt Context (Injected Before Any Tool Calls)

The chatbot receives a comprehensive system prompt built by `_build_system_prompt()` containing:

### Fixed System Role
- Identity: UCSC Brain Explorer assistant for mapping single-cell RNA-seq onto brain atlases using UCE
- Behavior: Answer about mapping job, reference data, cell types, biological meaning
- Disclaimer: Specialized for brain cell mapping; redirect if asked outside scope
- Chart constraints: No strikethrough formatting
- Suggestions: Always include 3-4 suggestions on first response; thereafter only if conversation has clear next direction
- Tool guidance: Use compare_columns for any pairwise column analysis; apply QC filters where relevant

### Job-Specific Info
- User filename: job.original_filename
- Cell count: Total number of cells uploaded (formatted with thousands separators)

### Per-Reference Information

For each completed projection, the system prompt includes:

**Reference Identity**:
- Reference name and version label
- Projection status (pending/running/complete/error)
- Abstract (up to 1500 chars, HTML stripped)
- List of publications with dataset IDs and cell counts
- Total cells in reference
- Brain tissues/regions covered (e.g., dorsolateral prefrontal cortex, hippocampus, cerebellum)
- Complete list of cell types (exact prediction labels)
- Disease states (if applicable)
- Developmental stages (up to 6)

**Mapping Results**:
- Total cells in output
- **Pipeline predictions** (UCE model outputs, clearly distinguished from user labels):
  - For each prediction column: rank (top1/top2), reference column name
  - Distribution: unclassified count, top 15 categories with counts and percentages
- **Prediction confidence scores**:
  - Same distribution but for score columns
- **User-supplied annotations** (from uploaded file):
  - Column name and distribution (counts, percentages)
  - Unclassified counts
- **Numeric/Boolean columns**:
  - Min, max, mean for numeric (rounded to 4 decimals)
  - True/False counts for boolean
  - Null counts for both
- **Auto-generated column assessment**:
  - Model-generated interpretation of user columns (platform hints, QC metrics to filter, annotations present)
  - Cached as column_notes in projection result

### RAG Context (Retrieved Chunks)
If available, relevant excerpts from source papers matching user query are appended, with source labels (DOI or metadata).

## 4. Biological Concepts in Scope

The system is designed for users interested in:

- **Cell identity & classification**:
  - Neuron subtypes (excitatory, inhibitory, by morphology/gene expression)
  - Glial cells (astrocytes, oligodendrocytes, microglia, etc.)
  - Cell type taxonomies and hierarchies (GABAergic neuron variants, etc.)
  - Developmental origin vs. mature identity

- **Gene expression & molecular signatures**:
  - Marker genes: genes defining a cell type, their consistency across datasets
  - Differential expression: genes upregulated in one cell type vs. another
  - Expression patterns by cell type, brain region, developmental stage
  - Tissue-specific genes, region-enriched genes

- **Projection quality & confidence**:
  - Cell type prediction confidence scores (0-1, higher = more confident)
  - Distribution of prediction confidence
  - Nearest-neighbor distance to reference cells (mean euclidean distance metric)
  - Unclassified cells (no clear neighbor voting)
  - Top1 vs. top2 predictions (second-best matches reveal ambiguous cells)

- **Cross-dataset validation**:
  - Comparison of pipeline predictions across multiple reference atlases
  - Agreement between user annotations and pipeline predictions
  - Association strength between user and predicted labels (Cramér's V)
  - Batch effects (comparing against reference predictions)

- **Rare cell types**:
  - Cell types with few cells in reference (affects voting confidence)
  - Rare populations in user dataset (cell type prediction robustness)

- **Quality control metrics** (user-supplied or computed):
  - Doublet scores, mitochondrial fractions, other QC metrics
  - Filtering cells by QC before interpretation
  - Effect of QC filtering on correlation with predictions

- **Anatomical context**:
  - Brain regions in reference (basal ganglia, cortex, hippocampus, midbrain, cerebellum, etc.)
  - Region-specific cell types and their prevalence
  - Disease/developmental state variants

- **Technical considerations**:
  - Species of origin (human, mouse, etc.) and gene ID formats
  - Expression normalization (log1p CPM) and how it affects interpretation
  - Sequencing platform effects on cell identification

## 5. Known Limitations

### Cannot Do
- **Access raw expression**: Chatbot cannot query expression of arbitrary genes; only top genes per subset
- **Export user data**: No download of processed Arrow file metadata (but users can download via UI)
- **Modify predictions**: No retraining or relabeling of cell types
- **Cross-species queries**: Limited to species in uploaded file and reference atlases
- **Batch correction**: No post-hoc batch effect removal; references handle batch internally
- **Custom reference building**: Fixed references only; cannot add user data as reference
- **Trajectory/differentiation analysis**: UMAP is spatial projection, not trajectory reconstruction
- **Spatial transcriptomics**: No handling of spatial coordinates (if present in input, treated as metadata)
- **VCF/genotype data**: Expression-based only, no genetic variant analysis
- **Pathway analysis**: No predefined pathway enrichment tools
- **Interactive tool use loops**: Max 5 tool iterations per response to avoid infinite loops

### Data Limitations
- **Metadata filtering**: Only columns in projected Arrow file are filterable; X_uce embeddings not exposed
- **Gene expression queries**: Only accessible for subsets of user data (via top_expressed_genes); not reference
- **Marker genes**: Only available if pre-computed JSONL file exists for reference
- **Score binning**: Confidence scores are binned into categories after projection; raw continuous scores not in Arrow
- **User annotation cardinality**: Columns with <2 or >100 unique values dropped from projection output
- **Numeric column handling**: Continuous columns binned; exact values lost in Arrow
- **Missing values**: Null indices in categorical columns mapped to "Unclassified"; no granular missing-reason tracking

### Performance/Scale
- **Cell count**: Tested on datasets up to millions of cells (reference); user input tested up to 100k+ cells
- **Gene expression service timeout**: 120-second timeout for DE/top-genes queries
- **Embedding retrieval**: RAG uses cosine distance on pre-computed embeddings; no real-time indexing

## 6. Suggested Question Categories

### A. Cell Type Identity & Composition
- What are the main cell types in my dataset?
- How many [cell type] cells did I sequence?
- What cell types did the pipeline predict?
- What are the top 5 cell type predictions?
- How many cells are unclassified (no prediction)?

### B. Annotation Validation
- How well do my [column A] labels agree with the pipeline's [prediction column]?
- Are my cluster annotations consistent with cell type predictions?
- What's the overlap between user annotations and reference predictions?
- Do my donor samples match pipeline predictions?

### C. Gene Expression & Markers
- What genes are highly expressed in [cell type]?
- What genes define [cell type]?
- What genes are differentially expressed between [cell type A] and [cell type B]?
- What are the marker genes for astrocytes?
- What's different about [my cluster] compared to the reference [cell type]?

### D. Projection Confidence & Quality
- How confident are the predictions?
- Which cells have uncertain predictions (top2 score close to top1)?
- Are predictions more confident for abundant cell types?
- How many cells remain unclassified?

### E. Cross-Reference Comparison
- Does my dataset agree across two different reference atlases?
- Which reference predicts my cells more confidently?
- Are predictions consistent across [reference A] and [reference B]?

### F. Quality Control & Filtering
- Should I filter cells with high mitochondrial content?
- Do doublet predictions correlate with pipeline cell types?
- How do predictions change if I exclude doublet_score > 0.5?

### G. Biological Context
- Which brain regions are well-covered in this reference?
- What cell types are GABAergic (inhibitory)?
- Which cell type is most abundant?
- Are there developmental stage-specific variants?

### H. Reference Metadata & Sources
- What publications are in this reference?
- How many cells are from [publication/dataset]?
- What tissues are covered?

### I. Problem-Solving
- My [cell type] annotation doesn't match predictions — why?
- How many cells are predicted as [unexpected type]?
- Why are some cells unclassified?
- Can I trust predictions for rare cell types?

### J. Exploratory / Open-Ended
- Summarize my mapping results.
- Are there interesting patterns in the data?
- What would you recommend I investigate?

---

## Appendix: Data Format Details

### Arrow File Schema
```
x (float32)                             UMAP X coordinate
y (float32)                             UMAP Y coordinate
prediction_by_<col>_top1 (categorical)  Top predicted label
prediction_by_<col>_top1_score (cat.)   Confidence score, binned [0-1]
prediction_by_<col>_top2 (categorical)  Runner-up prediction
prediction_by_<col>_top2_score (cat.)   Runner-up score, binned
[user metadata columns]                 Selected, binned if numeric
```

### KNN Voting Algorithm
- k=30 nearest neighbors in UCE embedding space
- Distance-weighted using Gaussian kernel (sigma = median distance per cell)
- Score = weight of top label / total weight of all neighbors (clamped ≤ 1.0)

### Cramér's V (compare_columns)
- sqrt(chi2 / (n × (k−1))) where k = min(rows, cols) in contingency table
- Interpretation: 0–0.1 negligible, 0.1–0.3 weak, 0.3–0.5 moderate, >0.5 strong

### Marker Genes JSONL
```json
{"dataset_id": "...", "annotation_column": "...", "cell_type": "...", "genes": ["GENE1", ...]}
```
One line per cell-type × annotation-column × dataset combination.
