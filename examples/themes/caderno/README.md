# Caderno

Tema de exemplo para a instância, estruturado no formato de [pacote de tema](../../../docs/appearance.md#pacote-de-tema).

```sh
python manage.py appearance check-package examples/themes/caderno
python manage.py appearance build-package examples/themes/caderno --output caderno-1.0.0.zip
```

Para instalar manualmente, envie o arquivo `.zip` em `/platform/appearance/`. Para distribuir o tema junto com uma app, copie o diretório para `themes/caderno/` dentro da app, altere o `id` para `<app_id>.theme.caderno` e declare o tema em `runtime.instance_themes` ([criar e publicar um tema](../../../docs/themes.md)).
