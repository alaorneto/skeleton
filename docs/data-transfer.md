# Exportação e importação CSV

O núcleo exporta e importa registros de tabelas de negócio expostas (`TableSpec(expose_api=True)`) em CSV, por job e só com o que a pessoa pode ler ou escrever. O código fica em `skeleton/transfer` ([#182](https://github.com/alaorneto/skeleton/issues/182), [#183](https://github.com/alaorneto/skeleton/issues/183)).

## Formato

- UTF-8 com BOM e `;` como separador, para o Excel em pt-BR abrir sem o assistente. A importação também aceita `,`, detectado pela linha de cabeçalho.
- O cabeçalho traz os nomes dos campos: os campos de sistema (`sysid`, `revision`, `created_at`, …) seguidos dos `read_fields` da tabela, menos os `sensitive_fields`.
- Datas e horas em ISO 8601, decimais com ponto, booleanos `true`/`false` e vazio para nulo.
- **Injeção de fórmulas:** uma célula de texto que começa com `=`, `+`, `-`, `@`, tab ou retorno de carro ganha um apóstrofo na frente, para a planilha não avaliar a fórmula. Números, datas e booleanos nunca recebem o prefixo. A importação remove o apóstrofo que a exportação acrescentou.

## Exportação

1. Na lista de registros, **Exportar CSV** leva os filtros da tela.
2. O pedido confere, ainda na requisição, que a pessoa lê a tabela e que os filtros são os da lista (`filter_records`). Filtro por campo sensível é recusado.
3. O job `transfer.export` roda como a própria pessoa, no contexto dela. Uma revogação entre o pedido e o job faz a exportação falhar fechada.
4. As linhas saem de `authorized_queryset` com os mesmos filtros e da projeção de `records_data`. Uma referência que a pessoa não lê sai vazia, como na API.
5. Acima de `SKELETON_EXPORT_MAX_ROWS` (padrão 100.000), o job falha com `too_many_rows` e não deixa arquivo parcial.
6. Quando o arquivo fica pronto, o job de sistema `transfer.notify` manda a notificação `transfer.ready`. Ele é separado porque o fan-out nunca notifica o ator da própria ação.

O arquivo fica no storage privado em `org/<organização>/transfers/<uuid>`. Só quem pediu vê o pedido em `/o/<org>/transfers/` e baixa o arquivo; para qualquer outra pessoa, inclusive o administrador, a resposta é 404. Cada download relê a permissão de leitura da tabela, e o arquivo sai sempre como anexo, com `nosniff`, `sandbox` e `no-store`.

## Retenção

`TransferJob` é estado de pipeline (`OperationalRecord`): não tem revisão, soft delete nem auditoria por linha. O job diário `transfer.retention` é o único caminho que apaga a linha e os arquivos, depois de `SKELETON_TRANSFER_RETENTION_DAYS` (padrão 7). O `files.orphans` mantém os objetos referenciados por uma transferência e remove os que um rollback deixou para trás.
