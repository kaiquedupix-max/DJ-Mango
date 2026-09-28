# DJ Mango 🥭🎵

Bot de música para Discord com reprodução de áudio do **YouTube por link ou por busca de texto**.

## Recursos

- `/play <busca-ou-link>` — busca uma música no YouTube ou toca um link direto.
- `/pause` — pausa a música atual.
- `/resume` — continua a reprodução.
- `/skip` — pula a música atual.
- `/queue` — mostra a fila.
- `/nowplaying` — mostra a música atual.
- `/volume <0-100>` — altera o volume.
- `/stop` — limpa a fila e desconecta o bot.
- `/musichelp` — mostra os comandos disponíveis.
- Fila independente por servidor.
- Re-resolução do stream antes de tocar cada faixa, evitando URLs expiradas.
- Suporte opcional a cookies do yt-dlp para conteúdos que exijam autenticação.

## Requisitos

- Python 3.12+
- FFmpeg
- Deno 2.3+ recomendado para o suporte atual do YouTube no yt-dlp
- Um bot criado no Discord Developer Portal com o escopo `applications.commands` e permissões para `View Channel`, `Connect`, `Speak`, `Send Messages` e `Embed Links`.

## Configuração local

1. Clone o repositório.
2. Copie `.env.example` para `.env`.
3. Preencha `DISCORD_TOKEN`.
4. Instale as dependências:

```bash
pip install -r requirements.txt
```

5. Certifique-se de que FFmpeg e Deno estejam disponíveis no PATH.
6. Inicie:

```bash
python bot.py
```

## Exemplos

Entre em um canal de voz e use:

```text
/play Evidências Chitãozinho e Xororó
/play Linkin Park Numb
/play https://www.youtube.com/watch?v=...
```

O bot entra no seu canal, coloca a música na fila e começa a tocar.

## Docker

O `Dockerfile` já instala FFmpeg, libopus e Deno.

```bash
cp .env.example .env
# edite .env e coloque seu token
docker compose up -d --build
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| `DISCORD_TOKEN` | Sim | Token do bot no Discord Developer Portal. |
| `DISCORD_GUILD_ID` | Não | ID de um servidor de teste. Se definido, os slash commands são sincronizados imediatamente nele. |
| `YTDLP_COOKIES_FILE` | Não | Caminho para um arquivo de cookies Netscape compatível com yt-dlp. |
| `DEFAULT_VOLUME` | Não | Volume inicial, de 0 a 100. Padrão: 50. |
| `LOG_LEVEL` | Não | Nível de log, por exemplo `INFO` ou `DEBUG`. |

## Observações sobre o YouTube

O projeto usa `yt-dlp` para pesquisar e resolver os streams do YouTube. Como o YouTube muda os mecanismos de reprodução com frequência, mantenha o `yt-dlp` atualizado. A dependência `yt-dlp[default]` inclui os scripts EJS usados atualmente pelo yt-dlp para resolver os desafios JavaScript do YouTube.

Alguns vídeos podem exigir login. Nesse caso, exporte cookies em formato Netscape, mantenha esse arquivo fora do Git e configure `YTDLP_COOKIES_FILE`.

## Segurança

Nunca faça commit do arquivo `.env`, do token do bot ou dos cookies. Se o token vazar, regenere-o imediatamente no Discord Developer Portal.
