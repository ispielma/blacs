#####################################################################
#                                                                   #
# /experiment_queue.py                                              #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import datetime
import logging
import os
import queue
import shutil
import sys
import threading
import time
from binascii import hexlify
from collections import defaultdict
from tempfile import gettempdir

from qtutils.qt.QtCore import Qt, QItemSelectionModel
from qtutils.qt.QtGui import QIcon
from qtutils.qt.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QTreeView,
)

import zprocess
from labscript_utils.ls_zprocess import ProcessTree
process_tree = ProcessTree.instance()
import labscript_utils.h5_lock, h5py

from qtutils import inmain_decorator, inmain

from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.connections import ConnectionTable
import labscript_utils.properties
from labscript_utils.shared_drive import path_to_agnostic, path_to_local

from blacs.tab_base_classes import (
    MODE_BUFFERED,
    MODE_TRANSITION_TO_BUFFERED,
)
import blacs.plugins as plugins

try:
    import runmanager.remote as runmanager_remote
except Exception:
    runmanager_remote = None


process_tree = ProcessTree.instance()


def tempfilename(prefix='BLACS-temp-', suffix='.h5'):
    """Return a filepath appropriate for use as a temporary file."""
    random_hex = hexlify(os.urandom(16)).decode()
    return os.path.join(gettempdir(), prefix + random_hex + suffix)


FILEPATH_COLUMN = 0


class QueueTreeview(QTreeView):
    def __init__(self, *args, **kwargs):
        QTreeView.__init__(self, *args, **kwargs)
        self.header().setStretchLastSection(True)
        self.setAutoScroll(False)
        self.add_to_queue = None
        self.delete_selection = None
        self._logger = logging.getLogger('BLACS.ShotExecutor')

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            event.accept()
            if self.delete_selection:
                self.delete_selection()
        QTreeView.keyPressEvent(self, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.CopyAction)
            event.accept()
            for url in event.mimeData().urls():
                path = str(url.toLocalFile())
                if path.endswith('.h5') or path.endswith('.hdf5'):
                    self._logger.info('Acceptable file dropped. Path is %s', path)
                    if self.add_to_queue:
                        self.add_to_queue(str(path))
                    else:
                        self._logger.info(
                            'Dropped file not added because add_to_queue is unavailable'
                        )
                else:
                    self._logger.info('Invalid file dropped. Path was %s', path)
        else:
            event.ignore()


class QueueManager(object):
    """BLACS shot executor.

    BLACS no longer owns an execution queue. Instead, when idle it requests the
    next shot from runmanager. The only BLACS-managed shot state is a single
    local override shot used for direct loading and local retry handling.
    """

    REPEAT_ALL = 0
    REPEAT_LAST = 1

    ICON_REPEAT_LAST = ':qtutils/fugue/arrow-repeat-once'

    SOURCE_LOCAL_OVERRIDE = 'local_override'
    SOURCE_RUNMANAGER = 'runmanager_queue'
    SOURCE_FALLBACK_REPEAT = 'fallback_repeat'

    def __init__(self, BLACS, ui):
        self._ui = ui
        self.BLACS = BLACS
        self.last_opened_shots_folder = BLACS.exp_config.get(
            'paths', 'experiment_shot_storage'
        )
        self._manager_running = True
        self._manager_paused = False
        self._manager_repeat = False
        self._manager_repeat_mode = self.REPEAT_LAST
        self.master_pseudoclock = self.BLACS.connection_table.master_pseudoclock

        self.current_queue = queue.Queue()
        self.current_shot_path = None
        self.current_shot_source_kind = None
        self.last_completed_shot = None
        self.last_completed_shot_ignore_repeat = False
        self._local_override_path = None
        self._updating_local_override_text = False

        self._logger = logging.getLogger('BLACS.ShotExecutor')
        self._runmanager_client = None
        self._runmanager_notify_client = None
        self._runmanager_comm_error_logged = False
        self._runmanager_notify_error_logged = False
        self._pending_completion_notifications = []
        self._pending_completion_notifications_lock = threading.Lock()
        self._completion_buffer_signal_queue = queue.Queue()

        self._ui.queue_pause_button.toggled.connect(self._toggle_pause)
        self._ui.queue_repeat_button.toggled.connect(self._toggle_repeat)
        self._ui.actionAdd_to_queue.triggered.connect(self.on_add_shots_triggered)
        self._ui.local_override_browse_button.setDefaultAction(
            self._ui.actionAdd_to_queue
        )
        self._ui.local_override_lineEdit.editingFinished.connect(
            self._on_local_override_editing_finished
        )
        self._ui.local_override_lineEdit.textChanged.connect(
            self._on_local_override_text_changed
        )

        self._ui.repeat_mode_select_button.setEnabled(False)
        self._ui.repeat_mode_select_button.hide()
        self._ui.queue_repeat_button.setToolTip(
            'Repeat the last completed shot if runmanager is empty or unreachable'
        )

        elide_label(
            self._ui.queue_status, self._ui.queue_status_verticalLayout, Qt.ElideRight
        )
        elide_label(
            self._ui.running_shot_name,
            self._ui.queue_status_verticalLayout,
            Qt.ElideLeft,
        )
        self.manager_repeat_mode = self.REPEAT_LAST
        self._sync_local_override_widgets()
        self._update_notify_buffer_count()

        self.manager = threading.Thread(target=self.manage)
        self.manager.daemon = True
        self.manager.start()
        self._completion_notifier = threading.Thread(
            target=self._completion_notification_mainloop
        )
        self._completion_notifier.daemon = True
        self._completion_notifier.start()

    def get_save_data(self):
        return {
            'manager_paused': self.manager_paused,
            'manager_repeat': self.manager_repeat,
            'manager_repeat_mode': self.manager_repeat_mode,
            'last_opened_shots_folder': self.last_opened_shots_folder,
            'local_override_path': self.get_local_override(),
        }

    def restore_save_data(self, data):
        if 'manager_paused' in data:
            self.manager_paused = data['manager_paused']
        if 'manager_repeat' in data:
            self.manager_repeat = data['manager_repeat']
        if 'manager_repeat_mode' in data:
            self.manager_repeat_mode = data['manager_repeat_mode']
        if 'last_opened_shots_folder' in data:
            self.last_opened_shots_folder = data['last_opened_shots_folder']
        if 'local_override_path' in data and data['local_override_path']:
            self.process_request(str(data['local_override_path']))
        legacy_files = list(data.get('files_queued', []))
        if legacy_files:
            self._logger.info(
                'Dropping restored BLACS queue contents and keeping only the first legacy shot as a local override'
            )
            self.process_request(str(legacy_files[0]))
        if data.get('pending_completion_notifications'):
            self._logger.info(
                'Dropping restored completed-shot notification buffer; BLACS no longer persists it across restarts'
            )

    @property
    @inmain_decorator(True)
    def manager_running(self):
        return self._manager_running

    @manager_running.setter
    @inmain_decorator(True)
    def manager_running(self, value):
        self._manager_running = bool(value)
        if not self._manager_running:
            try:
                self._completion_buffer_signal_queue.put(['close', None])
            except Exception:
                pass

    def _toggle_pause(self, checked):
        self.manager_paused = checked

    @property
    @inmain_decorator(True)
    def manager_paused(self):
        return self._manager_paused

    @manager_paused.setter
    @inmain_decorator(True)
    def manager_paused(self, value):
        value = bool(value)
        self._manager_paused = value
        if value != self._ui.queue_pause_button.isChecked():
            self._ui.queue_pause_button.setChecked(value)

    def _toggle_repeat(self, checked):
        self.manager_repeat = checked

    @property
    @inmain_decorator(True)
    def manager_repeat(self):
        return self._manager_repeat

    @manager_repeat.setter
    @inmain_decorator(True)
    def manager_repeat(self, value):
        value = bool(value)
        self._manager_repeat = value
        if value != self._ui.queue_repeat_button.isChecked():
            self._ui.queue_repeat_button.setChecked(value)

    @property
    @inmain_decorator(True)
    def manager_repeat_mode(self):
        return self._manager_repeat_mode

    @manager_repeat_mode.setter
    @inmain_decorator(True)
    def manager_repeat_mode(self, value):
        # Queue repeat modes no longer apply. Keep the compatibility property and
        # pin it to repeat-last semantics for legacy settings and plugins.
        self._manager_repeat_mode = self.REPEAT_LAST
        self._ui.queue_repeat_button.setIcon(QIcon(self.ICON_REPEAT_LAST))

    def _on_local_override_text_changed(self, text):
        if self._updating_local_override_text:
            return
        text = str(text).strip()
        self._ui.local_override_lineEdit.setToolTip(text)
        if not text:
            self._local_override_path = None

    def _on_local_override_editing_finished(self):
        text = str(self._ui.local_override_lineEdit.text()).strip()
        if not text:
            self.clear_local_override()
            return
        current_override = self.get_local_override()
        if text == current_override:
            return
        message = self.process_request(text)
        if not message.startswith('Local override shot loaded successfully'):
            QMessageBox.warning(self._ui, 'BLACS', message)
            self._sync_local_override_widgets()

    def on_add_shots_triggered(self):
        shot_file = QFileDialog.getOpenFileName(
            self._ui,
            'Select shot file',
            self.last_opened_shots_folder,
            'HDF5 files (*.h5 *.hdf5)',
        )
        if isinstance(shot_file, tuple):
            shot_file, _ = shot_file
        shot_file = str(shot_file)
        if not shot_file:
            return

        shot_file = os.path.abspath(shot_file)
        self.last_opened_shots_folder = os.path.dirname(shot_file)
        self.process_request(shot_file)

    def _delete_selected_items(self):
        self.clear_local_override()

    @inmain_decorator(True)
    def clear_local_override(self):
        self._local_override_path = None
        self._sync_local_override_widgets()

    @inmain_decorator(True)
    def _set_local_override(self, h5_filepath):
        self._local_override_path = str(h5_filepath)
        self._sync_local_override_widgets()

    @inmain_decorator(True)
    def _sync_local_override_widgets(self):
        text = self._local_override_path or ''
        self._updating_local_override_text = True
        try:
            if self._ui.local_override_lineEdit.text() != text:
                self._ui.local_override_lineEdit.setText(text)
            self._ui.local_override_lineEdit.setToolTip(text)
        finally:
            self._updating_local_override_text = False

    @inmain_decorator(True)
    def get_local_override(self):
        return self._local_override_path

    def _get_local_override_fallback(self):
        h5_filepath = self.get_local_override()
        if h5_filepath is None:
            return None
        try:
            return self._make_repeat_copy(h5_filepath, 'local_override')
        except Exception:
            self._logger.exception(
                'Failed to create local override fallback copy of %s', h5_filepath
            )
            return None

    @inmain_decorator(True)
    def append(self, h5files):
        if not h5files:
            return
        self._set_local_override(str(h5files[-1]))

    @inmain_decorator(True)
    def prepend(self, h5file):
        self._set_local_override(str(h5file))

    @inmain_decorator(wait_for_return=True)
    def is_in_queue(self, path):
        current = self.get_local_override()
        return current == path

    def _validate_connection_table(self, h5_filepath):
        try:
            new_conn = ConnectionTable(h5_filepath, logging_prefix='BLACS')
        except Exception:
            return False, "H5 file not accessible to Control PC\n"
        result, error = inmain(self.BLACS.connection_table.compare_to, new_conn)
        if result:
            return True, None
        message = (
            "Connection table of your file is not a subset of the experimental control apparatus.\n"
            "You may have:\n"
            "    Submitted your file to the wrong control PC\n"
            "    Added new channels to your h5 file, without rewiring the experiment and updating the control PC\n"
            "    Renamed a channel at the top of your script\n"
            "    Submitted an old file, and the experiment has since been rewired\n"
            "\n"
            "Please verify your experiment script matches the current experiment configuration, and try again\n"
            "The error was %s\n" % error
        )
        return False, message

    def _file_has_data(self, h5_filepath):
        with h5py.File(h5_filepath, 'r') as h5_file:
            return 'data' in h5_file['/']

    def _get_repeat_number(self, h5_filepath):
        with h5py.File(h5_filepath, 'r') as h5_file:
            return int(h5_file.attrs.get('run repeat', 0))

    def process_request(self, h5_filepath):
        h5_filepath = os.path.abspath(str(h5_filepath))
        result, message = self._validate_connection_table(h5_filepath)
        if not result:
            return message

        rerun = False
        try:
            rerun = self._file_has_data(h5_filepath)
        except Exception:
            return "H5 file not accessible to Control PC\n"

        current_override = self.get_local_override()
        if current_override == h5_filepath:
            rerun = True
        if self.last_completed_shot == h5_filepath:
            rerun = True

        if rerun:
            self._logger.debug(
                'Direct-loaded shot has already been used, creating a fresh copy'
            )
            try:
                h5_filepath = self._make_repeat_copy(
                    h5_filepath, repeat_reason='local_override'
                )
            except Exception:
                self._logger.exception('Failed to create a rerun copy for %s', h5_filepath)
                return 'Cannot create a re-run of this experiment. Is it a valid run file?'
            message = 'Local override shot loaded successfully: experiment to be re-run\n'
        else:
            message = 'Local override shot loaded successfully\n'

        self._set_local_override(h5_filepath)
        if self.manager_paused:
            message += 'Warning: Queue is currently paused\n'
        if not self.manager_running:
            message = 'Error: Queue is not running\n'
        return message

    def new_rep_name(self, h5_filepath):
        basename, ext = os.path.splitext(h5_filepath)
        if '_rep' in basename and ext == '.h5':
            reps = basename.split('_rep')[-1]
            try:
                reps = int(reps)
            except ValueError:
                pass
            else:
                return ''.join(basename.split('_rep')[:-1]) + '_rep%05d.h5' % (
                    reps + 1
                ), reps + 1
        return basename + '_rep%05d.h5' % 1, 1

    def clean_h5_file(
        self,
        h5file,
        new_h5_file,
        repeat_number=0,
        repeat_source=None,
        repeat_reason=None,
    ):
        try:
            with h5py.File(h5file, 'r') as old_file:
                with h5py.File(new_h5_file, 'w') as new_file:
                    groups_to_copy = [
                        'devices',
                        'calibrations',
                        'script',
                        'globals',
                        'connection table',
                        'labscriptlib',
                        'waits',
                        'time_markers',
                        'shot_properties',
                    ]
                    for group in groups_to_copy:
                        if group in old_file:
                            new_file.copy(old_file[group], group)
                    for name in old_file.attrs:
                        new_file.attrs[name] = old_file.attrs[name]
                    new_file.attrs['run repeat'] = repeat_number
                    if repeat_source is not None:
                        new_file.attrs['run repeat source'] = repeat_source
                    if repeat_reason is not None:
                        new_file.attrs['run repeat reason'] = repeat_reason
        except Exception:
            self._logger.exception('Clean H5 File Error.')
            return False
        return True

    def _make_repeat_copy(self, h5_filepath, repeat_reason):
        new_h5_filepath, repeat_number = self.new_rep_name(h5_filepath)
        while os.path.exists(new_h5_filepath):
            new_h5_filepath, repeat_number = self.new_rep_name(new_h5_filepath)
        try:
            repeat_source = path_to_agnostic(h5_filepath)
        except Exception:
            repeat_source = h5_filepath
        success = self.clean_h5_file(
            h5_filepath,
            new_h5_filepath,
            repeat_number=repeat_number,
            repeat_source=repeat_source,
            repeat_reason=repeat_reason,
        )
        if not success:
            raise RuntimeError('failed to create repeat copy')
        return new_h5_filepath

    def _get_runmanager_client(self):
        if runmanager_remote is None:
            raise RuntimeError('runmanager.remote is unavailable')
        if self._runmanager_client is None:
            self._runmanager_client = runmanager_remote.Client()
        return self._runmanager_client

    def _get_runmanager_notify_client(self):
        if runmanager_remote is None:
            raise RuntimeError('runmanager.remote is unavailable')
        if self._runmanager_notify_client is None:
            base_client = self._get_runmanager_client()
            timeout = getattr(base_client, 'timeout', 1) or 1
            timeout = min(timeout, 1)
            self._runmanager_notify_client = runmanager_remote.Client(
                host=base_client.host,
                port=base_client.port,
                timeout=timeout,
            )
        return self._runmanager_notify_client

    def _normalise_completion_notification_path(self, h5_filepath):
        path = str(h5_filepath).strip()
        if not path:
            return None
        if os.path.exists(path):
            path = os.path.abspath(path)
            return path_to_agnostic(path)
        return path

    def get_pending_completion_notifications(self):
        with self._pending_completion_notifications_lock:
            return list(self._pending_completion_notifications)

    def restore_pending_completion_notifications(self, paths):
        restored_paths = []
        for path in list(paths or []):
            try:
                normalised = self._normalise_completion_notification_path(path)
            except Exception:
                self._logger.exception(
                    'Failed to restore pending completion notification %s', path
                )
                continue
            if normalised:
                restored_paths.append(normalised)
        with self._pending_completion_notifications_lock:
            self._pending_completion_notifications = restored_paths
        self._update_notify_buffer_count()
        self._completion_buffer_signal_queue.put(['retry', None])

    @inmain_decorator(True)
    def _update_notify_buffer_count(self):
        count = len(self.get_pending_completion_notifications())
        text = 'Runmanager notify buffer: %d' % count
        self._ui.runmanager_notify_buffer_label.setText(text)
        self._ui.runmanager_notify_buffer_label.setToolTip(text)

    def _flush_pending_completion_notifications(self):
        while True:
            with self._pending_completion_notifications_lock:
                if not self._pending_completion_notifications:
                    break
                agnostic_path = self._pending_completion_notifications[0]

            try:
                self._get_runmanager_notify_client().notify_shot_complete(agnostic_path)
            except Exception:
                if not self._runmanager_notify_error_logged:
                    self._logger.exception(
                        'Failed to notify runmanager that shot %s completed',
                        agnostic_path,
                    )
                    self._runmanager_notify_error_logged = True
                return False

            with self._pending_completion_notifications_lock:
                if (
                    self._pending_completion_notifications
                    and self._pending_completion_notifications[0] == agnostic_path
                ):
                    self._pending_completion_notifications.pop(0)
                else:
                    try:
                        self._pending_completion_notifications.remove(agnostic_path)
                    except ValueError:
                        pass
            self._runmanager_notify_error_logged = False
            self._update_notify_buffer_count()
        return True

    def _completion_notification_mainloop(self):
        logger = logging.getLogger('BLACS.runmanager_notify_buffer')
        while self.manager_running:
            timeout = 1 if self.get_pending_completion_notifications() else 10
            try:
                try:
                    signal, data = self._completion_buffer_signal_queue.get(
                        timeout=timeout
                    )
                except queue.Empty:
                    signal, data = 'retry', None

                if signal == 'retry':
                    if self.get_pending_completion_notifications():
                        self._flush_pending_completion_notifications()
                elif signal == 'close':
                    break
                else:
                    raise ValueError('Invalid signal: %s' % str(signal))
            except Exception:
                logger.exception(
                    'Exception in runmanager notification buffer mainloop, continuing'
                )
        logger.info('Stopping runmanager notification buffer')

    def _notify_shot_complete(self, h5_filepath):
        try:
            agnostic_path = self._normalise_completion_notification_path(h5_filepath)
        except Exception:
            self._logger.exception(
                'Failed to convert completed shot path %s to agnostic form',
                h5_filepath,
            )
            return False
        if not agnostic_path:
            return False
        with self._pending_completion_notifications_lock:
            self._pending_completion_notifications.append(agnostic_path)
        self._update_notify_buffer_count()
        self._completion_buffer_signal_queue.put(['retry', None])
        return True

    def _normalise_runmanager_offer(self, response):
        if response in (None, False, ''):
            return None
        if isinstance(response, dict):
            if response.get('status') in ('empty', 'idle', 'none'):
                return None
            if response.get('empty'):
                return None
            agnostic_path = response.get('agnostic_path') or response.get('path')
            if not agnostic_path:
                return None
            return {
                'agnostic_path': agnostic_path,
                'source_kind': response.get('source_kind', self.SOURCE_RUNMANAGER),
            }
        if isinstance(response, (list, tuple)) and len(response) >= 2:
            return {
                'agnostic_path': response[0],
                'source_kind': (
                    response[1] if len(response) >= 2 else self.SOURCE_RUNMANAGER
                ),
            }
        if isinstance(response, str):
            return {
                'agnostic_path': response,
                'source_kind': self.SOURCE_RUNMANAGER,
            }
        return None

    def _path_from_offer(self, agnostic_path):
        candidate = str(agnostic_path)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
        return os.path.abspath(path_to_local(candidate))

    def _request_next_from_runmanager(self):
        try:
            response = self._get_runmanager_client().request('queue_request_next')
            offer = self._normalise_runmanager_offer(response)
            self._runmanager_comm_error_logged = False
        except Exception:
            if not self._runmanager_comm_error_logged:
                self._logger.exception('Failed to request the next shot from runmanager')
                self._runmanager_comm_error_logged = True
            return None, 'communication_error'

        if offer is None:
            return None, 'empty'

        path = self._path_from_offer(offer['agnostic_path'])
        result, message = self._validate_connection_table(path)
        if not result:
            self.manager_paused = True
            self.set_status('Rejected shot from runmanager\nQueue paused')
            self._logger.error(
                'Rejected runmanager shot %s because it failed validation: %s',
                path,
                message.strip(),
            )
            return None, 'invalid'

        offer['path'] = path
        return offer, 'ready'

    def _get_fallback_repeat_shot(self, reason):
        if not self.manager_repeat:
            return None
        if self.last_completed_shot is None:
            return None
        if self.last_completed_shot_ignore_repeat:
            return None
        try:
            path = self._make_repeat_copy(self.last_completed_shot, reason)
        except Exception:
            self._logger.exception(
                'Failed to create fallback repeat copy of %s', self.last_completed_shot
            )
            return None
        return {'path': path, 'source_kind': self.SOURCE_FALLBACK_REPEAT}

    def _get_next_shot(self):
        offer, status = self._request_next_from_runmanager()
        if offer is not None:
            return offer
        if status == 'empty':
            override = self._get_local_override_fallback()
            if override:
                return {'path': override, 'source_kind': self.SOURCE_LOCAL_OVERRIDE}
            fallback = self._get_fallback_repeat_shot('runmanager_empty')
            if fallback is not None:
                return fallback
            self.set_status('Idle')
            return None
        if status == 'communication_error':
            override = self._get_local_override_fallback()
            if override:
                return {'path': override, 'source_kind': self.SOURCE_LOCAL_OVERRIDE}
            fallback = self._get_fallback_repeat_shot('runmanager_comm_error')
            if fallback is not None:
                return fallback
            self.set_status('Runmanager unavailable')
            return None
        return None

    def _clear_current_shot(self):
        self.current_shot_path = None
        self.current_shot_source_kind = None

    @inmain_decorator(wait_for_return=True)
    def set_status(self, queue_status, shot_filepath=None):
        self._ui.queue_status.setText(str(queue_status))
        if shot_filepath is not None:
            self._ui.running_shot_name.setText(
                '<b>%s</b>' % str(os.path.basename(shot_filepath))
            )
        else:
            self._ui.running_shot_name.setText('')

    @inmain_decorator(wait_for_return=True)
    def get_status(self):
        return self._ui.queue_status.text()

    @inmain_decorator(wait_for_return=True)
    def transition_device_to_buffered(
        self, name, transition_list, h5file, restart_receiver
    ):
        tab = self.BLACS.tablist[name]
        if self.get_device_error_state(name, self.BLACS.tablist):
            return False
        tab.connect_restart_receiver(restart_receiver)
        tab.transition_to_buffered(h5file, self.current_queue)
        transition_list[name] = tab
        return True

    @inmain_decorator(wait_for_return=True)
    def get_device_error_state(self, name, device_list):
        return device_list[name].error_message

    def _abort_buffered_devices(self, devices_in_use, restart_function):
        self.current_queue = queue.Queue()
        for devicename, tab in devices_in_use.items():
            if tab.mode == MODE_BUFFERED or tab.mode == MODE_TRANSITION_TO_BUFFERED:
                tab.abort_buffered(self.current_queue)
            inmain(tab.disconnect_restart_receiver, restart_function)

    def manage(self):
        logger = logging.getLogger('BLACS.queue_manager.thread')
        process_tree.zlock_client.set_thread_name('queue_manager')
        logger.info('starting')

        h5py._errors.silence_errors()
        self.current_queue = queue.Queue()

        timeout_limit = 300  # seconds
        self.set_status('Idle')

        while self.manager_running:
            if self.manager_paused:
                if self.get_status() in ('Idle', 'Runmanager unavailable'):
                    logger.info('Paused')
                    self.set_status('Queue paused')
                time.sleep(1)
                continue

            shot = self._get_next_shot()
            if shot is None:
                time.sleep(1)
                continue

            path = shot['path']
            self.current_shot_path = path
            self.current_shot_source_kind = shot.get('source_kind')
            self.set_status('Preparing shot...', path)
            logger.info(
                'Preparing shot %s from %s', path, self.current_shot_source_kind
            )

            devices_in_use = {}
            transition_list = {}
            self.current_queue = queue.Queue()

            def abort_function():
                try:
                    self.current_queue.put(['Queue Manager', 'abort'])
                except Exception:
                    logger.exception('Could not send abort message to the executor')

            def restart_function(device_name):
                try:
                    self.current_queue.put([device_name, 'restart'])
                except Exception:
                    logger.exception(
                        'Could not send restart message for device %s', device_name
                    )

            try:
                timed_out = False
                error_condition = False
                abort = False
                restarted = False
                self.set_status('Transitioning to buffered...', path)

                inmain(self._ui.queue_abort_button.clicked.connect, abort_function)
                inmain(self._ui.queue_abort_button.setEnabled, True)

                for callback in plugins.get_callbacks('pre_transition_to_buffered'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception('Plugin callback raised an exception')

                start_time = time.time()

                with h5py.File(path, 'r') as hdf5_file:
                    devices_in_use = {}
                    start_order = {}
                    stop_order = {}
                    for name in hdf5_file['devices']:
                        device_properties = labscript_utils.properties.get(
                            hdf5_file, name, 'device_properties'
                        )
                        devices_in_use[name] = self.BLACS.tablist[name]
                        start_order[name] = device_properties.get('start_order', None)
                        stop_order[name] = device_properties.get('stop_order', None)

                start_groups = defaultdict(set)
                stop_groups = defaultdict(set)
                for name in devices_in_use:
                    start_groups[start_order[name]].add(name)
                    stop_groups[stop_order[name]].add(name)

                while (transition_list or start_groups) and not error_condition:
                    if not transition_list:
                        for name in start_groups.pop(min(start_groups)):
                            try:
                                success = self.transition_device_to_buffered(
                                    name, transition_list, path, restart_function
                                )
                                if not success:
                                    logger.error(
                                        '%s has an error condition, aborting run', name
                                    )
                                    error_condition = True
                                    break
                            except Exception:
                                logger.exception(
                                    'Exception while transitioning %s to buffered mode.',
                                    name,
                                )
                                error_condition = True
                                break
                        if error_condition:
                            break

                    try:
                        logger.debug(
                            'Waiting for devices to finish transitioning to buffered mode: %s',
                            str(transition_list),
                        )
                        device_name, result = self.current_queue.get(timeout=2)

                        if device_name == 'Queue Manager' and result == 'abort':
                            logger.info('abort signal received from GUI')
                            abort = True
                            break

                        if result == 'fail':
                            logger.info(
                                'abort signal received during transition to buffered of %s',
                                device_name,
                            )
                            error_condition = True
                            break
                        elif result == 'restart':
                            logger.info('Device %s was restarted, aborting shot.', device_name)
                            restarted = True
                            break

                        logger.debug(
                            '%s finished transitioning to buffered mode', device_name
                        )

                        if self.get_device_error_state(device_name, transition_list):
                            logger.error(
                                '%s has an error condition, aborting run', device_name
                            )
                            error_condition = True
                            break

                        del transition_list[device_name]
                    except queue.Empty:
                        for name in transition_list:
                            if self.get_device_error_state(name, transition_list):
                                error_condition = True
                                break

                        if error_condition:
                            break

                        if time.time() - start_time > timeout_limit:
                            logger.error('Transitioning to buffered mode timed out')
                            timed_out = True
                            break

                if timed_out or error_condition or abort or restarted:
                    if not abort:
                        self.manager_paused = True
                        self.prepend(path)
                    if timed_out:
                        self.set_status('Programming timed out\nQueue paused')
                    elif abort:
                        self.set_status('Aborted')
                    elif restarted:
                        self.set_status(
                            'Device restarted in transition to\nbuffered. Aborted. Queue paused.'
                        )
                    else:
                        self.set_status('Device(s) in error state\nQueue Paused')

                    self.current_queue = queue.Queue()
                    for tab in devices_in_use.values():
                        tab.abort_buffered(self.current_queue)
                        inmain(tab.disconnect_restart_receiver, restart_function)

                    inmain(self._ui.queue_abort_button.clicked.disconnect, abort_function)
                    inmain(self._ui.queue_abort_button.setEnabled, False)
                    self._clear_current_shot()
                    continue

                states, tab_positions, window_data, plugin_data = (
                    self.BLACS.front_panel_settings.get_save_data()
                )
                self.set_status(
                    'Running (program time: %.3fs)...' % (time.time() - start_time),
                    path,
                )

                experiment_finished_queue = queue.Queue()
                logger.debug('About to start the master pseudoclock')
                run_time = datetime.datetime.now()

                for callback in plugins.get_callbacks('science_starting'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception('Plugin callback raised an exception')

                self.BLACS.tablist[self.master_pseudoclock].start_run(
                    experiment_finished_queue
                )

                abort = False
                restarted = False
                done = False
                while not (abort or restarted or done):
                    try:
                        done = experiment_finished_queue.get(timeout=0.5) == 'done'
                    except queue.Empty:
                        pass
                    try:
                        device_name, result = self.current_queue.get_nowait()
                        if device_name == 'Queue Manager' and result == 'abort':
                            abort = True
                        if result == 'restart':
                            restarted = True
                        for device_name, tab in devices_in_use.items():
                            if self.get_device_error_state(device_name, devices_in_use):
                                restarted = True
                    except queue.Empty:
                        pass

                if abort or restarted:
                    for devicename, tab in devices_in_use.items():
                        if tab.mode == MODE_BUFFERED:
                            tab.abort_buffered(self.current_queue)
                        inmain(tab.disconnect_restart_receiver, restart_function)

                inmain(self._ui.queue_abort_button.clicked.disconnect, abort_function)
                inmain(self._ui.queue_abort_button.setEnabled, False)

                if restarted:
                    self.manager_paused = True
                    self.prepend(path)
                    self.set_status('Device restarted during run.\nAborted. Queue paused')
                elif abort:
                    self.set_status('Aborted')

                if abort or restarted:
                    self._clear_current_shot()
                    continue

                logger.info('Run complete')
                self.set_status('Saving data...', path)
            except Exception:
                logger.exception('Error in queue manager execution. Queue paused.')
                zprocess.raise_exception_in_thread(sys.exc_info())
                self.manager_paused = True
                repeat_number = self._get_repeat_number(path)
                temp_path = tempfilename()
                self.clean_h5_file(path, temp_path, repeat_number=repeat_number)
                try:
                    shutil.move(temp_path, path)
                except Exception:
                    msg = (
                        "Couldn't delete failed run file %s, " % path
                        + 'another process may be using it. Using alternate '
                        + 'filename for second attempt.'
                    )
                    logger.warning(msg, exc_info=True)
                    shutil.move(temp_path, path.replace('.h5', '_retry.h5'))
                    path = path.replace('.h5', '_retry.h5')
                self.prepend(path)
                self._abort_buffered_devices(devices_in_use, restart_function)
                self.set_status('Error in queue manager\nQueue paused')

                try:
                    inmain(self._ui.queue_abort_button.clicked.disconnect, abort_function)
                except Exception:
                    pass
                inmain(self._ui.queue_abort_button.setEnabled, False)
                self._clear_current_shot()
                continue
            finally:
                for callback in plugins.get_callbacks('science_over'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception('Plugin callback raised an exception')

            try:
                with h5py.File(path, 'r+') as hdf5_file:
                    self.BLACS.front_panel_settings.store_front_panel_in_h5(
                        hdf5_file,
                        states,
                        tab_positions,
                        window_data,
                        plugin_data,
                        save_conn_table=False,
                        save_queue_data=False,
                    )

                    data_group = hdf5_file['/'].require_group('data')
                    # stamp with the run time of the experiment
                    hdf5_file.attrs['run time'] = run_time.strftime('%Y%m%dT%H%M%S.%f')

                error_condition = False
                response_list = {}
                while stop_groups:
                    transition_list = {}
                    for name in stop_groups.pop(min(stop_groups)):
                        tab = devices_in_use[name]
                        try:
                            tab.transition_to_manual(self.current_queue)
                            transition_list[name] = tab
                        except Exception:
                            logger.exception(
                                'Exception while transitioning %s to manual mode.', name
                            )
                            error_condition = True
                    while transition_list:
                        logger.info(
                            'Waiting for devices to finish transitioning to manual mode: %s',
                            str(transition_list),
                        )
                        try:
                            name, result = self.current_queue.get(2)
                            if name == 'Queue Manager' and result == 'abort':
                                continue
                        except queue.Empty:
                            for name in transition_list.copy():
                                if self.get_device_error_state(name, transition_list):
                                    error_condition = True
                                    logger.debug('%s is in an error state', name)
                                    del transition_list[name]
                            continue
                        response_list[name] = result
                        if result == 'fail':
                            error_condition = True
                            logger.debug('%s failed to transition to manual', name)
                        elif result == 'restart':
                            error_condition = True
                            logger.debug(
                                '%s restarted during transition to manual', name
                            )
                        elif self.get_device_error_state(name, devices_in_use):
                            error_condition = True
                            logger.debug('%s is in an error state', name)
                        else:
                            logger.debug('%s finished transitioning to manual mode', name)
                        tab = devices_in_use[name]
                        inmain(tab.disconnect_restart_receiver, restart_function)
                        del transition_list[name]

                if error_condition:
                    self.set_status('Error in transtion to manual\nQueue Paused')
            except Exception:
                error_condition = True
                logger.exception('Error in queue manager execution. Queue paused.')
                self.set_status('Error in queue manager\nQueue paused')
                self._abort_buffered_devices(devices_in_use, restart_function)
                zprocess.raise_exception_in_thread(sys.exc_info())

            if error_condition:
                self.manager_paused = True
                repeat_number = self._get_repeat_number(path)
                temp_path = tempfilename()
                self.clean_h5_file(path, temp_path, repeat_number=repeat_number)
                try:
                    shutil.move(temp_path, path)
                except Exception:
                    msg = (
                        "Couldn't delete failed run file %s, " % path
                        + 'another process may be using it. Using alternate '
                        + 'filename for second attempt.'
                    )
                    logger.warning(msg, exc_info=True)
                    shutil.move(temp_path, path.replace('.h5', '_retry.h5'))
                    path = path.replace('.h5', '_retry.h5')
                self.prepend(path)
                self._clear_current_shot()
                continue

            logger.info('All devices are back in static mode.')

            send_completion_notification = True
            for callback in plugins.get_callbacks('analysis_cancel_send'):
                try:
                    if callback(path):
                        send_completion_notification = False
                        break
                except Exception:
                    logger.exception('Plugin callback raised an exception')

            if send_completion_notification:
                self._notify_shot_complete(path)

            for callback in plugins.get_callbacks('shot_complete'):
                try:
                    callback(path)
                except Exception:
                    logger.exception('Plugin callback raised an exception')

            ignore_repeat = False
            for callback in plugins.get_callbacks('shot_ignore_repeat'):
                try:
                    if callback(path):
                        ignore_repeat = True
                        break
                except Exception:
                    logger.exception('Plugin callback raised an exception')

            self.last_completed_shot = path
            self.last_completed_shot_ignore_repeat = ignore_repeat
            self._clear_current_shot()
            self.set_status('Idle')

        logger.info('Stopping')
