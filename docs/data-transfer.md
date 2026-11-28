# Exportação e importação CSV

O núcleo permite exportar e importar registros de tabelas de negócio expostas (`TableSpec(expose_api=True)`) em formato CSV. O processamento ocorre em segundo plano por meio de jobs e respeita estritamente o que o solicitante tem permissão para ler ou gravar. O subsistema fica em `skeleton/transfer` ([#182](https://github.com/alaorneto/skeleton/issues/182), [#183](https://github.com/alaorneto/skeleton/issues/183)).

## Formato

- Codificação UTF-8 com BOM e separador `;`, permitindo abertura direta no Excel em português sem assistente de importação. Na importação, `,` também é aceito e detectado automaticamente pela linha de cabeçalho.
- O cabeçalho lista os nomes dos campos: campos de sistema (`sysid`, `revision`, `created_at`, …) seguidos dos `read_fields` da tabela, excluindo `sensitive_fields`.
- Datas e horas seguem o padrão ISO 8601, decimais usam ponto, booleanos usam `true` ou `false`, e valores nulos saem vazios.
- **Proteção contra injeção de fórmulas.** Células de texto que começam com `=`, `+`, `-`, `@`, tabulação ou retorno de carro recebem um apóstrofo inicial para impedir que planilhas avaliem fórmulas. Números, datas e booleanos nunca recebem esse prefixo. Na importação, o apóstrofo adicionado pela exportação é removido automaticamente.

## Exportação

1. Na listagem de registros, a ação **Exportar CSV** aproveita os filtros ativos na tela.
2. Ainda durante a requisição, o sistema verifica se a pessoa lê a tabela e valida os filtros com a mesma regra da lista (`filter_records`). Filtros sobre campos sensíveis são recusados.
3. O job `transfer.export` executa como a própria pessoa, no contexto dela. Se o acesso for revogado entre o pedido e a execução do job, a exportação falha fechada.
4. As linhas são lidas via `authorized_queryset` com os mesmos filtros e projetadas por `records_data`. Referências que a pessoa não pode ler saem vazias, como na API.
5. Se o resultado ultrapassar `SKELETON_EXPORT_MAX_ROWS` (padrão 100 000 linhas), o job falha com `too_many_rows` sem deixar arquivo parcial.
6. Assim que o arquivo fica pronto, o job de sistema `transfer.notify` envia a notificação `transfer.ready` — um job separado porque o fan-out nunca notifica o ator da própria ação.

O arquivo gerado é salvo no armazenamento privado em `org/<organização>/transfers/<uuid>`. Apenas quem solicitou consegue ver o pedido em `/o/<org>/transfers/` e baixar o arquivo; para qualquer outra pessoa — inclusive o administrador —, a rota responde 404. Cada download revalida a permissão de leitura da tabela e entrega o arquivo sempre como anexo, com `nosniff`, `sandbox` e `no-store`.

A exportação filtrada de eventos de auditoria utiliza o mesmo pipeline: veja [auditoria](audit-soft-delete.md#exportação-csv).

## Importação

1. Na listagem de registros, a ação **Importar CSV** abre o envio, acessível apenas a quem tem permissão para criar registros na tabela.
2. O arquivo precisa estar em UTF-8 e respeitar `SKELETON_UPLOAD_MAX_BYTES`. Após o envio, é gravado no armazenamento privado e o job `transfer.import_preview` executa como a pessoa.
3. **Prévia.** Cada linha percorre o mesmo caminho de escrita do gateway (`create_record` e `update_record`), aplicando `write_fields`, autorização e sequências. Tudo executa dentro de um savepoint sempre desfeito ao final, garantindo que a prévia não grave dados nem gere auditoria. O resumo informa total de linhas, quantidade de erros e, havendo falhas, disponibiliza um relatório CSV no formato `linha;coluna;mensagem`.
4. **Colunas aceitas.** Apenas `sysid`, `revision` e os `write_fields` da tabela são permitidos (excluindo campos preenchidos por sequência). Qualquer outra coluna, como `owner`, invalida o arquivo inteiro.
5. **Criação e atualização.** Linhas sem `sysid` criam novos registros; linhas com `sysid` atualizam registros existentes e exigem a `revision` correspondente ao que foi exportado. Uma revisão divergente gera erro naquela linha sem sobrescrever dados. Da mesma forma, tentar alterar uma coluna congelada de um registro selado gera erro na linha, identificado pela coluna `record_sealed` no relatório, enquanto o job conclui normalmente.
6. **Aplicação.** Apenas prévias sem erros podem ser aplicadas. O job `transfer.import_apply` reprocessa o arquivo em uma única transação atômica: basta uma linha falhar — por exemplo, se algum registro tiver sido alterado após a prévia — para que nada seja gravado.
7. **Limite de linhas.** Arquivos acima de `SKELETON_IMPORT_MAX_ROWS` (padrão 10 000 linhas) fazem a importação falhar.

Tanto `transfer.import_preview` quanto `transfer.import_apply` têm tentativa única, evitando reprocessar um arquivo de forma divergente do que a pessoa revisou. Ao final de cada etapa, `transfer.notify` avisa o solicitante.

## Retenção

`TransferJob` guarda o estado do pipeline como `OperationalRecord` (sem revisão, exclusão lógica nem auditoria por linha). O job diário `transfer.retention` é o único caminho que apaga os registros e seus arquivos no armazenamento após `SKELETON_TRANSFER_RETENTION_DAYS` (padrão 7 dias). O job `files.orphans` preserva os arquivos referenciados por transferências ativas e remove apenas objetos deixados por eventuais rollbacks.
