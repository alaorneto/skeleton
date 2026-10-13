# Anotações

## Contract
Extensão opt-in `example_notes`, com dependências técnicas instaladas offline. Cada Note pertence a uma organização e deriva de SecuredRecord. A configuração `heading` é texto de até 200 caracteres, sem segredos. URLs próprias usam `/o/<org>/apps/example_notes/` e `/api/v1/o/<org>/apps/example_notes/summary/`. Navegação exige ativação e table.read; o servidor revalida as duas. Administração de ativação/configuração exige organization_admin e CAS. Não há admin global de registros tenant.

## Acceptance
A/B não compartilham notas nem configuração; tabela sem read é negada; desligar a app bloqueia também o gateway e preserva dados/audit. O sumário conta somente registros autorizados. Body é redigido nos eventos. Reativação não modifica IDs nem ACL. Specs/ADRs são obrigatórias e verificadas pelo check estático.
