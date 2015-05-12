import time
import re

from threading import Thread
import hangups.ui.utils
from slackclient import SlackClient

import asyncio
import logging
import hangups

import emoji

""" SlackRTM plugin for listening to hangouts and slack and syncing messages between the two.
config.json will have to be configured as follows:
"slackrtm": [{
  "key": SLACK_API_KEY,
  "synced_conversations": [
  ["SLACK_CHANNEL_ID1", "CONV_ID1"],
  ["SLACK_CHANNEL_ID1", "CONV_ID1"]
  ]
}]

You can (theoretically) set up as many slack sinks per bot as you like, by extending the list"""


def _initialise(Handlers, bot=None):
    if bot:
        _start_slackrtm_sinks(bot)
    else:
        print("slackrtm: Slack sinks could not be initialized.")
    Handlers.register_handler(_handle_slackout)
    Handlers.register_handler(_handle_membership_change, type="membership")
    return []


def _start_slackrtm_sinks(bot):
    # Start and asyncio event loop
    loop = asyncio.get_event_loop()

    slack_sink = bot.get_config_option('slackrtm')
    if not isinstance(slack_sink, list):
        return

    threads = []
    for sinkConfig in slack_sink:
        # start up slack listener in a separate thread
        t = Thread(
            target=start_listening, 
            args=(bot, loop, sinkConfig)
            )
        t.daemon = True
        t.start()
        threads.append(t)
    logging.info(_("_start_slackrtm_sinks(): %d sink thread(s) started" % len(threads)))


class SlackRTM(object):
    def __init__(self, sink_config, bot):
        self.bot = bot
        self.config = sink_config
        self.apikey = self.config['key']

        self.slack = SlackClient(self.apikey)
        self.slack.rtm_connect()

        self.update_usernames()
        self.update_channelnames()
        self.my_uid = self.slack.server.login_data['self']['id']

        self.hangoutids = {}
        self.hangoutnames = {}
        for c in self.bot.list_conversations():
            name = hangups.ui.utils.get_conv_name(c, truncate=True)
            self.hangoutids[ name ] = c.id_
            self.hangoutnames[ c.id_ ] = name

        self.hosinks = {}
        self.slacksinks = {}
        for conv in self.config["synced_conversations"]:
            honame = ''
            if len(conv) == 3:
                honame = conv[2]
            else:
                if not conv[1] in self.hangoutnames:
                    print("slackrtm: could not find conv %s in bot's conversations!" % conv[1])
                    honame = conv[1]
                else:
                    honame = self.hangoutnames[ conv[1] ]
            if not conv[0] in self.hosinks:
                self.hosinks[ conv[0] ] = []
            self.hosinks[ conv[0] ].append( (conv[1], honame) )
            if not conv[1] in self.slacksinks:
                self.slacksinks[ conv[1] ] = []
            self.slacksinks[ conv[1] ].append( (conv[0], honame) )

    def update_usernames(self):
        self.usernames = {}
        for u in self.slack.server.login_data['users']:
            self.usernames[u['id']] = u['name']

    def get_username(self, user, default=None):
        if not user in self.usernames:
            self.update_usernames()
            if not user in self.usernames:
                print('slackrtm: could not find user "%s" although reloaded' % user)
                return default
        return self.usernames[user]

    def update_channelnames(self):
        self.channelnames = {}
        for c in self.slack.server.login_data['channels']:
            self.channelnames[c['id']] = c['name']

    def get_channelname(self, channel, default=None):
        if not channel in self.channelnames:
            self.update_channelnames()
            if not channel in self.channelnames:
                print('slackrtm: could not find channel "%s" although reloaded' % channel)
                return default
        return self.channelnames[channel]

    def rtm_read(self):
        return self.slack.rtm_read()

    def ping(self):
        return self.slack.server.ping()

    def matchReference(self, match):
        out = ""
        linktext = ""
        if match.group(5) == '|':
            linktext = match.group(6)
        if match.group(2) == '@':
            if linktext != "":
                out = linktext
            else:
                out = "@%s" % self.get_username(match.group(3), 'unknown:%s' % match.group(3))
        elif match.group(2) == '#':
            if linktext != "":
                out = linktext
            else:
                out = "#%s" % self.get_channelname(match.group(3), 'unknown:%s' % match.group(3))
        else:
            if linktext != "":
                out += linktext + ":"
            out += match.group(1)
        return out
    
    def textToHtml(self, text):
        reffmt = re.compile('<((.)([^|>]*))((\|)([^>]*)|([^>]*))>')
        text = reffmt.sub(self.matchReference, text)
        text = emoji.emojize(text)
        bfmt = re.compile(r'\*([^\*]*)\*')
        text = bfmt.sub(r'<b>\1</b>', text)
        ifmt = re.compile(r'_([^_]*)_')
        text = ifmt.sub(r'<i>\1</i>', text)
        text = text.replace("\r\n", "\n")
        text = text.replace("\n", " <br/>\n")
        return text

    def handle_reply(self, reply):
        if not 'type' in reply:
            print("slackrtm: No 'type' in reply:")
            print("slackrtm: "+str(reply))
            return
    
        if reply['type'] in ['pong', 'presence_change',  'user_typing']:
            # we ignore pong's as they are only answers for our pings
            return
    
        user = ''
        username = ''
        edited = ''
        is_bot = False
        if reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] == 'message_changed':
            edited = '(msgupd)'
            user = reply['message']['edited']['user']
            reply['text'] = reply['message']['text']
    
        if reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] == 'bot_message' and not 'user' in reply:
            is_bot = True
        elif not 'text' in reply or not 'user' in reply:
            print("slackrtm: no text/user in reply: "+str(reply))
            return
        else:
            user = reply['user']

        from_ho = ''
        if not is_bot and self.my_uid == reply["user"]:
            # this is a HO relayed join/leave message, check from which HO
            hofmt = re.compile(r'^(.* has added .* to |.* has left )_(.+)_$')
            match = hofmt.match(reply['text'])
            if match:
                #print('slackrtm: found match in text: %s' % match.group(2))
                from_ho = match.group(2)

        if is_bot:
            # this might be a HO relayed message, check from which HO
            hofmt = re.compile(r'^.* \(via HO:(.+)\)$')
            match = hofmt.match(reply['username'])
            if match:
                #print('slackrtm: found match in username: %s' % match.group(1))
                from_ho = match.group(1)
            # in any case, we take the username field as username as there is no 'user'
            username = reply['username']
    
        if not is_bot:
            username = self.get_username(user, user)

        response = "<b>%s%s:</b> %s" % (username, edited, self.textToHtml(reply["text"]))
        channel = None
        is_private = False
        if 'channel' in reply:
            channel = reply['channel']
        elif 'group' in reply:
            channel = reply['group']
            is_private = True
        if not channel:
            print('slackrtm: no channel or group in respone')
            return

        for hoid, honame in self.hosinks.get(channel, []):
            if from_ho == honame:
                print('slackrtm: rejecting to relay our own message: %s' % response)
                continue
            print('slackrtm: found slack channel, forwarding to HO %s: %s' % (str(hoid), str(response)))
            if not self.bot.send_html_to_user(hoid, response):
                self.bot.send_html_to_conversation(hoid, response)

    def handle_ho_message(self, event, photo_url):
        for channel_id, honame in self.slacksinks.get(event.conv_id, []):
            fullname = '%s (via HO:%s)' % (event.user.full_name, honame)
            print("slackrtm: Sending to channel %s: %s" % (channel_id, event.text))
            self.slack.api_call('chat.postMessage',
                                channel=channel_id,
                                text=event.text,
                                username=fullname,
                                link_names=True,
                                icon_url=photo_url)

    def handle_ho_membership(self, event):
        # Generate list of added or removed users
        links = []
        for user_id in event.conv_event.participant_ids:
            user = event.conv.get_user(user_id)
            links.append('<https://plus.google.com/%s/about|%s>' % (user.id_.chat_id, user.full_name))
        names = ', '.join(links)
    
        for channel_id, honame in self.slacksinks.get(event.conv_id, []):
            # JOIN
            if event.conv_event.type_ == hangups.MembershipChangeType.JOIN:
                invitee = '<https://plus.google.com/%s/about|%s>' % (event.user_id.chat_id, event.user.full_name)
                message = '%s has added %s to _%s_' % (invitee, names, honame)
            # LEAVE
            else:
                message = '%s has left _%s_' % (names, honame)
            print('slackrtm: %s' % message)

            print("slackrtm: Sending to channel/group %s: %s" % (channel_id, message))
            self.slack.api_call('chat.postMessage',
                                channel=channel_id,
                                text=message,
                                as_user=True,
                                link_names=True)


def start_listening(bot, loop, config):
    asyncio.set_event_loop(loop)

    try:
        listener = SlackRTM(config, bot)
        last_ping = 0
        while True:
            replies = listener.rtm_read()
            if len(replies):
                if 'type' in replies[0]:
                    if replies[0]['type'] == 'hello':
                        #print('slackrtm: ignoring first replies including type=hello message to avoid message duplication: %s...' % str(replies)[:30])
                        continue
            for reply in replies:
                try:
                    listener.handle_reply(reply)
                except Exception as e:
                    print('slackrtm: unhandled exception during handle_reply(%s): %s' % (str(reply), str(e)))
            now = int(time.time())
            if now > last_ping + 3:
                listener.ping()
                last_ping = now
            time.sleep(.1)
    except KeyboardInterrupt:
        # close, nothing to do
        return
    except Exception as e:
        print('slackrtm: start_listening(): unhandled exception: %s' % str(e))
    return


@asyncio.coroutine
def _handle_slackout(bot, event, command):
    """forward messages to slack over webhook"""

    slack_sink = bot.get_config_option('slackrtm')

    if not isinstance(slack_sink, list):
        return

    for sinkConfig in slack_sink:
        try:
            try:
                response = yield from bot._client.getentitybyid([event.user_id.chat_id])
                photo_url = "http:" + response.entities[0].properties.photo_url
            except Exception as e:
                print("slackrtm: Could not pull avatar for %s: %s" %(event.user.full_name, str(e)))

            slackout = SlackRTM(sinkConfig, bot)
            slackout.handle_ho_message(event, photo_url)
            time.sleep(.1)
        except Exception as e:
            print('slackrtm: _handle_slackout threw: %s' % str(e))


@asyncio.coroutine
def _handle_membership_change(bot, event, command):

    slack_sink = bot.get_config_option('slackrtm')

    if not isinstance(slack_sink, list):
        return

    for sinkConfig in slack_sink:
        try:
            slackout = SlackRTM(sinkConfig, bot)
            slackout.handle_ho_membership(event)
            time.sleep(.1)
        except Exception as e:
            print('slackrtm: _handle_membership_change threw: %s' % str(e))
