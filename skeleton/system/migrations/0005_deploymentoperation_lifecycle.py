from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('sk_system', '0004_deployment')]

    operations = [
        migrations.AlterField(
            model_name='deploymentoperation',
            name='operation',
            field=models.CharField(max_length=16, choices=[
                ('install', 'Instalação'), ('update', 'Atualização'),
                ('uninstall', 'Desinstalação'), ('reinstall', 'Reinstalação'),
                ('core_update', 'Atualização do núcleo'),
            ]),
        ),
    ]
