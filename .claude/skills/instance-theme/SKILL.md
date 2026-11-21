---
name: instance-theme
description: Criar, conferir, montar e publicar um tema da instância (pacote de tema com theme.json, theme.css, imagens e fontes) a partir de um sistema de design ou de uma marca, num projeto derivado do Skeleton ou numa app. Use SEMPRE que o pedido falar em "tema", "tema da instância", "marca", "identidade visual", "aplicar a marca", "sistema de design", "design tokens", "logo", "favicon", "ícone", "fonte da marca", "cores da marca", "modo escuro da marca", "pacote de tema", "theme.json", "personalizar a aparência", ou quando você estiver prestes a sobrescrever um template do núcleo (base.html, base_entrance.html, error_base.html) para mudar cores, logo ou nome.
---

# Tema da instância

A norma é o `AGENTS.md` ("não sobrescreva templates do shell do núcleo"); o formato e as regras estão em [docs/appearance.md](../../../docs/appearance.md#pacote-de-tema) e o procedimento completo em [docs/themes.md](../../../docs/themes.md). Este skill é o roteiro curto. Escreva textos e comentários em português.

## Procedimento

1. **Leia** `docs/themes.md` e a seção "Pacote de tema" de `docs/appearance.md`. Confira a versão do núcleo instalada (`python -c "from skeleton.versioning import core_version; print(core_version())"`): os pacotes de tema existem a partir da versão que traz `manage.py appearance check-package`; se o comando não existir, o núcleo é antigo — pare e diga ao usuário que é preciso atualizar o núcleo, em vez de contornar.
2. **Inventarie o sistema de design**: cor primária, cores secundárias e de estado, cinzas, tipografia (corpo e títulos, com licença), raios, sombras, regras do modo escuro, logo horizontal, logo para fundo escuro, símbolo.
3. **Escolha `tokens.brand_color`**: o tom da marca que dá contraste AA (4,5:1) com texto branco. Se o tom oficial for claro demais, use um mais escuro da mesma família para `brand_color` e guarde o oficial como token próprio (`--<prefixo>-brand`).
4. **Crie o diretório do pacote** no repositório do projeto (por exemplo `temas/<nome>/`) com a árvore de `docs/themes.md`: `theme.json`, `theme.css`, `images/`, `fonts/`, `LICENSES/`.
5. **Escreva o `theme.json`** com `format: 1`, um `id` estável do projeto, `version`, `name`, `compatibility.skeleton` (faixa que inclui o núcleo instalado), `tokens`, `brand` (nome do produto e rodapé, se o produto tiver), `images`, `fonts` e `css`.
6. **Escreva o `theme.css`** só com os tokens do contrato (`--color-*`, `--radius-*`, `--font-*`, `--shadow-*`), tokens próprios com prefixo do projeto e regras sobre as classes de componente estáveis listadas em `docs/ui-design.md`. Sem `<` (nem em comentário), sem `url(`, sem `@import`. Modo escuro por `@media (prefers-color-scheme: dark)`.
7. **Prepare as imagens**: logo e logo escuro em SVG simples/otimizado (ou PNG/WebP), ícone PNG 512 × 512. **Fontes** em `.woff2`, direto em `fonts/`, com a licença em `LICENSES/`.
8. **Confira**: `python manage.py appearance check-package <diretório>` até sair "Pacote válido.". Corrija pela mensagem (ela diz o arquivo e a linha); não afrouxe a regra.
9. **Monte**: `python manage.py appearance build-package <diretório> --output <arquivo.zip>`.
10. **Publique** pelo caminho que o usuário pedir: envio em `/platform/appearance/` ("Instalar pacote de tema") ou `python manage.py appearance install-package <zip>` (idempotente). Instalar não ativa: pré-visualize e ative pela tela.
11. **Verifique no navegador** com a pré-visualização do tema e, depois de ativar, o login, uma página de organização e o modo escuro.
12. Se o projeto já tinha cópias de templates do shell (W046 em `manage.py check`), remova-as depois de o tema estar ativo, e também context processors que só trocavam o nome do produto.

## Pare e pergunte

- A fonte da marca não tem licença que permita auto-hospedagem.
- A cor primária não atinge AA com branco e o usuário não aceita um tom mais escuro.
- O sistema de design pede mudança de leiaute, menu ou componente: isso não é tema.
- O núcleo instalado não tem os comandos de pacote de tema.

## Nunca

- Sobrescrever `skeleton/web/base.html`, `error_base.html`, `platform/base.html`, `account/base_entrance.html` ou outro template do shell para aplicar a marca.
- Editar `skeleton/web/static/` ou `frontend/` do núcleo, carregar fonte ou CSS de CDN, ou escrever cor ou nome fixos em template.
- Desligar a validação do CSS ou do SVG, ou silenciar W046 sem motivo registrado.
