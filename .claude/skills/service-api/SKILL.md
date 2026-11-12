---
name: service-api
description: Projetar, implementar, testar e documentar a API de serviços de uma app deste repositório (classes em `service_api.py` que outras apps consomem em processo) e consumir a API de outra app ou do núcleo (`skeleton.service_api`). Use SEMPRE ao criar uma app nova, ao acrescentar uma operação que outra app precise chamar, ao integrar duas apps, ao usar notificações, identidade, autorização, jobs, eventos ou arquivos do núcleo a partir de uma app, ao revisar um `service_api.py`, ou quando o pedido falar em "API de serviços", "classe de serviço", "service API", "expor para outras apps", "consumir a app X", "integração entre apps", "DTO", "contrato entre apps".
---

# API de serviços entre apps

A norma é o `AGENTS.md` (seção "API de serviços entre apps"); o contrato completo é [docs/service-api.md](../../../docs/service-api.md). Este skill é o procedimento. Leia [references/templates.md](references/templates.md) antes de escrever código, testes ou a documentação da API.

Escreva docs e comentários em português, como o restante do repositório; identificadores em inglês.

## 0. Antes de editar

1. Vincule o trabalho a uma issue pelo skill `issue-workflow` (criar app, expor operação e integrar apps são desenvolvimento versionado).
2. Verifique o que já existe no checkout, porque partes do contrato ainda estão sendo entregues:
   - `skeleton/service_api/` (base, DTOs, erros, `contract`, `registry`), `runtime.service_api` no manifesto (core `>=0.4.0`), os checks E018/E019/W019 e o kit `skeleton.testing.service_api` estão entregues (epic #112). Num projeto com core mais antigo, atualize o core antes; não crie uma base local paralela.
   - Serviços do núcleo em `skeleton/service_api/` (#102–#109). Use os que existirem; para os demais, use as superfícies públicas listadas na especificação, nunca módulos internos do núcleo.
3. Leia o `TableSpec` dos modelos envolvidos (`read_fields`, `write_fields`, `sensitive_fields`) e as políticas da app.

## 1. Projetar a API (antes do código)

Para cada operação que outra app vai precisar, defina e escreva na issue:

- **Nome e tipo:** consulta (substantivo: `task`, `tasks`, `open_count`) ou comando (imperativo: `create_task`, `close_task`).
- **Parâmetros keyword-only:** sempre `actor`; `expected_revision` em alterações; `idempotency_key` em criações que jobs possam repetir; filtros explícitos (sem DSL livre).
- **Retorno:** DTO, `Page[DTO]` ou primitivo. Liste os campos, só de `read_fields` e fora de `sensitive_fields`.
- **Autorização:** `TablePermission` e ação, regra por registro, capacidade exigida.
- **Erros:** `NotFound`, `Forbidden`, `InvalidInput`, `Conflict(code)`, `Unavailable`.
- **Efeitos:** escritas, jobs, eventos e notificações; nada externo dentro do método.

Exponha o mínimo que os consumidores precisam. Cada método público é compromisso de versão.

## 2. Implementar

1. `service_api.py` no pacote da app, com DTOs e classes de serviço (modelo em `references/templates.md`).
2. Regras de negócio ficam em funções internas (`services.py`). O `service_api` valida a entrada, autoriza, chama a regra interna e converte para DTO.
3. Consultas partem de `authorized_queryset(Model, user=actor, action=...)`. Comandos localizam o registro por queryset autorizado e chamam `require_record_action(record, user=actor, action=...)`.
4. Defina `app_label` da app na classe: o `@service_method` exige a app ativa e responde `Unavailable` (o E018 recusa `app_label` diferente).
5. Alterações: aplique `expected_revision` no CAS; o `@service_method` já converte `ConcurrentUpdate` em `Conflict(code="stale_revision")`, então não capture a exceção nem importe o módulo interno de exceções do núcleo. Nunca tente de novo em silêncio.
6. Não abra `system_context`, não troque de organização, não faça commit e não envie email/HTTP dentro do método. Use jobs, eventos ou `on_commit` com valores materializados.
7. Views, REST e jobs da própria app passam a chamar o `service_api`, para existir um único caminho.

## 3. Declarar

- Manifesto: `runtime.service_api = {"module": "applications.<pkg>.service_api", "classes": [...]}`, com `compatibility.skeleton` `>=0.4.0`. Rode `manage.py check` (E018/E019) e `manage.py package doctor applications/<pkg>`.
- Consumir outra app: declare o `app_id` dela em `dependencies` com faixa de versão. Importe só `applications.<outra>.service_api`, nunca `models`, `services`, `forms` ou `views` dela. Trate `Unavailable` como integração desligada.
- Do núcleo, importe só as superfícies públicas da tabela da especificação.

## 4. Testar

Para cada método público (modelo em `references/templates.md`):

- caminho autorizado com DTO correto e sem campos sensíveis;
- default deny (`NotFound` para leitura, `Forbidden` para ação);
- isolamento A/B em retorno, contagem e erro;
- app desativada → `Unavailable`;
- ator divergente do contexto → `Forbidden`;
- comandos: CAS com um vencedor, idempotência e rollback sem efeitos residuais;
- `assert_service_contract` e os helpers do kit (`build_service_scenario`, `assert_tenant_isolated`, `assert_denied_as_not_found`, `assert_unavailable_when_disabled`, `assert_rejects_other_actor`, `assert_cas_conflict`).

Rode em PostgreSQL com a role runtime (`tools/dev_postgres.py start` e `env`):

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests <testes da app>
.venv/bin/python -m pytest tests/integration_postgres <testes PG da app>
```

Os casos que só leem o mesmo cenário (default deny, A/B, ator divergente) podem dividir um preparo com o fixture `subtests`, em vez de pagar uma restauração do schema por caso; os comandos que escrevem ficam em testes próprios. As demais regras de custo e de execução paralela estão em [Escrever testes que mantêm a suíte rápida](../../../docs/testing.md#escrever-testes-que-mantêm-a-suíte-rápida).

Registre na issue e em `docs/testing.md` somente o que rodou de fato. Serviço indisponível é bloqueio de verificação, não aprovação.

## 5. Documentar

- `docs/service-api.md` da app, pelo modelo: visão geral, dependência, serviços e métodos (Autorização, Erros, Efeitos, exemplo), DTOs, eventos, histórico de versões.
- Mudança incompatível: versão *major* no manifesto e entrada no histórico. Depreciação com `@deprecated(since, removal)` por pelo menos uma *minor*.
- Se a mudança afetar o contrato geral, atualize `docs/service-api.md` do repositório e, se for regra, o `AGENTS.md`.

## 6. Checklist de revisão

- [ ] Todo método público é keyword-only, recebe `actor`, tem tipos e docstring com Autorização/Erros/Efeitos.
- [ ] Nenhum retorno é modelo, QuerySet ou dict mutável; nenhum campo sensível sem justificativa documentada.
- [ ] Autorização dentro do método; inexistente e não autorizado respondem igual.
- [ ] App inativa gera `Unavailable`; nenhuma chamada abre `system_context` ou troca de organização.
- [ ] Alterações com `expected_revision`; criações repetíveis com `idempotency_key`.
- [ ] Sem efeito externo dentro do método; rollback do chamador não deixa rastros.
- [ ] Consumidor importa só `service_api` e declara a dependência no manifesto.
- [ ] Testes A/B, default deny, desativada, ator divergente, CAS e idempotência em PostgreSQL com a role runtime.
- [ ] `docs/service-api.md` da app atualizado, com histórico de versões.
