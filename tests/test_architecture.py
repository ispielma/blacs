"""What runmanager may ask of BLACS.

BLACS answers runmanager's questions and takes no orders: the gate on hardware
execution, the error that closed it, restarting a device and Abort stay with
the operator standing here.
"""
import unittest

# fixtures does the guarded import of BLACS, once, for every test module.
from fixtures import BlacsServer


class WhatBlacsWillAnswerTests(unittest.TestCase):
    def test_blacs_offers_runmanager_nothing_that_changes_it(self):
        # The base handler dispatches to any handle_<command> method, so this
        # is all a remote runmanager can reach: hello and one read-only
        # question.
        offered = [name for name in dir(BlacsServer) if name.startswith('handle_')]
        self.assertEqual(
            offered,
            ['handle_get_status', 'handle_hello'],
            'Enabling Request shots, clearing the error that stopped it, '
            'restarting a device and aborting a shot belong to the operator '
            'standing at this apparatus, and a second runmanager sharing one '
            'BLACS could otherwise stop it.',
        )


if __name__ == '__main__':
    unittest.main()
