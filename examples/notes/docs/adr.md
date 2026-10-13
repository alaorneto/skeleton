# Extensão declarada e ativação organizacional

## Context
Instalar uma app Django não implica autorização de negócio nem habilitação em todos os tenants. O catálogo físico não executa DDL.

## Decision
AppManifest registra dependências, configuração escalar, rotas próprias e navegação no código. AppInstallation mantém estado auditado por organização, com CAS e lock de Organization para mudanças de dependências. Ausência de estado usa o default_enabled declarado, preservando o comportamento de apps já instaladas no upgrade. Desativação é reversível; remoção de software/schema é manutenção offline revisada.

## Consequences
Nenhuma instalação ou migration é executada por HTTP. Ativação não concede TablePermission. O exemplo demonstra HTML/API/configuração e administração tenant, sem registrar tabelas organizacionais no admin global. Mudanças de manifesto exigem deployment e checks; configuração sensível é recusada e o JSON de configuração é redigido integralmente na auditoria.
