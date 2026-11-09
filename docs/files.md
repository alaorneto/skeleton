# Arquivos e anexos

O núcleo guarda arquivos num **armazenamento privado** (`skeleton/files`, label `sk_files`), nunca público: os bytes só saem por uma view autorizada.

## Armazenamento privado

`STORAGES["skeleton_files"]` é configurado em `skeleton/conf/base.py` e nos perfis:

| Variável | Efeito |
|---|---|
| `SKELETON_FILES_BACKEND=local` (padrão) | `FileSystemStorage` em `SKELETON_FILES_ROOT` (padrão `var/files` do projeto), sem `base_url` |
| `SKELETON_FILES_BACKEND=s3` | `storages.backends.s3.S3Storage` com `default_acl=private`, URLs assinadas de no máximo 60 s e sem sobrescrita. Exige o extra opcional `django-skeleton[s3]` (`django-storages`, `boto3`) e `SKELETON_FILES_S3_BUCKET` (com `SKELETON_FILES_S3_REGION`/`SKELETON_FILES_S3_ENDPOINT` opcionais) |
| `SKELETON_UPLOAD_MAX_BYTES` | Limite de upload do parser (padrão 10 MB) |

`skeleton/files/storage.py` oferece `put_stream`, `open_object`, `delete`, `presigned_url` e `list_prefix`. As chaves são sempre `org/<organização>/<anexo>`, dois UUIDs: o nome de arquivo do usuário nunca entra na chave, então não atravessa diretórios, não colide e não vaza numa URL. `put_stream` grava a chave exata, recusa objeto existente e interrompe acima do limite.

O check de deploy `skeleton.E024` (`manage.py check --deploy`) reprova armazenamento ausente, local sem `location`, dentro de `STATIC_ROOT` ou com `base_url`, e S3 com ACL pública, URL sem assinatura ou domínio público.

## Anexos

`sk_files.Attachment` (`OrganizationRecord`, auditado) guarda os metadados de um arquivo anexado a um registro: `target_table` (FK PROTECT ao catálogo), `target_sysid`, `filename`, `content_type`, `size`, `sha256`, `storage_key` (sensível, redigido na auditoria e nunca devolvido), `state` (`pending`, `available`, `quarantined`, `rejected`), `scanned_at` e `scan_result_code`. Os bytes ficam no armazenamento privado.

Uma tabela opta por anexos no `TableSpec`:

```python
from skeleton.system.registry import AttachmentPolicy, TableSpec

TableSpec(..., attachments=AttachmentPolicy(max_bytes=5 * 1024 * 1024, types=("application/pdf", "image/png")))
```

| Regra | Resultado |
|---|---|
| Tabela sem opt-in | 404 |
| Alvo de outra organização, inexistente, excluído ou não legível | 404 (um alvo excluído esconde os anexos) |
| Listar | exige leitura do alvo |
| Enviar, remover, restaurar | exigem update do alvo; quem só lê recebe 403 |
| Tipo pelo conteúdo fora da política, ou extensão incoerente | 400 |
| Acima de `max_bytes` ou de `SKELETON_UPLOAD_MAX_BYTES` | 413 |
| Remover e restaurar | exigem `If-Match` (428 sem ele, 412 com revisão antiga) |

O tipo é detectado pelos primeiros bytes (PDF, PNG, JPEG, GIF, WebP, ZIP/DOCX/XLSX e texto UTF-8 para `.txt`/`.csv`), numa tabela de assinaturas no código, e precisa coincidir com a extensão; o `Content-Type` enviado pelo cliente é ignorado. A tabela própria substituiu a dependência `filetype` prevista no plano: reconhece só os tipos que uma política pode permitir e não acrescenta pacote aos locks. HTML e SVG nunca são aceitos.

Sem antivírus configurado (`SKELETON_FILES_SCAN`, #62), o anexo nasce `available`; com ele, nasce `pending`. Se a gravação da linha falhar, o objeto já gravado é apagado. Remover é exclusão lógica: os bytes ficam até a limpeza de órfãos, para que restaurar funcione.

| Superfície | Rota |
|---|---|
| API | `GET/POST /api/v1/o/<org>/tables/<table_key>/records/<sysid>/attachments/` (multipart, campo `file`), `DELETE .../attachments/<id>/`, `POST .../attachments/<id>/restore/` |
| HTML | Painel "Anexos" no detalhe do registro, com envio (`multipart/form-data`) e remoção por formulário |

## Download

Arquivos só saem por `/o/<org>/tables/<table_key>/<sysid>/attachments/<id>/download/`, que autoriza **a cada pedido**: leitura do alvo, anexo vivo e no estado `available`. Inexistente, excluído, `pending`, `quarantined`, `rejected` e não autorizado respondem 404, e a revogação vale no pedido seguinte. Não há URL pública.

O middleware tenant recusa respostas em streaming dentro do contexto do banco. A view devolve uma `DetachedFileResponse` (`skeleton/common/responses.py`) só com valores prontos (chave, nome e tipo). Depois que a transação comita e os contextos terminam, o middleware a transforma na resposta real, que lê os bytes do armazenamento **sem nenhuma consulta ao ORM**.

| Entrega | Configuração |
|---|---|
| Streaming pelo Django (padrão) | `FileResponse` lendo do armazenamento local |
| Nginx | `SKELETON_FILES_ACCEL=nginx` e `SKELETON_FILES_ACCEL_PREFIX` (padrão `/_protected/`): resposta vazia com `X-Accel-Redirect: <prefixo>/<chave>` |
| Apache/lighttpd | `SKELETON_FILES_ACCEL=sendfile`: `X-Sendfile` com o caminho local |
| S3 | Redirecionamento para URL pré-assinada de no máximo 60 s, com `Content-Disposition: attachment` forçado |

Toda resposta leva `Content-Disposition: attachment` (nome em RFC 5987), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: no-store` e `Referrer-Policy: no-referrer`. Nada é servido inline, nem imagens nesta fase; um tipo fora da lista segura (HTML, SVG) sai como `application/octet-stream`.
