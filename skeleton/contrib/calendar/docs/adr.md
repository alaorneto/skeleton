# Calendário pessoal como registro privado do tenant

## Context
A agenda precisa de um calendário de cada pessoa dentro da organização e de calendários compartilhados. Os eventos herdam a autorização do calendário, e o administrador da organização não deve ler o conteúdo dos calendários pessoais alheios.

## Decision
O calendário pessoal é um `SecuredRecord` privado do membro, com visibilidade travada em `private` pelo modelo e por uma restrição no banco. A delegação usa as concessões de registro existentes. A app é first-party, distribuída no wheel do núcleo, para seguir o mesmo trem de release.

## Consequences
O administrador vê que o calendário existe, mas os eventos dele aparecem só como "Ocupado". Remover a app preserva os dados (schema retido pelo snapshot do wheel).
