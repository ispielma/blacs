"""The runmanager/BLACS shot exchange, with both real implementations talking.

Each repository's own tests stand in for the other side: runmanager's exchange
tests answer a fake BLACS, and BLACS's shot-loop tests answer a fake
runmanager. Neither can catch the two halves drifting apart, because each
describes the other rather than running it. blacs depends on runmanager -- the
dependency runs that way and only that way -- so this is the one place both
implementations can be put in one process and made to talk.

The seam is the ZMQ socket, and only that. ``ZMQClient.get`` is replaced with a
direct call into the other side's request handler, so both clients pack their
own requests, both servers dispatch them, and everything above that is real:
``RunManager.queue_exchange``/``apply_shot_outcome``/``offer_shot`` over a real
``QueueManager``, and ``ShotExecutor``'s real shot loop and exchange. What is
therefore not covered here is the transport itself -- sockets, serialisation,
timeouts and the two servers' own loops -- and the apparatus: the connection
table check and the device tabs, which ``process_request`` and a tabless
``BLACS`` stand in for so that a whole shot can happen without hardware.
"""
import os
import shutil
import tempfile
import threading
import types
import unittest

import labscript_utils.h5_lock  # must precede h5py, as it does in BLACS itself
import h5py
from qtutils.qt.QtWidgets import QApplication

import fixtures
import runmanager.__main__
from runmanager.__main__ import RemoteServer, RunManager
from runmanager.blacs_status import (
    BlacsStatusMonitor,
    blacs_activity_display,
    blacs_link_display,
)
from runmanager.queueing import EMPTY_QUEUE_DEFAULT_LABSCRIPT, QueueManager
import runmanager.blacs_status
import runmanager.remote

# fixtures does the guarded import of BLACS; by the time this runs the module
# is in sys.modules, so importing it again here costs nothing and warns nothing.
from fixtures import FakeUi, LoopbackExperimentServer, make_executor
import blacs.__main__

from blacs import shot_execution
from blacs.shot_execution import ShotExecutor


_qapplication = None


def ensure_qapplication():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes the Qt machinery with it.
        _qapplication = QApplication([])


# ---------------------------------------------------------------- runmanager


class FakeOutputBox(object):
    """What runmanager shows its user, which is where protocol trouble shows."""

    def __init__(self):
        self.lines = []

    def output(self, text, red=False):
        self.lines.append(text)

    def said(self, *words):
        return [line for line in self.lines if all(word in line for word in words)]


class FakeAnalysisSubmission(object):
    """Where a completed shot goes on to lyse."""

    def __init__(self):
        self.submitted = []

    def notify_shot_complete(self, path):
        self.submitted.append(path)


class RunmanagerApp(object):
    """Runmanager's exchange, over the surface of it the exchange uses.

    ``RunManager.__init__`` builds the whole application, so this holds the few
    things the exchange reaches for. The exchange methods are RunManager's own
    and the queue underneath is a real QueueManager, so the rules applied here
    are runmanager's. Only producing a default shot -- which evaluates globals
    and compiles a labscript file -- and submitting to lyse are stood in for.
    """

    queue_exchange = RunManager.queue_exchange
    apply_shot_outcome = RunManager.apply_shot_outcome
    offer_shot = RunManager.offer_shot

    def __init__(self):
        self.output_box = FakeOutputBox()
        self.queue_manager = QueueManager(
            lambda item: None,
            lambda labscript_file, path: True,
            lambda path: None,
            self.output_box.output,
            threading.Event(),
            lambda enabled: None,
        )
        self.analysis_submission = FakeAnalysisSubmission()
        self.default_shot_files = []
        self.default_shots_taken = 0

    def take_default_shot(self, labscript_file):
        self.default_shots_taken += 1
        # None once there is no prepared default shot left, which is what
        # runmanager answers while the compile it started off this thread has
        # not finished yet.
        return self.default_shot_files.pop(0) if self.default_shot_files else None

    def discard_default_shot(self):
        self.default_shot_files = []

    def rows(self):
        return self.queue_manager.controller.get_queue_display_items()


class LoopbackRemoteServer(object):
    """Runmanager's own request handling, without binding a port."""

    handler = RemoteServer.handler
    handle_queue_exchange = RemoteServer.handle_queue_exchange


class LoopbackRunmanagerClient(runmanager.remote.Client):
    """runmanager.remote.Client with the socket replaced by a direct call.

    Everything above the socket is the real client: BLACS's exchange goes
    through ``Client.queue_exchange``, which packs the request runmanager's
    server unpacks.
    """

    def __init__(self, server, on_new_pass=None):
        runmanager.remote.Client.__init__(self, host='localhost', port=0, timeout=1)
        self.server = server
        self.on_new_pass = on_new_pass
        # How many exchange replies to lose on the way back to BLACS: the
        # request reaches runmanager and changes its queue, and BLACS learns
        # nothing of what came back.
        self.replies_to_lose = 0
        # ZMQClient gives each instance its own get(), so the socket is
        # replaced here rather than overridden on the class:
        self.get = self._loopback

    def _loopback(self, port, host, data=None, timeout=None):
        if data[0] == 'hello' and self.on_new_pass is not None:
            # The shot loop checks that runmanager is there once, at the top of
            # every pass in which it has no shot in hand, so this is where a
            # test counts passes of the loop.
            self.on_new_pass()
        response = self.server.handler(data)
        if data[0] == 'queue_exchange' and self.replies_to_lose:
            self.replies_to_lose -= 1
            return None
        return response


# --------------------------------------------------------------------- BLACS


class FakeTab(object):
    """The master pseudoclock, for a shot with no devices in it."""

    def __init__(self, on_start_run):
        self.on_start_run = on_start_run

    def start_run(self, notifications):
        self.on_start_run()
        notifications.put('done')


class FakeFrontPanelSettings(object):
    def get_save_data(self):
        return {}, {}, {}, {}

    def store_front_panel_in_h5(self, hdf5_file, *args, **kwargs):
        pass


class FakeBLACS(fixtures.FakeBLACS):
    """A BLACS with no device tabs, so a shot can run without hardware."""

    def __init__(self, on_start_run):
        self.front_panel_settings = FakeFrontPanelSettings()
        self.tablist = {'pseudoclock': FakeTab(on_start_run)}


class LoopbackBlacsStatusClient(runmanager.blacs_status.Client):
    """runmanager's BLACS status client, with the socket replaced."""

    def __init__(self, server):
        runmanager.blacs_status.Client.__init__(self, host='localhost', port=0)
        self.server = server
        self.get = self._loopback

    def _loopback(self, port, host, data=None, timeout=None):
        return self.server.handler(data)


class IntegrationFixture(object):
    """One runmanager and one BLACS, wired to each other at the socket."""

    def setUp(self):
        ensure_qapplication()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

        self.runmanager = RunmanagerApp()
        self.addCleanup(self.runmanager.queue_manager.shutdown)
        self.real_runmanager_app = getattr(runmanager.__main__, 'app', None)
        runmanager.__main__.app = self.runmanager
        self.addCleanup(self.restore_runmanager_app)

        # What the apparatus does with each shot it is given, in the order the
        # shots reach it. 'completed' runs it through to the end; anything else
        # is a shot BLACS could not take up at all.
        self.outcomes = []
        # Whether the operator presses Abort while the next shot is under way.
        # Until this existed the only failure the harness could produce was a
        # rejection -- a shot that never started -- so nothing here exercised
        # the latch that a shot failing on the hardware sets.
        self.abort_next_shot = False
        self.shots_run = []
        self.while_running = []
        self.passes_left = 0
        self.executor = self.make_executor()
        self.real_blacs_app = getattr(blacs.__main__, 'app', None)
        blacs.__main__.app = types.SimpleNamespace(shot_executor=self.executor)
        self.addCleanup(self.restore_blacs_app)

        self.status_client = LoopbackBlacsStatusClient(LoopbackExperimentServer())
        self.statuses = []
        self.monitor = BlacsStatusMonitor(
            self.statuses.append, client=self.status_client
        )

        self.real_process_tree = shot_execution.process_tree
        self.real_sleep = shot_execution.time.sleep
        shot_execution.process_tree = types.SimpleNamespace(
            zlock_client=types.SimpleNamespace(set_thread_name=lambda name: None)
        )

    def tearDown(self):
        shot_execution.process_tree = self.real_process_tree
        shot_execution.time.sleep = self.real_sleep

    def restore_runmanager_app(self):
        if self.real_runmanager_app is None:
            del runmanager.__main__.app
        else:
            runmanager.__main__.app = self.real_runmanager_app

    def restore_blacs_app(self):
        if self.real_blacs_app is None:
            del blacs.__main__.app
        else:
            blacs.__main__.app = self.real_blacs_app

    def make_executor(self):
        # The plain executor, then the few things this suite needs behind it.
        executor = make_executor(
            blacs=FakeBLACS(self.on_start_run), logger_name='test.integration'
        )
        executor._manager_running = True
        executor._runmanager_request_client = LoopbackRunmanagerClient(
            LoopbackRemoteServer(), on_new_pass=self.count_pass
        )
        executor.master_pseudoclock = 'pseudoclock'
        executor.process_request = self.process_request
        return executor

    def process_request(self, h5_filepath):
        """Stand in for the connection-table check and the rerun copy.

        This is where the apparatus begins, and the only place the scripted
        outcome for a shot is applied: everything above it -- the exchange, the
        queue row, the outcome that goes back -- is the real thing.
        """
        self.shots_run.append(h5_filepath)
        outcome = self.outcomes.pop(0) if self.outcomes else 'completed'
        if outcome != 'completed':
            return None, outcome
        return h5_filepath, 'Experiment added successfully\n'

    def on_start_run(self):
        """Called while a shot is under way, before it finishes."""
        if self.abort_next_shot:
            self.abort_next_shot = False
            # What the Abort button puts there. The loop reads it in the same
            # turn as the shot finishing, and an abort takes precedence, so
            # this is a shot stopped part-way rather than one that completed.
            self.executor.notify_queue.put(['Shot Executor', 'abort'])
        self.while_running.append(
            {
                'rows': self.runmanager.rows(),
                'status': self.monitor.poll(),
            }
        )

    def make_shot_file(self, name):
        path = os.path.join(self.directory, name)
        with h5py.File(path, 'w') as h5_file:
            # A shot with no devices in it: every device loop in the shot
            # executor is then empty, so the shot runs without hardware.
            h5_file.create_group('devices')
        return path

    def count_pass(self):
        self.passes_left -= 1
        if self.passes_left <= 0:
            # The loop reads this at the top of each pass, so it still runs
            # this one -- shot and all -- to the end before stopping.
            self.executor._manager_running = False

    def run_loop(self, passes=1):
        """Run the real shot loop for a given number of passes.

        A pass is one turn of the loop with no shot in hand: an exchange, then
        whatever it produced. The loop is stopped by pass count rather than by
        running out of work, because a BLACS falling back to its local override
        shot never runs out.
        """
        self.passes_left = passes
        # A pass is counted by the exchange it makes, so a change that stops
        # BLACS exchanging would leave the loop turning for ever -- and with
        # sleep stubbed out, doing it at full speed. A test that hangs reports
        # nothing; bound the turns as well, generously enough not to cut a real
        # one short, and let the assertions say what went wrong.
        turns = [0]
        limit = 50 * max(1, passes)

        def slept(seconds):
            turns[0] += 1
            if turns[0] > limit:
                self.executor._manager_running = False

        # Rebind the name in the module under test rather than assigning to
        # the real time module's sleep: shot_execution does `import time`, so
        # that assignment replaced sleep for every thread in the interpreter.
        self.real_time = shot_execution.time
        self.addCleanup(setattr, shot_execution, 'time', self.real_time)
        shot_execution.time = types.SimpleNamespace(
            sleep=slept, time=self.real_time.time, monotonic=self.real_time.monotonic
        )
        self.executor._manager_running = True
        ShotExecutor._manage(self.executor)


class SuccessfulShotTests(IntegrationFixture, unittest.TestCase):
    """The path a shot takes when everything works.

    Enable requests, take the offer, run it, report completion, take the next.
    """

    def test_a_queued_shot_runs_is_removed_and_the_next_one_is_offered(self):
        first = self.make_shot_file('shot_a.h5')
        second = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': first, 'compiled': True}, {'path': second, 'compiled': True}]
        )
        shot_ids = [row['shot_id'] for row in self.runmanager.queue_manager.export_state()['items']]

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(
            self.shots_run, [first, second], 'both queued shots ran, in order'
        )
        self.assertEqual(
            self.runmanager.rows(), [], 'a completed shot leaves the queue'
        )
        self.assertEqual(
            [os.path.basename(path) for path in self.runmanager.analysis_submission.submitted],
            ['shot_a.h5', 'shot_b.h5'],
            'and is forwarded for analysis',
        )
        # The row BLACS is executing stays in the queue, marked running, and it
        # is the row that was offered -- the same stable id throughout.
        running = [
            [(row['path'], row['state']) for row in pass_['rows']]
            for pass_ in self.while_running
        ]
        self.assertEqual(
            running,
            [[(first, 'running'), (second, '')], [(second, 'running')]],
            'the running shot is visible in the queue and the rest waits behind it',
        )
        self.assertEqual(
            [pass_['status']['shot_id'] for pass_ in self.while_running],
            shot_ids,
            'BLACS names the id runmanager offered, unchanged, while it runs',
        )

    def test_the_completed_shot_is_retired_before_the_next_one_is_chosen(self):
        # One exchange carries the outcome and the request, and runmanager
        # applies the outcome first. If it did not, the completed row would
        # still be at the head and would be offered again.
        first = self.make_shot_file('shot_a.h5')
        second = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': first, 'compiled': True}, {'path': second, 'compiled': True}]
        )
        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(
            len(self.shots_run), 2, 'no shot was handed out twice'
        )


class LostReplyTests(IntegrationFixture, unittest.TestCase):
    """A row still marked running is offered again, and that is safe.

    The inference licensing it spans both applications and can be read from
    neither side alone: BLACS is sequential, asks for a shot only when it is
    idle, and carries the last shot's outcome on the same exchange, so a
    request that does not retire the row runmanager has marked running proves
    this BLACS is not running it.
    """

    def test_a_lost_offer_reply_costs_a_pass_and_not_the_shot(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        offered_id = self.runmanager.queue_manager.export_state()['items'][0]['shot_id']
        self.executor._runmanager_request_client.replies_to_lose = 1

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(
            self.shots_run, [shot], 'the shot ran exactly once, not never and not twice'
        )
        self.assertEqual(
            self.while_running[0]['status']['shot_id'],
            offered_id,
            'and under the id the lost offer had already given it',
        )
        self.assertTrue(
            self.runmanager.output_box.said('still marked as running', 'again'),
            'runmanager says it is handing the row out again',
        )
        self.assertEqual(self.runmanager.rows(), [])
        self.assertEqual(
            len(self.runmanager.analysis_submission.submitted),
            1,
            'and one run of it reached lyse',
        )


class FailureLatchTests(IntegrationFixture, unittest.TestCase):
    """A shot the apparatus could not finish stops it, and is retried by hand."""

    def test_an_aborted_shot_stops_requests_stays_red_and_is_retried(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        offered_id = self.runmanager.queue_manager.export_state()['items'][0]['shot_id']
        self.abort_next_shot = True

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertFalse(
            self.executor.requesting_shots,
            'the apparatus stopped mid-shot, so BLACS waits for an operator',
        )
        self.assertIn('Aborted', self.executor.local_error)
        rows = self.runmanager.rows()
        self.assertEqual(
            [(row['path'], row['state']) for row in rows],
            [(shot, 'failed'), (later, '')],
            'the shot stays at the head of the queue, in red',
        )
        self.assertIn('Aborted', rows[0]['tooltip'], 'with the reason BLACS gave')
        self.assertEqual(
            self.runmanager.analysis_submission.submitted,
            [],
            'a shot that did not run is not analysed',
        )
        self.assertEqual(self.shots_run, [shot], 'and no later shot was started')

        # Asking for shots again acknowledges the error, and runmanager offers
        # the same row back under the same id.
        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(self.shots_run, [shot, shot, later])
        self.assertEqual(
            self.while_running[0]['status']['shot_id'],
            offered_id,
            'the retry is the same queue row, under the same stable id',
        )
        self.assertEqual(self.runmanager.rows(), [])


class RejectedShotTests(IntegrationFixture, unittest.TestCase):
    """A shot BLACS cannot read is runmanager's to fix, and stops nothing.

    A file that has gone, or a connection table that does not match. Nothing
    about the apparatus is wrong, so BLACS keeps asking; runmanager holds that
    row and stops offering it, which is what keeps the two from trading the
    same refusal once a second. Stopping BLACS instead would mean somebody had
    to stand at it and start it again over a file only runmanager can put right.
    """

    def test_blacs_reports_it_and_carries_on(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        self.outcomes = ['H5 file not accessible to Control PC\n']

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertTrue(
            self.executor.requesting_shots, 'the apparatus was never the problem'
        )
        self.assertIsNone(self.executor.local_error)
        rows = self.runmanager.rows()
        self.assertEqual(
            [(row['path'], row['state']) for row in rows],
            [(shot, 'rejected'), (later, '')],
            'the row is held, and held apart from a shot that merely failed',
        )
        self.assertIn('H5 file not accessible', rows[0]['tooltip'])
        self.assertEqual(
            self.shots_run,
            [shot],
            'and it is not offered again, however many times BLACS asks',
        )

    def test_deleting_the_row_lets_the_queue_go_on(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        self.outcomes = ['H5 file not accessible to Control PC\n']
        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        rejected = self.runmanager.rows()[0]
        self.runmanager.queue_manager.delete_rows([rejected['shot_id']])
        self.run_loop(passes=2)

        self.assertEqual(self.shots_run, [shot, later])
        self.assertEqual(self.runmanager.rows(), [])

    def test_runmanager_says_what_blacs_reported(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        self.outcomes = ['Not a valid run file\n']

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertEqual(self.runmanager.rows()[0]['state'], 'rejected')
        self.assertTrue(
            self.runmanager.output_box.said('rejected', 'Not a valid run file')
        )


class PausedQueueTests(IntegrationFixture, unittest.TestCase):
    """A paused runmanager withholds work; it does not stop the apparatus."""

    def test_a_paused_queue_leaves_blacs_running_its_own_shot(self):
        queued = self.make_shot_file('shot_a.h5')
        override = self.make_shot_file('override.h5')
        self.runmanager.queue_manager.enqueue([{'path': queued, 'compiled': True}])
        self.runmanager.queue_manager.set_paused(True)
        self.executor._ui.local_override_lineEdit.setText(override)

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertEqual(
            self.shots_run,
            [override, override],
            'the apparatus keeps working on the local override shot',
        )
        self.assertTrue(
            self.executor.requesting_shots,
            'a paused runmanager is not telling this apparatus to stop',
        )
        self.assertIsNone(self.executor.local_error, 'nothing here needs attention')
        self.assertEqual(
            [(row['path'], row['state']) for row in self.runmanager.rows()],
            [(queued, '')],
            'the withheld shot is untouched, and is not marked running',
        )

    def test_a_local_override_shot_reaches_neither_runmanager_nor_lyse(self):
        # BLACS's own keepalive shot is in no runmanager queue, and a
        # runmanager user could not know it happened. Its completions must not
        # retire a row or turn up in lyse alongside work someone asked for.
        override = self.make_shot_file('override.h5')
        self.executor._ui.local_override_lineEdit.setText(override)

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertEqual(self.shots_run, [override, override])
        self.assertEqual(
            self.runmanager.analysis_submission.submitted,
            [],
            'a shot of BLACS\'s own is not analysed',
        )
        self.assertEqual(
            self.runmanager.output_box.said('completed'),
            [],
            'and runmanager was told nothing about it',
        )
        self.assertIsNone(
            self.while_running[0]['status']['shot_id'],
            'nor does BLACS claim it is running a queued shot',
        )


class StatusPullTests(IntegrationFixture, unittest.TestCase):
    """Runmanager asks BLACS what it is doing; BLACS never pushes."""

    def test_the_status_pull_follows_the_shot_blacs_is_running(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])

        self.assertEqual(
            blacs_activity_display(self.monitor.poll())[0],
            'BLACS: not requesting shots',
            'BLACS starts up not requesting shots',
        )

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        text, tooltip = blacs_activity_display(self.while_running[0]['status'])
        self.assertEqual(text, 'BLACS: running shot_a.h5')
        self.assertIn('shot_a.h5', tooltip)
        self.assertEqual(
            blacs_activity_display(self.monitor.poll())[0],
            'BLACS: requesting shots',
            'and once the shot is over it is asking for another',
        )

    def test_a_blacs_that_answers_is_online_whatever_it_is_doing(self):
        # The light beside the destination checkbox is the link, not the
        # queue: BLACS is online through all of this, including while it is
        # sitting there deliberately not asking for work.
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])

        self.assertEqual(blacs_link_display(self.monitor.poll())[0], 'online')

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertEqual(
            blacs_link_display(self.while_running[0]['status'])[0], 'online'
        )
        self.assertEqual(blacs_link_display(self.monitor.poll())[0], 'online')

    def test_the_status_pull_carries_the_reason_blacs_stopped(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        self.abort_next_shot = True

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        answer = self.monitor.poll()
        text, tooltip = blacs_activity_display(answer)
        self.assertIn('stopped', text)
        self.assertIn('Aborted', text)
        self.assertIn('Aborted', tooltip)
        self.assertEqual(
            blacs_link_display(answer)[0],
            'online',
            'an apparatus that stopped is still answering',
        )
        self.assertEqual(
            self.statuses[-1], self.monitor.poll(), 'every answer is reported'
        )


class DefaultShotTests(IntegrationFixture, unittest.TestCase):
    """Work runmanager does on a user's behalf is queue work like any other."""

    def test_a_default_shot_is_a_visible_queue_row_and_reaches_lyse(self):
        default_shot = self.make_shot_file('default_shot_0.h5')
        labscript_file = os.path.join(self.directory, 'default.py')
        open(labscript_file, 'w').close()
        self.runmanager.default_shot_files = [default_shot]
        self.runmanager.queue_manager.set_empty_queue_policy(
            EMPTY_QUEUE_DEFAULT_LABSCRIPT
        )
        self.runmanager.queue_manager.set_default_labscript_file(labscript_file)

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertEqual(self.shots_run, [default_shot])
        self.assertEqual(
            [(row['path'], row['state']) for row in self.while_running[0]['rows']],
            [(default_shot, 'running')],
            'the shot runmanager produced is visible in its queue while it runs',
        )
        self.assertTrue(
            self.while_running[0]['status']['shot_id'],
            'and it is a queued shot as far as BLACS is concerned',
        )
        self.assertEqual(
            [os.path.basename(path) for path in self.runmanager.analysis_submission.submitted],
            ['default_shot_0.h5'],
            'a completed default shot is analysed',
        )
        self.assertEqual(self.runmanager.rows(), [])


if __name__ == '__main__':
    unittest.main()
