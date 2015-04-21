""" MPF plugin which enables the Backbox Control Protocol (BCP) v1.0alpha"""
# bcp.py
# Mission Pinball Framework
# Written by Brian Madden & Gabe Knuth
# Released under the MIT License. (See license info at the end of this file.)

# The Backbox Control Protocol was conceived and developed by:
# Quinn Capen
# Kevin Kelm
# Gabe Knuth
# Brian Madden
# Mike ORourke

# Documentation and more info at http://missionpinball.com/mpf

import logging
import socket
import threading
import urllib
import urlparse
from Queue import Queue
import copy

from mpf.game.player import Player
from mpf.system.config import Config

__bcp_version_info__ = ('1', '0')
__bcp_version__ = '.'.join(__bcp_version_info__)


def preload_check(machine):
    return True


def decode_command_string(bcp_string):
    bcp_command = urlparse.urlsplit(bcp_string.lower().decode('utf-8'))
    try:
        kwargs = urlparse.parse_qs(bcp_command.query)

    except AttributeError:
        kwargs = dict()

    for k, v in kwargs.iteritems():
        kwargs[k] = urllib.unquote(v[0])

    return bcp_command.path, kwargs


def encode_command_string(bcp_command, **kwargs):

    scrubbed_kwargs = dict()

    try:
        for k, v in kwargs.iteritems():
            scrubbed_kwargs[k.lower()] = str(v).lower()

        scrubbed_kwargs = urllib.urlencode(kwargs)

    except (TypeError, AttributeError):
        pass

    return unicode(urlparse.urlunparse((None, None, bcp_command.lower(), None,
                                        scrubbed_kwargs, None)), 'utf-8')


class BCP(object):
    '''The parent class for the BCP client.

    This class can support connections with multiple remote hosts at the same
    time using multiple instances of the BCPClient class.

    Args:
        machine: A reference to the main MPF machine object.

    The following BCP commands are currently implemented:
        attract_start
        attract_stop
        ball_start?player=x&ball=x
        ball_end
        error
        game_start
        game_end
        goodbye
        hello?version=xxx
        mode_start?name=xxx&priority=xxx
        mode_stop?name=xxx
        player_add?player=x
        player_score?value=x&prev_value=x&change=x
        player_turn_start?player=x
        player_variable?name=x&value=x&prev_value=x&change=x
        switch?name=x&state=x
        trigger?name=xxx

    Todo:
        # to ignore commands
        commands and param names are case-insensitive
        id is up to 32 chars for shows, ball, etc.
        auto resume

        config?volume=1&volume_steps=20
        config?language=english

        set
        get

        reset

        timer started
        timer paused
        timer tick
        timer cancel
        timer complete

        MC requests MPF to send switch states

    '''

    def __init__(self, machine):
        if ('BCP' not in machine.config or
                'connections' not in machine.config['BCP']):
            return

        self.log = logging.getLogger('BCP')
        self.machine = machine
        self.machine.bcp = self

        self.config = machine.config['BCP']
        self.receive_queue = Queue()
        self.bcp_events = dict()
        self.connection_config = self.config['connections']
        self.bcp_clients = list()

        self.bcp_receive_commands = {'error': self.bcp_receive_error,
                                     'switch': self.bcp_receive_switch,
                                     'trigger': self.bcp_receive_trigger
                                    }

        self.dmd = self.machine.platform.configure_dmd()

        self._setup_bcp_connections()
        self.filter_player_events = True
        self.send_player_vars = False
        self.mpfmc_trigger_events = set()

        try:
            self.bcp_events = self.config['event_map']
            self.process_bcp_events()
        except KeyError:
            pass

        if ('player_variables' in self.config and
                self.config['player_variables']):

            self.send_player_vars = True

            if (type(self.config['player_variables']) is str and
                    self.config['player_variables'] == '__all__'):
                self.filter_player_events = False

            else:
                self.config['player_variables'] = (
                    Config.string_to_list(self.config['player_variables']))

        self._setup_player_monitor()
        self.register_mpfmc_trigger_events(self.machine.config)

        try:
            self.register_triggers(self.machine.config['Triggers'])
        except KeyError:
            pass

        self.machine.events.add_handler('timer_tick', self.get_bcp_messages)
        self.machine.events.add_handler('game_starting', self.bcp_game_start)
        self.machine.events.add_handler('player_add_success',
                                        self.bcp_player_added)
        self.machine.events.add_handler('machine_reset_phase_1',
                                        self.bcp_reset)

        self.machine.modes.register_start_method(self.bcp_mode_start, 'Mode')
        self.machine.modes.register_start_method(self.register_triggers,
                                                 'Triggers')
        self.machine.modes.register_load_method(
            self.register_mpfmc_trigger_events)

    def _setup_bcp_connections(self):
        for name, settings in self.connection_config.iteritems():
            if 'host' not in settings:
                break

            self.bcp_clients.append(BCPClient(self.machine, name,
                                              settings, self.receive_queue))

    def remove_bcp_connection(self, bcp_client):
        """Removes a BCP connection to a remote BCP host.

        Args:
            bcp_client: A reference to the BCPClient instance you want to
                remove.

        """
        try:
            self.bcp_clients.remove(self)
        except ValueError:
            pass

    def _setup_player_monitor(self):
        Player.monitor_enabled = True
        self.machine.register_monitor('player', self._player_var_change)

    def _player_var_change(self, name, value, prev_value, change):

        if name == 'score':
            self.send('player_score', value=value, prev_value=prev_value,
                      change=change)

        elif self.send_player_vars and (
                not self.filter_player_events or
                name in self.config['player_variables']):
            self.send(bcp_command='player_variable',
                      name=name,
                      value=value,
                      prev_value=prev_value,
                      change=change)

    def process_bcp_events(self):
        """Processes the BCP Events from the config."""
        # config is localized to BCPEvents

        for event, settings in self.bcp_events.iteritems():

            if 'params' in settings:

                self.machine.events.add_handler(event, self._bcp_event_callback,
                                                command=settings['command'],
                                                params=settings['params'])

            else:
                self.machine.events.add_handler(event, self._bcp_event_callback,
                                                command=settings['command'])

    def _bcp_event_callback(self, command, params=None, **kwargs):
        if params:

            params = copy.deepcopy(params)

            for param, value in params.iteritems():

                # Are there any text variables to replace on the fly?
                # todo should this go here?
                if '%' in value:

                    # first check for player vars (%var_name%)
                    if self.machine.game and self.machine.game.player:
                        for name, val in self.machine.game.player:
                            if '%' + name + '%' in value:
                                value = value.replace('%' + name + '%', str(val))

                    # now check for single % which means event kwargs
                    for name, val in kwargs.iteritems():
                        if '%' + name in value:
                            params[param] = value.replace('%' + name, str(val))

            self.send(command, **params)

        else:
            self.send(command)

    def register_mpfmc_trigger_events(self, config, **kwargs):

        self.log.debug("Registering Trigger Events")

        try:
            for event in config['ShowPlayer'].keys():
                self.create_trigger_event(event)
        except KeyError:
            pass

        try:
            for event in config['SlidePlayer'].keys():
                self.create_trigger_event(event)
        except KeyError:
            pass

        try:
            for k, v in config['SoundPlayer'].iteritems():
                if 'start_events' in v:
                    for event in Config.string_to_list(v['start_events']):
                        self.create_trigger_event(event)
                if 'stop_events' in v:
                    for event in Config.string_to_list(v['stop_events']):
                        self.create_trigger_event(event)
        except KeyError:
            pass

    def create_trigger_event(self, event):
        """Registers a BCP trigger based on an MPF event.

        Args:
            event: String name of the event you're registering this trigger for.

        The BCP trigger will be registered with the same name as the MPF event.
        For example, if you pass the event "foo_event", the BCP command that
        will be sent when that event is posted will be trigger?name=foo_event.

        """

        if event not in self.mpfmc_trigger_events:

            self.machine.events.add_handler(event, handler=self.send,
                                            bcp_command='trigger',
                                            name=event)
            self.mpfmc_trigger_events.add(event)

    def register_triggers(self, config, priority, mode):
        # config is localized to 'Trigger'

        event_list = list()

        for event, settings in config.iteritems():

            params = dict()

            try:
                params = copy.deepcopy(settings['params'])
            except KeyError:
                pass

            try:
                event_list.append(self.machine.events.add_handler(
                    event, handler=self.send, bcp_command='trigger',
                    name=settings['bcp_name'], **params))
            except KeyError:
                self.log.warning("Could not create trigger event for '%s'. "
                                 "Settings: %s",
                                 event, settings)

        return self.machine.events.remove_handlers_by_keys, event_list

    def send(self, bcp_command, callback=None, **kwargs):
        """Sends a BCP message.

        Args:
            bcp_command: String name of the BCP command that will be sent.
            callback: An optional callback method that will be called as soon as
                the BCP command is sent.
            **kwargs: Optional kwarg pairs that will be sent as parameters along
                with the BCP command.

        Example:
            If you call this method like this:
                send('trigger', ball=1, string'hello')

            The BCP command that will be sent will be this:
                trigger?ball=1&string=hello

        """

        bcp_string = encode_command_string(bcp_command, **kwargs)

        for client in self.bcp_clients:
            client.send(bcp_string)

        if callback:
            callback()

    def get_bcp_messages(self):
        while not self.receive_queue.empty():
            cmd, kwargs = self.receive_queue.get(False)

            self.log.info("Processing command: %s %s", cmd, kwargs)

            # todo convert to try. Haven't done it yet though because I couldn't
            # figure out how to make it not swallow exceptions and it was
            # getting annoying to troubleshoot
            if cmd in self.bcp_receive_commands:
                self.bcp_receive_commands[cmd](**kwargs)
            else:
                self.log.warning("Received invalid BCP command: %s", cmd)
                self.send('error', message='invalid command',
                          command=cmd)

    def shutdown(self):
        """Prepares the BCP clients for MPF shutdown."""
        for client in self.bcp_clients:
            client.stop()

    def bcp_receive_error(self, **kwargs):
        """A remote BCP host has sent a BCP error message, indicating that a
        command from MPF was not recognized.

        This method only posts a warning to the log. It doesn't do anything else
        at this point.

        """

        self.log.warning('Received Error command from host with parameters: %s',
                         kwargs)

    def bcp_mode_start(self, config, priority, mode, **kwargs):
        """Sends BCP 'mode_start' to the connected BCP hosts and schedules
        automatic sending of 'mode_stop' when the mode stops.
        """
        self.send('mode_start', name=mode.name, priority=priority)

        return self.bcp_mode_stop, mode.name

    def bcp_mode_stop(self, name, **kwargs):
        """Sends BCP 'mode_stop' to the connected BCP hosts."""
        self.send('mode_stop', name=name)

    def bcp_reset(self):
        """Sends the 'reset' command to the remote BCP host."""
        self.send('reset')

    def bcp_receive_switch(self, **kwargs):
        """Processes an incoming switch state change request from a remote BCP
        host.
        """
        self.machine.switch_controller.process_switch(name=kwargs['name'],
                                                      state=int(kwargs['state']),
                                                      logical=True)

    def bcp_receive_dmd_frame(self, data):
        """Called when the BCP client receives a new DMD frame from the remote
        BCP host. This method forwards the frame to the physical DMD.
        """
        self.dmd.update(data)

    def bcp_game_start(self, **kwargs):
        """Sends the BCP 'game_start' and 'player_added?number=1' commands to
        the remote BCP hosts.
        """
        self.send('game_start')
        self.send('player_added', number=1)

    def bcp_player_added(self, player, num):
        """Sends BCP 'player_added' to the connected BCP hosts."""
        if num > 1:
            self.send('player_added', number=num)

    def bcp_trigger(self, name, **kwargs):
        """Sends BCP 'trigger' to the connected BCP hosts."""
        self.send('trigger', name=name, **kwargs)

    def bcp_receive_trigger(self, name=None, **kwargs):
        """Processes an incoming trigger command from a remote BCP host.
        """
        if not name:
            return

        if 'callback' in kwargs:
            self.machine.events.post(event='trigger_' + name,
                                     callback=self.bcp_trigger,
                                     name=kwargs.pop('callback'),
                                     **kwargs)

        else:
            self.machine.events.post(event='trigger_' + name, **kwargs)


class BCPClient(object):

    def __init__(self, machine, name, config, receive_queue):
        """Sets up a BCP socket client.

        Args:
            host: String of the host name.
            port: Integer of the port name.
        """

        self.log = logging.getLogger('BCPClient.' + name)

        self.machine = machine
        self.name = name
        self.receive_queue = receive_queue

        config_spec = '''
                        host: string
                        port: int|5050
                        connection_attempts: int|-1
                        require_connection: boolean|False
                        '''

        self.config = Config.process_config(config_spec, config)

        self.sending_queue = Queue()
        self.receive_thread = None
        self.sending_thread = None
        self.socket = None
        self.connection_attempts = 0
        self.attempt_socket_connection = True
        self.send_goodbye = True

        self.bcp_commands = {'hello': self.receive_hello,
                             'goodbye': self.receive_goodbye,
                            }

        self.setup_client_socket()

    def setup_client_socket(self):

        self.connection_attempts += 1
        if (self.config['connection_attempts'] == -1 or
                self.connection_attempts < self.config['connection_attempts']):

            self.log.debug("Attempting socket connection. Attempt: %s, Max: %s",
                           self.connection_attempts,
                           self.config['connection_attempts'])

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                self.socket.connect((self.config['host'], self.config['port']))
                self.log.info("Connected to remote BCP host %s:%s",
                              self.config['host'], self.config['port'])
                self.connection_attempts = 0

            except socket.error, v:
                self.socket = None
                self.log.info("Failed to connect to remote BCP host %s:%s. "
                              "Error: %s", self.config['host'],
                              self.config['port'], v)
                if self.config['require_connection']:
                    self.log.critical("BCP connection 'require_connection' "
                                      "setting is True. Unable to continue.")
                    self.machine.done = True

            if self.create_socket_threads():
                self.send_hello()

        else:
            self.attempt_socket_connection = False
            self.log.debug("Max socket connection attempts reached. Giving up")

    def create_socket_threads(self):
        """Creates and starts the sending and receiving threads for the BCP
        socket.

        Returns:
            True if the socket exists and the threads were started. False if
            not.
        """

        if self.socket:

            self.receive_thread = threading.Thread(target=self.receive_loop)
            self.receive_thread.daemon = True
            self.receive_thread.start()

            self.sending_thread = threading.Thread(target=self.sending_loop)
            self.sending_thread.daemon = True
            self.sending_thread.start()

            return True

        else:
            return False

    def stop(self):
        """Stops and shuts down the socket client."""
        self.log.info("Stopping socket client")

        if self.socket:
            if self.send_goodbye:
                self.send('goodbye')

            self.socket.close()
            self.socket = None  # Socket threads will exit on this

    def send(self, message):
        """Sends a message to the BCP host.

        Args:
            message: String of the message to send.
        """

        if not self.socket and self.attempt_socket_connection:
            self.setup_client_socket()

        self.sending_queue.put(message)

    def receive_loop(self):
        """Receive loop which reads incoming data, assembles commands, and puts
        them onto the receive queue.

        This method is run as a thread.
        """

        # Implementation note: Sockets don't necessarily receive the entire
        # message in one socket.recv() call. BCP separates messages based on the
        # '\n' character. So we have to split the incoming messages by \n, but
        # if there are any leftover characters we have to save them and add them
        # whatever we get on the next recv() read to it. So that's what all this
        # craziness is here.

        fragment = ''  # used to save a partial incoming message

        while self.socket:
            data = self.socket.recv(4096)
            if data:

                # if there's an existing fragment, join our new data to it
                if fragment:
                    data = fragment + data

                # if we still don't have \n, it's still a fragment
                if '\n' not in data:
                    fragment = data

                # we have at least one \n in our data
                else:
                    messages = data.split("\n")

                    # if the \n is not the last char...
                    if messages[-1:]:
                        # save whatever was after the last \n to a new fragment
                        fragment = messages[-1:][0]
                        # trim that last fragment from our messages list
                        messages = messages[:-1]

                    # now process the remaining complete messages
                    for message in messages:
                        if message:
                            if message.startswith('dmd_frame'):
                                # If we received a dmd_frame command, we process
                                # them here immediately since they're a special
                                # case.
                                self.machine.bcp.dmd.update(message[10:])

                            else:
                                self.log.info('<<<<<<<<<<<<<< Received "%s"',
                                              message)
                                cmd, kwargs = decode_command_string(message)

                                if cmd in self.bcp_commands:
                                    self.bcp_commands[cmd](**kwargs)
                                else:
                                    self.receive_queue.put((cmd, kwargs))

    def sending_loop(self):
        """Sending loop which transmits data from the sending queue to the
        remote socket.

        This method is run as a thread.
        """
        while self.socket:
            message = self.sending_queue.get()

            try:
                self.log.info('>>>>>>>>>>>>>> Sending "%s"', message)
                self.socket.sendall(message + '\n')

            except (IOError, AttributeError):
                # MPF is probably in the process of shutting down
                pass

    def receive_hello(self, **kwargs):
        """Processes incoming BCP 'hello' command."""
        self.log.info('Received BCP Hello from host with kwargs: %s', kwargs)

    def receive_goodbye(self):
        """Processes incoming BCP 'goodbye' command."""
        self.send_goodbye = False
        self.stop()
        self.machine.bcp.remove_bcp_connection(self)

        if self.config['require_connection']:
            self.machine.bcp.shutdown()
            self.machine.done = True

    def send_hello(self):
        """Sends BCP 'hello' command."""
        self.send('hello?version=' + __bcp_version__)

    def send_goodbye(self):
        """Sends BCP 'goodbye' command."""
        self.send('goodbye')


# The MIT License (MIT)

# Copyright (c) 2013-2015 Brian Madden and Gabe Knuth

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
