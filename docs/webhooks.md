# Webhooks

Sistemas externos recebem eventos de domínio da organização por HTTPS, sem consultar a API em laço. O subsistema fica em `skeleton/integrations` (label `sk_integrations`) e é parte do epic #82.

## Endpoints (#63)

`sk_integrations.WebhookEndpoint` é um `OrganizationRecord` com `name`, `url`, `principal`, `event_types`, `secret_ciphertext`, `previous_secret_ciphertext`, `previous_valid_until`, `is_enabled`, `disabled_at` e `disabled_reason`. Não entra no gateway genérico: só os serviços de `skeleton.integrations.webhooks.services` e as superfícies abaixo o alteram.

| Regra | Resultado |
|---|---|
| Sem `integrations.manage` (só o administrador da organização a tem) | 404, como se o recurso não existisse |
| URL `http://`, com credenciais, porta fora de `SKELETON_OUTBOUND_HTTP_PORTS` ou que resolve para endereço interno | 400 em `url`: o [cliente de saída](architecture.md#segurança-o-que-a-arquitetura-não-promete) valida ao salvar e de novo a cada entrega |
| Tipo de evento desconhecido ou declarado sem `webhook=True` | 400 em `event_types` |
| `principal` que não é conta de serviço ativa desta organização | 400 em `principal` |
| `PATCH`, `DELETE` e rotação sem `If-Match` / com revisão antiga | 428 / 412 (409 no HTML) |

O **principal** é uma [conta de serviço](access-control.md#contas-de-serviço) da organização: as entregas agem como ela e só falam de registros que ela pode ler.

### Segredo de assinatura

O segredo (`whsec_…`) é gerado pelo servidor e cifrado com o keyring `INTEGRATION_ENCRYPTION_KEYS` (a primeira chave cifra, todas decifram; separado de `SECRET_KEY` e dos outros keyrings). Os checks `skeleton.E026`/`E027` recusam chave inválida e segredo gravado sem keyring. As duas colunas cifradas são `sensitive_fields`: a auditoria não guarda nem o texto cifrado.

Ele aparece **uma única vez**: na resposta da criação e na da rotação, ambas com `Cache-Control: no-store` e exigindo autenticação recente (no HTML, redireciona para reautenticar; na API, 403 `reauthentication_required`). Nenhuma leitura posterior o devolve. Rotacionar gera um segredo novo e mantém o anterior assinando por 24 horas (`previous_valid_until`), para o receptor trocar sem perder entregas.

### Superfícies

| HTML | API (`/api/v1/`) | O que faz |
|---|---|---|
| `GET /o/<org>/integrations/webhooks/` | `GET o/<org>/webhooks/` | Lista, sem segredos |
| `GET`/`POST …/webhooks/new/` | `POST o/<org>/webhooks/` | Cria e devolve o segredo (201) |
| `GET`/`POST …/webhooks/<id>/` | `GET`/`PATCH`/`DELETE o/<org>/webhooks/<id>/` | Lê com ETag, altera (`name`, `url`, `principal`, `event_types`, `is_enabled`) ou remove (exclusão lógica) |
| `POST …/webhooks/<id>/rotate/` | `POST o/<org>/webhooks/<id>/rotate/` | Rotaciona e devolve o segredo novo |
| `POST …/webhooks/<id>/delete/` | — | Remove |

Reativar à mão um endpoint desativado limpa `disabled_at` e `disabled_reason`.
