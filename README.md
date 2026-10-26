# Auto Redeem Gift Code

A Discord bot that redeems **Whiteout Survival** gift codes for every registered player at once. It watches an announcement channel for new codes and redeems them automatically, or an admin can trigger a redeem by hand.

## Tech
- Python 3.10+
- [discord.py](https://pypi.org/project/discord.py/) 2.x
- requests

## How to run
1. Install the dependencies:
   ```bash
   pip install discord.py requests
   ```
2. Open `main.py` and fill in the config at the top:
   - `DISCORD_BOT_TOKEN`: your bot's token
   - `ANNOUNCE_CHANNEL_ID`: the channel where gift codes get posted
   - `GUILD_ID`: your server's ID
   - `STATE_ID`: the default state/kingdom for players
3. Start the bot:
   ```bash
   python main.py
   ```

Player data is saved to `players.json` and `redeemed_results.json`, next to the script.

## Commands
| Command | Who | What it does |
|---|---|---|
| `/register fid name [state]` | Anyone | Adds a player (or updates their name/state) |
| `/list` | Anyone | Shows every registered player |
| `/unregister fid` | Admin | Removes a player |
| `/redeem code` | Admin | Redeems a code for all registered players |

If someone posts a code in the announcement channel, the bot redeems it automatically and posts a summary.
