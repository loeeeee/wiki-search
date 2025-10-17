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
