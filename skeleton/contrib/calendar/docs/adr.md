# Calendário pessoal como registro privado do tenant

## Context
A agenda precisa oferecer um calendário pessoal para cada pessoa dentro da organização, além de calendários compartilhados. Os eventos herdam a autorização do calendário ao qual pertencem, e o administrador da organização não deve conseguir ler o conteúdo dos calendários pessoais alheios.

## Decision
O calendário pessoal é um `SecuredRecord` privado pertencente ao membro, com visibilidade travada em `private` tanto no modelo quanto por restrição no banco. A delegação de acesso reutiliza as concessões de registro (`RecordGrant`) existentes. A agenda é distribuída como app first-party no wheel do núcleo, acompanhando o mesmo trem de release.

## Consequences
O administrador consegue ver que o calendário pessoal existe, mas enxerga os eventos dele apenas como "Ocupado". Remover a app preserva os dados no banco, pois o schema é retido pelo snapshot do wheel.

## Eventos gerenciados por outras apps (#537)

**Context.** Apps de agendamento precisam projetar cada compromisso na agenda de quem realiza o atendimento sem permitir que a pessoa o mova ou apague pela tela da agenda, sem gerar dezenas de notificações diárias e, em alguns casos, sem um ator humano (como em jobs ou agendamentos feitos pelo portal externo).

**Decision.** O evento recebe os campos imutáveis `source_app`, `source_key` e `source_path`, escritos exclusivamente pelo serviço de eventos gerenciados, inclusive em `system_context`. O evento nasce privado e sem descrição por padrão, e os participantes são sincronizados já com estado `accepted`, sem disparar notificações. Como a API é em processo, a regra de cada app informar apenas o próprio label é assegurada por revisão de código.

**Consequences.** A agenda nunca armazena os detalhes do compromisso, que permanecem na app de origem acessados pelo link. Alterar o calendário de um compromisso exclui o evento antigo e cria um novo com outro UID. Grade de horários, detecção de conflitos e lista de espera continuam sob responsabilidade da app de agendamento.
