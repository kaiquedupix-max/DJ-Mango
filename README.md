# DJ Mango 🎵

Bot de música para Discord com reprodução de áudio do YouTube por link ou por busca de texto.

## Recursos

- `/play <busca-ou-link>` — busca uma música no YouTube ou toca um link direto.
- `/pause` — pausa a música atual.
- `/resume` — continua a reprodução.
- `/skip` — pula a música atual.
- `/queue` — mostra a fila.
- `/nowplaying` — mostra a música atual.
- `/volume <0-100>` — altera o volume.
- `/stop` — limpa a fila e desconecta o bot.
- Fila independente por servidor.
- Reconexão do FFmpeg para streams.
- Suporte opcional a cookies do yt-dlp para conteúdos que exijam autenticação.
- Dockerfile e docker-compose prontos para deploy.

## Requisitos

- Python 3.12+
- FFmpeg
- Um bot criado no Discord Developer Portal com permissão de `Connect`, `Speak`, `View Channel` e o escopo `applications.commands`.

## Configuração local

1. Clone o repositório.
2. Copie `.env.example` para `.env`.
3. Preencha `DISCORD_TOKEN`.
4. Instale as dependências:

```bash
pip install -r requirements.txt
```

5. Certifique-se de que o FFmpeg está instalado e disponível no PATH.
6. Inicie:

```bash
python bot.py
```

## Docker

```bash
cp .env.example .env
# edite .env e coloque seu token
docker compose up -d --build
```

O container já instala FFmpeg e libopus.

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| `DISCORD_TOKEN` | Sim | Token do bot no Discord Developer Portal. |
| `DISCORD_GUILD_ID` | Não | ID de um servidor de teste. Se definido, os slash commands são sincronizados imediatamente apenas nele. |
| `YTDLP_COOKIES_FILE` | Não | Caminho para um arquivo de cookies compatível com yt-dlp. Útil se o YouTube exigir autenticação. |
| `DEFAULT_VOLUME` | Não | Volume inicial, de 0 a 100. Padrão: 50. |

## Uso

Entre em um canal de voz e use, por exemplo:

```text
/play Evidências Chitãozinho e Xororó
/play https://www.youtube.com/watch?v=...
```

O bot entra no seu canal, adiciona a música à fila e começa a tocar.

## Observações sobre o YouTube

O projeto usa `yt-dlp` para resolver buscas e obter o stream de áudio. O YouTube altera seus mecanismos com frequência, então mantenha o `yt-dlp` atualizado. Se um vídeo exigir login ou validação adicional, configure `YTDLP_COOKIES_FILE`.

## Segurança

Nunca faça commit do arquivo `.env` nem do token do bot. Se o token vazar, regenere-o imediatamente no Discord Developer Portal.
