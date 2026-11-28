---
name: instance-theme
description: Criar, conferir, montar e publicar um tema da instância (pacote de tema com theme.json, theme.css, imagens e fontes) a partir de um sistema de design ou de uma marca, num projeto derivado do Skeleton ou numa app. Use SEMPRE que o pedido falar em "tema", "tema da instância", "marca", "identidade visual", "aplicar a marca", "sistema de design", "design tokens", "logo", "favicon", "ícone", "fonte da marca", "cores da marca", "modo escuro da marca", "pacote de tema", "theme.json", "personalizar a aparência", ou quando você estiver prestes a sobrescrever um template do núcleo (base.html, base_entrance.html, error_base.html) para mudar cores, logo ou nome.
---

# Tema da instância

A regra geral está no `AGENTS.md` (não sobrescreva templates do shell do núcleo). O formato do pacote está em [docs/appearance.md](../../../docs/appearance.md#pacote-de-tema) e o guia detalhado em [docs/themes.md](../../../docs/themes.md). Este skill reúne o roteiro operacional curto. Escreva textos e comentários em português.

## Procedimento

1. **Leia a referência e confira o núcleo:** revise `docs/themes.md` e a seção *Pacote de tema* de `docs/appearance.md`. Verifique a versão instalada (`python -c "from skeleton.versioning import core_version; print(core_version())"`). Se o núcleo ainda não oferecer `manage.py appearance check-package`, interrompa e informe ao usuário que é preciso atualizar o núcleo em vez de improvisar atalhos.
2. **Mapeie o sistema de design:** levante cor primária, cores secundárias e de estado, escala de cinzas, tipografia (corpo e títulos, conferindo licenças), raios, sombras, regras do modo escuro, logo horizontal, logo para fundo escuro e ícone.
3. **Escolha `tokens.brand_color`:** use um tom da marca que atinja contraste AA (4,5:1) com texto branco. Se a cor oficial for clara demais, use uma variação mais escura da mesma família em `brand_color` e mantenha o tom original em um token próprio (`--<prefixo>-brand`).
4. **Crie a estrutura do pacote:** monte o diretório (por exemplo `temas/<nome>/`) seguindo `docs/themes.md`: `theme.json`, `theme.css`, `images/`, `fonts/` e `LICENSES/`.
5. **Preencha `theme.json`:** declare `format: 1`, `id` estável, `version`, `name`, `compatibility.skeleton` cobrindo o núcleo instalado, `tokens`, `brand` (nome do produto e rodapé, se aplicável), `images`, `fonts` e `css`.
6. **Escreva `theme.css`:** use apenas os tokens do contrato (`--color-*`, `--radius-*`, `--font-*`, `--shadow-*`), tokens próprios com prefixo do projeto e seletores das classes estáveis listadas em `docs/ui-design.md`. Não use `<` (nem em comentários), `url(` ou `@import`. Defina o modo escuro com `@media (prefers-color-scheme: dark)` — nunca com `[data-color-scheme]`, pois o núcleo converte a media query automaticamente quando a pessoa força claro ou escuro.
7. **Prepare imagens e fontes:** use SVG simples e otimizado (ou PNG/WebP) para os logos e PNG 512 × 512 para o ícone. Coloque as fontes `.woff2` diretamente em `fonts/` e as licenças correspondentes em `LICENSES/`.
8. **Valide o pacote:** execute `python manage.py appearance check-package <diretório>` até obter `"Pacote válido."`. Corrija cada apontamento indicado (arquivo e linha) sem afrouxar validações.
9. **Gere o arquivo `.zip`:** execute `python manage.py appearance build-package <diretório> --output <arquivo.zip>`.
10. **Se o tema pertencer a uma app:** coloque o diretório em `themes/<slug>/` dentro da app (ou gere via `skeleton_startapp ... --with-theme <slug>`), use `id` no formato `<app_id>.theme.<slug>`, declare em `runtime.instance_themes` com o piso `INSTANCE_THEMES_CORE_MINIMUM` e valide na suíte com `skeleton.testing.appearance.assert_app_themes_valid("<app_label>")`. Nesse caso, o tema é distribuído com a app e não precisa de `.zip` avulso.
11. **Publique:** envie o `.zip` em `/platform/appearance/` (*Instalar pacote de tema*) ou rode `python manage.py appearance install-package <zip>` (idempotente). Instalar não ativa automaticamente: pré-visualize e ative pela interface.
12. **Confira no navegador:** valide a pré-visualização e, após ativar, teste o login, uma página de organização e os modos claro e escuro (tanto o do sistema quanto a seleção manual no menu da conta).
13. **Limpe sobrescritas antigas:** se o projeto tinha cópias de templates do shell apontadas por `W046` no `manage.py check`, remova-as após ativar o tema, bem como context processors usados apenas para trocar o nome do produto.

## Pare e pergunte

- A fonte da marca não possui licença compatível com auto-hospedagem.
- A cor primária não alcança contraste AA com branco e o usuário não aprova um tom mais escuro.
- O sistema de design exige alterar layout, menus ou estrutura de componentes (o que foge do escopo de tema).
- O núcleo instalado ainda não possui os comandos de pacote de tema.

## Nunca

- Sobrescrever `skeleton/web/base.html`, `error_base.html`, `platform/base.html`, `account/base_entrance.html` ou qualquer template do shell para aplicar marca.
- Editar `skeleton/web/static/` ou `frontend/` do núcleo, carregar fontes/CSS de CDN ou fixar cores e nomes diretamente em templates.
- Desativar a validação de CSS ou SVG, ou silenciar `W046` sem justificativa registrada.
