"""Behavioural tests for the status BLACS serves to a remote runmanager.

BLACS already had a server, so these exercise ExperimentServer's handler over
the same surface a runmanager reaches it through. The server itself binds a
socket in its constructor, so the handler is called against a stand-in the way
the runmanager tests call RunManager's own methods.
"""
import unittest
import warnings

with warnings.catch_warnings():
    # Importing BLACS proper installs labscript_utils.excepthook's warning
    # logger, which logs through a deprecated call that warns in turn, so any
    # warning raised while it is installed recurses until the stack runs out.
    # Nothing here is interested in import-time warnings; catch_warnings puts
    # the runner's own handler back afterwards.
    warnings.simplefilter('ignore')
    from blacs.__main__ import ExperimentServer
    import blacs.__main__


class FakeShotExecutor(object):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.requesting_shots = False
        self.local_error = 'Aborted'

    def get_status_snapshot(self):
        return dict(self.snapshot)


class FakeBLACS(object):
    def __init__(self, snapshot):
        self.shot_executor = FakeShotExecutor(snapshot)


class FakeExperimentServer(object):
    """ExperimentServer's own request handling, without binding a port."""

    handler = ExperimentServer.handler
    handle_get_status = ExperimentServer.handle_get_status
    process = ExperimentServer.process


SNAPSHOT = {
    'requesting_shots': True,
    'status': 'Running (program time: 0.100s)...',
    'shot_id': 'shot-1',
    'shot_path': '/tmp/shot_a.h5',
    'error': None,
}


class StatusServerTests(unittest.TestCase):
    def setUp(self):
        self.blacs = FakeBLACS(SNAPSHOT)
        self.real_app = getattr(blacs.__main__, 'app', None)
        blacs.__main__.app = self.blacs
        self.server = FakeExperimentServer()

    def tearDown(self):
        if self.real_app is None:
            del blacs.__main__.app
        else:
            blacs.__main__.app = self.real_app

    def request(self, command, *args, **kwargs):
        return self.server.handler([command, args, kwargs])

    def test_a_status_request_gets_what_blacs_is_doing(self):
        self.assertEqual(self.request('get_status'), SNAPSHOT)

    def test_blacs_answers_hello_so_runmanager_can_see_it_is_there(self):
        self.assertEqual(self.request('hello'), 'hello')

    def test_a_direct_shot_submission_is_still_refused(self):
        # The old callers sent a bare filepath, and still get told that BLACS
        # takes its shots from a runmanager queue now rather than being handed
        # them. Only the new [command, args, kwargs] shape is dispatched.
        message = self.server.handler('/tmp/shot_a.h5')
        self.assertIn('no longer accepts direct shot submissions', message)

    def test_an_unknown_command_comes_back_as_an_error(self):
        response = self.request('make_the_tea')
        self.assertIsInstance(
            response, Exception, 'the server answers rather than dying'
        )

    # That the server offers nothing which changes BLACS is the boundary rule
    # rather than a fact about this server, so it is enforced in
    # test_architecture.py alongside the other half of it.


if __name__ == '__main__':
    unittest.main()


class CloseBeforeTheServerExistsTests(unittest.TestCase):
    """Closing BLACS while it is still starting.

    The experiment server is started after BLACS itself, deliberately: one
    started earlier would spend the connection table load telling a runmanager
    that this BLACS had failed rather than that it was still starting. But the
    main window is shown partway through that startup, and it goes on for a
    while afterwards building device tabs and restoring tab positions.

    The close handler shuts the server down unconditionally, so a close
    arriving in that window raised NameError inside a Qt event handler -- after
    the handler had already set the exiting flag, which is what stops it running
    again. The result was a window that could not be closed at all.
    """

    def test_the_server_name_exists_before_the_server_does(self):
        self.assertIn(
            'experiment_server',
            vars(blacs.__main__),
            'the close handler reads this as a module global, so it has to '
            'resolve from the moment a window exists to be closed',
        )
        self.assertIsNone(
            blacs.__main__.experiment_server,
            'and it says there is no server yet rather than being absent',
        )
