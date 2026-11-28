# Webhooks

Webhooks permitem que sistemas externos recebam eventos de domínio da organização por HTTPS, sem precisar consultar a API em laço. O subsistema fica em `skeleton/integrations` (label `sk_integrations`).

## Endpoints (#63)

`sk_integrations.WebhookEndpoint` é um `OrganizationRecord` com os campos `name`, `url`, `principal`, `event_types`, `secret_ciphertext`, `previous_secret_ciphertext`, `previous_valid_until`, `is_enabled`, `disabled_at` e `disabled_reason`. Ele não entra no gateway genérico: apenas os serviços de `skeleton.integrations.webhooks.services` e as superfícies abaixo podem alterá-lo.

| Regra | Resultado |
|---|---|
| Sem `integrations.manage` (capacidade exclusiva do administrador da organização) | 404, como se o recurso não existisse |
| URL `http://`, com credenciais, porta fora de `SKELETON_OUTBOUND_HTTP_PORTS` ou que resolva para endereço interno | 400 em `url`: o [cliente de saída](architecture.md#segurança-o-que-a-arquitetura-não-promete) valida ao salvar e novamente a cada entrega |
| Tipo de evento desconhecido ou declarado sem `webhook=True` | 400 em `event_types` |
| `principal` que não é conta de serviço ativa da organização | 400 em `principal` |
| `PATCH`, `DELETE` e rotação sem `If-Match` ou com revisão antiga | 428 sem o cabeçalho ou 412 com revisão antiga (409 no HTML) |

O **principal** é uma [conta de serviço](access-control.md#contas-de-serviço) da organização: todas as entregas atuam com a identidade dela e só notificam registros que ela tem permissão para ler (em [tabelas derivadas](access-control.md#tabelas-derivadas), a leitura do filho é verificada pela âncora).

### Segredo de assinatura

O segredo (`whsec_…`) é gerado pelo servidor e cifrado com o keyring `INTEGRATION_ENCRYPTION_KEYS` (a primeira chave cifra e todas decifram; separado de `SECRET_KEY` e dos demais keyrings). Os checks `skeleton.E026` e `E027` reprovam chaves inválidas e segredos gravados sem keyring. As duas colunas cifradas estão em `sensitive_fields`, de modo que a auditoria não registra nem mesmo o texto cifrado.

O segredo é exibido **uma única vez**: na resposta da criação e na resposta da rotação, ambas com `Cache-Control: no-store` e exigindo autenticação recente (no HTML redireciona para reautenticação; na API responde 403 `reauthentication_required`). Nenhuma leitura posterior volta a expô-lo. Ao rotacionar, o servidor gera um novo segredo e mantém o anterior assinando entregas por 24 horas (`previous_valid_until`), permitindo que o receptor faça a troca sem perda de eventos.

### Superfícies

| HTML | API (`/api/v1/`) | O que faz |
|---|---|---|
| `GET /o/<org>/integrations/webhooks/` | `GET o/<org>/webhooks/` | Lista os endpoints, sem segredos |
| `GET` e `POST …/webhooks/new/` | `POST o/<org>/webhooks/` | Cria o endpoint e devolve o segredo (201) |
| `GET` e `POST …/webhooks/<id>/` | `GET`, `PATCH` e `DELETE o/<org>/webhooks/<id>/` | Lê com `ETag`, altera (`name`, `url`, `principal`, `event_types`, `is_enabled`) ou remove (exclusão lógica) |
| `POST …/webhooks/<id>/rotate/` | `POST o/<org>/webhooks/<id>/rotate/` | Rotaciona e devolve o novo segredo |
| `POST …/webhooks/<id>/delete/` | — | Remove o endpoint |

Criar, alterar, rotacionar e remover exigem autenticação recente em ambas as superfícies — na API, o erro 403 `reauthentication_required` ocorre após o 404 de quem não tem `integrations.manage` e antes do 428 ou 412 de revisão. Já reenviar uma entrega e enviar ping exigem apenas a capacidade, CSRF e a revisão atual do endpoint.

Reativar manualmente um endpoint desativado limpa `disabled_at` e `disabled_reason`.

## Entrega (#64)

O ciclo de entrega acontece de forma assíncrona, revalidando permissões e destino em cada tentativa:

1. **Fan-out.** O consumidor `webhooks.fanout` recebe eventos de domínio na transação de `events.consume` (em `system_context` da organização). Para tipos com `webhook=True`, cria uma `WebhookDelivery` por endpoint ativo inscrito naquele tipo e enfileira `webhooks.deliver` executando como o **principal** do endpoint (`run_as="actor"`). A entrega é idempotente: o par `(endpoint, evento)` é único.
2. **Revalidação e envio.** `webhooks.deliver` revalida todas as condições atuando como o principal (que precisa ter vínculo ativo): confirma se o endpoint continua ativo, se o principal pode ler o registro (caso contrário, marca a entrega como `unauthorized` e nada é enviado) e se a URL continua resolvendo para um destino HTTPS público. Só então envia a requisição por `skeleton.common.outbound_http`.
3. **Resultado e retentativas.** Respostas `2xx` marcam `succeeded`. Respostas `5xx`, `408`, `429`, timeouts (incluindo respostas que não concluem em 20 s) e falhas de rede geram retentativas após **1 min, 5 min, 30 min, 2 h, 6 h, 12 h e 24 h** (`retrying`, em novos jobs com `run_after`). Qualquer outro `4xx`, assim como destinos recusados pela validação de rede, falha imediatamente (`failed`).
4. **Saúde e desativação automática.** Cada tentativa com falha incrementa `WebhookEndpointHealth`, enquanto qualquer sucesso zera o contador. **Quinze falhas consecutivas** desativam o endpoint (`disabled_reason=consecutive_failures`) e notificam os administradores da organização (`webhooks.endpoint_disabled`, com o nome do webhook e a contagem de falhas como marcadores; abrir a notificação ou o `{link}` leva à página do webhook para quem tem `integrations.manage`). Um endpoint desativado não recebe novas entregas, e as já agendadas são puladas (`skipped`).
5. **Bloqueios mínimos.** Durante a chamada HTTP, apenas a linha da entrega fica bloqueada: o endpoint é lido sem lock e bloqueado brevemente só depois para atualizar o resultado, garantindo que alterações administrativas nunca esperem requisições HTTP. O lock dentro do job tem `lock_timeout` de 5 s; se estourar, o job falha e é retentado. No cadastro e na alteração, a validação da URL (incluindo resolução DNS) ocorre antes do lock da organização.
6. **Principal desativado e entregas abandonadas.** Se o principal perder o vínculo ativo, os webhooks associados são desativados (`disabled_reason=principal_deactivated`, com a mesma notificação aos administradores) na mesma transação da desativação da conta de serviço; o fan-out também não cria entregas quando o principal está inativo. Trocar o principal mantém as entregas abertas e as reenfileira como o novo principal. Entregas abertas que permanecem 48 h sem atualização perderam o job e são encerradas como `failed` com `error_code="abandoned"`.

`WebhookDelivery` e `WebhookEndpointHealth` são [tabelas operacionais](data-model.md#tabelas-operacionais-adr): não têm revisão, exclusão lógica nem auditoria por linha, e operam sob RLS de tenant.

### Corpo e cabeçalhos

O corpo é um JSON compacto (com chaves ordenadas), fixado na criação da entrega e idêntico em todas as retentativas. Ele carrega **apenas identificadores**: cabe ao sistema receptor consultar os detalhes pela API usando sua própria credencial.

```json
{"data":{"record":"<uuid>","table":"example_notes.note"},"id":"<uuid do evento>","occurred_at":"2026-11-10T12:00:00+00:00","organization":"<uuid>","type":"notes.note_closed","version":1}
```

| Cabeçalho | Valor |
|---|---|
| `Content-Type` | `application/json` |
| `Skeleton-Webhook-Id` | UUID da entrega (mantido nas retentativas para deduplicação no receptor) |
| `Skeleton-Webhook-Event` | Tipo do evento |
| `Skeleton-Webhook-Signature` | `t=<unix>,v1=<hex>`; durante uma rotação, `t=<unix>,v1=<atual>,v1=<anterior>` |

### Verificar a assinatura

`v1` é o HMAC-SHA256, em hexadecimal, de `<t>.<corpo>` calculado com o segredo. O receptor deve aceitar a entrega se **pelo menos um** `v1` conferir e se `t` estiver próximo do relógio local (por exemplo, até 5 minutos de diferença), rejeitando reenvios antigos.

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
| `GET /o/<org>/integrations/webhooks/<id>/deliveries/` | `GET o/<org>/webhooks/<id>/deliveries/` (paginado) e `GET …/deliveries/<entrega>/` | Lista as entregas do endpoint das mais recentes para as mais antigas, mostrando evento, estado, tentativas, próxima tentativa, status HTTP, código de erro e até **1 KB** do corpo da resposta |
| `POST …/deliveries/<entrega>/resend/` | `POST o/<org>/webhooks/<id>/deliveries/<entrega>/resend/` | Reenvia a entrega (202) |
| `POST …/webhooks/<id>/ping/` | `POST o/<org>/webhooks/<id>/ping/` | Envia o evento reservado `webhook.ping` (202) |

As mesmas restrições de acesso se aplicam: somente quem possui `integrations.manage` acessa essas rotas, e entregas de outra organização ou de outro endpoint respondem 404. O trecho salvo da resposta contém apenas texto imprimível (caracteres de controle viram `�`) e é sempre exibido escapado no HTML.

- **Reenvio.** Cria uma **nova entrega** para o mesmo evento, mantendo corpo e `event_id`, vinculada à original via `resend_of` (que aponta sempre para a primeira entrega do evento). A entrega original, suas tentativas e seu resultado permanecem inalterados. A nova entrega passa por todas as validações normais, incluindo a leitura do registro pelo principal. Tanto o reenvio quanto o teste exigem a revisão atual do **endpoint**: `If-Match` na API (428 sem o cabeçalho, 412 se alterada) ou o campo `revision` no formulário HTML. Endpoints desativados recusam ambas as operações (400 `endpoint_disabled`).
- **Teste (`webhook.ping`).** Utiliza o mesmo formato de corpo, assinatura e cabeçalhos, com `type: "webhook.ping"`, `data: {}` e sem registro associado, dispensando verificação de leitura.
- **Retenção.** O job periódico `webhooks.retention` (a cada 6 horas, por organização) é o único caminho que remove entregas. Em cada execução, primeiro encerra entregas abandonadas (abertas sem atualização há 48 h), que permanecem visíveis no log até uma rodada posterior. Em seguida, remove entregas em estado final (`succeeded`, `failed`, `unauthorized`, `skipped`) criadas há mais de `SKELETON_WEBHOOK_RETENTION_DAYS` dias (30 por padrão). Entregas com retentativa agendada permanecem na tabela, e a entrega original de um reenvio ainda vivo só é removida após o reenvio expirar.
