# Agenda

A agenda (`skeleton_calendar`) é a primeira app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)). O operador a instala com `SKELETON_BUNDLED_APPS=calendar`; a plataforma a disponibiliza e cada organização a ativa, como qualquer app.

## Contract
`Calendar` (`SecuredRecord`) pertence a uma organização: `name` (até 150), `kind` (`personal` ou `shared`, imutável), `is_default`, `color` (`#RRGGBB`), `timezone` (IANA) e `description` (até 2000).

- Cada membro tem **um** calendário pessoal padrão por organização, criado na primeira visita (`ensure_personal_calendar`); duas visitas simultâneas criam um só, pela restrição única parcial.
- O calendário pessoal é sempre `private`: mudar a visibilidade responde 400, e o dono não muda. A delegação a outra pessoa usa concessões de registro (`RecordGrant`), não visibilidade.
- O calendário pessoal padrão não pode ser excluído.
- Calendários criados pelo gateway da tabela (`/api/v1/o/<org>/tables/skeleton_calendar.calendar/records/`) são compartilhados; as escritas passam pelos serviços da app (`RecordOperations`).
- Sem `TablePermission` de leitura em `skeleton_calendar.calendar`, a agenda não existe para o membro (404). A ação do administrador `member-access` concede ao papel `member` as permissões básicas (`read`, `create`, `update`, `delete`) pelos serviços de acesso.

### Eventos (#69)
`Event` (`OrganizationRecord`, fora do gateway genérico) pertence a um calendário: `uid` (único entre eventos vivos da organização), `title`, `description`, `location`, `all_day`, `starts_at`/`ends_at` (UTC) ou `start_date`/`end_date` (dia inteiro, fim exclusivo), `tzid`, `is_private`, `status` (`confirmed`, `tentative`, `cancelled`), `organizer` e `sequence` (cresce a cada alteração).

- Ler um evento exige ler o calendário; criar, alterar, cancelar ou excluir exige `update` no calendário. No calendário pessoal de outra pessoa, só uma delegação de `update` (concessão de registro) escreve: o bypass do administrador lê, mas não escreve.
- Um evento privado aparece como "Ocupado" (sem título, descrição nem local) para quem não é o organizador nem o dono do calendário. No calendário pessoal de outra pessoa, o administrador sem delegação vê todos os eventos só como "Ocupado".
- Título, descrição e local são redigidos na auditoria, para que o histórico não revele o que a tela esconde.
- Fim igual ou anterior ao início responde 400, no modelo e por restrição no banco. Alterações usam CAS pela revisão.

### Recorrência (#70)
O evento mestre guarda a regra (`rrule`), `rdates` e `exdates`; uma linha de substituição aponta para o mestre (`series`) e nomeia a ocorrência que substitui (`recurrence_id`, o início original, como o RECURRENCE-ID do iCalendar, com o mesmo `uid`).

- Subconjunto aceito: `FREQ` DAILY, WEEKLY, MONTHLY ou YEARLY, com `INTERVAL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS` e `WKST`, sempre limitado por `COUNT` (até 1000) ou `UNTIL`. Qualquer outra parte (`FREQ=SECONDLY`, `BYHOUR`) responde 400.
- A regra roda no relógio local do `tzid` (`python-dateutil` + `zoneinfo`): um evento semanal às 09:00 continua às 09:00 locais na troca de horário, e o instante UTC muda.
- `expand()` lê uma janela de até 400 dias e devolve até 1000 ocorrências, com `truncated` quando para antes.
- Edição de uma ocorrência: "só esta" cria ou altera a substituição; "esta e as seguintes" divide a série (a primeira parte termina antes da ocorrência, um novo mestre com `uid` novo começa nela e as substituições posteriores passam para ele mantendo os IDs); "todas" altera o mestre. Cancelar uma ocorrência acrescenta um EXDATE.
- `recurrence_end` guarda o fim da série para consultas por janela.

## Acceptance
- Um calendário pessoal padrão por membro, inclusive com criação concorrente.
- Mudar a visibilidade de calendário pessoal responde 400.
- Membro sem TablePermission recebe 404.
- A organização B não lê calendários de A.
- Quem lê o calendário sem `update` lê os eventos e não os cria.
- Evento privado aparece só como "Ocupado" para os demais leitores; o administrador vê só blocos ocupados no calendário pessoal de outra pessoa.
- Fim igual ou anterior ao início responde 400; dia inteiro é guardado como datas.
- Um evento semanal às 09:00 continua às 09:00 locais na troca de horário (Europe/Berlin e o histórico de America/Sao_Paulo).
- EXDATE remove uma ocorrência; uma substituição move uma.
- `FREQ=SECONDLY` responde 400; uma expansão acima do limite é truncada e sinalizada.
- "Esta e as seguintes" divide a série sem perder IDs.
