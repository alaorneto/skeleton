# Importação e exportação CSV

`skeleton/transfer` exporta em CSV os registros que a pessoa lê, com os mesmos filtros da lista, por um job ([#182](https://github.com/alaorneto/skeleton/issues/182), epic [#92](https://github.com/alaorneto/skeleton/issues/92)). Ninguém recebe um arquivo com o que não poderia ler na tela, e só quem pediu baixa o resultado.

## Fluxo da exportação

1. Na lista de uma tabela exposta, **Exportar CSV** envia os filtros da lista (nunca a página). Pela REST, `POST /api/v1/o/<org>/exports/` com `{"table_key": "...", "filters": {...}}` responde `202` com o pedido; `Idempotency-Key` repete o mesmo pedido sem duplicá-lo. Apps usam `TransferService.request_export`.
2. O pedido valida os filtros exatamente como a lista (`filter_records`): campo desconhecido, relação, JSON ou valor inválido respondem 400. Além disso, a exportação recusa `page`/`page_size` e filtros por campo sensível: uma coluna que nunca sai no arquivo também não seleciona linhas.
3. O pedido grava um `TransferJob` e um job `transfer.export` na transação do chamador. O payload do job tem só o `transfer_id`.
4. O worker executa o job **como a pessoa** (`run_as="actor"`): o vínculo é revalidado antes do handler, e o handler relê os registros por `record_queryset` (TablePermission `read` e regras por registro, com RLS). Sem leitura nesse momento, o pedido termina `failed` com `unauthorized`, sem arquivo.
5. Acima de `SKELETON_EXPORT_MAX_ROWS` (100.000 por padrão), o pedido termina `failed` com `too_many_rows` e nenhum byte é gravado. Senão, o CSV vai para o armazenamento privado em `org/<org>/transfers/<transfer>` e o pedido termina `succeeded`.
6. Ao fim, com sucesso ou falha, a pessoa recebe a notificação obrigatória `transfer.finished`, que leva à página **Exportações** (`/o/<org>/transfers/`).
7. O download (`/o/<org>/transfers/<id>/download/`) reautoriza a cada pedido: só quem pediu, só pedido concluído e não vencido, e só se a pessoa ainda lê a tabela (TablePermission e app ativa). Qualquer outra situação, inclusive outra pessoa que administre a organização, responde 404. Os bytes saem depois da transação (`DetachedFileResponse`), como anexo forçado com `nosniff`, `sandbox` e `no-store`.

A REST lista e lê os próprios pedidos em `GET /api/v1/o/<org>/transfers/` e `.../transfers/<id>/`; o `download_url` aponta para a rota de download, que usa a sessão, como os anexos.

## O arquivo

- UTF-8 com BOM e `;` como separador, para abrir com acentos corretos no Excel e no LibreOffice em pt-BR. Linhas terminam em CRLF.
- Colunas: `sysid`, `revision` e os `read_fields` que não são `sensitive_fields`, na ordem do spec. Campos sensíveis nunca saem.
- Uma FK que a pessoa não lê sai vazia, com a mesma projeção da lista (`records_data`).
- Datas e datas e horas em ISO 8601, decimais com ponto, booleanos `true`/`false`, JSON serializado com chaves ordenadas.
- Uma célula de texto que começa com `=`, `+`, `-`, `@`, tab ou retorno de carro ganha um `'` na frente, para a planilha não a executar como fórmula. Números tipados não são texto e saem como são (`-5` continua número).

## Retenção

`transfer.retention` roda a cada hora em cada organização e apaga o objeto e a linha dos pedidos vencidos: `SKELETON_TRANSFER_RETENTION_DAYS` (7 por padrão) depois do pedido. O objeto vai primeiro; uma queda entre os dois deixa uma linha sem bytes, que o download já responde com 404. Pedidos que nunca rodaram (por exemplo, de quem perdeu o vínculo antes do job) também vencem e saem.

## Modelo

`sk_transfer.TransferJob` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) (`OperationalRecord`): estado de pipeline, com isolamento tenant e guard de identidade, sem revisão, soft delete nem auditoria por linha, nunca no gateway. Guarda quem pediu, a tabela, os filtros validados, o estado, a contagem, o tamanho, o nome do arquivo, o motivo da falha e a chave do objeto privado (`storage_key`, sensível). A infraestrutura está congelada em `sk_transfer.0001_initial`.

## Configuração

| Setting | Padrão | Efeito |
|---|---|---|
| `SKELETON_EXPORT_MAX_ROWS` | `100000` | Linhas de uma exportação; acima, o job falha sem arquivo |
| `SKELETON_TRANSFER_RETENTION_DAYS` | `7` | Dias entre o pedido e a remoção da linha e do arquivo |
