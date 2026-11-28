# Política de segurança

## Versões suportadas

Somente a versão publicada mais recente do `django-skeleton` (o campo `version` em `pyproject.toml` na branch `main`) recebe correções de segurança. O agente de deployment e as apps first-party acompanham o mesmo número e a mesma política. Projetos derivados devem atualizar o núcleo para receber as correções.

## Como relatar

Use **"Report a vulnerability"** na aba **Security** deste repositório (relato privado de vulnerabilidades do GitHub). O relato fica visível apenas para os mantenedores.

**Não abra issue, PR ou discussão pública com detalhes de exploração.** Divulgar uma falha em público expõe todos os projetos derivados antes que haja correção disponível. Se o botão de relato privado não estiver visível, abra uma issue pública apenas pedindo um canal de contato, sem descrever a falha.

## O que incluir

- O componente afetado (por exemplo `skeleton/access`, `skeleton/integrations`, `deployment_agent` ou uma app em `skeleton/contrib`).
- A versão publicada ou o commit em que o problema ocorre.
- O impacto: o que um atacante consegue ler, alterar ou executar, e qual o nível de acesso inicial necessário (anônimo, membro de organização, administrador de organização ou operador).
- Os passos mínimos para reproduzir, de preferência em um checkout local com dados de teste.

Nunca inclua dados pessoais reais nem segredos de produção no relato.

## O que esperar

Você receberá confirmação de recebimento e uma avaliação em prazo razoável. Confirmada a vulnerabilidade, a correção entra por meio de uma issue resumida com os labels `bug` e `security` (sem passos de exploração) e um PR vinculado. Os detalhes técnicos são divulgados somente após a publicação da versão corrigida, com crédito ao autor do relato caso deseje.

## Escopo

Estão no escopo os controles descritos em [docs/access-control.md](docs/access-control.md), [docs/audit-soft-delete.md](docs/audit-soft-delete.md) e [docs/deployment.md](docs/deployment.md). Ficam fora de escopo os limites que a arquitetura declara explicitamente não garantir ([docs/architecture.md](docs/architecture.md#segurança-o-que-a-arquitetura-não-promete)) — como proteção contra quem já controla o DBA ou owner do banco, o SQLite da instância e sua chave mestra, ou o host do agente — além de falhas decorrentes de configurações explicitamente desaconselhadas na documentação.
