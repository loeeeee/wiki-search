from django.db import models


class Article(models.Model):
    page_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    title = models.CharField(max_length=512, unique=True, db_index=True)
    plain_text_paragraphs = models.JSONField(default=list)
    is_disambiguation = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.title


class Vocabulary(models.Model):
    term = models.CharField(max_length=128, unique=True, db_index=True)
    document_frequency = models.PositiveIntegerField(default=0)
    idf_value = models.FloatField(default=0.0)

    def __str__(self) -> str:
        return self.term


class TFIDFIndex(models.Model):
    article = models.OneToOneField('Article', on_delete=models.CASCADE, related_name='tfidf')
    tfidf_vector = models.JSONField(default=dict)
    l2_norm = models.FloatField(default=0.0)

    class Meta:
        indexes = [
            models.Index(fields=['article']),
        ]


class Redirect(models.Model):
    source_page_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    source_title = models.CharField(max_length=512, unique=True, db_index=True)
    target = models.ForeignKey('Article', on_delete=models.CASCADE, related_name='redirects')

    def __str__(self) -> str:
        return f"{self.source_title} -> {self.target.title}"


class InternalLink(models.Model):
    from_article = models.ForeignKey('Article', on_delete=models.CASCADE, related_name='out_links', null=True, blank=True)
    from_page_id = models.PositiveBigIntegerField(null=True, db_index=True)
    to_article = models.ForeignKey('Article', on_delete=models.CASCADE, related_name='in_links', null=True, blank=True)
    to_title = models.CharField(max_length=512)
    anchor_text = models.CharField(max_length=512, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['from_article']),
            models.Index(fields=['from_page_id']),
            models.Index(fields=['to_article']),
            models.Index(fields=['to_title']),
        ]


class InvertedIndex(models.Model):
    """Fast candidate filtering for TF-IDF search using inverted index."""
    term = models.ForeignKey('Vocabulary', on_delete=models.CASCADE, related_name='inverted_entries')
    article = models.ForeignKey('Article', on_delete=models.CASCADE, related_name='inverted_entries')
    tf_idf_score = models.FloatField(default=0.0)

    class Meta:
        indexes = [
            models.Index(fields=['term', 'tf_idf_score']),  # For query filtering
            models.Index(fields=['article']),
        ]
        unique_together = [['term', 'article']]  # Prevent duplicate entries

    def __str__(self) -> str:
        return f"{self.term.term} -> {self.article.title} ({self.tf_idf_score:.4f})"


class PageRank(models.Model):
    """Store precomputed PageRank scores for articles."""
    article = models.OneToOneField('Article', on_delete=models.CASCADE, related_name='pagerank')
    score = models.FloatField(default=0.0, db_index=True)
    iteration_count = models.PositiveIntegerField(default=0)  # Convergence tracking
    last_computed = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['-score']),  # For ordering by PageRank score
        ]

    def __str__(self) -> str:
        return f"{self.article.title} (PR: {self.score:.6f})"
