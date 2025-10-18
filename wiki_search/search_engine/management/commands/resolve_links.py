from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "This command has been consolidated into 'load_wiki_dump'. "
        "Please run: python manage.py load_wiki_dump"
        )

    def handle(self, *args, **options):
        raise SystemExit(
            "resolve_links is deprecated. Use 'python manage.py load_wiki_dump' instead."
        )
