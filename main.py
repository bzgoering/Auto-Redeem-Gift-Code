import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

import discord
from discord import app_commands

# ----------------------------------------------------------------------------
# CONFIG -- edit these
# ----------------------------------------------------------------------------

DISCORD_BOT_TOKEN = "{Token}"
ANNOUNCE_CHANNEL_ID = ###################  # channel where codes get posted
GUILD_ID = ################### # your server's ID, for instant slash-command sync
STATE_ID = ####  # fallback only: used when a player has no state of their own

# Regex used to pull a code out of a Discord announcement message.
# Adjust to match how codes are actually formatted/announced.
CODE_PATTERN = re.compile(r"\b[A-Za-z0-9]{6,12}\b")

REQUEST_DELAY_SECONDS = 1.5  # pause between players to stay under rate limits
# Store data files next to this script, not in whatever directory the bot
# happens to be launched from.
BASE_DIR = Path(__file__).resolve().parent
PLAYERS_FILE = BASE_DIR / "players.json"            # flat list of {fid, name}
RESULTS_FILE = BASE_DIR / "redeemed_results.json"   # code -> {fid: "claimed"}

# ----------------------------------------------------------------------------
# WOS API internals (reverse-engineered from the official redemption site)
# ----------------------------------------------------------------------------

API_URL = "https://wos-giftcode-api.centurygame.com/api"
SALT = "tB87#kPtkxqOS2"
HTTP_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Origin": "https://wos-giftcode.centurygame.com",
    "Referer": "https://wos-giftcode.centurygame.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

_session = requests.Session()
_retry = Retry(total=5, backoff_factor=1, status_forcelist=[429], allowed_methods=False)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _sign(params: dict) -> str:
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    return hashlib.md5((ordered + SALT).encode("utf-8")).hexdigest()


# Transient responses worth a short in-place retry before giving up on a player.
#   40004 TIMEOUT RETRY  -- server asked us to retry
#   40019 TOO FREQUENT   -- per-FID rate limit, needs a real cooldown
_RETRY_ERR_CODES = {40004, 40019}
_MAX_REDEEM_ATTEMPTS = 4


def _redeem_request(fid: str, code: str, kid) -> dict:
    """One signed redemption POST.

    The gift-code API was rebuilt in 2026: there's no longer a separate /player
    login or a CAPTCHA step. Redemption is a single call that also carries the
    player's State/Kingdom (`kid`) and a *second*-based timestamp.
    """
    data = {
        "fid": fid,
        "cdk": code,
        "kid": str(kid),
        "time": str(int(time.time())),
    }
    data["sign"] = _sign(data)
    resp = _session.post(f"{API_URL}/gift_code", data=data, headers=HTTP_HEADERS, timeout=30)
    return resp.json()


def _redeem(fid: str, code: str, kid) -> dict:
    """Redeems `code` for `fid` in state `kid`, retrying the transient
    rate-limit / server-retry responses a few times before giving up."""
    last = {}
    for _ in range(_MAX_REDEEM_ATTEMPTS):
        last = _redeem_request(fid, code, kid)
        if last.get("err_code") in _RETRY_ERR_CODES:
            # 40019 needs a genuine cooldown; 40004 just wants another beat.
            time.sleep(60 if last.get("err_code") == 40019 else 2)
            continue
        return last
    return last


def _validate_fid(fid: str, kid) -> bool:
    """True if `fid` is a real player in state `kid`.

    There's no login endpoint anymore, so we probe the redeem endpoint with a
    throwaway code. The API validates player+state *before* the code, so a real
    player in the right state returns 40014 (CDK NOT FOUND) while a bad FID or
    wrong state returns 40020 (USER INFO ERROR). Nothing is claimed either way.
    """
    resp = _redeem_request(fid, "ZZINVALIDCHECK00", kid)
    return resp.get("err_code") == 40014


# ----------------------------------------------------------------------------
# Storage helpers
# ----------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_players() -> list:
    """Loads players.json as a flat list of {"fid", "name", "state"} dicts.

    Each player carries their own state/kingdom, since an alliance's members
    aren't all in one state. Entries saved before `state` existed fall back to
    STATE_ID.

    FIDs are a single shared list, not tied to any Discord user. Older formats
    are migrated/flattened on load, de-duplicated by fid:
      - dict keyed by Discord user id -> list of entries (previous version)
      - a single entry stored as a bare dict
      - bare FID strings
    """
    raw = _load_json(PLAYERS_FILE)
    # Old format: dict keyed by user id. New format: already a flat list.
    groups = raw.values() if isinstance(raw, dict) else [raw]

    players, seen = [], set()
    for group in groups:
        if isinstance(group, dict):  # legacy: single entry stored as a dict
            group = [group]
        for entry in group:
            if isinstance(entry, str):  # legacy: bare FID string
                entry = {"fid": entry, "name": entry}
            fid = entry["fid"]
            if fid in seen:  # same FID registered twice -- keep one
                continue
            seen.add(fid)
            players.append({
                "fid": fid,
                "name": entry.get("name", fid),
                "state": entry.get("state", STATE_ID),
            })
    return players


def _registered_players() -> list:
    """Returns [{"id": fid, "name": display_name, "state": kid}, ...] for every
    registered FID."""
    return [
        {"id": e["fid"], "name": e["name"], "state": e["state"]}
        for e in _load_players()
    ]


def _claim_status(entry) -> str:
    """Reads the claim status from a results-file entry, tolerating both the
    legacy bare-string form ("claimed") and the current {"name", "status"} form."""
    if isinstance(entry, dict):
        return entry.get("status", "")
    return entry or ""


# ----------------------------------------------------------------------------
# Redemption logic
# ----------------------------------------------------------------------------

def redeem_code_for_all(code: str, players: list = None) -> str:
    players = players if players is not None else _registered_players()
    if not players:
        return "No players are registered yet -- have everyone run /register first."

    results = _load_json(RESULTS_FILE)
    results.setdefault(code, {})

    redeemed, claimed, invalid, errors = 0, 0, 0, 0
    invalid_lines, error_lines = [], []

    for player in players:
        fid, name = player["id"], player["name"]
        state = player.get("state", STATE_ID)

        if _claim_status(results[code].get(fid)) == "claimed":
            claimed += 1
            continue

        response = _redeem(fid, code, state)
        msg = str(response.get("msg", "")).strip().rstrip(".").upper()
        err_code = response.get("err_code")

        # Code-level problems apply to everyone -- stop the whole run.
        if err_code == 40014:
            print(f"[redeem] {code}: code does not exist -- aborted")
            return "The gift code doesn't exist."
        if err_code == 40007:
            print(f"[redeem] {code}: code expired -- aborted")
            return "The gift code has expired."
        if err_code == 40005:
            print(f"[redeem] {code}: claim limit reached -- aborted")
            return "The gift code's claim limit has been reached."

        if msg == "SUCCESS" or err_code == 40011:  # redeemed (or same-type exchange)
            redeemed += 1
            results[code][fid] = {"name": name, "status": "claimed"}
        elif err_code == 40008:  # already received
            claimed += 1
            results[code][fid] = {"name": name, "status": "claimed"}
        elif err_code == 40020:  # FID not in the state we sent
            invalid += 1
            invalid_lines.append(
                f"{name} ({fid}): not found in state {state} -- re-check the FID/state"
            )
        elif err_code == 40001:  # role does not exist
            invalid += 1
            invalid_lines.append(f"{name} ({fid}): no such player")
        elif err_code in (40006, 40017, 40018):  # eligibility requirements not met
            reasons = {
                40006: "furnace level too low",
                40017: "not enough spending",
                40018: "VIP level too low",
            }
            invalid += 1
            invalid_lines.append(f"{name} ({fid}): {reasons[err_code]} for this code")
        elif err_code in _RETRY_ERR_CODES:  # still rate-limited after retries
            errors += 1
            error_lines.append(f"{name} ({fid}): rate limited -- try again later")
        else:
            errors += 1
            error_lines.append(f"{name} ({fid}): {response.get('msg', response)}")

        time.sleep(REQUEST_DELAY_SECONDS)

    _save_json(RESULTS_FILE, results)

    print(
        f"[redeem] {code}: redeemed={redeemed} claimed={claimed} "
        f"invalid={invalid} errors={errors}"
    )

    summary = (
        f"Code `{code}`: {redeemed} redeemed, {claimed} already claimed, "
        f"{invalid} invalid, {errors} errors out of {len(players)} registered players."
    )
    if invalid_lines:
        summary += "\n**Invalid (code is valid, character couldn't redeem):**\n" + "\n".join(invalid_lines)
    if error_lines:
        summary += "\n**Errors (request failed):**\n" + "\n".join(error_lines)
    return summary


# ----------------------------------------------------------------------------
# Discord bot
# ----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True


class GiftCodeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


client = GiftCodeBot()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}. Watching channel {ANNOUNCE_CHANNEL_ID}.")


def _clean_fid(raw: str) -> str:
    """Strips a pasted FID down to just its digits. Copying from the WOS app
    injects hidden characters (zero-width spaces, non-breaking spaces, RTL
    marks, etc.); since FIDs are purely numeric, keeping only 0-9 removes all
    of that no matter where it sits in the string."""
    return re.sub(r"[^0-9]", "", raw)


@client.tree.command(name="register", description="Register a Whiteout Survival FID for auto gift-code redemption (you can register more than one)")
@app_commands.describe(
    fid="The in-game FID to add (tap the avatar in-game to find it)",
    name="Display name to show for this player (the API no longer exposes in-game names)",
    state=f"The player's state/kingdom number (defaults to {STATE_ID})",
)
async def register(interaction: discord.Interaction, fid: str, name: str, state: int = STATE_ID):
    await interaction.response.defer(ephemeral=True)

    fid = _clean_fid(fid)
    if not fid:
        await interaction.followup.send(
            "That doesn't look like an FID -- it should be a number. Try again.", ephemeral=True
        )
        return

    display_name = name.strip()
    if not display_name:
        await interaction.followup.send(
            "Please provide a name for this player.", ephemeral=True
        )
        return

    # No login endpoint anymore -- validate the FID against their state instead.
    if not await asyncio.to_thread(_validate_fid, fid, state):
        await interaction.followup.send(
            f"Couldn't validate FID `{fid}` in state {state} -- double check the FID "
            f"(and that the player is in state {state}) and try again.",
            ephemeral=True,
        )
        return

    players = _load_players()

    existing = next((e for e in players if e["fid"] == fid), None)
    if existing:
        # Already registered: treat a new name and/or state as an update.
        changes = []
        if existing["name"] != display_name:
            changes.append(f"renamed **{existing['name']}** -> **{display_name}**")
            existing["name"] = display_name
        if existing["state"] != state:
            changes.append(f"moved state {existing['state']} -> {state}")
            existing["state"] = state

        if changes:
            _save_json(PLAYERS_FILE, players)
            print(f"[register] {interaction.user} updated FID {fid}: {'; '.join(changes)}")
            await interaction.followup.send(
                f"FID `{fid}` was already registered -- " + ", ".join(changes) + ".",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"FID `{fid}` (**{existing['name']}**, state {existing['state']}) "
                "is already registered.",
                ephemeral=True,
            )
        return

    players.append({"fid": fid, "name": display_name, "state": state})
    _save_json(PLAYERS_FILE, players)

    print(f"[register] {interaction.user} registered FID {fid} ({display_name}) in state {state}")

    await interaction.followup.send(
        f"Added! **{display_name}** (FID `{fid}`, state {state}) will now get gift codes "
        f"auto-redeemed. {len(players)} FID(s) registered in total.",
        ephemeral=True,
    )


@client.tree.command(name="unregister", description="[Admin] Remove an FID from gift-code redemption")
@app_commands.describe(fid="The FID to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def unregister(interaction: discord.Interaction, fid: str):
    fid = _clean_fid(fid)
    players = _load_players()

    remaining = [e for e in players if e["fid"] != fid]
    if len(remaining) == len(players):
        await interaction.response.send_message(f"FID `{fid}` isn't registered.", ephemeral=True)
        return

    _save_json(PLAYERS_FILE, remaining)
    print(f"[unregister] {interaction.user} removed FID {fid}")
    await interaction.response.send_message(f"Removed FID `{fid}`.", ephemeral=True)


@client.tree.command(name="list", description="List all FIDs registered for gift-code redemption")
async def list_fids(interaction: discord.Interaction):
    players = _load_players()
    if players:
        lines = "\n".join(
            f"- **{e['name']}** (FID `{e['fid']}`, state {e['state']})" for e in players
        )
        await interaction.response.send_message(
            f"Registered FIDs ({len(players)}):\n{lines}", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "No FIDs are registered yet -- use /register to add one.", ephemeral=True
        )


@client.tree.command(name="redeem", description="[Admin] Manually redeem a gift code for all registered players")
@app_commands.describe(code="The gift code to redeem")
@app_commands.checks.has_permissions(manage_guild=True)
async def redeem(interaction: discord.Interaction, code: str):
    # Defer immediately: it's the lightest-weight ack (lands inside Discord's
    # 3-second window) and buys 15 minutes to post the result, which the slow
    # redemption loop below needs. If the token is already stale (10062), log
    # and bail instead of crashing the command.
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        print(f"/redeem: interaction expired before defer (code={code}); skipping.")
        return

    summary = await asyncio.to_thread(redeem_code_for_all, code)
    for chunk in _split_for_discord(summary):
        await interaction.followup.send(chunk)


def _split_for_discord(text: str, limit: int = 2000) -> list:
    """Splits `text` into chunks that each fit under Discord's per-message
    character limit. Prefers to break on newlines so summary lines stay intact;
    only hard-splits a single line if it alone exceeds the limit."""
    chunks, current = [], ""
    for line in text.split("\n"):
        # A single line longer than the limit: emit it in raw slices.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        # +1 accounts for the "\n" we'd re-insert between lines.
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]


def _message_text(message: discord.Message) -> str:
    """All searchable text in a message: plain content plus every embed's
    title/description/fields/footer/author. Gift-code announcements usually
    arrive as an embed from another bot, where message.content is empty."""
    parts = [message.content or ""]
    for emb in message.embeds:
        parts += [emb.title or "", emb.description or ""]
        if emb.footer and emb.footer.text:
            parts.append(emb.footer.text)
        if emb.author and emb.author.name:
            parts.append(emb.author.name)
        for field in emb.fields:
            parts += [field.name or "", field.value or ""]
    return "\n".join(parts)


def _find_code(text: str):
    """Pulls a likely gift code out of text. Embeds are full of ordinary words
    that also match CODE_PATTERN, so prefer a token mixing letters AND digits
    (how WOS codes look). Returns None if nothing code-like is found."""
    candidates = CODE_PATTERN.findall(text)
    for c in candidates:
        if any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c):
            return c
    return None


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # Auto-detect codes posted in the announcement channel (codes often arrive
    # as an embed from another bot, so scan embeds too -- not just content).
    if message.channel.id != ANNOUNCE_CHANNEL_ID:
        return

    code = _find_code(_message_text(message))
    if not code:
        print(f"[auto] message in announce channel from {message.author}, no code found")
        return

    print(f"[auto] detected code {code} from {message.author}")
    await message.channel.send(f"🎁 Auto-redeemed for: `{code}`")
    summary = await asyncio.to_thread(redeem_code_for_all, code)
    for chunk in _split_for_discord(summary):
        await message.channel.send(chunk)


if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN)
