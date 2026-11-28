---
name: service-api
description: Projetar, implementar, testar e documentar a API de serviços de uma app deste repositório (classes em `service_api.py` que outras apps consomem em processo) e consumir a API de outra app ou do núcleo (`skeleton.service_api`). Use SEMPRE ao criar uma app nova, ao acrescentar uma operação que outra app precise chamar, ao integrar duas apps, ao usar notificações, identidade, autorização, jobs, eventos ou arquivos do núcleo a partir de uma app, ao revisar um `service_api.py`, ou quando o pedido falar em "API de serviços", "classe de serviço", "service API", "expor para outras apps", "consumir a app X", "integração entre apps", "DTO", "contrato entre apps".
---

# API de serviços entre apps

As regras normativas estão no `AGENTS.md` (*API de serviços entre apps*) e o contrato completo em [docs/service-api.md](../../../docs/service-api.md). Este skill orienta o passo a passo de implementação. Leia [references/templates.md](references/templates.md) antes de escrever código, testes ou documentação da API.

Escreva documentação e comentários em português, mantendo identificadores de código em inglês.

## 0. Antes de editar

1. Carregue o skill `dev-workflow` e vincule a mudança a uma issue antes de editar qualquer arquivo. Um piso `since=` novo recebe a versão calculada pelo skill para o próximo *minor*.
2. Confira o estado do núcleo no checkout:
   - `skeleton/service_api/` (base, DTOs, erros, `contract` e `registry`), `runtime.service_api` no manifesto (`SERVICE_API_CORE_MINIMUM` é `0.0.0`), os checks `E018`/`E019`/`W019` e o kit `skeleton.testing.service_api` fazem parte do núcleo publicado. Em projetos com core antigo, atualize o core antes — nunca crie uma base paralela.
   - Use os serviços públicos do núcleo disponíveis em `skeleton/service_api/` e as demais superfícies públicas listadas na especificação, nunca módulos internos.
3. Revise os `TableSpec` dos modelos envolvidos (`read_fields`, `write_fields`, `sensitive_fields`) e as políticas de acesso da app.

## 1. Projetar a API (antes do código)

Para cada operação que outra app precisará chamar, especifique na issue:

- **Nome e natureza:** consulta (substantivo: `task`, `tasks`, `open_count`) ou comando (imperativo: `create_task`, `close_task`).
- **Parâmetros keyword-only:** sempre `actor`; `expected_revision` em alterações; `idempotency_key` em criações que possam ser repetidas por jobs; filtros explícitos (sem DSL livre).
- **Retorno:** DTO, `Page[DTO]` ou tipo primitivo, expondo apenas campos de `read_fields` fora de `sensitive_fields`.
- **Autorização:** `TablePermission`, ação exigida, regra por registro e capacidade necessária.
- **Erros:** `NotFound`, `Forbidden`, `InvalidInput`, `Conflict(code)` e `Unavailable`.
- **Efeitos:** escritas, jobs, eventos e notificações — sem efeitos externos síncronos dentro do método.

Exponha apenas o mínimo necessário: todo método público passa a integrar o contrato versionado da app.

## 2. Implementar

1. Crie `service_api.py` no pacote da app com DTOs e classes de serviço (veja `references/templates.md`).
2. Mantenha a lógica de negócio em funções internas (`services.py`). A camada `service_api` valida entradas, autoriza, delega à função interna e converte o resultado em DTO.
3. Inicie consultas com `authorized_queryset(Model, user=actor, action=...)`. Em comandos, localize o registro pelo queryset autorizado e valide a ação com `require_record_action(record, user=actor, action=...)`.
4. Defina `app_label` da própria app na classe de serviço para que `@service_method` valide se a app está ativa e lance `Unavailable` caso contrário (`E018` recusa divergências).
5. Em alterações, passe `expected_revision` ao CAS. O `@service_method` já traduz `ConcurrentUpdate` para `Conflict(code="stale_revision")` — não capture a exceção nem importe módulos internos de erro, e nunca retente silenciosamente.
6. Não abra `system_context`, não troque de organização, não faça commit e não execute I/O externo (email ou HTTP) dentro do método; delegue para jobs, eventos ou `on_commit` com dados já materializados.
7. Faça views, endpoints REST e jobs da própria app chamarem `service_api` para manter um único caminho autorizado.

## 3. Declarar

- No manifesto da app, declare `runtime.service_api = {"module": "applications.<pkg>.service_api", "classes": [...]}` com `compatibility.skeleton` compatível (`SERVICE_API_CORE_MINIMUM` é `0.0.0`). Valide com `manage.py check` (`E018`/`E019`) e `manage.py package doctor applications/<pkg>`.
- Para consumir outra app, declare seu `app_id` com faixa de versão em `dependencies`, importe apenas `applications.<outra>.service_api` (nunca `models`, `services`, `forms` ou `views`) e trate `Unavailable` como integração desativada.
- Do núcleo, importe apenas as superfícies públicas documentadas.

## 4. Testar

Para cada método público (veja `references/templates.md`), cubra:

- caminho autorizado devolvendo DTO correto e sem campos sensíveis;
- *default deny* (`NotFound` na leitura, `Forbidden` em ações);
- isolamento entre organizações A e B em retornos, contagens e erros;
- app desativada retornando `Unavailable`;
- ator diferente do contexto retornando `Forbidden`;
- comandos: CAS com vencedor único, idempotência e rollback sem efeitos residuais;
- `assert_service_contract` e os helpers de `skeleton.testing.service_api` (`build_service_scenario`, `assert_tenant_isolated`, `assert_denied_as_not_found`, `assert_unavailable_when_disabled`, `assert_rejects_other_actor`, `assert_cas_conflict`).

Execute em PostgreSQL com a role runtime (`tools/dev_postgres.py start` e `env`):

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests <testes da app>
.venv/bin/python -m pytest tests/integration_postgres <testes PG da app>
```

Casos somente de leitura sobre o mesmo cenário (*default deny*, A/B, ator divergente) podem compartilhar o preparo usando `subtests`, evitando uma restauração de schema por caso; comandos que escrevem ficam em testes separados. Consulte [Escrever testes que mantêm a suíte rápida](../../../docs/testing.md#escrever-testes-que-mantêm-a-suíte-rápida).

Registre na issue e em `docs/testing.md` apenas o que foi realmente executado; serviço indisponível é bloqueio, nunca aprovação.

## 5. Documentar

- Mantenha `docs/service-api.md` da app seguindo o modelo: visão geral, dependência, serviços e métodos (Autorização, Erros, Efeitos e exemplo), DTOs, eventos publicados e histórico de versões.
- Quebra de compatibilidade exige versão *major* no manifesto e registro no histórico; depreciações usam `@deprecated(since, removal)` por pelo menos uma *minor*.
- Se a alteração impactar o contrato geral, atualize `docs/service-api.md` do repositório e, havendo mudança de regra, o `AGENTS.md`.

## 6. Checklist de revisão

- [ ] Todo método público é *keyword-only*, recebe `actor`, tem anotações de tipo e docstring com Autorização, Erros e Efeitos.
- [ ] Nenhum retorno expõe modelo, QuerySet, dict mutável ou campo sensível sem justificativa documentada.
- [ ] A autorização ocorre dentro do método; recurso inexistente e não autorizado respondem igual.
- [ ] App desativada lança `Unavailable`; nenhum método abre `system_context` ou muda de organização.
- [ ] Alterações usam `expected_revision`; criações repetíveis aceitam `idempotency_key`.
- [ ] Sem efeito externo síncrono no método; rollback do chamador não deixa rastros.
- [ ] Consumidores importam apenas `service_api` e declaram a dependência no manifesto.
- [ ] Testes de isolamento A/B, default deny, app desativada, ator divergente, CAS e idempotência executados em PostgreSQL com a role runtime.
- [ ] `docs/service-api.md` da app atualizado com o histórico de versões.
