from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("search_engine", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Vocabulary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("term", models.CharField(db_index=True, max_length=128, unique=True)),
                ("document_frequency", models.PositiveIntegerField(default=0)),
                ("idf_value", models.FloatField(default=0.0)),
            ],
        ),
        migrations.CreateModel(
            name="TFIDFIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tfidf_vector", models.JSONField(default=dict)),
                ("l2_norm", models.FloatField(default=0.0)),
                (
                    "article",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tfidf",
                        to="search_engine.article",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="tfidfindex",
            index=models.Index(fields=["article"], name="search_arti_articl_idx"),
        ),
    ]


