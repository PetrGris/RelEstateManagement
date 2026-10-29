import uuid
from django.db import migrations, models


def fill_tokens(apps, schema_editor):
    Payment = apps.get_model("portfolio", "Payment")
    for payment in Payment.objects.using(schema_editor.connection.alias).all().iterator():
        payment.token = uuid.uuid4()
        payment.save(update_fields=["token"])


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="charge",
            name="cancelled_reason",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="payment",
            name="void_reason",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="payment",
            name="token",
            field=models.UUIDField(null=True, unique=True, editable=False),
        ),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="payment",
            name="token",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
    ]
