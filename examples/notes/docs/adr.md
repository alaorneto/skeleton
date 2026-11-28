# Extensão declarada e ativação organizacional

Este registro de decisão explica por que o exemplo Notes separa código instalado, uso por organização e permissões. Veja [apps, pacotes e deployment](../../../docs/concepts.md#apps-pacotes-e-deployment) para os conceitos e [a especificação](spec.md) para o comportamento esperado.

## Context
Instalar uma app Django a disponibiliza no ambiente compartilhado, mas isso não significa que todas as organizações devam usá-la ou ler seus registros. É preciso conseguir desligar o uso em uma organização sem perder dados, mantendo configuração e autorização em camadas distintas. O catálogo físico apenas descreve as tabelas existentes: alterar seus metadados não executa DDL, ou seja, não cria nem modifica estruturas no banco.

## Decision
`AppManifest` declara no código as dependências, a configuração escalar, as rotas próprias e a navegação. O manifesto é versionado junto com a app, enquanto `AppInstallation` armazena a ativação e a configuração auditadas por organização. As alterações usam CAS (comparação da revisão antes da escrita) e travam a linha de `Organization` para validar mudanças de dependências sem condições de corrida.

Sem linha de estado persistida, a app permanece desativada: a ativação é sempre explícita. Organizações que já usavam o Notes ativo por padrão mantêm o estado por meio de `upgrade_app_activation` do núcleo. Desativar é reversível e preserva tanto as notas quanto suas regras de acesso. Operações globais de software são solicitadas pelo administrador da plataforma e executadas pelo agente separado — requisições web nunca instalam código nem executam migrations. Remover dados ou schemas exige manutenção offline revisada.

O manifesto v2 canônico declara a apresentação de forma imutável junto ao código, sem executar DDL nem consultas em `ready()`. Os spans de grade (`code` 4, `title` 8, `body` 12) e `rows` 8 funcionam em todo núcleo publicado; o core usa `django-crispy-forms` 2.7 com pack SSR próprio apenas como renderizador dos formulários existentes. O painel administrativo da app fica restrito a parâmetros, enquanto listas e formulários de notas usam o viewer tenant fora dele. Abas, seções, rótulos e dimensões nunca ampliam campos nem acesso além de `TableSpec`. Modelos sem metadados de UI continuam usando o shell e os padrões do núcleo, dispensando implementações paralelas de formulários ou arquivos de interface extras.

Outras apps consomem o Notes exclusivamente via `NoteService` (`service_api.py`), trabalhando com DTOs imutáveis e autorização revalidada em cada método. As views e a API REST da própria app chamam esse mesmo serviço, validando o contrato continuamente e evitando regras de acesso duplicadas.

## Consequences
Ativar a app não concede `TablePermission`: mesmo que o menu apareça, o servidor revalida a ação e o acesso ao registro. Essa separação impede que a simples presença do software seja tratada como autorização. O exemplo demonstra HTML, API, configuração e administração organizacional sem registrar tabelas de negócio no admin global.

Alterações no manifesto exigem deployment e novas verificações, pois impactam código, dependências e rotas de todas as organizações. Já a configuração organizacional pode mudar em tempo de execução, sem novo deploy, dentro dos campos declarados e protegida por revisão. Configurações sensíveis são recusadas: segredos pertencem ao deployment privado, e o JSON permitido é integralmente redigido na auditoria.

Preservar dados e estado torna a desativação segura e reversível, mas não libera espaço em banco nem equivale a desinstalar a app; da mesma forma, retirar o código globalmente não autoriza apagar seu histórico. Checks de documentação e testes simulados validam contratos estruturais, mas não substituem a homologação de um pacote para produção.

## Fluxo de publicação no próprio exemplo

**Contexto.** Quando o núcleo ganhou o motor de fluxos de processo, precisou de um exemplo opt-in que exercitasse etapas, bifurcações, formulários e aprovações.

**Decisão.** O fluxo foi incorporado ao próprio Notes, atuando sobre a anotação, em vez de criar uma segunda app de exemplo. A app apenas declara o fluxo e invoca `FlowService`, sem criar tabelas adicionais.

**Consequências.** Nenhuma migration nova foi necessária, e a versão do Notes avançou para 0.4.0. Como todo núcleo publicado já inclui `skeleton.flows`, o piso de core no manifesto permanece `>=0.0.0`.
