# Segurança

## Modelo de ameaça

Aplicação local, para um só utilizador. O servidor escuta apenas em `127.0.0.1:8765`, valida `Host` e `Origin`, e não tem autenticação: confia em tudo o que corre como o teu utilizador. Não a coloques acessível pela rede.

## Reportar uma vulnerabilidade

Usa "Report a vulnerability" na aba Security do repositório (aviso privado). Não abras uma issue pública com detalhes de exploração.

## Dados pessoais

`workspace.json` (caminhos, nomes e ids de sessão), `last-restore.json` e `notifier.log` são locais e estão no `.gitignore`. Antes de fazer fork ou commit, confirma com `git status` que não os estás a adicionar.
