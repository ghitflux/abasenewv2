from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('esteira', '0004_docissue_data_referencia_negocio_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transicao',
            name='acao',
            field=models.CharField(blank=True, max_length=80),
        ),
    ]
