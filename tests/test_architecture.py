"""BLACS's half of the guard on the runmanager/BLACS boundary.

The same two rules the runmanager half enforces, from this side, plus the one
check neither repository can make alone. They are about what the two
applications may say to each other and nothing else: renaming a helper or
moving a class must not fail a test in this file.

  1. BLACS asks runmanager for nothing but the one exchange -- and every
     command it does ask for exists on runmanager's client. blacs depends on
     runmanager, so this is the only place the two command surfaces can be
     compared; runmanager cannot import blacs to do it from there.

  2. BLACS answers runmanager's questions and takes no orders. The gate on
     hardware execution, the error that closed it, restarting a device and
     Abort stay with the operator standing here.

Runmanager's half -- what its own client and server offer -- is in
``runmanager/tests/test_architecture.py``, so that a runmanager-only test run
still fails on a runmanager regression.
"""
import ast
import os
import unittest
import warnings

import runmanager.remote

with warnings.catch_warnings():
    # See test_status_server: importing BLACS proper installs a warning logger
    # that warns as it logs, so any warning raised while it is installed
    # recurses until the stack runs out.
    warnings.simplefilter('ignore')
    from blacs.__main__ import ExperimentServer

from blacs import shot_execution

# The commands that carried the old handoff: BLACS asked for a shot, said it
# had taken it, said it would not, and reported the result on a channel of its
# own. queue_exchange does all four now. See ISSUES_PRD.md.
SUPERSEDED_COMMANDS = (
    'queue_request_next',
    'shot_accepted',
    'shot_rejected',
    'notify_shot_complete',
)


def runmanager_methods_blacs_calls():
    """The runmanager client methods the shot loop asks for, by name.

    Every request to runmanager goes through runmanager_rpc(), which takes the
    method name as its third argument, so the names are read out of the source
    rather than guessed from what the module mentions.
    """
    source = open(shot_execution.__file__).read()
    called = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'runmanager_rpc':
            continue
        if len(node.args) > 2 and isinstance(node.args[2], ast.Constant):
            called.add(node.args[2].value)
    return called


def fail(rule, detail):
    raise AssertionError(
        '%s\n\nThe architecture guard in %s enforces this. See the module '
        'docstring there, and the "Synchronising with runmanager" section of '
        'docs/source/shot-management.rst.\n\n%s'
        % (rule, os.path.basename(__file__), detail)
    )


class WhatBlacsAsksRunmanagerForTests(unittest.TestCase):
    def test_blacs_asks_for_nothing_from_the_superseded_protocol(self):
        back = sorted(runmanager_methods_blacs_calls() & set(SUPERSEDED_COMMANDS))
        if back:
            fail(
                'BLACS calls the superseded shot-handoff method(s) %s again.'
                % ', '.join(back),
                'A shot is offered, reported on and requested through one '
                'queue_exchange, which is what lets runmanager apply the '
                'outcome before choosing the next shot. That ordering is what '
                'makes reoffering a row still marked running sound, so a '
                'second route round it reintroduces a shot being handed out '
                'twice.',
            )

    def test_every_command_blacs_sends_is_one_runmanager_offers(self):
        # The two halves of this protocol live in two repositories and are
        # released separately, so this is where they are compared. A rename on
        # either side that is not made on the other shows up as BLACS asking
        # for something runmanager does not have.
        called = runmanager_methods_blacs_calls()
        missing = sorted(
            name for name in called if not hasattr(runmanager.remote.Client, name)
        )
        if missing:
            fail(
                'BLACS asks runmanager for %s, which runmanager.remote.Client '
                'does not offer.' % ', '.join(missing),
                'The two applications have drifted apart: at run time BLACS '
                'would report runmanager as unavailable and fall back to its '
                'local override shot indefinitely, with nothing saying why.',
            )
        self.assertEqual(
            called,
            {'say_hello', 'queue_exchange'},
            'BLACS reaches runmanager only to check it is there and to '
            'exchange one shot; anything else is a second protocol',
        )


class WhatBlacsWillAnswerTests(unittest.TestCase):
    def test_blacs_offers_runmanager_nothing_that_changes_it(self):
        # ExperimentServer.handler dispatches whatever handle_<command> method
        # it finds, so the inventory of those methods is the whole surface a
        # remote runmanager can reach. One read-only question, and no more.
        offered = [name for name in dir(ExperimentServer) if name.startswith('handle_')]
        if offered != ['handle_get_status']:
            fail(
                'BLACS\'s server now offers %s.' % ', '.join(offered),
                'Only get_status may be served. Enabling Request shots, '
                'clearing the error that stopped it, restarting a device and '
                'aborting a shot belong to the operator standing at this '
                'apparatus: a runmanager able to do any of them remotely would '
                'take back the ownership boundary this branch drew, and a '
                'second runmanager sharing one BLACS could then stop it. '
                'Answering what BLACS is doing stays allowed -- that is what '
                'get_status is for.',
            )

    def test_a_superseded_command_is_not_among_them(self):
        back = sorted(
            command
            for command in SUPERSEDED_COMMANDS
            if hasattr(ExperimentServer, 'handle_' + command)
        )
        self.assertEqual(
            back, [], 'the old handoff must not come back through BLACS\'s server'
        )


if __name__ == '__main__':
    unittest.main()
