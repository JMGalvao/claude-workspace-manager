# Claude Workspace

Gestor local para as tuas sessões do Claude Code. Com um clique (ou ao iniciar sessão) abre uma janela de terminal por sessão e **retoma cada conversa exata** (`claude --resume <id>`). Tem uma interface web, e no GNOME coloca cada janela no seu workspace, mostra o estado no painel superior e avisa quando uma sessão termina.

A interface está em português.

## Plataformas

| | Linux (GNOME, Wayland ou X11) | macOS | Windows |
|---|---|---|---|
| Retomar sessões (`--resume`) | sim | sim (experimental) | ainda não |
| Interface web | sim | sim | ainda não |
| Cada janela no seu workspace | sim (extensão GNOME) | não (o macOS não tem API para isso) | n/a |
| Painel superior e notificação com clique | sim | notificação simples | n/a |
| Fechar sessões e terminais | sim | sim (experimental) | n/a |

Testado em Ubuntu 24.04 com GNOME 46. O suporte a macOS foi escrito sem acesso a um Mac: se algo falhar, abre uma issue com o erro.

## Instalar (Linux/GNOME)

Requisitos: `python3` (só biblioteca standard), `claude` no PATH, `gnome-terminal`, `curl`.

```bash
git clone <URL deste repositório> claude-workspace
cd claude-workspace
python3 install.py        # escolhe o instalador certo para o teu sistema
```

(`./install.sh` faz o mesmo no Linux; `./install.sh --trust-folders` ativa a opção de pastas confiáveis, ver Segurança.)

Termina sessão e volta a entrar uma vez (para o GNOME carregar a extensão). Depois abre o gestor com **Super+W** ou pelo menu de aplicações.

## Instalar (macOS, experimental)

Requisitos: `python3`, `claude` no PATH, Terminal.app.

```bash
python3 install.py        # ou ./install-macos.sh
./open-manager.sh
```

O macOS pede permissão de Automação para controlar o Terminal: aceita. Os "workspaces" na interface funcionam como grupos (a ordem de abertura), mas as janelas não são colocadas em Spaces.

## Uso

1. Abre o gestor (`http://localhost:8765`).
2. Importa as sessões que já tens abertas ("Importar") ou adiciona projetos à mão (pasta, perfil, workspace, flags).
3. "Restaurar workspace" abre tudo. Em "Restaurar no login" ligas isso ao arranque do computador.

A tua configuração fica em `workspace.json`, criado na primeira instalação a partir de `workspace.example.json`. **Esse ficheiro tem os teus caminhos e ids de sessão e está no `.gitignore`: não o publiques.**

## O layout segue as sessões ativas

"No layout" (a caixa de cada sessão) é automático:
- Uma sessão nova que abras (em qualquer terminal) entra sozinha no layout, no workspace onde está a janela.
- Só sai do layout quando a fechas tu (X da janela, `/exit`) ou carregas em "Parar" na app.
- Desligar o PC, terminar sessão, reiniciar o servidor, "Reiniciar" ou "Reiniciar desatualizadas" **não** tiram nada do layout: no próximo "Restaurar" voltam todas.
- Se desligares a caixa de uma sessão que está a correr, ela continua a correr mas fora do layout.

## Segurança

- O servidor só escuta em `127.0.0.1` e rejeita pedidos com `Host` ou `Origin` que não sejam o dele (protege contra páginas web que tentem falar com ele).
- Não há autenticação: **qualquer programa a correr como tu pode usar a API**, que consegue abrir `claude` em qualquer pasta que o utilizador indique. É o mesmo nível de confiança de um terminal teu. Não exponhas a porta (nada de `0.0.0.0`, túneis ou port-forward).
- Os argumentos são passados ao shell com `shlex.quote` e os ids de sessão são validados como UUID.
- **Pasta confiável automática** (opcional, desligada por defeito): o Claude Code pergunta "confias nesta pasta?" na primeira vez. Com `"auto_trust": true` em `workspace.json` (e `./install.sh --trust-folders` para o terminal normal), este projeto escreve `hasTrustDialogAccepted` em `~/.claude.json` para as pastas que geres, saltando essa pergunta. Isso retira uma proteção do Claude Code: só atives se confiares nas pastas que listas.
- A flag `--dangerously-skip-permissions` só é usada se a puseres tu nas flags de um projeto.
- Nada é enviado para a internet por este projeto.

Para reportar uma vulnerabilidade, ver [SECURITY.md](SECURITY.md).

## Como funciona (resumo)

- `server.py`: servidor HTTP (biblioteca standard) com a API e o estado. Lê as sessões vivas em `~/.claude/sessions/*.json` (ficheiros do próprio Claude Code).
- `index.html`: a interface web.
- `extension/`: extensão GNOME Shell (ponte D-Bus para mover janelas, painel superior, banner mais longo).
- `trust.py`: a parte opcional de pastas confiáveis.
- `install.sh`, `install-macos.sh`, `open-manager.sh`, `restore-on-login.sh`: instalação e arranque.

## Limitações conhecidas

- A ligação entre janela e sessão (no GNOME) faz-se pelo título da janela, por isso nomes iguais podem confundir.
- Reiniciar o servidor a meio de um restauro deixa o restauro incompleto.
- Windows não está suportado ainda (o Windows não tem API pública estável para posicionar janelas em desktops virtuais; um suporte mínimo seria só retomar sessões).

## Licença

MIT, ver [LICENSE](LICENSE).
