# Comentários em registros

O subsistema `skeleton/comments` permite adicionar comentários a registros de negócio ([#187](https://github.com/alaorneto/skeleton/issues/187), epic [#93](https://github.com/alaorneto/skeleton/issues/93)). A autorização deriva do registro-alvo, seguindo o mesmo princípio dos [anexos](files.md#anexos): quem lê um registro pode ler seus comentários e, se a tabela habilitar o recurso, publicar novos comentários.

## Ativar numa tabela

Para habilitar comentários em uma tabela `SecuredRecord` exposta, declare `CommentPolicy` no `TableSpec`:

```python
from skeleton.system.registry import CommentPolicy, TableSpec

TableSpec(..., expose_api=True, comments=CommentPolicy())
```

Sem essa política, a tabela não oferece superfície de comentários: listagem, criação e demais rotas retornam 404, e o painel não é exibido.

Uma [tabela derivada](access-control.md#tabelas-derivadas) (`access_parent`) também aceita `CommentPolicy`: ler e comentar exigem permissão de leitura sobre o filho (decidida pela âncora), e menções só alcançam quem lê a âncora. Nesses casos, a app usa `CommentService` e exibe os comentários em suas próprias páginas; a notificação de menção em um registro filho não direciona para a página genérica, que não existe para tabelas derivadas.

## Regras

| Ação | Quem pode executar | Sem permissão |
|---|---|---|
| Ler | Quem lê o registro-alvo | 404 (inexistente, de outra organização, excluído ou ilegível respondem igual) |
| Comentar | Quem lê o registro-alvo em uma tabela com `CommentPolicy` | 404 |
| Editar | Apenas o autor | 403 |
| Excluir (exclusão lógica) e restaurar | O autor e o administrador da organização (moderação) | 403 |

- O autor é sempre o ator que criou o comentário (`created_by`); nenhum campo de autor é aceito no corpo da requisição.
- Edição, exclusão e restauração exigem a revisão lida (`If-Match` na REST, `revision` no formulário HTML e `expected_revision` no serviço). Revisões antigas retornam 412 na REST, 409 na página e `Conflict("stale_revision")` no serviço, sem gravar.
- Editar preenche `edited_at`. A exclusão é lógica e a restauração preserva o UUID original. Comentários excluídos ficam visíveis para restauração apenas ao autor e ao administrador.
- Toda escrita é registrada pelos triggers de auditoria de `sk_comments.comment`. Como a tabela não é exposta no gateway genérico, sua auditoria é privilegiada e aparece para o administrador da organização.

### Comentários de registro selado (#529)

Com `SealPolicy(comments="open")` (padrão), um registro selado continua aceitando comentários normalmente. Com `comments="frozen"`, após o selo ficam bloqueados novos comentários, edições, exclusões e restaurações: `CommentService` retorna `Conflict("record_sealed")`, a API retorna 409 `record_sealed` e as páginas respondem 409. Comentários nunca entram no resumo do selo.

## Texto

O corpo do comentário é texto simples, de 1 a 5 000 caracteres, sem Markdown, sem HTML e sem caracteres de controle (preservando quebras de linha). Na apresentação, todo texto é escapado antes de converter URLs `http` e `https` em links com `rel="nofollow noopener noreferrer"`. Nenhum outro esquema vira link e nada digitado vira marcação HTML. O corpo não é classificado em `personal_fields` nem em `sensitive_fields`.

## Superfícies

- **Página do registro.** Painel **Comentários** com listagem, formulário para novo comentário e ações de editar, excluir e restaurar conforme as permissões.
- **Telas de fluxo.** As telas de tarefa e de instância de um [fluxo de processo](flows.md#superfícies) exibem o mesmo painel de comentários do registro de negócio associado ([#421](https://github.com/alaorneto/skeleton/issues/421)), sob as mesmas regras: apenas para quem lê o registro e somente se a tabela declarar `CommentPolicy`. Os formulários enviam `next` com o caminho da tela, aceito apenas dentro de `/o/<org>/flows/` da mesma organização; fora disso, redirecionam para a página do registro.
- **Linha do tempo.** Cada comentário legível pela pessoa aparece como item na timeline (`register_timeline_source("comments", ...)`).
- **REST** (`/api/v1/o/<org>/tables/<tabela>/records/<id>/`): `comments/` (`GET` e `POST {"body": "..."}`), `comments/<comentário>/` (`PATCH {"body": "..."}` e `DELETE`, com `If-Match`) e `comments/<comentário>/restore/` (`POST` com `If-Match`). O esquema OpenAPI descreve essas rotas apenas para tabelas com `CommentPolicy` que o ator consegue ler.
- **API de serviços.** `CommentService` (`skeleton.service_api.comments`), com os métodos `comments`, `add_comment`, `edit_comment`, `delete_comment` e `restore_comment`. Veja o [catálogo](service-api.md#commentservice).

## Modelo

`sk_comments.Comment` é uma entidade organizacional (`OrganizationRecord`) com `target_table` (FK `PROTECT` para o catálogo de tabelas) e `target_sysid` imutáveis, além de `body` e `edited_at`. Sua infraestrutura (guard, RLS de tenant e auditoria) está congelada em `sk_comments.0001_initial`. A spec do núcleo não expõe gateway nem `write_fields`, garantindo que toda escrita passe pelos serviços dedicados.

## Menções

Um comentário pode mencionar pessoas usando o marcador `@[nome](sysid)` ([#188](https://github.com/alaorneto/skeleton/issues/188)). O painel lista em **Mencionar alguém** os membros elegíveis com seus respectivos marcadores, e a API REST oferece `.../comments/mentionable/` (retornando nome de exibição, sem email, e o marcador) para clientes que montam seletor próprio.

- O servidor confia apenas no `sysid`: o nome é apenas o rótulo inserido pelo seletor e nunca é resolvido a partir do texto. Na renderização, o rótulo aparece escapado como `@nome`.
- Ao criar ou editar um comentário, cada pessoa mencionada precisa ser membro ativo da organização e ter leitura sobre o registro. Caso contrário, o comentário é recusado com 400 (`InvalidInput` no serviço), com mensagem uniforme para qualquer motivo, sem gravar nada. O limite é de até 20 menções por comentário.
- Cada pessoa mencionada recebe uma única notificação `comments.mention` por comentário (deduplicada por comentário e destinatário): edições não repetem avisos anteriores e notificam apenas quem foi incluído pela primeira vez. Mencionar a si mesmo não gera notificação.
- O fan-out reautoriza cada destinatário via `NotificationType.authorize`: se a pessoa perdeu leitura do registro ou o comentário foi excluído antes do envio, ela não recebe nada. Abrir a notificação — ou clicar no `{link}` do texto ([#806](https://github.com/alaorneto/skeleton/issues/806)) — leva diretamente ao comentário (`#comment-<id>` na página do registro) apenas enquanto o acesso continuar válido.

Reações, respostas encadeadas (*threads*) e menções a grupos ficam fora do escopo.
