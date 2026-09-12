"""Stand-ins shared by BLACS's tests.

``FakeUi`` is the schema of ``main.ui``'s widget names written down in Python.
It used to be written down in more than one test file, which meant a widget
could be renamed, one copy fixed, and the other left describing a window that
no longer exists -- with the suite still green. That is the one thing a
stand-in for the UI must not do, so there is one copy of it here.

``make_executor`` is here for the same reason. A field added to
``ShotExecutor`` had to be added by hand to each place a test built one, and
missing the second showed up not as a failing test but as a status server that
appeared to be offline, because the snapshot raised on the attribute that was
not there.

The two callers do not need the same executor, so this builds the plain one and
returns it for the caller to add to. What it must not do is leave a field out.
"""
import logging
import sys
import threading
import types
import warnings


class _SilentSplash(object):
    """A splash screen that is never built and never shown.

    ``blacs/__main__.py`` builds a ``Splash`` and calls ``show()`` at module
    scope, and only hides it under ``if __name__ == '__main__'``. So merely
    importing the module puts the startup banner on the screen of whoever is
    running the tests and leaves it there, because nothing in a test run
    reaches the line that takes it down.

    Standing in for the splash module before the import is what avoids that,
    and it avoids it completely: ``Splash.__init__`` is also what creates the
    ``QApplication``, so with this in place the import creates no Qt
    application at all rather than a hidden one. Tests that need a real
    QApplication build their own -- see ``test_main_window_layout.py``, which
    must also ``show()`` its window, because Qt does not lay out a widget that
    was never shown.
    """

    def __init__(self, *args, **kwargs):
        pass

    def show(self):
        pass

    def hide(self):
        pass

    def update_text(self, *args, **kwargs):
        pass


def _import_blacs_without_starting_it():
    """Import ``blacs.__main__`` for the real classes the tests borrow.

    The tests exercise BLACS's own methods rather than descriptions of them,
    which means importing the module that defines them. Importing it must not
    start the application, so the splash module is stood in for over the
    import and put back afterwards, leaving ``sys.modules`` as it was found.
    """
    fake_splash = types.ModuleType('labscript_utils.splash')
    fake_splash.Splash = _SilentSplash
    fake_splash.get_qapplication = lambda *args, **kwargs: None

    saved = sys.modules.get('labscript_utils.splash')
    sys.modules['labscript_utils.splash'] = fake_splash
    try:
        with warnings.catch_warnings():
            # Importing BLACS proper installs labscript_utils.excepthook's
            # warning logger, which logs through a deprecated call that warns
            # in turn, so any warning raised while it is installed recurses
            # until the stack runs out. Nothing here is interested in
            # import-time warnings, and catch_warnings puts the runner's own
            # handler back afterwards. Done once, here, so that every module
            # importing BLACS need not repeat it.
            warnings.simplefilter('ignore')
            import blacs.__main__
        return blacs.__main__
    finally:
        if saved is None:
            del sys.modules['labscript_utils.splash']
        else:
            sys.modules['labscript_utils.splash'] = saved


blacs_main = _import_blacs_without_starting_it()
ExperimentServer = blacs_main.ExperimentServer

from blacs.shot_execution import PublishedStatus, ShotExecutor


class FakeTextWidget(object):
    def __init__(self):
        self._text = ''

    def text(self):
        return self._text

    def setText(self, value):
        self._text = str(value)


class FakeButton(object):
    def __init__(self):
        self._checked = False
        self.clicked = types.SimpleNamespace(
            connect=lambda callback: None, disconnect=lambda callback: None
        )

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = bool(value)

    def setEnabled(self, value):
        pass


class FakeIndicator(object):
    def setPixmap(self, pixmap):
        pass

    def setToolTip(self, tooltip):
        pass


class FakeUi(object):
    """The widget names main.ui carries, and nothing behind them."""

    def __init__(self):
        self.local_override_lineEdit = FakeTextWidget()
        self.shot_request_button = FakeButton()
        self.shot_abort_button = FakeButton()
        self.shot_status = FakeTextWidget()
        self.running_shot_name = FakeTextWidget()
        self.runmanager_online = FakeIndicator()
        self.runmanager_status_label = FakeIndicator()


class FakeConfig(object):
    def getfloat(self, section, option, fallback=None):
        return fallback


class FakeBLACS(object):
    """A BLACS with nothing in it, for tests that never reach a device."""

    exp_config = FakeConfig()


def make_executor(ui=None, blacs=None, logger_name='test.shot_executor'):
    """A ShotExecutor with every field set and nothing behind it.

    Built with ``__new__`` because ``__init__`` reaches for the application.
    Every attribute the class expects is set here, so that a field added to
    ShotExecutor is added in one place and a test that forgets one fails
    loudly rather than through a snapshot that quietly raises.
    """
    executor = ShotExecutor.__new__(ShotExecutor)
    executor._ui = FakeUi() if ui is None else ui
    executor.BLACS = FakeBLACS() if blacs is None else blacs
    executor._logger = logging.getLogger(logger_name)
    executor._manager_running = False
    executor._requesting_shots = False
    executor._pending_outcome = None
    executor._current_shot_id = None
    executor._next_rep_index = {}
    executor._runmanager_request_client = None
    executor._runmanager_request_error_logged = False
    executor._runmanager_online = ''
    executor.failure_reason = None
    executor.local_error = None
    executor.published_status = PublishedStatus('', None, None)
    executor.last_opened_shots_folder = ''
    executor.master_pseudoclock = None
    # The shot loop's thread. __init__ starts it; an executor built here has no
    # loop running, and a test that wants one puts a started thread here.
    executor.manager = threading.Thread(target=lambda: None)
    return executor


class LoopbackExperimentServer(object):
    """BLACS's own request handling, without binding a port.

    The real server binds a socket in its constructor, so tests that want the
    handler call it against this instead. Borrowing the methods rather than
    describing them is the point: what is exercised is BLACS's own dispatch.
    """

    handler = ExperimentServer.handler
    handle_get_status = ExperimentServer.handle_get_status
    process = ExperimentServer.process
