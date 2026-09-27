#####################################################################
#                                                                   #
# /blacs/client.py                                                  #
#                                                                   #
# Copyright 2026, JQI                                               #
# Author: Ian Spielman                                              #
#                                                                   #
# This file is part of blacs, in the labscript suite                #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""The client through which other programs reach BLACS's server."""

from labscript_utils.ls_zprocess import ZMQClient


class BlacsClient(ZMQClient):
    """A client of a BLACS server.

    BLACS answers questions and takes no orders: its server offers ``hello``
    and ``get_status``, and nothing that changes what the apparatus does.
    """

    server = 'blacs'
    default_port = 42517

    def get_status(self):
        """Return what BLACS is doing.

        Returns
        -------
        dict
            ``requesting_shots``, whether BLACS is asking runmanager for shots;
            ``status``, the text of its status display; ``shot_id`` and
            ``shot_path`` of the shot it is running, the path
            shared-drive-agnostic, ``shot_id`` None for a local override shot
            and both None when no shot is running; and ``error``, why BLACS
            stopped requesting shots, or None.
        """
        return self.request('get_status')
