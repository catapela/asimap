#!/usr/bin/env python
#
# File: $Id$
#
"""
The heart of the asimap server process to handle a single user's
mailbox for multiple IMAP clients.

We get all of our data relayed to us from the main asimapd server via
connections on localhost.
"""

# system imports
#
import sys
import socket
import asyncore
import asynchat
import logging
import os
import pwd
import sqlite3
import mailbox

# asimap imports
#
import asimap
import asimap.parse

from asimap.client import Authenticated
from asimap.db import Database

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5

####################################################################
#
def set_user_server_program(prg):
    """
    Sets the 'USER_SERVER_PROGRAM' attribute on this module (so other modules
    will known how to launch the user server.)

    Arguments:
    - `prg`: An absolute path to the user server program.
    """
    module = sys.modules[__name__]
    setattr(module, "USER_SERVER_PROGRAM", prg)
    return

##################################################################
##################################################################
#
class IMAPUserClientHandler(asynchat.async_chat):
    """
    This class receives messages from the main server process.

    These messages are recevied by the main server process from an IMAP client
    and it has sent them on to us to process.

    All of the messages we receive will be for an IMAP client that has
    successfully authenticated with the main server.

    The messages will be in the form of a decimal ascii integer followed by a
    new line that represents the length of the entire IMAP message we are being
    sent.

    After that will be the IMAP message (of the pre-indicated length.)

    To send messages back to the IMAP client we follow the same protocol.
    """

    LINE_TERMINATOR     = "\n"

    ##################################################################
    #
    def __init__(self, sock, port, server, options):
        """
        """
        asynchat.async_chat.__init__(self, sock = sock)

        self.log = logging.getLogger("%s.IMAPUserClientHandler" % __name__)
        self.reading_message = False
        self.ibuffer = []
        self.set_terminator(self.LINE_TERMINATOR)

        # A reference to our entry in the server.handlers dict so we can remove
        # it when our connection to the main server is shutdown.
        #
        self.port = port
        
        # A handle on the server process and its database connection.
        #
        self.server = server
        self.options = options
        self.client_handler = Authenticated(self, self.server)
        return

    ##################################################################
    #
    def log_info(self, message, type = "info"):
        """
        Replace the log_info method with one that uses our stderr logger
        instead of trying to write to stdout.

        Arguments:
        - `message`: The message to log
        - `type`: Type of message to log.. maps to 'info','error',etc on the
                  logger object.
        """
        if type not in self.ignore_log_types:
            if type == "info":
                self.log.info(message)
            elif type == "error":
                self.log.error(message)
            elif type == "warning":
                self.log.warning(message)
            elif type == "debug":
                self.log.debug(message)
            else:
                self.log.info(message)
    
    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        self.log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)
        return

    ##################################################################
    #
    def found_terminator(self):
        """
        We have come across a message terminator from the IMAP client talking
        to us.

        This is invoked in two different states:

        1) we have hit LINE_TERMINATOR and we were waiting for it.  At this
           point the buffer should contain an integer as an ascii string. This
           integer is the length of the actual message.

        2) We are reading the message itself.. we read the appropriate number
           of bytes from the channel.

        If (2) then we exit the state where we are reading the IMAP message
        from the channel and set the terminator back to LINE_TERMINATOR so that
        we can read the rest of the message from the IMAP client.
        """
        self.log.debug("found_terminator")

        if not self.reading_message:
            # We have hit our line terminator.. we should have an ascii
            # representation of an int in our buffer.. read that to determine
            # how many characters the actual IMAP message we need to read is.
            #
            try:
                msg_length = int("".join(self.ibuffer).strip())
                self.ibuffer = []
                self.log.debug("Read IMAP message length indicator: %d" % \
                                   msg_length)
                self.reading_message = True
                self.set_terminator(msg_length)
            except ValueError,e:
                self.log.error("found_terminator(): expected an int, got: "
                               "'%s'" % "".join(self.ibuffer))
            return

        # If we were reading a full IMAP message, then we switch back to
        # reading lines.
        #
        imap_msg = "".join(self.ibuffer)
        self.ibuffer = []
        self.reading_message = False
        self.set_terminator(self.LINE_TERMINATOR)

        self.log.debug("Got complete IMAP message: %s" % imap_msg)

        # Parse the IMAP message. If we can not parse it hand back a 'BAD'
        # response to the IMAP client.
        #
        try:
            imap_cmd = asimap.parse.IMAPClientCommand(imap_msg)
            imap_cmd.parse()

        except asimap.parse.BadCommand, e:
            # The command we got from the client was bad...  If we at least
            # managed to parse the TAG out of the command the client sent us we
            # use that when sending our response to the client so it knows what
            # message we had problems with.
            #
            if imap_cmd.tag is not None:
                self.push("%s BAD %s\r\n" % (imap_cmd.tag, str(e)))
            else:
                self.push("* BAD %s\r\n" % str(e))
            return

        # Message parsed successfully. Hand it off to the message processor to
        # respond to.
        #
        self.client_handler.command(imap_cmd)

        # If our state is "logged_out" after processing the command then the
        # client has logged out of the authenticated state. We need to close
        # our connection to the main server process.
        #
        if self.client_handler.state == "logged_out":
            self.log.info("Client has logged out of the subprocess")

            # Be sure to remove our entry from the server.handlers dict.
            #
            del self.server.handlers[self.port]
            
            if self.socket is not None:
                self.close()
        return

    ##################################################################
    #
    def handle_close(self):
        """
        Huh. The main server process severed its connection with us. That is a
        bit strange, but, I guess it crashed or something.
        """
        self.log.info("main server closed its connection with us.")
        if self.socket is not None:
            self.close()

        # Be sure to remove our entry from the server.handlers dict.
        #
        del self.server.handlers[self.port]
        return

##################################################################
##################################################################
#
class IMAPUserServer(asyncore.dispatcher):
    """
    Listen on a port on localhost for connections from the
    asimapd. When we get one create an IMAPUserClientHandler object that
    gets the new connection (and handles all further IMAP related
    communications with the client.)
    """

    ##################################################################
    #
    def __init__(self, options, maildir):
        """
        Setup our dispatcher.. listen on a port we are supposed to accept
        connections on. When something connects to it create an
        IMAPClientHandler and pass it the socket.

        Arguments:
        - `options` : The options set on the command line
        - `maildir` : The directory our mailspool and database are in
        """
        
        self.options = options

        asyncore.dispatcher.__init__(self)
        self.log = logging.getLogger("%s.IMAPUserServer" % __name__)

        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(("127.0.0.1", 0))
        self.address = self.socket.getsockname()
        self.listen(BACKLOG)
        self.maildir = maildir
        self.mailbox = mailbox.MH(self.maildir, create = True)
        self.db = Database(maildir)

        # The dict mapping the port connected on to specific handlers.  This is
        # how other handlers can see each other which they need to do for
        # various async updates that they may cause each other when processing
        # imap commands.
        #
        self.handlers = { }
        return

    ##################################################################
    #
    def log_info(self, message, type = "info"):
        """
        Replace the log_info method with one that uses our stderr logger
        instead of trying to write to stdout.

        Arguments:
        - `message`:
        - `type`:
        """
        if type not in self.ignore_log_types:
            if type == "info":
                self.log.info(message)
            elif type == "error":
                self.log.error(message)
            elif type == "warning":
                self.log.warning(message)
            elif type == "debug":
                self.log.debug(message)
            else:
                self.log.info(message)
    
    ##################################################################
    #
    def handle_accept(self):
        """
        A client has connected to us. Create the IMAPClientHandler object to
        handle that client and let it deal with it.
        """

        pair = self.accept()
        if pair is not None:
            sock,addr = pair
            self.log.info("Incoming connection from %s" % repr(pair))
            handler = IMAPUserClientHandler(sock, addr[1], self, self.options)
            self.handlers[addr[1]] = handler
        
