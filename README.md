# DJ Mango 🥭🎵

Bot público de música para Discord com reprodução do **YouTube por link ou busca de texto**, preparado para rodar no Coolify e crescer para muitos servidores com a mesma aplicação/token.

## Arquitetura correta para vários servidores

O DJ Mango usa **uma única conta de bot pública**.

Isso significa:

- 1 token do Discord.
- 1 aplicação no Discord Developer Portal.
- Nenhum `guild_id` precisa ser cadastrado.
- Slash commands globais.
- Uma fila independente por servidor.
- Uma conexão de voz independente por servidor.
- Sharding automático conforme a quantidade de servidores crescer.

O mesmo DJ Mango pode estar, por exemplo, em:

- Servidor A → tocando na call Geral.
- Servidor B → tocando na call Música.
- Servidor C → tocando na call Rust.

Tudo ao mesmo tempo, com filas completamente independentes.

## Limitação do Discord dentro do mesmo servidor

Uma única conta de bot só pode manter **uma conexão de voz por servidor**.

Então, se o DJ Mango já estiver na call `Geral` do Servidor A, ele não consegue ficar simultaneamente também na call `VIP` do mesmo Servidor A usando a mesma identidade.

Isso não impede o bot de tocar simultaneamente em milhares de servidores diferentes.

## Comandos globais

| Comando | Função |
| --- | --- |
| `/play <busca-ou-link>` | Busca no YouTube ou toca um link |
| `/pause` | Pausa a música atual |
| `/resume` | Continua a reprodução |
| `/skip` | Pula a faixa atual |
| `/queue` | Mostra a fila |
| `/nowplaying` | Mostra a música atual |
| `/volume <0-100>` | Ajusta o volume |
| `/stop` | Limpa a fila e sai da call |
| `/status` | Mostra servidores, shards e sessões de voz |
| `/musichelp` | Mostra os comandos |

Os comandos são sincronizados globalmente e ficam disponíveis em todos os servidores que adicionarem o bot.

## Exemplos

```text
/play Linkin Park Numb
/play Evidências Chitãozinho e Xororó
/play https://www.youtube.com/watch?v=...
```

## Deploy no Coolify

Use o repositório diretamente no Coolify e selecione o **Dockerfile** como método de build.

Este bot é um processo persistente e **não precisa expor porta HTTP nem usar domínio**.

Cadastre somente as variáveis necessárias:

```env
DISCORD_TOKEN=SEU_TOKEN
DEFAULT_VOLUME=50
IDLE_TIMEOUT=120
LOG_LEVEL=INFO
```

Opcionalmente:

```env
YTDLP_COOKIES_FILE=/caminho/para/cookies.txt
```

Depois faça o deploy.

Nos logs você deve ver algo parecido com:

```text
DJ Mango online como DJ Mango (...) | guilds=3 | shards=1
```

Conforme o bot crescer, o `AutoShardedBot` distribui os servidores entre shards do Gateway.

## Bot público

No Discord Developer Portal:

1. Abra a aplicação do DJ Mango.
2. Configure o bot como público.
3. Em OAuth2 / Installation, habilite instalação em servidores.
4. Inclua os escopos de bot e comandos da aplicação.
5. Garanta as permissões:
   - Ver Canal
   - Conectar
   - Falar
   - Enviar Mensagens
   - Inserir Links

O mesmo link de instalação serve para todos os servidores. Você não precisa cadastrar IDs de servidores manualmente.

## Requisitos

- Python 3.12+
- FFmpeg
- Deno 2.3+ recomendado pelo yt-dlp
- `discord.py[voice]`
- `yt-dlp[default]`

## YouTube

O projeto usa `yt-dlp` para pesquisar no YouTube e resolver os streams de áudio. O stream é obtido novamente quando cada música começa para evitar URLs temporárias expiradas.

Alguns vídeos podem exigir autenticação. Nesse caso, configure `YTDLP_COOKIES_FILE` com cookies em formato Netscape.

## Escala

Para poucos ou muitos servidores, continua sendo a mesma aplicação/token.

Quando o bot crescer bastante, o Discord exige sharding. O código já usa `commands.AutoShardedBot`, então a base já está preparada para isso.

## Segurança

Nunca envie para o GitHub:

- token do Discord;
- arquivo `.env`;
- cookies do YouTube.

Se um token vazar, regenere-o no Discord Developer Portal.
