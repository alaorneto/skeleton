# Comentários em registros

`skeleton/comments` permite comentar registros ([#187](https://github.com/alaorneto/skeleton/issues/187), epic [#93](https://github.com/alaorneto/skeleton/issues/93)). A autorização vem do registro alvo, no mesmo padrão dos [anexos](files.md#anexos): quem lê um registro lê os comentários dele e, se a tabela aceitar comentários, comenta.

## Ativar numa tabela

Uma tabela `SecuredRecord` exposta aceita comentários com `CommentPolicy`:

```python
from skeleton.system.registry import CommentPolicy, TableSpec

TableSpec(..., expose_api=True, comments=CommentPolicy())
```

Sem a política, a tabela não tem superfície de comentários: a lista, a criação e as demais rotas respondem 404, e o painel não aparece.

## Regras

| Ação | Quem | Sem permissão |
|---|---|---|
| Ler | Quem lê o registro alvo | 404 (inexistente, de outra organização, excluído ou ilegível respondem igual) |
| Comentar | Quem lê o registro alvo, numa tabela com `CommentPolicy` | 404 |
| Editar | Só o autor | 403 |
| Excluir (exclusão lógica) e restaurar | O autor e o administrador da organização (moderação) | 403 |

- O autor é o ator que criou o comentário (`created_by`); nenhum campo de autor vem do corpo da requisição.
- Edição, exclusão e restauração exigem a revisão lida (`If-Match` na REST, `revision` no formulário, `expected_revision` no serviço). Revisão antiga responde 412 na REST, 409 na página e `Conflict("stale_revision")` no serviço, sem gravar.
- A edição marca `edited_at`. A exclusão é lógica e a restauração mantém o UUID. Comentários excluídos só aparecem, para restaurar, ao autor e ao administrador.
- Toda escrita é auditada pelos triggers da tabela (`sk_comments.comment`). Como a tabela não é exposta no gateway, a auditoria dos comentários é privilegiada: aparece para o administrador da organização.

## Texto

O corpo é texto simples, de 1 a 5.000 caracteres, sem Markdown nem HTML e sem caracteres de controle (quebras de linha são mantidas). A apresentação sempre escapa o texto; depois, URLs `http` e `https` viram links com `rel="nofollow noopener noreferrer"`. Nenhum outro esquema vira link e nada do que a pessoa digitou vira marcação. O corpo não é tratado como dado pessoal (`personal_fields`) nem é sensível.

## Superfícies

- **Página do registro:** painel **Comentários** com a lista, o formulário de novo comentário e, conforme a regra, editar, excluir e restaurar.
- **Linha do tempo:** cada comentário que a pessoa lê aparece como item (`register_timeline_source("comments", ...)`).
- **REST** (`/api/v1/o/<org>/tables/<tabela>/records/<id>/`): `comments/` (GET, POST `{"body": "..."}`), `comments/<comentário>/` (PATCH `{"body": "..."}` e DELETE, com `If-Match`) e `comments/<comentário>/restore/` (POST com `If-Match`). O schema OpenAPI só descreve essas rotas para tabelas com a política que o ator lê.
- **API de serviços:** `CommentService` (`skeleton.service_api.comments`): `comments`, `add_comment`, `edit_comment`, `delete_comment` e `restore_comment`. Veja [o catálogo](service-api.md#commentservice).

## Modelo

`sk_comments.Comment` é uma entidade organizacional própria (`OrganizationRecord`), com `target_table` (FK `PROTECT` ao catálogo de tabelas) e `target_sysid`, imutáveis, `body` e `edited_at`. A infraestrutura (guard, RLS tenant e auditoria) está congelada em `sk_comments.0001_initial`. A spec do núcleo não tem gateway nem `write_fields`: as escritas passam só pelos serviços acima.

Reações, respostas em thread e menções a grupos estão fora do escopo.
