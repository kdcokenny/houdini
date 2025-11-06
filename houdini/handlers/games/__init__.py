import random
import time

from sqlalchemy.dialects.postgresql import insert

from houdini import handlers
from houdini.constants import ClientType
from houdini.converters import OptionalConverter
from houdini.data.game import PenguinGameData
from houdini.data.room import Room
from houdini.handlers import XTPacket
from houdini.handlers.play.moderation import cheat_ban
from houdini.handlers.play.navigation import handle_join_room

default_score_games = {904, 905, 906, 912, 916, 917, 918, 919, 950, 952}

# Maximum allowed scores per game room to prevent score manipulation
# Format: room_id: (max_score, coins_per_score_unit)
max_scores_per_game = {
    903: 10000,   # Hydro Hopper (10000 score = 1000 coins max)
    904: 1000,    # Cart Surfer (full score payout)
    905: 1000,    # Catchin' Waves (full score payout)
    906: 250,     # Ice Fishing (full score payout)
    912: 800,     # Aqua Grabber (full score payout)
    916: 1000,    # Jet Pack Adventure
    917: 1000,    # Puffle Rescue
    918: 800,     # Smoothie Smash
    919: 800,     # Puffle Round Up
    950: 800,     # Dance Contest
    952: 800,     # Pufflescape
}


def determine_coins_earned(p, score):
    return score if p.room.id in default_score_games else score // 10


def validate_game_score(p, score):
    """
    Validate that score is within reasonable limits for the game.
    Returns (is_valid, capped_score, reason)
    """
    max_score = max_scores_per_game.get(p.room.id, 15000)  # Default max for unlisted games

    if score < 0:
        return False, 0, "Negative score"

    if score > max_score:
        return False, max_score, f"Score exceeds maximum {max_score}"

    return True, score, "Valid"


async def validate_game_session(p):
    """
    Validate that player has been in game room for minimum time.
    Prevents instant score submission exploits.
    Returns (is_valid, time_in_room, reason)
    """
    game_session_key = f'{p.id}.game_session.{p.room.id}'
    session_start = await p.server.redis.get(game_session_key)

    if session_start is None:
        return False, 0, "No game session found"

    time_in_room = time.time() - float(session_start)

    # Minimum 15 seconds in room before accepting score
    # Adjust based on fastest legitimate game completion times
    min_game_time = 15

    if time_in_room < min_game_time:
        return False, time_in_room, f"Game completed too quickly ({time_in_room:.1f}s < {min_game_time}s)"

    return True, time_in_room, "Valid"


async def validate_game_rate_limit(p):
    """
    Validate that player hasn't exceeded maximum games per hour.
    Prevents rapid game cycling exploits.
    Returns (is_valid, games_played, reason)
    """
    rate_limit_key = f'{p.id}.game_rate_limit'
    games_played = await p.server.redis.get(rate_limit_key)

    if games_played is None:
        games_played = 0
    else:
        games_played = int(games_played)

    # Maximum 60 games per hour (1 per minute average)
    # Allows bursts but prevents sustained abuse
    max_games_per_hour = 60

    if games_played >= max_games_per_hour:
        return False, games_played, f"Too many games in last hour ({games_played}/{max_games_per_hour})"

    # Increment counter
    await p.server.redis.incr(rate_limit_key)

    # Set expiry to 1 hour if this is the first game
    if games_played == 0:
        await p.server.redis.expire(rate_limit_key, 3600)

    return True, games_played + 1, "Valid"


async def determine_coins_overdose(p, coins):
    overdose_key = f'{p.id}.overdose'
    last_overdose = await p.server.redis.get(overdose_key)

    if last_overdose is None:
        # First game - set initial timestamp and allow
        await p.server.redis.set(overdose_key, time.time())
        return False

    minutes_since_last_dose = ((time.time() - float(last_overdose)) // 60) + 1
    max_game_coins = p.server.config.max_coins_per_min * minutes_since_last_dose

    if coins > max_game_coins:
        return True

    # Update timestamp but don't delete - accumulate over time
    await p.server.redis.set(overdose_key, time.time())
    return False


@handlers.handler(XTPacket('j', 'jr'), before=handle_join_room)
async def handle_overdose_key(p, room: Room):
    # Only set timestamp when joining a game room, never delete
    # This prevents overdose bypass via rapid room cycling
    if room.game:
        overdose_key = f'{p.id}.overdose'
        # Only set if not already set (preserve existing timestamp)
        if not await p.server.redis.exists(overdose_key):
            await p.server.redis.set(overdose_key, time.time())

        # Track game session start time for minimum play time validation
        game_session_key = f'{p.id}.game_session.{room.id}'
        await p.server.redis.set(game_session_key, time.time())
        await p.server.redis.expire(game_session_key, 300)  # 5 minute expiry


@handlers.disconnected
@handlers.player_attribute(joined_world=True)
async def disconnect_overdose_key(p):
    # Set expiry on disconnect instead of deleting
    # This prevents disconnect/reconnect bypass while allowing eventual cleanup
    if p.room is not None and p.room.game:
        overdose_key = f'{p.id}.overdose'
        # Keep the key for 1 hour after disconnect
        await p.server.redis.expire(overdose_key, 3600)


async def game_over_cooling(p):
    await p.send_xt('zo', p.coins, '', 0, 0, 0)


@handlers.handler(XTPacket('m', ext='z'))
@handlers.player_in_room(802)
async def handle_send_move_puck(p, _, x: int, y: int, speed_x: int, speed_y: int):
    p.server.puck = (x, y)
    await p.room.send_xt('zm', p.id, x, y, speed_x, speed_y)


@handlers.handler(XTPacket('gz', ext='z'))
@handlers.player_in_room(802)
async def handle_get_puck(p):
    await p.send_xt('gz', *p.server.puck)


@handlers.handler(XTPacket('zo', ext='z'))
@handlers.cooldown(10, callback=game_over_cooling)
async def handle_get_game_over(p, score: int):
    # If the room is Card Jitsu Snow, it this should do nothing
    if p.room.id == 996:
        return

    # card-jitsus except snow have special handling
    card_jitsu_rooms = [995, 998, 997]
    is_card_jitsu = p.room.id in card_jitsu_rooms

    # Waddle minigames don't normally need the end screen
    if p.waddle and not is_card_jitsu:
        return

    if p.room.game and not p.table:
        # Validate game session timing
        session_valid, time_in_room, session_reason = await validate_game_session(p)
        if not session_valid:
            return await cheat_ban(p, p.id, comment=f"Game session invalid: {session_reason}")

        # Validate global rate limit
        rate_valid, games_count, rate_reason = await validate_game_rate_limit(p)
        if not rate_valid:
            return await cheat_ban(p, p.id, comment=f"Game rate limit exceeded: {rate_reason}")

        # Validate score before processing
        is_valid, validated_score, reason = validate_game_score(p, score)
        if not is_valid:
            return await cheat_ban(p, p.id, comment=f"Invalid game score: {reason}")

        coins_earned = determine_coins_earned(p, validated_score)

        if not is_card_jitsu:
            if await determine_coins_overdose(p, coins_earned):
                return await cheat_ban(p, p.id, comment="Coins overdose")

        stamp_info = "", 0, 0, 0

        if p.room.stamp_group:
            stamp_info = await p.get_game_end_stamps_info(True)
            # has all stamps in game
            if stamp_info[1] == stamp_info[2]:
                coins_earned *= 2

        if not is_card_jitsu:
            await p.update(
                coins=min(p.coins + coins_earned, p.server.config.max_coins)
            ).apply()
        await p.send_xt("zo", p.coins, *stamp_info)


@handlers.handler(XTPacket('ggd', ext='z'), client=ClientType.Vanilla)
async def handle_get_game_data(p, index: int = 0):
    game_data = await PenguinGameData.select('data').where((PenguinGameData.penguin_id == p.id) &
                                                           (PenguinGameData.room_id == p.room.id) &
                                                           (PenguinGameData.index == index)).gino.scalar()
    await p.send_xt('ggd', game_data or '')


@handlers.handler(XTPacket('sgd', ext='z'), client=ClientType.Vanilla)
@handlers.cooldown(5)
async def handle_set_game_data(p, index: OptionalConverter(int) = 0, *, game_data: str):
    if p.room.game:
        data_insert = insert(PenguinGameData).values(penguin_id=p.id, room_id=p.room.id, index=index, data=game_data)
        data_insert = data_insert.on_conflict_do_update(
            constraint='penguin_game_data_pkey',
            set_=dict(data=game_data),
            where=((PenguinGameData.penguin_id == p.id)
                   & (PenguinGameData.room_id == p.room.id)
                   & (PenguinGameData.index == index))
        )

        await data_insert.gino.scalar()


@handlers.handler(XTPacket('zr', ext='z'), client=ClientType.Vanilla)
@handlers.player_attribute(agent_status=True)
async def handle_get_game_again(p):
    games = list(range(1, 11))

    games_string = f'{games.pop(random.randrange(len(games)))},' \
                   f'{games.pop(random.randrange(len(games)))},' \
                   f'{games.pop(random.randrange(len(games)))}'
    await p.send_xt('zr', games_string, random.randint(1, 6))


@handlers.handler(XTPacket('zc', ext='z'), client=ClientType.Vanilla)
@handlers.player_attribute(agent_status=True)
@handlers.cooldown(5)
async def handle_game_complete(p, medals: int):
    medals = min(6, medals)
    await p.update(career_medals=p.career_medals + medals,
                   agent_medals=p.agent_medals + medals).apply()


@handlers.disconnected
@handlers.player_attribute(joined_world=True)
async def clear_stamp_sessions(p):
    """When disconnected, clear stamps in case any were obtained and not properly handled"""
    await p.clear_stamps_session()
