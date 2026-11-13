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

## Entrega (#64)

1. O consumidor `webhooks.fanout` recebe todo evento de domínio (na transação do `events.consume`, em `system_context` da organização). Para um tipo com `webhook=True`, cria uma `WebhookDelivery` por endpoint ativo inscrito nele e enfileira `webhooks.deliver` como o **principal** do endpoint (`run_as="actor"`). A entrega é idempotente: `(endpoint, evento)` é único.
2. `webhooks.deliver` revalida tudo como o principal, que precisa ter vínculo ativo: o endpoint continua ativo; o principal lê o registro (senão a entrega fica `unauthorized` e nada é enviado); a URL ainda é um destino HTTPS público. Depois envia por `skeleton.common.outbound_http`.
3. 2xx: `succeeded`. 5xx, 408, 429, timeout (inclusive a resposta que não termina em 20 s, #224) e falha de rede são retentados depois de **1 min, 5 min, 30 min, 2 h, 6 h, 12 h e 24 h** (`retrying`, um job novo com `run_after`). Qualquer outro 4xx, ou um destino recusado, falha na hora (`failed`).
4. Cada tentativa que falha conta em `WebhookEndpointHealth`, e um sucesso zera a contagem. **Quinze falhas seguidas** desativam o endpoint (`disabled_reason=consecutive_failures`) e notificam os administradores da organização (`webhooks.endpoint_disabled`). Um endpoint desativado não recebe entregas novas, e as já agendadas são ignoradas (`skipped`).

`WebhookDelivery` e `WebhookEndpointHealth` são [tabelas operacionais](data-model.md#tabelas-operacionais-adr): sem revisão, exclusão lógica ou auditoria por linha, e com RLS tenant.

### Corpo e cabeçalhos

O corpo é JSON compacto (chaves ordenadas), fixado na criação da entrega e igual em todas as tentativas. Leva **só identificadores**: o receptor busca os detalhes pela API com credencial própria.

```json
{"data":{"record":"<uuid>","table":"example_notes.note"},"id":"<uuid do evento>","occurred_at":"2026-11-10T12:00:00+00:00","organization":"<uuid>","type":"notes.note_closed","version":1}
```

| Cabeçalho | Valor |
|---|---|
| `Content-Type` | `application/json` |
| `Skeleton-Webhook-Id` | UUID da entrega (igual nas retentativas: use para deduplicar) |
| `Skeleton-Webhook-Event` | Tipo do evento |
| `Skeleton-Webhook-Signature` | `t=<unix>,v1=<hex>`; durante uma rotação, `t=<unix>,v1=<atual>,v1=<anterior>` |

### Verificar a assinatura

`v1` é o HMAC-SHA256, em hexadecimal, de `<t>.<corpo>` com o segredo. Aceite a entrega se **algum** `v1` conferir e se `t` estiver perto do relógio do receptor (por exemplo, 5 minutos), para recusar reenvios antigos.

```python
import hashlib, hmac, time


def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    items = [item.split("=", 1) for item in header.split(",")]
    timestamp = next(value for key, value in items if key == "t")
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(key == "v1" and hmac.compare_digest(value, expected) for key, value in items)
```

Vetor de teste (`tests/unit/test_webhook_signature.py`): segredo `whsec_test_current`, `t=1700000000`, corpo `{"id":"evt_1","type":"notes.webhook_ready"}` → `v1=42a3634c4a23b13b6b9711dc81fb1424a540673f5363b94294dc2c2da727f1b1`.

## Log, reenvio e teste (#65)

| HTML | API (`/api/v1/`) | O que faz |
|---|---|---|
| `GET /o/<org>/integrations/webhooks/<id>/deliveries/` | `GET o/<org>/webhooks/<id>/deliveries/` (paginado) e `GET …/deliveries/<entrega>/` | Entregas do endpoint, das mais novas para as mais antigas: evento, estado, tentativas, próxima tentativa, status, código de erro e até **1 KB** da resposta |
| `POST …/deliveries/<entrega>/resend/` | `POST o/<org>/webhooks/<id>/deliveries/<entrega>/resend/` | Reenvia (202) |
| `POST …/webhooks/<id>/ping/` | `POST o/<org>/webhooks/<id>/ping/` | Envia o evento reservado `webhook.ping` (202) |

Valem as mesmas regras dos endpoints: só quem tem `integrations.manage`, e entregas de outra organização ou de outro endpoint respondem 404. O trecho da resposta é texto imprimível (caracteres de controle viram `�`) e a página o mostra escapado, nunca como HTML.

**Reenvio** cria uma entrega **nova** do mesmo evento, com o mesmo corpo e o mesmo `event_id`, ligada à original por `resend_of` (sempre a primeira entrega do evento). A original, suas tentativas e seu resultado não mudam. A entrega nova passa por todas as verificações da entrega normal, inclusive a leitura do registro pelo principal. Reenvio e teste exigem a revisão atual do **endpoint**: `If-Match` na API (428 sem ele, 412 se mudou) ou o campo `revision` no HTML. Um endpoint desativado recusa os dois (400 `endpoint_disabled`).

**Teste** (`webhook.ping`) usa o mesmo corpo, assinatura e cabeçalhos, com `type: "webhook.ping"`, `data: {}` e sem registro, por isso não depende de leitura.

**Retenção:** o job periódico `webhooks.retention` (a cada 6 horas, por organização) é o único caminho que apaga entregas. Ele remove as que estão num estado final (`succeeded`, `failed`, `unauthorized`, `skipped`) e foram criadas há mais de `SKELETON_WEBHOOK_RETENTION_DAYS` dias (30 por padrão). Uma entrega com retentativa agendada fica, e a original de um reenvio ainda vivo espera o reenvio expirar.
