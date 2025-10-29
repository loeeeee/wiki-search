from django.contrib import admin

from .models import Article, InternalLink


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("page_id", "title", "is_disambiguation")
    search_fields = ("title",)


@admin.register(InternalLink)
class InternalLinkAdmin(admin.ModelAdmin):
    list_display = ("from_article", "to_article", "to_title")
    search_fields = ("to_title", "from_article__title", "to_article__title")
