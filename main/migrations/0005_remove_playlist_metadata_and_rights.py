from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0004_genuine_mix_settings"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="playlist",
            name="category",
        ),
        migrations.RemoveField(
            model_name="playlist",
            name="description",
        ),
        migrations.RemoveField(
            model_name="playlist",
            name="rights_confirmed",
        ),
        migrations.RemoveField(
            model_name="track",
            name="rights_confirmed",
        ),
    ]
