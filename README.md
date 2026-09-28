# DJ Mango 🥭🎵

Bot de música para Discord com reprodução do **YouTube por link ou por busca de texto**, preparado para rodar no Coolify e atender vários servidores.

## Como funciona a concorrência

O Discord mantém um estado de voz por bot dentro de cada servidor. Na prática, **uma conta de bot pode ficar em apenas uma call por servidor de cada vez**.

O DJ Mango resolve isso com um **pool de bots**:

- O bot principal recebe todos os slash commands.
- O bot principal também pode tocar música.
- Cada token adicional em `DISCORD_WORKER_TOKENS` cria um worker.
- Cada worker adiciona **+1 call simultânea por servidor**.
- O usuário continua usando somente `/play` no DJ Mango principal.
- O sistema escolhe automaticamente um bot livre para a call.
- Cada call tem fila, música atual e volume independentes.
- O mesmo worker pode tocar simultaneamente em servidores diferentes.

### Exemplo

Com:

```env
DISCORD_TOKEN=TOKEN_DO_DJ_MANGO
DISCORD_WORKER_TOKENS=TOKEN_DJ_MANGO_2,TOKEN_DJ_MANGO_3,TOKEN_DJ_MANGO_4
```

você terá **4 DJs disponíveis por servidor**:

- Call 1 → DJ Mango
- Call 2 → DJ Mango 2
- Call 3 → DJ Mango 3
- Call 4 → DJ Mango 4

Ao mesmo tempo, esses bots podem atender outros servidores também.

> Importante: os workers são contas de bot separadas no Discord Developer Portal. Não existe uma forma suportada de uma única conta do Discord se "duplicar" dentro do mesmo servidor.

## Bot público

Para outras pessoas adicionarem o DJ Mango ao próprio servidor, configure o bot principal como público no Discord Developer Portal.

Os workers também precisam ser adicionados ao servidor quando aquele servidor quiser mais de uma call simultânea. O comando `/pool` mostra quantos estão instalados, quantos estão ocupados e gera links para adicionar os workers que faltam.

Se um servidor instalar apenas o bot principal, ele continua funcionando normalmente, com **1 call simultânea naquele servidor**.

## Comandos

| Comando | Função |
| --- | --- |
| `/play <busca-ou-link>` | Toca/busca música na call em que você está |
| `/pause` | Pausa a música da sua call |
| `/resume` | Continua a reprodução |
| `/skip` | Pula a faixa |
| `/queue` | Mostra a fila da sua call |
| `/nowplaying` | Mostra a música atual |
| `/volume <0-100>` | Ajusta o volume da sua call |
| `/stop` | Limpa a fila e tira o DJ da sua call |
| `/pool` | Mostra capacidade, calls ativas e workers disponíveis |
| `/musichelp` | Mostra os comandos |

## YouTube

Exemplos:

```text
/play Linkin Park Numb
/play Evidências Chitãozinho e Xororó
/play https://www.youtube.com/watch?v=...
```

O projeto usa `yt-dlp` e FFmpeg. O stream é obtido novamente quando cada música começa, evitando que URLs temporárias expirem enquanto ficam na fila.

## Deploy no Coolify

Use o repositório diretamente no Coolify e escolha o **Dockerfile** como método de build.

Este projeto é um worker persistente e **não precisa expor porta HTTP nem domínio**.

Configure nas variáveis de ambiente do Coolify:

```env
DISCORD_TOKEN=token_do_bot_principal
DISCORD_WORKER_TOKENS=token_worker_2,token_worker_3,token_worker_4
DEFAULT_VOLUME=50
IDLE_TIMEOUT=120
LOG_LEVEL=INFO
```

Para produção pública, deixe `DISCORD_GUILD_ID` vazio. Assim os comandos são registrados globalmente e ficam disponíveis nos servidores onde o bot principal for instalado.

Depois faça o deploy/redeploy. Nos logs você deve ver algo parecido com:

```text
Iniciando DJ Mango com 4 bot(s): 1 principal + 3 worker(s).
DJ Mango principal conectado...
Worker 1 conectado...
Worker 2 conectado...
Worker 3 conectado...
```

## Criando os workers

No Discord Developer Portal:

1. Crie o aplicativo principal, por exemplo **DJ Mango**.
2. Crie outros aplicativos para a capacidade extra, por exemplo **DJ Mango 2**, **DJ Mango 3**, **DJ Mango 4**.
3. Em cada aplicativo, crie o bot e copie o token.
4. Coloque o primeiro token em `DISCORD_TOKEN`.
5. Coloque os demais em `DISCORD_WORKER_TOKENS`.
6. Adicione o bot principal ao servidor.
7. Para múltiplas calls no mesmo servidor, adicione também os workers. O comando `/pool` fornece os links dos workers configurados que ainda não estão naquele servidor.

Os workers precisam somente das permissões de **Ver Canal**, **Conectar** e **Falar**. O bot principal precisa também enviar mensagens e embeds para responder aos comandos.

## Configuração local

Requisitos:

- Python 3.12+
- FFmpeg
- Deno 2.3+ recomendado pelo yt-dlp para o suporte atual do YouTube

Instalação:

```bash
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

O Dockerfile já instala FFmpeg, libopus e Deno.

## Variáveis

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| `DISCORD_TOKEN` | Sim | Token do DJ Mango principal |
| `DISCORD_WORKER_TOKENS` | Não | Tokens extras separados por vírgula, ponto e vírgula ou quebra de linha |
| `DISCORD_GUILD_ID` | Não | Somente para teste rápido em um servidor |
| `YTDLP_COOKIES_FILE` | Não | Arquivo Netscape de cookies do yt-dlp |
| `DEFAULT_VOLUME` | Não | Volume inicial; padrão 50 |
| `IDLE_TIMEOUT` | Não | Tempo para liberar a call após a fila acabar; padrão 120 s |
| `LOG_LEVEL` | Não | INFO, DEBUG etc. |

## Segurança

Nunca faça commit de token, `.env` ou cookies. Se algum token vazar, regenere-o imediatamente no Discord Developer Portal.
