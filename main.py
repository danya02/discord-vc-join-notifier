import discord
from discord.ext import commands
import pymongo
import names
from fuzzywuzzy import process as fwproc

import os
import typing
import enum
import random

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.typing = False
intents.presences = False
intents.voice_states = True

bot = commands.Bot(command_prefix=os.getenv('BOT_COMMAND_PREFIX') or '>', intents=intents)

client = pymongo.MongoClient('mongodb://mongo:27017/')
rules = client.rules

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
    
    def as_discord_model(self, guild):
        if self.type == 'member':
            return guild.get_member(self.id)
        elif self.type == 'role':
            return guild.get_role(self.id)

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
    Action.STREAMING: '{user} started a stream in {channel}',
    Action.UNSTREAMING: '{user} stopped a stream in {channel}',
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


class Rule:
    def __init__(self, *, guild, trigger, channel_to_mention, users_to_mention):
        self.guild = guild
        if not isinstance(trigger, Trigger):
            trigger = Trigger(**trigger)
        self.trigger = trigger
        if not isinstance(channel_to_mention, discord.TextChannel):
            raise TypeError('channel_to_mention must be a TextChannel')
        self.channel_to_mention = channel_to_mention
        self.users_to_mention = users_to_mention
        self.name_indexes = generate_name_indexes()

    @property
    def name(self):
        return name_indexes_to_words(self.name_indexes)

    @property
    def color(self):
        return discord.Color.random(seed=self.name)

    def as_embed(self):
        emb = discord.Embed()
        emb.title = self.name
        emb.color = self.color
        emb.add_field(name='When this ' + self.trigger.userlike.type, value=self.trigger.userlike.as_discord_model(self.guild).mention)
        emb.add_field(name='Does this action', value=self.trigger.action)
        if self.trigger.channel:
            emb.add_field(name='In this voice channel', value=self.trigger.channel.mention)
        emb.add_field(name='Then write to this text channel', value=self.channel_to_mention.mention)
        if len(self.users_to_mention) != 0:
            emb.add_field(name='While mentioning these', value=' '.join(map(lambda x: x.as_discord_model(self.guild).mention, self.users_to_mention)))
        return emb



@bot.command()
async def ping(ctx):
    await ctx.send('pong')

@bot.command()
async def add_rule(ctx,
                   who: typing.Optional[typing.Union[discord.Member, discord.Role]]=None,
                   does_what: typing.Optional[str]=Action.JOINS,
                   in_where: typing.Optional[discord.VoiceChannel]=None,
                   tell_who: discord.ext.commands.Greedy[typing.Union[discord.Member, discord.Role]]=None):
    await ctx.send('who: '+repr(who))
    await ctx.send('does_what: '+repr(does_what))
    await ctx.send('in_where: '+repr(in_where))
    await ctx.send('tell_who: '+repr(tell_who))

@bot.event
async def on_voice_state_update(member, before, after):
    await member.send('You just changed your state from '+str(before)+' to '+str(after))


bot.run(TOKEN)
