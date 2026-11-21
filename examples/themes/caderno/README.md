# Caderno

Tema de exemplo da instância, no formato de [pacote de tema](../../../docs/appearance.md#pacote-de-tema).

```sh
python manage.py appearance check-package examples/themes/caderno
python manage.py appearance build-package examples/themes/caderno --output caderno-1.0.0.zip
```

Para enviar à mão, instale o zip em `/platform/appearance/`. Para entregar junto de uma app, copie o diretório para `themes/caderno/` da app, troque o `id` para `<app_id>.theme.caderno` e declare-o em `runtime.instance_themes` ([criar e publicar um tema](../../../docs/themes.md)).
