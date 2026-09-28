# DJ Mango 🥭🎵

Bot de música para Discord focado em YouTube. Aceita **link direto**, **playlist** ou **busca pelo nome da música**.

## Comandos

| Comando | Função |
| --- | --- |
| /play <busca> | Toca um link do YouTube ou pesquisa pelo nome |
| /pause | Pausa a música atual |
| /resume | Continua a reprodução |
| /skip | Pula a faixa atual |
| /queue | Mostra a fila |
| /nowplaying | Mostra a faixa atual |
| /shuffle | Embaralha a fila |
| /loop | Repete a música atual ou a fila inteira |
| /volume <0-100> | Ajusta o volume |
| /stop | Limpa a fila e sai do canal |
| /ajuda | Mostra os comandos |

Cada servidor possui sua própria fila. O bot sai automaticamente do canal depois de ficar sem músicas pelo tempo configurado em IDLE_TIMEOUT.

## Requisitos

- Python 3.12+
- FFmpeg
- Node.js recomendado pelo yt-dlp para os desafios JavaScript atuais do YouTube
- Bot criado no Discord Developer Portal

Permissões recomendadas para o bot: **View Channel**, **Connect**, **Speak**, **Send Messages**, **Embed Links** e o escopo **applications.commands**.

## Configuração local

1. Clone o repositório.
2. Copie `.env.example` para `.env`.
3. Coloque seu token em `DISCORD_TOKEN`.
4. Instale as dependências:

```bash
pip install -r requirements.txt
```

5. Tenha o FFmpeg disponível no PATH.
6. Inicie:

```bash
python bot.py
```

Exemplos no Discord:

```text
/play Evidências Chitãozinho e Xororó
/play Linkin Park Numb
/play https://www.youtube.com/watch?v=...
/play https://www.youtube.com/playlist?list=...
```

## Docker

O Dockerfile já instala FFmpeg e Node.js.

```bash
cp .env.example .env
# edite o .env e adicione o token
docker compose up -d --build
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| DISCORD_TOKEN | Sim | Token do bot |
| DISCORD_GUILD_ID | Não | ID de um servidor de teste; faz os slash commands aparecerem imediatamente nele |
| DEFAULT_VOLUME | Não | Volume inicial de 0 a 100; padrão 50 |
| IDLE_TIMEOUT | Não | Tempo em segundos para sair do canal quando a fila acabar; padrão 120 |
| YTDLP_COOKIES_FILE | Não | Caminho para cookies Netscape usados pelo yt-dlp quando o YouTube exigir autenticação |
| YTDLP_PROXY | Não | Proxy opcional para o yt-dlp |

## YouTube

O DJ Mango usa **yt-dlp** para busca, resolução do vídeo e obtenção do stream de áudio. O stream é re-resolvido quando cada música começa, evitando deixar URLs temporárias expirarem dentro da fila.

O YouTube muda os mecanismos de reprodução com frequência. Se a reprodução parar de funcionar, atualize o yt-dlp. Alguns vídeos também podem exigir uma sessão autenticada; nesse caso use YTDLP_COOKIES_FILE.

## Segurança

Nunca envie seu `.env`, token do Discord ou arquivo de cookies para o GitHub. Esses arquivos estão ignorados pelo `.gitignore`.
