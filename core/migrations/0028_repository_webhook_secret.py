# Generated by Django 4.2.2 on 2023-07-24 16:38

from django.db import migrations, models

from utils.migrations import RiskyAddField


class Migration(migrations.Migration):
    """
    BEGIN;
    --
    -- Add field webhook_secret to repository
    --
    ALTER TABLE "repos" ADD COLUMN "webhook_secret" text NULL;
    COMMIT;
    """

    dependencies = [
        ("core", "0027_alter_commit_report_rename_report_commit__report_and_more"),
    ]

    operations = [
        RiskyAddField(
            model_name="repository",
            name="webhook_secret",
            field=models.TextField(null=True),
        ),
    ]
