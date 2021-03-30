import discord
from discord.ext import commands
import motor.motor_asyncio
import names
from fuzzywuzzy import process as fwproc

import os
import typing
import enum
import random
import datetime
import hashlib
import traceback

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.typing = False
intents.presences = False
intents.voice_states = True

bot = commands.Bot(command_prefix=os.getenv('BOT_COMMAND_PREFIX') or '>', intents=intents)

client = motor.motor_asyncio.AsyncIOMotorClient('mongodb://mongo:27017/')
db = client.db
rules = db.rules

def generate_name_indexes(attempts = 50):
    for _ in range(attempts):
        left_ind = random.randint(0, len(names.LEFT)-1)
        center_ind = random.randint(0, len(names.CENTER)-1)
        right_ind = random.randint(0, len(names.RIGHT)-1)
        indexes = [left_ind, center_ind, right_ind]
        # TODO: check if indexes are already in db
        return indexes

def name_indexes_to_words(indexes):
    outp = []
    for ind, wordlist in zip(indexes, names.LISTS):
        outp.append(wordlist[ind])
    return '-'.join(outp)

def parse_name_indexes(left, center, right):
    indexes = []
    exact = True
    for part, wordlist in zip([left, center, right], names.LISTS):
        found, match = fwproc.extractOne(part, wordlist)
        index = wordlist.index(found)
        if match != 100: exact = False
        indexes.append(index)
    return indexes, exact



class UserlikeType(enum.Enum):
    MEMBER = 'member'
    ROLE = 'role'

class Userlike:
    __slots__ = ['type', 'id']
    def __init__(self, type, id):
        type = UserlikeType(type)
        self.type = type
        self.id = id

    @classmethod
    def from_discord_model(cls, model):
        if isinstance(model, discord.Member): return cls(UserlikeType.MEMBER, model.id)
        if isinstance(model, discord.Role): return cls(UserlikeType.ROLE, model.id)
        raise TypeError('unknown type: ', type(model))
    
    async def as_discord_model(self, guild):
        if self.type == UserlikeType.MEMBER:
            return guild.get_member(self.id) or await guild.fetch_member(self.id)
        elif self.type == UserlikeType.ROLE:
            return guild.get_role(self.id) or await guild.fetch_role(self.id)
        else:
            raise TypeError('Unexpected type of self:', self.type)

    def as_mention(self):
        if self.type == UserlikeType.MEMBER:
            return '<@'+str(self.id)+'>'
        else:
            return '<@&'+str(self.id)+'>'

class Action(enum.Enum):
    JOINS = 'joins'
    LEAVES = 'leaves'
    MUTED = 'muted'
    UNMUTED = 'unmuted'
    DEAFENED = 'deafened'
    UNDEAFENED = 'undeafened'
    STREAMING = 'streaming'
    UNSTREAMING = 'unstreaming'

ACTION_MESSAGES = {
    Action.JOINS: '{user} joined channel {channel}',
    Action.LEAVES: '{user} left channel {channel}',
    Action.MUTED: '{user} became muted in {channel}',
    Action.UNMUTED: '{user} stopped being muted in {channel}',
    Action.DEAFENED: '{user} became deafened in {channel}',
    Action.UNDEAFENED: '{user} stopped being deafened in {channel}',
    Action.STREAMING: '{user} started a stream  or enabled video in {channel}',
    Action.UNSTREAMING: '{user} stopped a stream or disabled video in {channel}',
}

ACTION_VERBS = {
    Action.JOINS: 'joins the channel',
    Action.LEAVES: 'leaves the channel',
    Action.MUTED: 'becomes muted',
    Action.UNMUTED: 'stops being muted',
    Action.DEAFENED: 'becomes deafened',
    Action.UNDEAFENED: 'stops being deafened',
    Action.STREAMING: 'starts a stream or video',
    Action.UNSTREAMING: 'stops a stream or video'
}

class Trigger:
    __slots__ = ['userlike', 'action', 'channel']
    def __init__(self, *, userlike=None, action=None, channel=None):
        if userlike is not None and not isinstance(userlike, Userlike):
            userlike = Userlike(**userlike)
        self.userlike = userlike
        if channel is not None and not isinstance(channel, discord.VoiceChannel): raise TypeError('channel must be a VoiceChannel, not' + str(type(channel)))
        self.channel = channel
        if action is not None:
            action = Action(action)
        self.action = action

class Event:
    def __init__(self, *, user, state_before, state_after):
        self.channel = state_before.channel or state_after.channel
        self.user = user
        self.action = None
        if state_before.channel is None and state_after.channel is not None:
            self.action = Action.JOINS
            return
        if state_after.channel is None and state_before.channel is not None:
            self.action = Action.LEAVES
            return
        
        # it is important to check for deafening before muting, because the default Discord client applies both changes at the same time
        if (state_before.deaf + state_before.self_deaf) < (state_after.deaf + state_after.self_deaf):
            self.action = Action.DEAFENED
            return
        if (state_before.deaf + state_before.self_deaf) > (state_after.deaf + state_after.self_deaf):
            self.action = Action.UNDEAFENED
            return
        
        if (state_before.mute + state_before.self_mute) < (state_after.mute + state_after.self_mute):
            self.action = Action.MUTED
            return
        if (state_before.mute + state_before.self_mute) > (state_after.mute + state_after.self_mute):
            self.action = Action.UNMUTED
            return
        
        if (state_before.self_stream + state_before.self_video) < (state_after.self_stream + state_after.self_video):
            self.action = Action.STREAMING
            return
        if (state_before.self_stream + state_before.self_video) > (state_after.self_stream + state_after.self_video):
            self.action = Action.UNSTREAMING
            return

        if self.action == None:
            raise ValueError('No change detected between two states:', state_before, state_after)

    def __repr__(self):
        return f'Event<user={repr(self.user)}, action={repr(self.action)}, channel={repr(self.channel)}>'

class Rule:
    def __init__(self, *, guild, trigger, channel_to_mention, users_to_mention):
        self.guild = guild
        if not isinstance(trigger, Trigger):
            trigger = Trigger(**trigger)
        self.trigger = trigger
        if not isinstance(channel_to_mention, discord.TextChannel):
            raise TypeError('channel_to_mention must be a TextChannel')
        self.channel_to_mention = channel_to_mention
        self.users_to_mention = [Userlike(**i) if isinstance(i, dict) else Userlike.from_discord_model(i) if isinstance(i, discord.Role) or isinstance(i, discord.Member) else i for i in users_to_mention]
        self.name_indexes = generate_name_indexes()

    @property
    def name(self):
        return name_indexes_to_words(self.name_indexes)


    @property
    def name_hash(self):
        md5 = hashlib.md5(bytes(self.name, 'utf8')).hexdigest()
        return int(md5, 16)

    @property
    def color(self):
        return discord.Color.from_hsv((self.name_hash%1024)/1024, 1,  1)

    def as_embed(self):
        emb = discord.Embed()
        emb.title = self.name
        emb.color = self.color
        if self.trigger.userlike:
            when_this = 'When this '+self.trigger.userlike.type.value
            user_line = self.trigger.userlike.as_mention()
        else:
            when_this = 'When anybody'
            user_line = 'who can do this action'
        emb.add_field(name=when_this,
                      value=user_line)
        emb.add_field(name='Does this action', value=ACTION_VERBS[self.trigger.action])
        if self.trigger.channel:
            emb.add_field(name='In this voice channel', value=self.trigger.channel.mention)
        emb.add_field(name='Then write to this text channel', value=self.channel_to_mention.mention)
        if len(self.users_to_mention or []) != 0:
            emb.add_field(name='While mentioning these', value=' '.join(map(lambda x: x.as_mention(), self.users_to_mention)))
        return emb

    async def send_notification(self, ctx, event):
        notification_text = ' '.join(map(lambda x: x.as_mention(), self.users_to_mention))
        notification_text += ' ' + ACTION_MESSAGES[self.trigger.action].format(user=event.user, channel=event.channel)
        emb = discord.Embed()
        emb.color = self.color
        emb.description = 'This notification was created by rule `'+self.name+'`.'
        emb.timestamp = datetime.datetime.now()
        await ctx.send(content=notification_text, embed=emb)
        


@bot.command()
async def ping(ctx):
    await ctx.send('pong')

@bot.command(brief='Add a notification rule.',
help='''Add a rule to send notifications on voice channel events.

All parameters are optional.
- who: user or role that performs an action, default is "everyone".
- does_what: what action is performed, default is "joins", valid options are: ''' + ', '.join(map(lambda x: x.value, Action)) + '''.
- in_where: name of voice channel in which the action is performed, default is "every voice channel". If this is multiple words, enclose it in "quotation marks".
- tell_who: which users will be mentioned when the event happens.''')
@discord.ext.commands.guild_only()
async def add_rule(ctx,
                   who: typing.Optional[typing.Union[discord.Member, discord.Role]]=None,
                   does_what: typing.Optional[str]=Action.JOINS,
                   in_where: typing.Optional[discord.VoiceChannel]=None,
                   tell_who: discord.ext.commands.Greedy[typing.Union[discord.Member, discord.Role]]=None):
    try:
        if who is not None:
            who = Userlike.from_discord_model(who)
        try:
            does_what = Action(does_what)
        except ValueError:
            await ctx.send('Action `'+does_what+'` is not recognized, valid options are: `'+'`, `'.join(map(lambda x: x.value, Action))+'`.')
        trig = Trigger(userlike=who, action=does_what, channel=in_where)
        rule = Rule(guild=ctx.channel.guild, trigger=trig, channel_to_mention=ctx.channel, users_to_mention=tell_who)
        await ctx.send('Your new rule is:', embed=rule.as_embed())
    except Exception as e:
        await on_command_error(ctx, e)

@bot.event
async def on_voice_state_update(member, before, after):
    try:
#        try:
        ev = Event(user=member, state_before=before, state_after=after)
#        except ValueError: # if no handleable change occurred, ignore.
#            return
    
        await member.send('You just caused this event: '+repr(ev))
    except Exception as e:
        await on_command_error(None, e, guild=member.guild)

@add_rule.error
@bot.event
async def on_command_error(ctx, exception, guild=None):
    emb = discord.Embed()
    emb.color = discord.Color.red()
    emb.title = 'An error occurred! :('
    emb.description = '```\n'+''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))+'\n```'
    
    # Try to send this anywhere we can.
    # First, use the default context.
    # If that fails, or the context is not available (for example from custom invocations), then try the guild's system notifications channel.
    # If that also fails, try every channel on the guild in order, continuing to the next one for every failure.
    # If all of this fails, we ignore the exception.

    if ctx:
        try:
            await ctx.send(embed=emb)
            return True
        except discord.Forbidden: pass
        if not guild: guild = ctx.guild
    if not guild: return False
    if guild.system_channel:
        try:
            await guild.system_channel.send(embed=emb)
            return True
        except discord.Forbidden:
            pass
        for chan in guild.channels:
            try:
                await chan.send(embed=emb)
                return True
            except discord.Forbidden:
                pass
        

    return False

bot.run(TOKEN)
