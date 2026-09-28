# DJ Mango 🥭🎵

Bot público de música para Discord com reprodução do YouTube por link ou busca, painel clicável, autoplay inteligente e sharding automático.

## Principais recursos

- Um único bot público para vários servidores.
- Slash commands globais.
- Fila independente por servidor.
- Reprodução simultânea em servidores diferentes.
- Painel clicável dentro do Discord.
- Autoplay por **mesmo estilo** ou **mesmo artista**.
- Pré-carregamento da próxima música antes da atual terminar.
- Cache temporário apagado automaticamente para não ocupar disco.
- Auto sharding para escalar o bot.

## Comandos

| Comando | Função |
| --- | --- |
| `/play <busca-ou-link>` | Busca no YouTube ou toca um link |
| `/pause` | Pausa |
| `/resume` | Continua |
| `/skip` | Pula |
| `/queue` | Mostra a fila |
| `/nowplaying` | Música atual |
| `/volume <0-100>` | Ajusta volume |
| `/stop` | Limpa fila e sai |
| `/panel` | Abre o painel clicável |
| `/status` | Mostra status global |
| `/musichelp` | Ajuda |

## Painel

O painel aparece automaticamente quando começa a reprodução e também pode ser aberto com:

```text
/panel
```

Ele possui controles para:

- pausar/continuar;
- pular;
- parar;
- embaralhar;
- volume -10/+10;
- autoplay desligado;
- autoplay **Mesmo estilo**;
- autoplay **Mesmo artista**.

A fila mostra um ⚡ quando a próxima música já foi pré-carregada.

## Autoplay e pré-carregamento

Quando o autoplay está ligado e a música atual está chegando ao fim, o DJ Mango tenta preparar a próxima antes da faixa terminar.

O modo **Mesmo estilo** usa recomendações relacionadas à música atual. O modo **Mesmo artista** prioriza músicas do mesmo canal/artista.

Por padrão, o pré-carregamento começa aproximadamente **35 segundos antes do fim**:

```env
AUTOPLAY_PREFETCH_SECONDS=35
```

Se já houver uma música manualmente adicionada à fila, ela tem prioridade sobre o autoplay e também pode ser pré-carregada.

## Cache e espaço em disco

As músicas pré-carregadas ficam temporariamente em:

```text
/tmp/dj-mango-cache
```

O comportamento é:

1. a próxima música é baixada temporariamente;
2. ela toca pelo arquivo local quando chegar a vez;
3. assim que termina, o arquivo é apagado;
4. ao usar `/stop`, o cache daquele servidor é apagado;
5. ao reiniciar o bot, qualquer sobra antiga do cache é limpa.

Isso evita acumular músicas no armazenamento do dedicado.

O caminho pode ser alterado:

```env
MUSIC_CACHE_DIR=/tmp/dj-mango-cache
```

## Deploy no Coolify

Use o Dockerfile do repositório.

O bot não precisa de domínio ou porta HTTP.

Variáveis recomendadas:

```env
DISCORD_TOKEN=SEU_TOKEN
DEFAULT_VOLUME=50
IDLE_TIMEOUT=120
AUTOPLAY_PREFETCH_SECONDS=35
MUSIC_CACHE_DIR=/tmp/dj-mango-cache
LOG_LEVEL=INFO
```

Opcionalmente:

```env
YTDLP_COOKIES_FILE=/caminho/para/cookies.txt
```

Depois faça redeploy.

## Escala

O DJ Mango usa `commands.AutoShardedBot`. A mesma aplicação/token atende todos os servidores.

Cada servidor mantém sua própria fila e conexão de voz.

A limitação do Discord continua sendo uma conexão de voz por conta de bot dentro do mesmo servidor.

## Segurança

Nunca envie para o GitHub:

- token do Discord;
- `.env`;
- cookies do YouTube.

Se o token vazar, regenere-o no Discord Developer Portal.
