# Extensão declarada e ativação organizacional

Este registro de decisão explica por que o exemplo separa código instalado, uso por organização e permissões. Veja [apps, pacotes e deployment](../../../docs/concepts.md#apps-pacotes-e-deployment) para os conceitos e [a especificação](spec.md) para o comportamento esperado.

## Context
Uma app Django instalada passa a existir no ambiente compartilhado, mas isso não significa que todas as organizações possam usá-la ou ler seus registros. É necessário permitir desligar seu uso sem perder dados, além de controlar configuração e autorização separadamente. O catálogo físico descreve tabelas existentes; alterar seus metadados não executa DDL, isto é, não cria nem modifica estruturas do banco.

## Decision
`AppManifest` registra no código as dependências, a configuração escalar, as rotas próprias e a navegação. O manifesto é versionado junto com a app, enquanto `AppInstallation` guarda ativação e configuração auditadas por organização. As alterações usam CAS (comparação da revisão antes de gravar) e bloqueiam `Organization` para validar mudanças de dependências sem corrida entre operações.

Se ainda não há uma linha de estado, vale o `default_enabled` declarado: isso preserva o comportamento das apps já instaladas durante uma atualização. Desativar é reversível e não remove notas nem suas regras de acesso. Operações globais de software são solicitadas pelo administrador da plataforma e executadas pelo agente separado; a requisição web não instala código nem executa migrations. Remover dados ou schema exige manutenção offline revisada.

O manifesto v2 canônico declara apresentação imutável junto ao código, sem DDL ou consultas em `ready()`. Notes 0.2.0 exige core >=0.3.0 para spans code 4/title 8/body 12 e rows 8; o core usa django-crispy-forms 2.7 com pack SSR próprio, somente como renderer dos formulários existentes. O admin é reservado a parâmetros; listas/formulários de notas usam o viewer tenant fora dele. Abas, seções, labels e dimensões não concedem campos ou acesso além de `TableSpec`. Um modelo sem metadados de UI continua usando o shell/defaults do core, evitando exigir uma segunda implementação de formulário ou arquivo UI paralelo.

## Consequences
Ativação não concede `TablePermission`; mesmo com menu visível, o servidor revalida a ação e o registro. Essa separação evita tratar a presença de software como autorização. O exemplo demonstra HTML, API, configuração e administração organizacional sem registrar tabelas de negócio no admin global.

Mudanças do manifesto exigem deployment e verificações, pois podem afetar código, dependências e rotas de todas as organizações. Já a configuração organizacional pode mudar sem instalar uma versão nova, dentro dos campos declarados e com revisão. Configuração sensível é recusada: segredos pertencem ao deployment privado, e o JSON permitido é redigido integralmente na auditoria.

Preservar estado e dados torna a desativação segura e reversível, mas não libera espaço de banco nem equivale a desinstalar a app. A retirada global do código também não autoriza apagar seu histórico. Checks de documentação e testes simulados não constituem aprovação de um pacote para produção.
