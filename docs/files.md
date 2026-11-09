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
