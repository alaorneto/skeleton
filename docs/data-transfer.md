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

A auditoria filtrada usa o mesmo pipeline: veja [auditoria](audit-soft-delete.md#exportação-csv).

## Importação

1. Na lista de registros, **Importar CSV** abre o envio. Só quem pode criar na tabela entra.
2. O arquivo precisa estar em UTF-8 e dentro de `SKELETON_UPLOAD_MAX_BYTES`. Ele fica no storage privado, e o job `transfer.import_preview` roda como a pessoa.
3. **Prévia:** cada linha passa pelo mesmo caminho de escrita do gateway (`create_record`/`update_record`), com `write_fields`, autorização e sequências. Tudo roda dentro de um savepoint que é sempre desfeito, então a prévia não grava nada nem deixa auditoria. O resultado traz o número de linhas, o número de erros e, quando há erros, um relatório CSV com `linha;coluna;mensagem`.
4. **Colunas:** valem só `sysid`, `revision` e os `write_fields` da tabela, sem os campos de sequência. Qualquer outra coluna, como `owner`, recusa o arquivo inteiro.
5. **Linhas:** uma linha sem `sysid` cria um registro. Uma linha com `sysid` atualiza o registro e precisa da `revision` que a pessoa exportou. Uma revisão diferente é erro daquela linha, nunca sobrescrita.
6. **Confirmação:** só uma prévia sem erros pode ser aplicada. O job `transfer.import_apply` repete o processamento numa transação que só é mantida se todas as linhas passarem. Basta uma linha falhar, por exemplo porque o registro mudou depois da prévia, para nada ser gravado.
7. **Limite:** acima de `SKELETON_IMPORT_MAX_ROWS` (padrão 10.000), a importação falha.

Os dois jobs têm uma tentativa só, porque reprocessar um arquivo que a pessoa já avaliou mudaria o que ela viu. Quando cada etapa termina, `transfer.notify` avisa a pessoa.

## Retenção

`TransferJob` é estado de pipeline (`OperationalRecord`): não tem revisão, soft delete nem auditoria por linha. O job diário `transfer.retention` é o único caminho que apaga a linha e os arquivos, depois de `SKELETON_TRANSFER_RETENTION_DAYS` (padrão 7). O `files.orphans` mantém os objetos referenciados por uma transferência e remove os que um rollback deixou para trás.
