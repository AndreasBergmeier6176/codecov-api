# Generated by Django 2.1.3 on 2020-05-14 21:10

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('codecov_auth', '0005_auto_20200508_2014'),
    ]

    operations = [
        migrations.AddField(
            model_name='owner',
            name='student',
            field=models.BooleanField(default=False),
        ),
    ]
