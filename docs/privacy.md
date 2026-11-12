# Privacidade: direitos do titular

O núcleo atende os direitos da pessoa sobre os dados que ele mesmo guarda: a identidade global, o perfil, os tokens e, em cada organização, o vínculo, as notificações e os anexos enviados. Os registros de negócio pertencem a cada organização, que é a controladora deles, e ficam fora daqui.

## Exportação dos próprios dados

Em `/profile/privacy/`, a pessoa baixa um ZIP com `dados.json` ([#190](https://github.com/alaorneto/skeleton/issues/190)). O arquivo traz:

- a identidade (email, nomes, criação, último login) e o perfil (nome de exibição, idioma, fuso);
- os metadados dos tokens de API: prefixo, nome, escopos, validade e revogação, nunca o digest;
- em cada organização em que a pessoa tem vínculo ativo, o vínculo, as notificações recebidas (tipo, datas, alvo e snapshot) e os metadados dos anexos que ela enviou.

A exportação é montada na própria requisição. Ela é uma rota agregada que lê cada organização num contexto só de leitura, com o vínculo revalidado. O ZIP sai como anexo e com `no-store`, então não há arquivo guardado para proteger ou expirar. O download exige autenticação recente e grava o evento de segurança `privacy.export`. Nunca aparecem dados de outra pessoa, segredos ou registros de negócio.
