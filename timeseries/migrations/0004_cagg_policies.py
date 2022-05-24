# Generated by Django 3.1.13 on 2022-05-24 14:51

from django.db import migrations

CAGG_POLICIES = {
    "hour": {
        "lookback": "1 day",
        "refresh_every": "1 hour",
    },
    "day": {
        "lookback": "1 week",
        "refresh_every": "1 day",
    },
    "week": {"lookback": "30 days", "refresh_every": "1 day"},
}


class Migration(migrations.Migration):

    dependencies = [
        ("timeseries", "0003_measurement_summary"),
    ]

    operations = [
        migrations.RunSQL(
            f"""
            select add_continuous_aggregate_policy(
                'timeseries_measurement_summary_{name}',
                start_offset => INTERVAL '{intervals['lookback']}',
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '{intervals['refresh_every']}'
            );
            """,
            reverse_sql="select remove_continuous_aggregate_policy('timeseries_measurement_summary_{name}');",
        )
        for name, intervals in CAGG_POLICIES.items()
    ]
