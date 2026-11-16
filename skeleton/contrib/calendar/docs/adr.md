# Calendário pessoal como registro privado do tenant

## Context
A agenda precisa de um calendário de cada pessoa dentro da organização e de calendários compartilhados. Os eventos herdam a autorização do calendário, e o administrador da organização não deve ler o conteúdo dos calendários pessoais alheios.

## Decision
O calendário pessoal é um `SecuredRecord` privado do membro, com visibilidade travada em `private` pelo modelo e por uma restrição no banco. A delegação usa as concessões de registro existentes. A app é first-party, distribuída no wheel do núcleo, para seguir o mesmo trem de release.

## Consequences
O administrador vê que o calendário existe, mas os eventos dele aparecem só como "Ocupado". Remover a app preserva os dados (schema retido pelo snapshot do wheel).

## Eventos gerenciados por outras apps (#537)

**Context.** Uma app de agendamento precisa projetar cada compromisso na agenda de quem atende sem que a pessoa o mova ou apague pela tela da agenda, sem notificar trinta vezes por dia e, às vezes, sem ator humano (um job ou um agendamento feito por alguém de fora).

**Decision.** O evento ganha `source_app`, `source_key` e `source_path`, imutáveis, e só o serviço de eventos gerenciados o escreve, inclusive em `system_context`. Ele é privado e sem descrição por padrão; os participantes são sincronizados já como `accepted`, sem notificação. A app que chama passa o próprio label: a API é em processo, e essa regra vale por revisão de código.

**Consequences.** A agenda nunca guarda o detalhe do compromisso (fica na origem, pelo link). Mudar o calendário de um compromisso exclui o evento antigo e cria outro com UID novo. Grade de horários, conflitos e lista de espera continuam na app de agendamento.
