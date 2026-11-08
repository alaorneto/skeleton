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
   - `skeleton/service_api/` (base, DTOs, erros: issue #97). Se não existir, escreva o serviço com a mesma forma (classe sem estado, métodos keyword-only com `actor`, DTOs congelados) usando exceções do Django/`skeleton.common.exceptions`. Registre na issue o follow-up de adotar `ServiceAPI`/`@service_method` quando #97 entrar e avise o usuário. Não crie uma base local paralela no projeto.
   - `runtime.service_api` no validador de manifesto (#98) e os checks E018/E019 (#98/#99). Se não existirem, declare o campo só quando o validador aceitar; até lá, documente o módulo em `docs/service-api.md` da app.
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
4. Exija a app ativa no início de cada método (`require_application_enabled` hoje; `Unavailable` depois de #97/#105).
5. Alterações: aplique `expected_revision` no CAS e traduza `ConcurrentUpdate` para `Conflict(code="stale_revision")`. Nunca tente de novo em silêncio.
6. Não abra `system_context`, não troque de organização, não faça commit e não envie email/HTTP dentro do método. Use jobs, eventos ou `on_commit` com valores materializados.
7. Views, REST e jobs da própria app passam a chamar o `service_api`, para existir um único caminho.

## 3. Declarar

- Manifesto: `runtime.service_api = {"module": "applications.<pkg>.service_api", "classes": [...]}` (quando o validador aceitar).
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
- `assert_service_contract` quando o kit (#100) existir.

Rode SQLite e PostgreSQL com a role runtime:

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests/unit tests/integration_sqlite tests/accounts <testes da app>
.venv/bin/python -m pytest tests/integration_postgres <testes PG da app>
```

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
- [ ] Testes A/B, default deny, desativada, ator divergente, CAS e idempotência em SQLite e PostgreSQL runtime.
- [ ] `docs/service-api.md` da app atualizado, com histórico de versões.
