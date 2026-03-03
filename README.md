# QuestLogBot-Fluxer

A free, open source gaming community bot for [Fluxer](https://fluxer.app), built for the [QuestLog](https://casual-heroes.com/ql/) platform.

Fluxer uses the same gateway protocol as Discord, so this bot speaks native Fluxer with no library dependency.

## Features

- **XP System** - Earn XP for activity, track leaderboards
- **LFG (Looking for Group)** - Post and find group requests, synced with the QuestLog web LFG board
- **Moderation** - Ban, temp-ban (native Fluxer temp bans), kick, timeout
- **QuestLog Webhooks** - Activity feeds from QuestLog into your Fluxer channels (new posts, LFG, giveaways, new members)

## Commands

| Command | Description |
|---|---|
| `!help` | Show available commands |
| `!ping` | Check bot latency |
| `!xp [@user]` | Show your XP or another user's |
| `!leaderboard` | Top 10 XP earners in the server |
| `!lfg <game> [description]` | Post a Looking for Group request |
| `!lfglist` | Show active LFG posts |
| `!lfgjoin <id>` | Express interest in an LFG post |
| `!ban <@user> [reason]` | Ban a user |
| `!tempban <@user> <seconds> [reason]` | Temp-ban a user (Fluxer native) |
| `!kick <@user> [reason]` | Kick a user |
| `!timeout <@user> <seconds> [reason]` | Timeout a user |

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your values
3. Install dependencies: `pip install -r requirements.txt`
4. Run: `python bot.py`

On the Casual Heroes server, secrets live in `/etc/casual-heroes/warden.env` (shared with wardenbot).

## Architecture

Built with `aiohttp` - no discord.py dependency, full control over the Fluxer gateway and REST URLs.

```
bot.py              - Entry point
config.py           - Config, DB connection (shared with wardenbot)
client.py           - FluxerClient: gateway WebSocket + REST
cogs/
  core.py           - !help, !ping, !info
  xp.py             - XP tracking
  moderation.py     - Ban, kick, timeout
  lfg.py            - Looking for Group
webhooks/
  sender.py         - QuestLog -> Fluxer activity webhooks
```

Shares the `warden` MySQL database with [wardenbot](https://github.com/CasualHeroes/QuestLogBot) (the Discord bot) and the [QuestLog web platform](https://github.com/CasualHeroes/QuestLog).

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

You can use, modify, and self-host this bot freely. If you run a modified version as a network service, you must release your modifications under the same license. You may not sell or relicense this software under proprietary terms.

See [LICENSE](LICENSE) for the full license text.

## Links

- QuestLog platform: https://casual-heroes.com/ql/
- LFG board: https://casual-heroes.com/ql/lfg/
- Fluxer: https://fluxer.app
