# Generated by Django 4.2.2 on 2023-08-28 18:27

from django.db import migrations, models

from utils.migrations import RiskyAlterField, RiskyRunSQL


class Migration(migrations.Migration):
    dependencies = [
        ("codecov_auth", "0037_owner_uses_invoice"),
    ]

    operations = [
        RiskyAlterField(
            model_name="owner",
            name="uses_invoice",
            field=models.BooleanField(default=False, null=True),
        ),
        RiskyRunSQL(
            """
            UPDATE "owners" SET "uses_invoice" = false WHERE "uses_invoice" IS NULL;
            ALTER TABLE "owners" ALTER COLUMN "uses_invoice" SET DEFAULT false;
            """
        ),
    ]
