"""Behavioural tests for BLACS's side of the runmanager shot exchange.

ShotExecutor.__init__ starts the shot loop and wires up real Qt widgets,
neither of which belongs in a unit test, so these build an executor without it
and give it the small surface the methods under test actually use.
"""
import os
import shutil
import tempfile
import threading
import time
import types
import unittest

import zprocess

from fixtures import FakeBLACS, FakeUi, make_executor

# fixtures does the guarded import of BLACS; by the time this runs the module
# is in sys.modules, so importing it again here costs nothing and warns nothing.
import blacs.__main__
from blacs import shot_execution
from blacs.shot_execution import ShotExecutor


class FakeRunmanager(object):
    """Stands in for ShotExecutor.runmanager_rpc, recording what was sent."""

    def __init__(self, response=None, reached=True):
        self.response = response
        self.reached = reached
        self.calls = []

    def __call__(self, client_attr, error_attr, method_name, unavailable, *args, **kwargs):
        self.calls.append((method_name, args))
        if method_name == 'queue_exchange' and not args[1]:
            # Runmanager offers nothing to an exchange that did not ask:
            return self.reached, {'state': 'none', 'shot_id': None, 'path': None}
        return self.reached, self.response

    def sent(self, method_name):
        return [args for name, args in self.calls if name == method_name]


class StatusSnapshotTests(unittest.TestCase):
    """What BLACS publishes about itself for a runmanager user to read."""

    def test_the_snapshot_says_what_blacs_is_doing_and_which_shot(self):
        executor = make_executor()
        executor.requesting_shots = True
        executor._current_shot_id = 'shot-1'
        executor.set_status('Running (program time: 0.100s)...', '/tmp/shot_a.h5')

        snapshot = executor.get_status_snapshot()

        self.assertEqual(
            snapshot,
            {
                'requesting_shots': True,
                'status': 'Running (program time: 0.100s)...',
                'shot_id': 'shot-1',
                'shot_path': '/tmp/shot_a.h5',
                'error': None,
            },
        )

    def test_the_snapshot_carries_the_reason_requests_stopped(self):
        executor = make_executor()
        executor.set_status('Aborted\nRequests stopped')
        executor.stop_requesting_shots('Aborted')

        snapshot = executor.get_status_snapshot()

        self.assertFalse(snapshot['requesting_shots'])
        self.assertEqual(snapshot['error'], 'Aborted')
        self.assertEqual(snapshot['status'], 'Aborted\nRequests stopped')
        self.assertIsNone(snapshot['shot_path'], 'no shot is running')
        self.assertIsNone(snapshot['shot_id'])

    def test_the_snapshot_stops_naming_a_shot_once_one_is_over(self):
        # Whether a shot is under way is read from the published path, so a
        # status that went on naming the last shot would have runmanager
        # showing BLACS as running for ever.
        executor = make_executor()
        executor.set_status('Running...', '/tmp/shot_a.h5')
        self.assertEqual(executor.get_status_snapshot()['shot_path'], '/tmp/shot_a.h5')

        executor.set_status('Idle')

        self.assertIsNone(executor.get_status_snapshot()['shot_path'])

    def test_the_snapshot_answers_without_the_gui_thread(self):
        # A status query arrives on the server thread and has to be answered
        # while a shot is running and the GUI thread is busy with it. No Qt
        # event loop runs here, so a snapshot that went through the GUI thread
        # would never come back at all.
        executor = make_executor()
        executor.set_status('Transitioning to buffered...', '/tmp/shot_a.h5')
        answers = []
        asker = threading.Thread(
            target=lambda: answers.append(executor.get_status_snapshot())
        )
        asker.daemon = True
        asker.start()
        asker.join(timeout=10)

        self.assertFalse(
            asker.is_alive(), 'a status query must not wait on the GUI thread'
        )
        self.assertEqual(answers[0]['status'], 'Transitioning to buffered...')


class SnapshotDuringCompletionTests(unittest.TestCase):
    """The snapshot must not describe a moment that never existed.

    The id of the shot being run was cleared when its outcome was recorded, but
    the path stays until the next status is set -- and between the two run every
    shot_complete plugin callback, lyse submission among them, for as long as
    the user's plugins take. A poll landing in there saw a path with no id,
    which runmanager reads as the one thing it cannot be: a shot from no queue.
    It told the operator their queued shot was BLACS's own local override.
    """

    def test_a_completed_queued_shot_is_still_named_while_its_callbacks_run(self):
        executor = make_executor()
        executor.requesting_shots = True
        executor._current_shot_id = 'shot-1'
        executor.set_status('Saving data...', '/tmp/shot_a.h5')

        executor.report_shot_outcome('/tmp/shot_a.h5', 'completed')
        snapshot = executor.get_status_snapshot()

        self.assertEqual(
            snapshot['shot_path'],
            '/tmp/shot_a.h5',
            'the path is still shown, which is what makes the id matter',
        )
        self.assertEqual(
            snapshot['shot_id'],
            'shot-1',
            'and it is still the queued shot it always was: no id here means '
            'BLACS is running work of its own, which would be a lie',
        )

    def test_a_local_override_shot_still_has_no_id(self):
        executor = make_executor()
        executor.requesting_shots = True
        executor.set_status('Running...', '/tmp/override.h5')

        snapshot = executor.get_status_snapshot()

        self.assertIsNone(
            snapshot['shot_id'],
            'this one really is BLACS\'s own, and runmanager should say so',
        )

    def test_going_idle_clears_both(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.set_status('Saving data...', '/tmp/shot_a.h5')

        executor.set_status('Idle')

        snapshot = executor.get_status_snapshot()
        self.assertIsNone(snapshot['shot_path'])
        self.assertIsNone(snapshot['shot_id'])


class ExecutorReadDuringWrites(ShotExecutor):
    """A ShotExecutor that reads its own snapshot after each attribute write.

    Threads switch between bytecodes, so the states another thread can catch
    this object in are exactly the ones each attribute write leaves behind.
    Reading there covers every interleaving a status query could land on
    without racing for one, and without naming the attributes, which is the
    thing under test.

    Recording starts when a list is put in ``_snapshots_seen`` behind this
    hook's back, and an executor with none is left alone: the hook is armed
    once the state under test has been set up, never before.
    """

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        seen = self.__dict__.get('_snapshots_seen')
        if seen is not None:
            seen.append(self.get_status_snapshot())


class SnapshotIsOneStatusTests(unittest.TestCase):
    """A snapshot must describe one status, not the seam between two.

    set_status writes on the GUI thread while the snapshot is read on the
    server thread, and neither takes a lock. Published as separate fields, a
    reader landing between two of the writes read a status that never existed:
    the new text beside the last shot, or the new shot's path beside the last
    shot's id -- which is one shot reported under another's name.

    Nothing schedules a reader there reliably, so this does not race for it.
    It reads from between the writes themselves, which is every state a reader
    could have caught.
    """

    def test_a_reader_between_the_writes_never_sees_two_statuses_mixed(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.set_status('Running...', '/tmp/shot_a.h5')
        before = executor.get_status_snapshot()
        executor._current_shot_id = 'shot-2'

        # Armed here, with the first status published and the second one's id
        # in place, so that what is recorded is only the writes under test:
        seen = []
        executor.__class__ = ExecutorReadDuringWrites
        object.__setattr__(executor, '_snapshots_seen', seen)
        executor.set_status('Saving data...', '/tmp/shot_b.h5')
        after = executor.get_status_snapshot()

        self.assertNotEqual(
            before, after, 'the two statuses must differ, or there is no seam'
        )
        self.assertTrue(
            seen, 'no write was observed: the status is published some other way'
        )
        for snapshot in seen:
            self.assertIn(
                snapshot,
                (before, after),
                'a status query here would have described a moment that never '
                'existed: %r' % (snapshot,),
            )


class HeldOutcomeWhenTheLoopEndsTests(unittest.TestCase):
    """An outcome BLACS is holding when the shot loop stops.

    BLACS keeps an outcome until runmanager takes it. If the loop ends while
    one is held it used to be dropped: runmanager never learned the shot
    completed, its row stayed marked running, the shot was run again, and the
    first run's file was left on disk with real data that nothing analyses.

    Development delivered it, on a bounded deadline, from a thread of its own.
    The bound is the whole point -- it is what stops a runmanager that is not
    answering holding up the quit.

    And the line that names an outcome which still could not be delivered has
    to run however the loop ended. At the tail of the loop it was skipped by
    exactly the path that stranded outcomes most reliably: an error the loop
    could not handle, which is caught a frame above it.
    """

    def setUp(self):
        self.real_raise = shot_execution.zprocess.raise_exception_in_thread
        shot_execution.zprocess.raise_exception_in_thread = lambda info: None
        self.addCleanup(
            setattr,
            shot_execution.zprocess,
            'raise_exception_in_thread',
            self.real_raise,
        )

    def executor_holding_an_outcome(self, delivers=True):
        executor = make_executor()
        executor._pending_outcome = {
            'shot_id': 'shot-1',
            'status': 'completed',
            'message': '',
        }
        self.exchanges = []

        def exchange(request_shot, timeout=None):
            self.exchanges.append((request_shot, timeout))
            if delivers:
                executor._pending_outcome = None
            return {'state': 'none', 'shot_id': None, 'path': None}, delivers

        executor.exchange_with_runmanager = exchange
        return executor

    def test_a_held_outcome_is_delivered_when_the_loop_ends(self):
        executor = self.executor_holding_an_outcome()
        executor._manage = lambda: None

        executor.manage()

        self.assertEqual(len(self.exchanges), 1, 'one last exchange')
        request_shot, timeout = self.exchanges[0]
        self.assertFalse(request_shot, 'it asks for nothing; it is only delivering')
        self.assertTrue(
            timeout and timeout <= 10,
            'and it is bounded, so a runmanager that is not answering cannot '
            'hold the quit open',
        )

    def test_it_is_delivered_even_when_the_loop_died_on_an_error(self):
        executor = self.executor_holding_an_outcome()

        def boom():
            raise RuntimeError('a device tab vanished')

        executor._manage = boom

        executor.manage()

        self.assertEqual(
            len(self.exchanges),
            1,
            'the path that strands an outcome most reliably is the one that '
            'must not skip delivering it',
        )

    def test_an_outcome_that_cannot_be_delivered_names_its_shot(self):
        executor = self.executor_holding_an_outcome(delivers=False)

        def boom():
            raise RuntimeError('a device tab vanished')

        executor._manage = boom

        with self.assertLogs('test.shot_executor', level='WARNING') as captured:
            executor.manage()

        self.assertTrue(
            any('shot-1' in line for line in captured.output),
            'whoever reads the log has to be able to tell which run was lost',
        )


class RecordingTimer(object):
    """Stands in for QTimer so the quit poll can be driven by hand.

    finalise_quit reschedules itself on a real timer, which needs an event loop
    these tests do not run. Holding the callback instead runs the real poll --
    deadline and all -- one pass at a time.
    """

    def __init__(self):
        self.scheduled = []

    def singleShot(self, msec, callback):
        self.scheduled.append(callback)


class QuittingBLACS(object):
    """BLACS's own quit poll, with no tabs and no window.

    finalise_quit is borrowed rather than described, for the reason
    LoopbackExperimentServer borrows the request handler: what has to be
    exercised is BLACS's own decision that it has finished quitting.
    """

    finalise_quit = blacs.__main__.BLACS.finalise_quit

    def __init__(self, shot_executor):
        self.shot_executor = shot_executor
        self.tablist = {}
        self.exit_complete = False


class QuitWaitsForTheFinalReportTests(unittest.TestCase):
    """Quitting BLACS while the shot loop still has an outcome in hand.

    That final report happens in the shot loop's thread, on the way out, and
    that thread is a daemon: nothing stops the process ending first and taking
    the report with it. The loop is a sleep, or a whole communication timeout,
    away from even noticing it was stopped. Calling manage() by hand cannot see
    any of this -- it returns only once the report has been made, which is
    exactly what hid it -- so these drive BLACS's own finalise_quit poll
    against a loop running in a thread of its own.
    """

    def setUp(self):
        self.timer = RecordingTimer()
        real_timer = blacs.__main__.QTimer
        blacs.__main__.QTimer = self.timer
        self.addCleanup(setattr, blacs.__main__, 'QTimer', real_timer)

    def executor_holding_an_outcome(self):
        executor = make_executor()
        executor._manager_running = True
        executor._pending_outcome = {
            'shot_id': 'shot-1',
            'status': 'completed',
            'message': '',
        }
        return executor

    def start_shot_loop(self, executor):
        """Run the real manage(), and so its real final report, in a thread."""

        def loop():
            # _manager_running rather than the property, which runs on the GUI
            # thread: there is no event loop here to run it on.
            while executor._manager_running:
                time.sleep(0.01)
            # However it was stopped, the loop takes a moment to get out:
            time.sleep(0.2)

        executor._manage = loop
        executor.manager = threading.Thread(target=executor.manage)
        executor.manager.daemon = True
        self.addCleanup(self.stop_shot_loop, executor)
        executor.manager.start()

    def stop_shot_loop(self, executor):
        executor._manager_running = False
        executor.manager.join(timeout=5)

    def quit(self, blacs_app, deadline_in=2.0):
        """Drive finalise_quit the way its timer would, until BLACS is done."""
        blacs_app.shot_executor.stop()  # what closeEvent does
        gave_up_at = time.time() + deadline_in + 2
        blacs_app.finalise_quit(time.time() + deadline_in, {})
        while not blacs_app.exit_complete:
            self.assertTrue(self.timer.scheduled, 'the quit poll stopped polling')
            self.assertLess(time.time(), gave_up_at, 'BLACS never finished quitting')
            poll = self.timer.scheduled.pop()
            time.sleep(0.02)
            poll()

    def test_a_quit_while_an_outcome_is_held_waits_for_it_to_be_delivered(self):
        executor = self.executor_holding_an_outcome()
        delivered = threading.Event()

        def exchange(request_shot, timeout=None):
            executor._pending_outcome = None
            delivered.set()
            return {'state': 'none', 'shot_id': None, 'path': None}, True

        executor.exchange_with_runmanager = exchange
        self.start_shot_loop(executor)

        self.quit(QuittingBLACS(executor))

        self.assertTrue(
            delivered.is_set(),
            'BLACS declared itself finished before the outcome it was holding '
            'reached runmanager, and the process would have ended with it',
        )

    def test_quitting_is_not_delayed_when_there_is_nothing_to_deliver(self):
        executor = self.executor_holding_an_outcome()
        executor._pending_outcome = None
        exchanges = []

        def exchange(request_shot, timeout=None):
            exchanges.append(timeout)
            return {'state': 'none', 'shot_id': None, 'path': None}, True

        executor.exchange_with_runmanager = exchange
        # Left running, and never stopped: a loop with nothing in hand is
        # nothing to wait for, whatever it is in the middle of.
        self.start_shot_loop(executor)
        blacs_app = QuittingBLACS(executor)

        blacs_app.finalise_quit(time.time() + 2, {})

        self.assertTrue(blacs_app.exit_complete, 'there was nothing to wait for')
        self.assertEqual(self.timer.scheduled, [], 'so nothing was rescheduled')
        self.assertEqual(exchanges, [], 'and nothing was reported')

    def test_a_runmanager_that_never_answers_cannot_hold_the_quit_open(self):
        executor = self.executor_holding_an_outcome()
        answering = threading.Event()

        def exchange(request_shot, timeout=None):
            # A runmanager that is not answering. The real exchange is bounded
            # by its own timeout; the quit must not have to rely on that.
            answering.wait()
            return {'state': 'none', 'shot_id': None, 'path': None}, False

        executor.exchange_with_runmanager = exchange
        self.start_shot_loop(executor)
        # Registered after the loop so it is released before it is joined:
        self.addCleanup(answering.set)
        blacs_app = QuittingBLACS(executor)

        started = time.time()
        self.quit(blacs_app, deadline_in=0.3)

        self.assertLess(
            time.time() - started,
            1.5,
            'the deadline was 0.3s, and an apparatus with a runmanager that '
            'is not answering still has to be able to close BLACS',
        )
        self.assertFalse(answering.is_set(), 'it was still stuck when we quit')

    def test_an_outcome_that_could_not_be_delivered_is_named_before_the_exit(self):
        executor = self.executor_holding_an_outcome()

        def exchange(request_shot, timeout=None):
            # Runmanager did not take it, so it is still held:
            return {'state': 'none', 'shot_id': None, 'path': None}, False

        executor.exchange_with_runmanager = exchange
        self.start_shot_loop(executor)
        blacs_app = QuittingBLACS(executor)

        with self.assertLogs('test.shot_executor', level='WARNING') as captured:
            self.quit(blacs_app)
            # Read inside the block, because when the line was written is the
            # whole point: quit() returns the moment BLACS declares itself
            # finished, and the process ends there.
            named = [line for line in captured.output if 'shot-1' in line]

        self.assertTrue(
            named, 'the run that was lost has to be named before BLACS goes'
        )


class RequestShotsControlTests(unittest.TestCase):
    def test_request_shots_is_not_part_of_saved_state(self):
        executor = make_executor()
        executor.requesting_shots = True
        self.assertEqual(
            set(executor.get_save_data()),
            {'last_opened_shots_folder', 'local_override_path'},
            'whether BLACS requests shots must not be saved',
        )

    def test_restoring_saved_state_never_starts_requesting_shots(self):
        executor = make_executor()
        executor.restore_save_data(
            {
                'manager_paused': False,
                'last_opened_shots_folder': '/tmp/shots',
                'local_override_path': '/tmp/override.h5',
            }
        )
        self.assertFalse(executor.requesting_shots)
        self.assertEqual(executor.last_opened_shots_folder, '/tmp/shots')
        self.assertEqual(
            executor._ui.local_override_lineEdit.text(), '/tmp/override.h5'
        )


def offer(shot_id, path):
    return {'state': 'shot', 'shot_id': shot_id, 'path': path}


NOTHING_OFFERED = (
    # The four ways an exchange can come back without a shot: a runmanager
    # whose queue its own user has paused, one with nothing queued -- or whose
    # next shot is still compiling, which looks the same from here -- one we
    # cannot reach at all, and one whose reply we could not make sense of.
    #
    # Each case gives what runmanager answered, whether we reached it, the
    # state the exchange reports for it, and what BLACS then says it is doing.
    (
        'a paused queue',
        {'state': 'paused', 'shot_id': None, 'path': None},
        True,
        'paused',
        'Runmanager queue paused',
    ),
    (
        'nothing to offer',
        {'state': 'none', 'shot_id': None, 'path': None},
        True,
        'none',
        'Requesting shots',
    ),
    ('an unreachable runmanager', None, False, 'none', 'Runmanager unavailable'),
    (
        'a reply we cannot read',
        'not a response at all',
        True,
        'none',
        'Requesting shots',
    ),
)


class ExchangeTests(unittest.TestCase):
    def test_exchange_reports_the_finished_shot_and_takes_the_next_one(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a_rep00001.h5', 'completed')
        runmanager = FakeRunmanager(offer('shot-2', '/tmp/shot_b.h5'))
        executor.runmanager_rpc = runmanager

        response, reached = executor.exchange_with_runmanager(True)

        outcome, request_shot = runmanager.sent('queue_exchange')[0]
        self.assertEqual(outcome['shot_id'], 'shot-1')
        self.assertEqual(outcome['status'], 'completed')
        self.assertTrue(
            outcome['path'].endswith('shot_a_rep00001.h5'),
            'the outcome names the file that was actually run',
        )
        self.assertTrue(request_shot)
        self.assertEqual(response['shot_id'], 'shot-2')
        self.assertEqual(response['path'], '/tmp/shot_b.h5')
        self.assertTrue(reached)

    def test_an_outcome_runmanager_has_taken_is_not_reported_again(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a.h5', 'completed')
        runmanager = FakeRunmanager(offer('shot-2', '/tmp/shot_b.h5'))
        executor.runmanager_rpc = runmanager

        executor.exchange_with_runmanager(True)
        executor.exchange_with_runmanager(True)

        outcomes = [outcome for outcome, _ in runmanager.sent('queue_exchange')]
        self.assertEqual([bool(outcome) for outcome in outcomes], [True, False])

    def test_an_outcome_that_did_not_get_through_rides_on_the_next_exchange(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a.h5', 'completed')
        executor.runmanager_rpc = FakeRunmanager(reached=False)

        _, reached = executor.exchange_with_runmanager(True)
        self.assertFalse(reached)

        runmanager = FakeRunmanager(offer('shot-2', '/tmp/shot_b.h5'))
        executor.runmanager_rpc = runmanager
        executor.exchange_with_runmanager(True)
        outcome, _ = runmanager.sent('queue_exchange')[0]
        self.assertEqual(outcome['shot_id'], 'shot-1')

    def test_only_runmanager_saying_it_is_paused_makes_it_paused(self):
        # An exchange that came back with no shot says which of the four ways
        # it was, so that the shot loop can say why no queued work is arriving.
        # Only runmanager's own answer may say paused: a runmanager we never
        # reached, and one whose reply we could not read, have told us nothing
        # about their queue.
        for description, answer, was_reached, state, _ in NOTHING_OFFERED:
            with self.subTest(runmanager=description):
                executor = make_executor()
                executor.runmanager_rpc = FakeRunmanager(answer, reached=was_reached)

                response, reached = executor.exchange_with_runmanager(True)

                self.assertEqual(response['state'], state)
                self.assertIsNone(response['path'], 'and no shot came with it')
                self.assertIs(reached, was_reached)

    def test_only_a_shot_runmanager_offered_has_an_outcome_to_report(self):
        executor = make_executor()
        executor.report_shot_outcome('/tmp/local_override.h5', 'completed')
        runmanager = FakeRunmanager({'state': 'none', 'shot_id': None, 'path': None})
        executor.runmanager_rpc = runmanager
        executor.exchange_with_runmanager(True)
        outcome, _ = runmanager.sent('queue_exchange')[0]
        self.assertIsNone(outcome, 'a local shot is not in runmanager\'s queue')


class ShotLoopFixture(object):
    """Drive the real shot loop over a fake runmanager.

    Nothing here reaches hardware: the loop is stopped after a couple of passes
    without ever being offered a shot it can run.
    """

    def setUp(self):
        self.real_process_tree = shot_execution.process_tree
        self.real_time = shot_execution.time
        shot_execution.process_tree = types.SimpleNamespace(
            zlock_client=types.SimpleNamespace(set_thread_name=lambda name: None)
        )

    def tearDown(self):
        shot_execution.process_tree = self.real_process_tree
        shot_execution.time = self.real_time

    def count_passes_by_sleeping(self, on_sleep):
        """Stand in for the module's ``time``, counting the loop's sleeps.

        The module does ``import time``, so ``shot_execution.time`` is the
        standard library module itself: assigning to its ``sleep`` replaced it
        for every thread in the interpreter, and any incidental sleep reached
        during a pass -- inside zlock, inside h5py, inside anything added later
        -- was counted as one. Rebinding the name in the module under test
        leaves the real module alone."""
        shot_execution.time = types.SimpleNamespace(
            sleep=on_sleep,
            time=self.real_time.time,
            monotonic=self.real_time.monotonic,
        )

    def run_loop(self, executor, passes=2):
        """Run the real loop for a number of passes, counted by its sleeps.

        No turn bound, unlike the integration fixture's loop. There a pass is
        counted by the exchange it makes, so sleeps still happen and counting
        them bounds the turns. Here the sleep *is* the counter, so a loop that
        stopped sleeping altogether would not be caught by counting sleeps, and
        the obvious alternative -- running _manage on a worker thread and
        joining with a timeout -- does not work: set_status is
        @inmain_decorator, so off the main thread with no event loop running it
        would block for ever. Left as it is deliberately rather than guarded by
        something that cannot fire."""
        remaining = [passes]

        def stop_after_a_couple_of_passes(seconds):
            remaining[0] -= 1
            if remaining[0] <= 0:
                executor._manager_running = False

        self.count_passes_by_sleeping(stop_after_a_couple_of_passes)
        ShotExecutor._manage(executor)

    def make_looping_executor(self, response, reached=True):
        executor = make_executor()
        executor._manager_running = True
        runmanager = FakeRunmanager(response, reached=reached)
        executor.runmanager_rpc = runmanager
        return executor, runmanager


class ShotLoopTests(ShotLoopFixture, unittest.TestCase):
    def test_requesting_shots_asks_runmanager_for_work(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = True
        self.run_loop(executor)
        exchanges = runmanager.sent('queue_exchange')
        self.assertTrue(exchanges, 'an enabled BLACS exchanges with runmanager')
        self.assertEqual([request_shot for _, request_shot in exchanges], [True, True])

    def test_not_requesting_shots_makes_no_exchange_at_all(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = False
        self.run_loop(executor)
        self.assertEqual(runmanager.calls, [])

    def test_the_last_outcome_still_goes_out_after_requests_are_switched_off(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = False
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a.h5', 'completed')

        self.run_loop(executor)

        exchanges = runmanager.sent('queue_exchange')
        self.assertEqual(len(exchanges), 1, 'one exchange, to report the outcome')
        outcome, request_shot = exchanges[0]
        self.assertEqual(outcome['status'], 'completed')
        self.assertFalse(request_shot, 'no further shot is asked for')

    def test_no_shot_is_taken_up_while_not_requesting_shots(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = False
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a.h5', 'completed')
        executor._ui.local_override_lineEdit.setText('/tmp/override.h5')
        taken_up = []

        def process_request(h5_filepath):
            taken_up.append(h5_filepath)
            return None, 'not a real shot file\n'

        executor.process_request = process_request

        self.run_loop(executor)

        self.assertEqual(taken_up, [], 'not even the local override shot runs')
        self.assertEqual(executor.get_status(), 'Not requesting shots')


class NoShotOfferedTests(ShotLoopFixture, unittest.TestCase):
    """A runmanager offering no shot is not telling this apparatus to stop.

    Pausing a queue is that runmanager user's policy about their own work, and
    a future second runmanager sharing this BLACS must not be able to stop the
    apparatus by pausing its queue. So a paused reply -- like an empty one, or
    no reply at all -- leaves BLACS requesting shots and running the local
    override shot that keeps the apparatus busy.
    """

    def test_no_shot_offered_never_stops_blacs_requesting_shots(self):
        for description, response, reached, _, _ in NOTHING_OFFERED:
            with self.subTest(runmanager=description):
                executor, _ = self.make_looping_executor(response, reached=reached)
                executor._requesting_shots = True

                self.run_loop(executor)

                self.assertTrue(
                    executor.requesting_shots,
                    'only this apparatus decides whether it stops',
                )
                self.assertIsNone(
                    executor.local_error, 'nothing here needs attention'
                )

    def test_no_shot_offered_falls_back_to_the_local_override_shot(self):
        for description, response, reached, _, _ in NOTHING_OFFERED:
            with self.subTest(runmanager=description):
                executor, _ = self.make_looping_executor(response, reached=reached)
                executor._requesting_shots = True
                executor._ui.local_override_lineEdit.setText('/tmp/override.h5')
                taken_up = []

                def process_request(h5_filepath):
                    # Stop short of running it: what is under test is that
                    # BLACS got as far as taking the fallback shot up.
                    taken_up.append(h5_filepath)
                    return None, 'not a real shot file\n'

                executor.process_request = process_request
                self.run_loop(executor)

                self.assertTrue(
                    taken_up, 'the apparatus keeps working on its local shot'
                )
                self.assertTrue(taken_up[0].endswith('override.h5'))

    def test_status_says_why_no_queued_work_is_arriving(self):
        for description, response, reached, _, status in NOTHING_OFFERED:
            with self.subTest(runmanager=description):
                executor, _ = self.make_looping_executor(response, reached=reached)
                executor._requesting_shots = True

                self.run_loop(executor)

                self.assertEqual(executor.get_status(), status)


class LocalFallbackTests(ShotLoopFixture, unittest.TestCase):
    """The local override shot keeps the apparatus busy; it is not lab work.

    It is BLACS's own, run because the configured runmanager had nothing to
    offer, so it belongs to no runmanager queue row and no runmanager user
    could know it happened. Its repetitions must not turn up in lyse alongside
    shots someone actually asked for -- but each repetition still gets its own
    file, so the data it did produce is never overwritten.
    """

    def test_a_completed_fallback_shot_is_not_reported_to_runmanager(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = True
        executor._ui.local_override_lineEdit.setText('/tmp/override.h5')
        # BLACS ran a runmanager shot on the pass before this one. That shot's
        # id must not still be attached when the fallback shot completes, or
        # runmanager would retire that row on the strength of a shot it never
        # offered, and send this fallback file to lyse in its place.
        executor._current_shot_id = 'shot-1'
        outcomes_after_completion = []

        def process_request(h5_filepath):
            # The loop has taken the fallback shot up. Complete it the way the
            # loop does once the devices are back in manual mode, then stop
            # short of the apparatus.
            executor.report_shot_outcome(h5_filepath, 'completed')
            outcomes_after_completion.append(executor._pending_outcome)
            return None, 'stopping short of the apparatus\n'

        executor.process_request = process_request
        self.run_loop(executor)

        self.assertEqual(
            outcomes_after_completion,
            [None],
            'a fallback shot has no outcome, so nothing reaches runmanager or lyse',
        )
        self.assertEqual(
            [outcome for outcome, _ in runmanager.sent('queue_exchange')],
            [None],
            'runmanager was told nothing about a shot of BLACS\'s own',
        )

    def test_each_fallback_repetition_gets_a_file_of_its_own(self):
        # The same override file is run over and over, and each run's data is
        # written to a fresh numbered copy rather than over the last one.
        executor = make_executor()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        override = os.path.join(directory, 'override.h5')
        open(override, 'w').close()

        repetitions = []
        for _ in range(3):
            path, repeat_number = executor.new_rep_name(override)
            self.assertFalse(
                os.path.exists(path), 'a repetition never lands on existing data'
            )
            open(path, 'w').close()
            repetitions.append((path, repeat_number))

        paths = [path for path, _ in repetitions]
        self.assertEqual(len(set(paths)), 3, 'each repetition is its own file')
        self.assertNotIn(override, paths, 'and none of them is the override itself')
        self.assertEqual([number for _, number in repetitions], [1, 2, 3])


def failing_manage():
    raise RuntimeError('the shot loop fell over')


class LocalErrorLatchTests(ShotLoopFixture, unittest.TestCase):
    """A shot that does not complete stops requests until an operator says go.

    The apparatus-side failures behind most of these -- a programming timeout,
    a device restart, a device error, an error during the run or the cleanup
    after it -- happen deep inside the shot loop and need real device tabs to
    reach, so the transition they share is exercised directly. The two
    reachable ones, a rejected runmanager shot and a rejected local override
    shot, are driven through the loop itself.
    """

    def test_a_shot_blacs_cannot_read_is_reported_without_stopping(self):
        # The shot is unusable, not the apparatus. Stopping here would need
        # somebody standing at this machine to start it again over a file that
        # is runmanager's to fix -- and a runmanager user watching from
        # elsewhere could not. So the outcome goes back and requests stay on;
        # runmanager holds that row and stops offering it, so the next exchange
        # simply brings nothing.
        executor, runmanager = self.make_looping_executor(
            offer('shot-1', '/tmp/shot_a.h5')
        )
        executor._requesting_shots = True
        executor.process_request = lambda path: (
            None,
            'H5 file not accessible to Control PC\n',
        )

        self.run_loop(executor)

        self.assertTrue(
            executor.requesting_shots,
            'a shot runmanager cannot supply is not a reason to stop the apparatus',
        )
        self.assertIsNone(executor.local_error, 'and nothing here needs attention')
        exchanges = runmanager.sent('queue_exchange')
        self.assertEqual(
            [request_shot for _, request_shot in exchanges],
            [True, True],
            'so the next exchange asks for work as usual',
        )
        outcome = exchanges[1][0]
        self.assertEqual(outcome['shot_id'], 'shot-1')
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('H5 file not accessible', outcome['message'])

    def test_requesting_shots_again_clears_the_error_and_asks_for_work(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor.stop_requesting_shots('Device(s) in error state')
        self.assertFalse(executor.requesting_shots)

        executor.requesting_shots = True

        self.assertIsNone(
            executor.local_error, 'requesting shots again acknowledges the error'
        )
        # FakeBLACS has no device tabs at all, so asking for work again cannot
        # have consulted them: recovery is one action, and the per-device check
        # when the shot is programmed stays the final authority.
        self.run_loop(executor)
        self.assertEqual(
            [request_shot for _, request_shot in runmanager.sent('queue_exchange')],
            [True, True],
        )

    def test_a_local_override_shot_that_cannot_run_stops_requests_silently(self):
        executor, runmanager = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = True
        executor._ui.local_override_lineEdit.setText('/tmp/override.h5')
        executor.process_request = lambda path: (None, 'Not a valid run file\n')

        self.run_loop(executor)

        self.assertFalse(executor.requesting_shots)
        self.assertIn('Not a valid run file', executor.local_error)
        self.assertIn(
            'Rejected local override shot',
            executor.get_status(),
            'a fallback shot that needs attention says so here too',
        )
        self.assertIsNone(executor._pending_outcome)
        self.assertEqual(
            [outcome for outcome, _ in runmanager.sent('queue_exchange')],
            [None],
            'a local override shot has no row in runmanager to report against',
        )

    def test_a_shot_loop_that_dies_stops_requests_and_says_why(self):
        executor = make_executor()
        executor._requesting_shots = True
        executor._manage = failing_manage
        shot_execution.zprocess = types.SimpleNamespace(
            raise_exception_in_thread=lambda info: None
        )
        try:
            ShotExecutor.manage(executor)
        finally:
            shot_execution.zprocess = zprocess

        self.assertFalse(executor.requesting_shots)
        self.assertIn('Shot execution stopped', executor.local_error)


class StatusWhenRequestsStopTests(ShotLoopFixture, unittest.TestCase):
    """What the status says once requests are switched off.

    It is the only reading an operator gets of the Request shots button beyond
    the button itself, so it has to be right whatever BLACS was saying before,
    and it must not bury a reason that requests stopped.
    """

    def run_changing_it_between_passes(self, executor, change):
        """Run the loop, calling ``change`` after the first pass.

        The change has to land between two passes of the same loop, because
        that is where it happens: a shot fails, or an operator unticks the
        button, while BLACS is running. Starting a second loop instead would
        not test it -- _manage sets the status afresh when it starts.
        """
        passes = [0]

        def at_the_end_of_a_pass(seconds):
            passes[0] += 1
            if passes[0] == 1:
                change()
            else:
                executor._manager_running = False

        self.count_passes_by_sleeping(at_the_end_of_a_pass)
        ShotExecutor._manage(executor)

    def test_whatever_was_on_screen_gives_way_to_not_requesting_shots(self):
        for description, response, reached, _, first_status in NOTHING_OFFERED:
            with self.subTest(started_from=description):
                executor, _ = self.make_looping_executor(response, reached=reached)
                executor._requesting_shots = True
                shown = []

                def stop_requesting():
                    shown.append(executor.get_status())
                    executor._requesting_shots = False

                self.run_changing_it_between_passes(executor, stop_requesting)

                self.assertEqual(shown, [first_status], 'what was on screen')
                self.assertEqual(
                    executor.get_status(),
                    'Not requesting shots',
                    '%r is something BLACS has stopped doing' % first_status,
                )

    def test_a_reason_requests_stopped_is_not_written_over(self):
        executor, _ = self.make_looping_executor(
            {'state': 'none', 'shot_id': None, 'path': None}
        )
        executor._requesting_shots = True

        def fail_the_way_a_shot_does():
            executor.stop_requesting_shots('Device(s) in error state')
            executor.set_status('Device(s) in error state\nRequests stopped')

        self.run_changing_it_between_passes(executor, fail_the_way_a_shot_does)

        self.assertIn(
            'Device(s) in error state',
            executor.get_status(),
            'the reason is all an operator has to act on',
        )


if __name__ == '__main__':
    unittest.main()
