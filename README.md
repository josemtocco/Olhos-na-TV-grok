# Olhos na TV → M3U para SS IPTV

Coletor automático que gera playlists M3U aninhadas (compatíveis com **SS IPTV**) a partir das páginas públicas de [olhosnatv.com.br](https://www.olhosnatv.com.br/).

## O que este projeto faz

1. Descobre os canais via **feed JSON/Atom do Blogger** (com paginação completa).
2. Em falha do feed, faz fallback para a **paginação HTML** do blog.
3. Extrai e valida streams (`.m3u8`, `.mpd`, etc.) com resolução limitada de iframes/players.
4. Mantém canais antigos quando a página falha temporariamente, se o stream antigo ainda responde.
5. Gera:
   - `playlist.m3u` — raiz com categorias (`type="playlist"`)
   - `categoria-*.m3u` — uma playlist por categoria
   - `channels.json` — estado persistente
6. Atualiza automaticamente a cada 6 horas via GitHub Actions.

## Melhorias desta versão

- **Paginação completa do feed JSON/Atom** (antes só a 1ª página ~150 posts era lida; o blog tem ~800).
- Nomes de canais com **capitalização legível** (`rede brasil` → `Rede Brasil`).
- Recuperação correta dos **rótulos de categoria** na playlist raiz.
- Flag `--dry-run` para testar sem gravar ficheiros.
- Mais testes unitários.
- Logging mais claro (`Entradas acumuladas`, `RESULTADO` com nº de categorias).
- `.gitignore` para `__pycache__` e artefactos locais.

## Instalação

Substitua os ficheiros do repositório por estes e faça commit/push.

Depois:

**GitHub → Actions → Atualizar playlist → Run workflow**

## Playlist para SS IPTV

Após a primeira execução bem-sucedida:

```
https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPOSITORIO/main/playlist.m3u
```

No repositório de referência:

```
https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/playlist.m3u
```

No SS IPTV: **Settings → Content → External Playlists** → cole a URL da `playlist.m3u`.

O `config.json` contém `github_raw_base`. Se o repositório mudar de nome/dono, atualize esse valor.

## Uso local

```bash
python -m pip install -r requirements.txt
python -m unittest -v
python scraper.py --verbose          # grava playlist
python scraper.py --verbose --dry-run  # só testa
```

## Diagnóstico (Actions)

Mensagens esperadas no log:

- `Lendo feed Blogger do próprio domínio`
- `Lendo feed JSON página N` / `Entradas acumuladas: ...`
- `[1/... ] NOME DO CANAL -> OK|falhou`
- `RESULTADO: descobertos=... ativos=... falhas=... novos=... removidos=... categorias=...`

Se aparecer `0 canais ativos` ou falha >90%, o workflow **interrompe** e a playlist anterior **não é substituída**.

## Configuração (`config.json`)

| Chave | Descrição |
|-------|-----------|
| `feed_page_size` | Posts por página do feed (default 150) |
| `max_feed_pages` | Máximo de páginas a percorrer (default 40) |
| `max_workers` | Threads paralelas (default 8) |
| `max_resolve_depth` | Profundidade de resolução de players (default 2) |
| `validate_streams` | Testa se o stream responde (default true) |
| `github_raw_base` | Base das URLs raw das categorias |
| `max_failure_ratio` | Aborta se a % de falhas for maior (default 0.90) |

## Fonte e aviso legal

Fonte: https://www.olhosnatv.com.br/

O projeto **apenas referencia** streams encontrados em páginas públicas; **não hospeda** vídeos. Use apenas conteúdo que tenha direito de aceder.
