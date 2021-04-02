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
import asyncio

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.typing = False
intents.presences = False
intents.voice_states = True

bot = commands.Bot(command_prefix=os.getenv('BOT_COMMAND_PREFIX') or '>', intents=intents)

client = motor.motor_asyncio.AsyncIOMotorClient('mongodb://mongo:27017/', username='root', password='rootpassword')
db = client.db

async def generate_name_indexes(attempts = 50):
    for _ in range(attempts):
        left_ind = random.randint(0, len(names.LEFT)-1)
        center_ind = random.randint(0, len(names.CENTER)-1)
        right_ind = random.randint(0, len(names.RIGHT)-1)
        indexes = [left_ind, center_ind, right_ind]
        existing = await db.rules.find_one({'name_indexes': indexes})
        if existing is not None:
            continue

        return indexes
    raise ValueError('Could not find unclaimed name indexes!')

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

    def __eq__(self, other):
        return (self.type, self.id) == (other.type, other.id)

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

    def to_json(self):
        return {'type': self.type.value, 'id': self.id}

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

    def to_json(self):
        return {'userlike': self.userlike.to_json() if self.userlike else None,
                'action': self.action.value,
                'channel': self.channel.id if self.channel else None}

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

    async def lookup_rules(self):
        q = []
        
        # match user
        this_user = {'trigger.userlike.type': UserlikeType.MEMBER.value,
                     'trigger.userlike.id': self.user.id}
        
        # match roles of user
        role_queries = []
        for role in self.user.roles:
            role_queries.append({'trigger.userlike.id': role.id})

        these_roles = {'$and':
                          [
                            {'trigger.userlike.type': UserlikeType.ROLE.value},
                            {'$or': role_queries}
                          ]
                      }

        # rule does not limit user
        no_user_limitation = {'trigger.userlike': None}

        correct_user = {'$or': [this_user, these_roles, no_user_limitation]}

        correct_action = {'trigger.action': self.action.value}

        correct_channel = {'$or': [{'trigger.channel': self.channel.id}, {'trigger.channel': None}]}
        
        compound_query = {'$and': [{'guild': self.user.guild.id}, correct_user, correct_action, correct_channel]}
        rules = await db.rules.find(compound_query).to_list(None)
        rules = rules or []
        rules = [Rule(**i) for i in rules]

        return rules

class Rule:
    def __init__(self, *, guild, trigger, channel_to_mention, users_to_mention, name_indexes=None, **kwargs):
        if isinstance(guild, discord.Guild):
            self.guild = guild
        else:
            self.guild = bot.get_guild(guild)
        if not isinstance(trigger, Trigger):
            trigger = Trigger(**trigger)
        self.trigger = trigger
        users_to_mention = users_to_mention or []
        if not isinstance(channel_to_mention, discord.TextChannel):
            channel_to_mention = self.guild.get_channel(channel_to_mention)
        self.channel_to_mention = channel_to_mention
        self.users_to_mention = [Userlike(**i) if isinstance(i, dict) else Userlike.from_discord_model(i) if isinstance(i, discord.Role) or isinstance(i, discord.Member) else i for i in users_to_mention if i]
        self.name_indexes = name_indexes

    @property
    def name(self):
        return name_indexes_to_words(self.name_indexes)

    async def generate_name(self):
        self.name_indexes = await generate_name_indexes()

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

    async def send_notification(self, event):
        notification_text = ' '.join(map(lambda x: x.as_mention(), self.users_to_mention))
        notification_text += ' ' + ACTION_MESSAGES[self.trigger.action].format(user=event.user, channel=event.channel)
        emb = discord.Embed()
        emb.color = self.color
        emb.description = 'This notification was created by rule `'+self.name+'`.'
        emb.timestamp = datetime.datetime.now()
        await self.channel_to_mention.send(content=notification_text, embed=emb)

    @staticmethod
    async def send_notifications_for_list(event, rules):
        notification_text = ACTION_MESSAGES[rules[0].trigger.action].format(user=event.user, channel=event.channel)
        mentions = dict()
        contributing_rules = dict()
        for rule in rules:
            mentions[rule.channel_to_mention] = mentions.get(rule.channel_to_mention, []) + (rule.users_to_mention or [])
            contributing_rules[rule.channel_to_mention] = contributing_rules.get(rule.channel_to_mention, []) + [rule]
        
        for channel in contributing_rules:
            emb = discord.Embed()
            rules_for_channel = contributing_rules[channel]
            emb.timestamp = datetime.datetime.now()
            if len(rules_for_channel)>1:
                emb.color = discord.Color.random() # because multiple rules are being applied, no one color may be used.
                emb.description = 'This notification was created by these rules: `' + '`, `'.join([i.name for i in rules_for_channel])+'`.'
            else:
                emb.color = rules_for_channel[0].color
                emb.description = 'This notification was created by rule `'+rules_for_channel[0].name+'`.' 
            mentions_for_channel = ' '.join([i.as_mention() for i in mentions[channel]])
            try:
                await channel.send(content=mentions_for_channel + ' ' + notification_text, embed=emb)
            except discord.Forbidden: pass
            
        
    def to_json(self):
        return {'guild': self.guild.id,
                'trigger': self.trigger.to_json(),
                'channel_to_mention': self.channel_to_mention.id,
                'users_to_mention': [i.to_json() for i in self.users_to_mention],
                'name_indexes': self.name_indexes}




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
        chk = rule.to_json()
        del chk['name_indexes']
        existing = await db.rules.find_one(chk)
        if existing:
            rule = Rule(**existing)
            await ctx.send(content='A rule that is identical to this one already exists, not registering.', embed=rule.as_embed())
            return

        await rule.generate_name()
        member_is_manager = ctx.author.permissions_for(ctx.channel).manage_guild
        if not member_is_manager:
            tell_who = tell_who or [ctx.author]
            if len(tell_who)>1 or tell_who[0] != ctx.author:
                ctx.send(ctx.author.mention+''', you have tried to mention people other than yourself with this rule without having the "Manage Server" permission.
Please edit your rule to exclude other users from the list of people to be notified.''', embed=rule.as_embed())
                return
        CHECK_MARK = '✅'
        CANCEL_MARK = '❌'
        emojis = [CHECK_MARK, CANCEL_MARK]
        msg = await ctx.send('Here is your new rule. Please confirm the parameters, then click '+CHECK_MARK+' to confirm or '+CANCEL_MARK+' to cancel.', embed=rule.as_embed())
        try:
            await msg.add_reaction(CHECK_MARK)
            await msg.add_reaction(CANCEL_MARK)
        except discord.Forbidden:
            await msg.edit(content='This bot cannot add reactions to messages, but this is required.')
            await cannot_add_reactions(ctx)
            return
        try:
            reaction, _ = await bot.wait_for('reaction_add',
                                             check=lambda r, u: r.emoji in emojis and r.message.id == msg.id and u == ctx.author,
                                             timeout=120)
        except asyncio.TimeoutError:
            await msg.edit(content='Rule confirmation timed out, to confirm this rule please repeat the command.')
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                await cannot_clear_reactions(ctx)
        reaction = reaction.emoji
        if reaction == CANCEL_MARK:
            await msg.edit(content='Rule cancelled, this rule will not be applied.')
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                await cannot_clear_reactions(ctx)
        else:
            await db.rules.insert_one(rule.to_json())
            await msg.edit(content='Rule confirmed.')
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                await cannot_clear_reactions(ctx)
    except Exception as e:
        await on_command_error(ctx, e)

@bot.command(brief='Delete an existing notification rule.',
help='''Display and optionally delete an existing rule by name.
To view the rules active in this channel, use the "show_rules" command.''')
async def del_rule(ctx, name):
    indexes, exact = parse_name_indexes(*(name.split('-')))
    prefix = ''
    if not exact:
        true_name = name_indexes_to_words(indexes)
        prefix += 'NOTE: Name `'+name+'` is not valid, assuming `'+true_name+'`.\n'
        name = true_name
    rule = await db.rules.find_one({'name_indexes': indexes})
    if not rule:
        await ctx.send(prefix + 'The rule by name `'+name+'` does not exist.')
        return
    rule = Rule(**rule)
    if rule.guild != ctx.guild:
        await ctx.send(prefix + 'A rule by name `'+name+'` was found, but it belongs to a different server so we cannot show it to you.')


    member_is_manager = ctx.author.permissions_for(ctx.channel).manage_guild
    mentions_nobody = len(rule.users_to_mention or [])==0
    mentions_only_me = False
    if rule.users_to_mention:
        mentions_only_me = rule.users_to_mention[0] == Userlike.from_discord_model(ctx.author)
    


    if member_is_manager or mentions_nobody or mentions_only_me:
        await ctx.send(content=prefix + 'This rule was found, but it mentions users other than you. '+\
            'If a rule mentions users, it can be removed by a server manager or by the only user mentioned, if applicable.', embed=rule.as_embed())
        return
    

    msg = await ctx.send(content=prefix + 'This rule was found, do you want to delete it? '+CHECK_MARK+' for yes, '+CANCEL_MARK+' for no.', embed=rule.as_embed())
    CHECK_MARK = '✅'
    CANCEL_MARK = '❌'
    emojis = [CHECK_MARK, CANCEL_MARK]
    try:
        await msg.add_reaction(CHECK_MARK)
        await msg.add_reaction(CANCEL_MARK)
    except discord.Forbidden:
        await msg.edit(content='This bot cannot add reactions to messages, but this is required.')
        await cannot_add_reactions(ctx)
        return
    try:
        reaction, _ = await bot.wait_for('reaction_add',
                                         check=lambda r, u: r.emoji in emojis and r.message.id == msg.id and u == ctx.author,
                                         timeout=120)
    except asyncio.TimeoutError:
        await msg.edit(content='Confirmation timed out, to confirm this action please repeat the command.')
        try:
            await msg.clear_reactions()
        except discord.Forbidden:
            await cannot_clear_reactions(ctx)
    reaction = reaction.emoji
    if reaction == CANCEL_MARK:
        await msg.edit(content='This rule will not be deleted.')

    await db.rules.delete_one({'name_indexes': indexes})
    await msg.edit(content='This rule was successfully deleted')
    try:
        await msg.clear_reactions()
    except discord.Forbidden:
        await cannot_clear_reactions(ctx)


@bot.command(brief='List rules active in this channel or server.',
help='''List the currently enabled rules in this channel or this server.

By default, shows the rules in current channel. To view rules in entire server, add "yes" as an optional parameter.''')
async def show_rules(ctx, in_entire_guild: bool=False):
    query = {'guild': ctx.guild.id}
    if not in_entire_guild:
        query['channel_to_mention'] = ctx.channel.id

    rule_cursor = db.rules.find(query)
    rule_list = await rule_cursor.to_list(None)
    rule_name_list = '\n'.join(map(lambda x: '`'+Rule(**x).name+'`', rule_list))
    await ctx.send('There are '+str(len(rule_list))+' rules active in this '+('server' if in_entire_guild else 'channel') + ':\n' + rule_name_list)
    
@bot.event
async def on_voice_state_update(member, before, after):
    try:
        try:
            ev = Event(user=member, state_before=before, state_after=after)
        except ValueError: # if no handleable change occurred, ignore.
            return

        rules = await ev.lookup_rules()
        if rules:
            if len(rules)==1:
                await rules[0].send_notification(ev)
            else:
                await Rule.send_notifications_for_list(ev, rules)

        #await member.send('You just caused this event: '+repr(ev))
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

async def cannot_add_reactions(ctx):
    await ctx.send('This bot cannot add reactions to messages. Please allow this bot the "Add Reactions" permission.')

async def cannot_clear_reactions(ctx):
    await ctx.send('This bot cannot remove reactions on messages. Please allow this bot the "Manage Messages" permission.')

bot.run(TOKEN)
