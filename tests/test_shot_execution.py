"""Behavioural tests for BLACS's side of the runmanager shot exchange.

ShotExecutor.__init__ starts the shot loop and wires up real Qt widgets,
neither of which belongs in a unit test, so these build an executor without it
and give it the small surface the methods under test actually use.
"""
import logging
import types
import unittest

from blacs import shot_execution
from blacs.shot_execution import ShotExecutor


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

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = bool(value)


class FakeUi(object):
    def __init__(self):
        self.local_override_lineEdit = FakeTextWidget()
        self.shot_request_button = FakeButton()
        self.shot_status = FakeTextWidget()
        self.running_shot_name = FakeTextWidget()


class FakeConfig(object):
    def getfloat(self, section, option, fallback=None):
        return fallback


class FakeBLACS(object):
    exp_config = FakeConfig()


class FakeRunmanager(object):
    """Stands in for ShotExecutor.runmanager_rpc, recording what was sent."""

    def __init__(self, response=None, reached=True):
        self.response = response
        self.reached = reached
        self.calls = []

    def __call__(self, client_attr, error_attr, method_name, unavailable, *args, **kwargs):
        self.calls.append((method_name, args))
        return self.reached, self.response

    def sent(self, method_name):
        return [args for name, args in self.calls if name == method_name]


def make_executor():
    executor = ShotExecutor.__new__(ShotExecutor)
    executor._ui = FakeUi()
    executor.BLACS = FakeBLACS()
    executor._logger = logging.getLogger('test.shot_executor')
    executor._requesting_shots = False
    executor._pending_outcome = None
    executor._current_shot_id = None
    executor.last_opened_shots_folder = ''
    return executor


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


class ExchangeTests(unittest.TestCase):
    def test_exchange_reports_the_finished_shot_and_takes_the_next_one(self):
        executor = make_executor()
        executor._current_shot_id = 'shot-1'
        executor.report_shot_outcome('/tmp/shot_a_rep00001.h5', 'completed')
        runmanager = FakeRunmanager(offer('shot-2', '/tmp/shot_b.h5'))
        executor.runmanager_rpc = runmanager

        shot_id, path, reached = executor.exchange_with_runmanager(True)

        outcome, request_shot = runmanager.sent('queue_exchange')[0]
        self.assertEqual(outcome['shot_id'], 'shot-1')
        self.assertEqual(outcome['status'], 'completed')
        self.assertTrue(
            outcome['path'].endswith('shot_a_rep00001.h5'),
            'the outcome names the file that was actually run',
        )
        self.assertTrue(request_shot)
        self.assertEqual((shot_id, path, reached), ('shot-2', '/tmp/shot_b.h5', True))

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

        self.assertEqual(
            executor.exchange_with_runmanager(True), (None, None, False)
        )

        runmanager = FakeRunmanager(offer('shot-2', '/tmp/shot_b.h5'))
        executor.runmanager_rpc = runmanager
        executor.exchange_with_runmanager(True)
        outcome, _ = runmanager.sent('queue_exchange')[0]
        self.assertEqual(outcome['shot_id'], 'shot-1')

    def test_no_shot_offered_leaves_blacs_with_nothing_to_run(self):
        executor = make_executor()
        executor.runmanager_rpc = FakeRunmanager({'state': 'none', 'shot_id': None, 'path': None})
        self.assertEqual(
            executor.exchange_with_runmanager(True), (None, None, True)
        )

    def test_only_a_shot_runmanager_offered_has_an_outcome_to_report(self):
        executor = make_executor()
        executor.report_shot_outcome('/tmp/local_override.h5', 'completed')
        runmanager = FakeRunmanager({'state': 'none', 'shot_id': None, 'path': None})
        executor.runmanager_rpc = runmanager
        executor.exchange_with_runmanager(True)
        outcome, _ = runmanager.sent('queue_exchange')[0]
        self.assertIsNone(outcome, 'a local shot is not in runmanager\'s queue')


class ShotLoopTests(unittest.TestCase):
    """Drive the real shot loop over a fake runmanager.

    Nothing here reaches hardware: the loop is stopped after a couple of passes
    without ever being offered a shot to run.
    """

    def setUp(self):
        self.real_process_tree = shot_execution.process_tree
        self.real_sleep = shot_execution.time.sleep
        shot_execution.process_tree = types.SimpleNamespace(
            zlock_client=types.SimpleNamespace(set_thread_name=lambda name: None)
        )

    def tearDown(self):
        shot_execution.process_tree = self.real_process_tree
        shot_execution.time.sleep = self.real_sleep

    def run_loop(self, executor, passes=2):
        remaining = [passes]

        def stop_after_a_couple_of_passes(seconds):
            remaining[0] -= 1
            if remaining[0] <= 0:
                executor._manager_running = False

        shot_execution.time.sleep = stop_after_a_couple_of_passes
        ShotExecutor._manage(executor)

    def make_looping_executor(self, response):
        executor = make_executor()
        executor._manager_running = True
        runmanager = FakeRunmanager(response)
        executor.runmanager_rpc = runmanager
        return executor, runmanager

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


if __name__ == '__main__':
    unittest.main()
