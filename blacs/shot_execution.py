#####################################################################
#                                                                   #
# /shot_execution.py                                           #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import queue
import logging
import os
import threading
import time
import datetime
import sys
import shutil
from collections import defaultdict
from tempfile import gettempdir
from binascii import hexlify

from qtutils.qt.QtCore import Qt, QSize
from qtutils.qt.QtGui import QIcon
from qtutils.qt.QtWidgets import QFileDialog

import zprocess
from labscript_utils.ls_zprocess import ProcessTree
process_tree = ProcessTree.instance()
import labscript_utils.h5_lock, h5py

from qtutils import inmain_decorator, inmain

from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.connections import ConnectionTable
import labscript_utils.properties
from labscript_utils.shared_drive import path_to_agnostic, path_to_local

from blacs.tab_base_classes import MODE_TRANSITION_TO_BUFFERED, MODE_BUFFERED
import blacs.plugins as plugins

try:
    import runmanager.remote as runmanager_remote
except Exception:
    runmanager_remote = None


def tempfilename(prefix='BLACS-temp-', suffix='.h5'):
    """Return a filepath appropriate for use as a temporary file"""
    random_hex = hexlify(os.urandom(16)).decode()
    return os.path.join(gettempdir(), prefix + random_hex + suffix)


class ShotExecutor(object):
    def __init__(self, BLACS, ui):
        self._ui = ui
        self.BLACS = BLACS
        self.last_opened_shots_folder = BLACS.exp_config.get('paths', 'experiment_shot_storage')
        self._manager_running = True
        self._manager_paused = False
        self.master_pseudoclock = self.BLACS.connection_table.master_pseudoclock
        self._runmanager_request_client = None
        self._runmanager_notify_client = None
        self._runmanager_request_error_logged = False
        self._runmanager_notify_error_logged = False
        self.failure_reason = None
        self.completed_shots = queue.Queue()
        
        self._logger = logging.getLogger('BLACS.ShotExecutor')

        # set up buttons
        self._ui.shot_pause_button.toggled.connect(self._toggle_pause)
        self._ui.local_override_browse_button.clicked.connect(
            self.browse_local_override
        )
        self._ui.local_override_lineEdit.textChanged.connect(
            self._ui.local_override_lineEdit.setToolTip
        )

        # Set the elision of the status labels:
        elide_label(self._ui.shot_status, self._ui.shot_status_verticalLayout, Qt.ElideRight)
        elide_label(self._ui.running_shot_name, self._ui.shot_status_verticalLayout, Qt.ElideLeft)
        self.runmanager_online = 'checking'

        self.manager = threading.Thread(target = self.manage)
        self.manager.daemon=True
        self.manager.start()
        self.completion_notifier = threading.Thread(target=self.notify_runmanager_of_completed_shots)
        self.completion_notifier.daemon = True
        self.completion_notifier.start()
        
    def get_save_data(self):
        return {'manager_paused':self.manager_paused,
                'last_opened_shots_folder': self.last_opened_shots_folder,
                'local_override_path': str(self._ui.local_override_lineEdit.text()).strip(),
               }
    
    def restore_save_data(self,data):
        if 'manager_paused' in data:
            self.manager_paused = data['manager_paused']
        if 'last_opened_shots_folder' in data:
            self.last_opened_shots_folder = data['last_opened_shots_folder']
        if 'local_override_path' in data and data['local_override_path']:
            self._ui.local_override_lineEdit.setText(str(data['local_override_path']))
        
    @property
    @inmain_decorator(True)
    def manager_running(self):
        return self._manager_running
        
    @manager_running.setter
    @inmain_decorator(True)
    def manager_running(self,value):
        value = bool(value)
        self._manager_running = value
        
    def _toggle_pause(self,checked):    
        self.manager_paused = checked

    @property
    @inmain_decorator(True)
    def manager_paused(self):
        return self._manager_paused
    
    @manager_paused.setter
    @inmain_decorator(True)
    def manager_paused(self,value):
        value = bool(value)
        self._manager_paused = value
        if value != self._ui.shot_pause_button.isChecked():
            self._ui.shot_pause_button.setChecked(value)

    @property
    @inmain_decorator(True)
    def runmanager_online(self):
        return self._runmanager_online

    @runmanager_online.setter
    @inmain_decorator(True)
    def runmanager_online(self, value):
        self._runmanager_online = str(value)

        icon_names = {
            'checking': ':/qtutils/fugue/hourglass',
            'online': ':/qtutils/fugue/tick',
            'offline': ':/qtutils/fugue/exclamation',
            '': ':/qtutils/fugue/status-offline',
        }
        tooltips = {
            'checking': 'Checking runmanager...',
            'online': 'Runmanager is responding',
            'offline': 'Runmanager is not responding',
            '': 'Runmanager status unknown',
        }

        icon = QIcon(icon_names.get(self._runmanager_online, ':/qtutils/fugue/exclamation-red'))
        pixmap = icon.pixmap(QSize(16, 16))
        tooltip = tooltips.get(
            self._runmanager_online,
            "Invalid runmanager status: %s" % self._runmanager_online,
        )
        if self.failure_reason:
            tooltip += '\n' + self.failure_reason

        self._ui.runmanager_online.setPixmap(pixmap)
        self._ui.runmanager_online.setToolTip(tooltip)
        self._ui.runmanager_status_label.setToolTip(tooltip)

    def browse_local_override(self):
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
        self._ui.local_override_lineEdit.setText(shot_file)

    def runmanager_rpc(
        self, client_attr, error_logged_attr, method_name, unavailable_message, *args
    ):
        try:
            self.runmanager_online = 'checking'
            if runmanager_remote is None:
                raise RuntimeError('runmanager.remote is unavailable')
            client = getattr(self, client_attr)
            if client is None:
                client = runmanager_remote.Client(timeout=1)
                setattr(self, client_attr, client)
            response = getattr(client, method_name)(*args)
            self.failure_reason = None
            self.runmanager_online = 'online'
            setattr(self, error_logged_attr, False)
            return True, response
        except Exception as exc:
            setattr(self, client_attr, None)
            self.failure_reason = str(exc)
            self.runmanager_online = 'offline'
            if not getattr(self, error_logged_attr):
                self._logger.info(unavailable_message, exc)
                setattr(self, error_logged_attr, True)
            return False, None

    def notify_runmanager_of_completed_shots(self):
        pending_agnostic_path = None
        while self.manager_running:
            if pending_agnostic_path is None:
                try:
                    pending_agnostic_path = self.completed_shots.get(timeout=1)
                except queue.Empty:
                    continue

            success, _ = self.runmanager_rpc(
                '_runmanager_notify_client',
                '_runmanager_notify_error_logged',
                'notify_shot_complete',
                'Runmanager unavailable while reporting shot completion: %s',
                pending_agnostic_path,
            )
            if success:
                pending_agnostic_path = None
            else:
                time.sleep(1)
    
    def process_request(self,h5_filepath):
        # check connection table
        try:
            new_conn = ConnectionTable(h5_filepath, logging_prefix='BLACS')
        except Exception:
            return None, "H5 file not accessible to Control PC\n"
        result,error = inmain(self.BLACS.connection_table.compare_to,new_conn)
        if result:
            # Has this run file been run already?
            with h5py.File(h5_filepath, 'r') as h5_file:
                if 'data' in h5_file['/']:
                    rerun = True
                else:
                    rerun = False
            if rerun:
                self._logger.debug('Run file has already been run! Creating a fresh copy to rerun')
                new_h5_filepath, repeat_number = self.new_rep_name(h5_filepath)
                # Keep counting up until we get a filename that isn't in the filesystem:
                while os.path.exists(new_h5_filepath):
                    new_h5_filepath, repeat_number = self.new_rep_name(new_h5_filepath)
                success = self.clean_h5_file(h5_filepath, new_h5_filepath, repeat_number=repeat_number)
                if not success:
                   return None, 'Cannot create a re run of this experiment. Is it a valid run file?'
                h5_filepath = new_h5_filepath
                message = "Experiment added successfully: experiment to be re-run\n"
            else:
                message = "Experiment added successfully\n"
            if self.manager_paused:
                message += "Warning: Shot execution is currently paused\n"
            if not self.manager_running:
                message = "Error: Shot execution is not running\n"
            return h5_filepath, message
        else:
            # TODO: Parse and display the contents of "error" in a more human readable format for analysis of what is wrong!
            message =  ("Connection table of your file is not a subset of the experimental control apparatus.\n"
                       "You may have:\n"
                       "    Submitted your file to the wrong control PC\n"
                       "    Added new channels to your h5 file, without rewiring the experiment and updating the control PC\n"
                       "    Renamed a channel at the top of your script\n"
                       "    Submitted an old file, and the experiment has since been rewired\n"
                       "\n"
                       "Please verify your experiment script matches the current experiment configuration, and try again\n"
                       "The error was %s\n"%error)
            return None, message
            
    def new_rep_name(self, h5_filepath):
        basename, ext = os.path.splitext(h5_filepath)
        if '_rep' in basename and ext == '.h5':
            reps = basename.split('_rep')[-1]
            try:
                reps = int(reps)
            except ValueError:
                # not a rep
                pass
            else:
                return ''.join(basename.split('_rep')[:-1]) + '_rep%05d.h5' % (reps + 1), reps + 1
        return basename + '_rep%05d.h5' % 1, 1
        
    def clean_h5_file(self, h5file, new_h5_file, repeat_number=0):
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
        except Exception:
            # raise
            self._logger.exception('Clean H5 File Error.')
            return False
            
        return True
    
    @inmain_decorator(wait_for_return=True)
    def set_status(self, status_text, shot_filepath=None):
        self._ui.shot_status.setText(str(status_text))
        if shot_filepath is not None:
            self._ui.running_shot_name.setText('<b>%s</b>'% str(os.path.basename(shot_filepath)))
        else:
            self._ui.running_shot_name.setText('')
        
    @inmain_decorator(wait_for_return=True)
    def get_status(self):
        return self._ui.shot_status.text()
    
    @inmain_decorator(wait_for_return=True)    
    def transition_device_to_buffered(self, name, transition_list, h5file, restart_receiver):
        tab = self.BLACS.tablist[name]
        if self.get_device_error_state(name,self.BLACS.tablist):
            return False
        tab.connect_restart_receiver(restart_receiver)
        tab.transition_to_buffered(h5file, self.notify_queue)
        transition_list[name] = tab
        return True
    
    @inmain_decorator(wait_for_return=True)
    def get_device_error_state(self,name,device_list):
        return device_list[name].error_message

    def _abort_buffered_devices(self, devices_in_use, restart_function):
        self.notify_queue = queue.Queue()
        for devicename, tab in devices_in_use.items():
            if tab.mode == MODE_BUFFERED or tab.mode == MODE_TRANSITION_TO_BUFFERED:
                tab.abort_buffered(self.notify_queue)
            inmain(tab.disconnect_restart_receiver, restart_function)
       
     
    def manage(self):
        logger = logging.getLogger('BLACS.shot_executor.thread')
        process_tree.zlock_client.set_thread_name('shot_executor')
        # While the program is running!
        logger.info('starting')
        
        # HDF5 prints lots of errors by default, for things that aren't
        # actually errors. These are silenced on a per thread basis,
        # and automatically silenced in the main thread when h5py is
        # imported. So we'll silence them in this thread too:
        h5py._errors.silence_errors()
        
        # This stores the notification queue currently being used to
        # communicate with tabs, so that abort signals can be put
        # to it when those tabs never respond and are restarted by
        # the user.
        self.notify_queue = queue.Queue()

        #TODO: put in general configuration
        timeout_limit = 300 #seconds
        self.set_status("Idle")
        path = None
        
        while self.manager_running:
            # If the pause button is pushed in, sleep
            if self.manager_paused:
                if self.get_status() == "Idle":
                    logger.info('Paused')
                    self.set_status("Execution paused")
                time.sleep(1)
                continue

            if path is None:
                agnostic_path = None
                requested_from_runmanager = False
                runmanager_failed = False
                request_succeeded, agnostic_path = self.runmanager_rpc(
                    '_runmanager_request_client',
                    '_runmanager_request_error_logged',
                    'queue_request_next',
                    'Runmanager unavailable while requesting the next shot: %s',
                )
                requested_from_runmanager = bool(agnostic_path)
                runmanager_failed = not request_succeeded

                if not agnostic_path:
                    local_override_path = str(
                        inmain(self._ui.local_override_lineEdit.text)
                    ).strip()
                    if local_override_path:
                        agnostic_path = path_to_agnostic(
                            os.path.abspath(local_override_path)
                        )

                if not agnostic_path:
                    if runmanager_failed:
                        self.set_status("Runmanager unavailable")
                    else:
                        self.set_status("Idle")
                    time.sleep(1)
                    continue

                path, message = self.process_request(path_to_local(str(agnostic_path)))
                if path is None:
                    logger.error(message.strip())
                    if requested_from_runmanager:
                        self.manager_paused = True
                        self.set_status("Rejected shot from runmanager\nExecution paused")
                    elif runmanager_failed:
                        self.set_status("Runmanager unavailable")
                    else:
                        self.set_status("Idle")
                    time.sleep(1)
                    continue

            self.set_status('Preparing shot...', path)
            logger.info('Got a file: %s'%path)
            
            devices_in_use = {}
            transition_list = {}   
            self.notify_queue = queue.Queue()

            # Function to be run when abort button is clicked
            def abort_function():
                try:
                    # Set device name to "Shot Executor" which will never be a labscript device name
                    # as it is not a valid python variable name (has a space in it!)
                    self.notify_queue.put(['Shot Executor', 'abort'])
                except Exception:
                    logger.exception('Could not send abort message to the shot executor')
        
            def restart_function(device_name):
                try:
                    self.notify_queue.put([device_name, 'restart'])
                except Exception:
                    logger.exception('Could not send restart message to the shot executor for device %s'%device_name)
        
            ##########################################################################################################################################
            #                                                       transition to buffered                                                           #
            ########################################################################################################################################## 
            try:  
                # A notification queue for when the tabs have
                # completed transitioning to buffered:        
                
                timed_out = False
                error_condition = False
                abort = False
                restarted = False
                self.set_status("Transitioning to buffered...", path)
                
                # Enable the abort button, and link in notify_queue:
                inmain(self._ui.shot_abort_button.clicked.connect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,True)
                                
                ##########################################################################################################################################
                #                                                        Plugin callbacks                                                                #
                ########################################################################################################################################## 
                for callback in plugins.get_callbacks('pre_transition_to_buffered'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception("Plugin callback raised an exception")

                start_time = time.time()
                
                with h5py.File(path, 'r') as hdf5_file:
                    devices_in_use = {}
                    start_order = {}
                    stop_order = {}
                    for name in  hdf5_file['devices']:
                        device_properties = labscript_utils.properties.get(
                            hdf5_file, name, 'device_properties'
                        )
                        devices_in_use[name] = self.BLACS.tablist[name]
                        start_order[name] = device_properties.get('start_order', None)
                        stop_order[name] = device_properties.get('stop_order', None)

                # Sort the devices into groups based on their start_order and stop_order
                start_groups = defaultdict(set)
                stop_groups = defaultdict(set)
                for name in devices_in_use:
                    start_groups[start_order[name]].add(name)
                    stop_groups[stop_order[name]].add(name)

                while (transition_list or start_groups) and not error_condition:
                    if not transition_list:
                        # Ready to transition the next group:
                        for name in start_groups.pop(min(start_groups)):
                            try:
                                # Connect restart signal from tabs to notify_queue and transition the device to buffered mode
                                success = self.transition_device_to_buffered(name,transition_list,path,restart_function)
                                if not success:
                                    logger.error('%s has an error condition, aborting run' % name)
                                    error_condition = True
                                    break
                            except Exception:
                                logger.exception('Exception while transitioning %s to buffered mode.'%(name))
                                error_condition = True
                                break
                        if error_condition:
                            break
                        
                    try:
                        # Wait for a device to transtition_to_buffered:
                        logger.debug('Waiting for the following devices to finish transitioning to buffered mode: %s'%str(transition_list))
                        device_name, result = self.notify_queue.get(timeout=2)
                        
                        #Handle abort button signal
                        if device_name == 'Shot Executor' and result == 'abort':
                            # we should abort the run
                            logger.info('abort signal received from GUI')
                            abort = True
                            break
                            
                        if result == 'fail':
                            logger.info('abort signal received during transition to buffered of %s' % device_name)
                            error_condition = True
                            break
                        elif result == 'restart':
                            logger.info('Device %s was restarted, aborting shot.'%device_name)
                            restarted = True
                            break
                            
                        logger.debug('%s finished transitioning to buffered mode' % device_name)
                        
                        # The tab says it's done, but does it have an error condition?
                        if self.get_device_error_state(device_name,transition_list):
                            logger.error('%s has an error condition, aborting run' % device_name)
                            error_condition = True
                            break

                        del transition_list[device_name]
                    except queue.Empty:
                        # It's been 2 seconds without a device finishing
                        # transitioning to buffered. Is there an error?
                        for name in transition_list:
                            if self.get_device_error_state(name,transition_list):
                                error_condition = True
                                break
                                
                        if error_condition:
                            break
                            
                        # Has programming timed out?
                        if time.time() - start_time > timeout_limit:
                            logger.error('Transitioning to buffered mode timed out')
                            timed_out = True
                            break

                # Handle if we broke out of loop due to timeout or error:
                if timed_out or error_condition or abort or restarted:
                    # Pause shot execution and set a status message.
                    # only if we aren't responding to an abort click
                    if not abort:
                        self.manager_paused = True
                    if timed_out:
                        self.set_status("Programming timed out\nExecution paused")
                    elif abort:
                        self.set_status("Aborted")
                        path = None
                    elif restarted:
                        self.set_status("Device restarted in transition to\nbuffered. Aborted. Execution paused.")
                    else:
                        self.set_status("Device(s) in error state\nExecution paused")
                        
                    # Abort the run for all devices in use:
                    # Recreate the notification queue here because we don't want
                    # to hear from devices that are still transitioning to
                    # buffered mode.
                    self.notify_queue = queue.Queue()
                    for tab in devices_in_use.values():                        
                        # We call abort buffered here, because if each tab is either in mode=BUFFERED or transition_to_buffered failed in which case
                        # it should have called abort_transition_to_buffered itself and returned to manual mode
                        # Since abort buffered will only run in mode=BUFFERED, and the state is not queued indefinitely (aka it is deleted if we are not in mode=BUFFERED)
                        # this is the correct method call to make for either case
                        tab.abort_buffered(self.notify_queue)
                        # We don't need to check the results of this function call because it will either be successful, or raise a visible error in the tab.
                        
                        # disconnect restart signal from tabs
                        inmain(tab.disconnect_restart_receiver,restart_function)
                        
                    # disconnect abort button and disable
                    inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                    inmain(self._ui.shot_abort_button.setEnabled,False)
                    
                    # Start a new iteration
                    continue
                
            
            
                ##########################################################################################################################################
                #                                                             SCIENCE!                                                                   #
                ##########################################################################################################################################
            
                # Get front panel data, but don't save it to the h5 file until the experiment ends:
                states,tab_positions,window_data,plugin_data = self.BLACS.front_panel_settings.get_save_data()
                self.set_status("Running (program time: %.3fs)..."%(time.time() - start_time), path)
                    
                # A notification queue for when the experiment has finished.
                experiment_finished_notifications = queue.Queue()
                logger.debug('About to start the master pseudoclock')
                run_time = datetime.datetime.now()

                ##########################################################################################################################################
                #                                                        Plugin callbacks                                                                #
                ########################################################################################################################################## 
                for callback in plugins.get_callbacks('science_starting'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception("Plugin callback raised an exception")

                #TODO: fix potential race condition if BLACS is closing when this line executes?
                self.BLACS.tablist[self.master_pseudoclock].start_run(
                    experiment_finished_notifications
                )
                
                                                
                # Wait for notification of the end of run:
                abort = False
                restarted = False
                done = False
                while not (abort or restarted or done):
                    try:
                        done = (
                            experiment_finished_notifications.get(timeout=0.5) == 'done'
                        )
                    except queue.Empty:
                        pass
                    try:
                        # Poll notify_queue for abort signals from the button or
                        # device restarts.
                        device_name, result = self.notify_queue.get_nowait()
                        if (device_name == 'Shot Executor' and result == 'abort'):
                            abort = True
                        if result == 'restart':
                            restarted = True
                        # Check for error states in tabs
                        for device_name, tab in devices_in_use.items():
                            if self.get_device_error_state(device_name,devices_in_use):
                                restarted = True
                    except queue.Empty:
                        pass
                        
                if abort or restarted:
                    for devicename, tab in devices_in_use.items():
                        if tab.mode == MODE_BUFFERED:
                            tab.abort_buffered(self.notify_queue)
                        # disconnect restart signal from tabs 
                        inmain(tab.disconnect_restart_receiver,restart_function)
                                            
                # Disable abort button
                inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,False)
                
                if restarted:                    
                    self.manager_paused = True
                    self.set_status("Device restarted during run.\nAborted. Execution paused")
                elif abort:
                    self.set_status("Aborted")
                    path = None
                    
                if abort or restarted:
                    # after disabling the abort button, we now start a new iteration
                    continue                
                
                logger.info('Run complete')
                self.set_status("Saving data...", path)
            # End try/except block here
            except Exception:
                logger.exception("Error in shot execution. Execution paused.")

                # Raise the error in a thread for visibility
                zprocess.raise_exception_in_thread(sys.exc_info())
                # clean up the h5 file
                self.manager_paused = True
                # is this a repeat?
                with h5py.File(path, 'r') as h5_file:
                    repeat_number = h5_file.attrs.get('run repeat', 0)
                # clean the h5 file:
                temp_path = tempfilename()
                self.clean_h5_file(path, temp_path, repeat_number=repeat_number)
                try:
                    shutil.move(temp_path, path)
                except Exception:
                    msg = ('Couldn\'t delete failed run file %s, ' % path + 
                           'another process may be using it. Using alternate ' 
                           'filename for second attempt.')
                    logger.warning(msg, exc_info=True)
                    shutil.move(temp_path, path.replace('.h5','_retry.h5'))
                    path = path.replace('.h5','_retry.h5')
                
                # Need to put devices back in manual mode
                self._abort_buffered_devices(devices_in_use, restart_function)
                self.set_status("Error in shot execution\nExecution paused")

                # disconnect and disable abort button
                inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,False)
                
                # Start a new iteration
                continue
                             
            ##########################################################################################################################################
            #                                                           SCIENCE OVER!                                                                #
            ##########################################################################################################################################
            finally:
                ##########################################################################################################################################
                #                                                        Plugin callbacks                                                                #
                ########################################################################################################################################## 
                for callback in plugins.get_callbacks('science_over'):
                    try:
                        callback(path)
                    except Exception:
                        logger.exception("Plugin callback raised an exception")

            
            ##########################################################################################################################################
            #                                                       Transition to manual                                                             #
            ##########################################################################################################################################
            # start new try/except block here                   
            try:
                with h5py.File(path,'r+') as hdf5_file:
                    self.BLACS.front_panel_settings.store_front_panel_in_h5(
                        hdf5_file,
                        states,
                        tab_positions,
                        window_data,
                        plugin_data,
                        save_conn_table=False,
                        save_shot_execution_data=False,
                    )

                    data_group = hdf5_file['/'].require_group('data')
                    # stamp with the run time of the experiment
                    hdf5_file.attrs['run time'] = run_time.strftime('%Y%m%dT%H%M%S.%f')
        
                error_condition = False
                response_list = {}
                # Keep transitioning tabs to manual mode and waiting on them until they
                # are all done or have all errored/restarted/failed. If one fails, we
                # still have to transition the rest to manual mode:
                while stop_groups:
                    transition_list = {}
                    # Transition the next group to manual mode:
                    for name in stop_groups.pop(min(stop_groups)):
                        tab = devices_in_use[name]
                        try:
                            tab.transition_to_manual(self.notify_queue)
                            transition_list[name] = tab
                        except Exception:
                            logger.exception('Exception while transitioning %s to manual mode.'%(name))
                            error_condition = True
                    # Wait for their responses:
                    while transition_list:
                        logger.info('Waiting for the following devices to finish transitioning to manual mode: %s'%str(transition_list))
                        try:
                            name, result = self.notify_queue.get(2)
                            if name == 'Shot Executor' and result == 'abort':
                                # Ignore any abort signals left in the
                                # notification queue, it is too
                                # late to abort in any case:
                                continue
                        except queue.Empty:
                            # 2 seconds without a device transitioning to manual mode.
                            # Is there an error:
                            for name in transition_list.copy():
                                if self.get_device_error_state(name, transition_list):
                                    error_condition = True
                                    logger.debug('%s is in an error state' % name)
                                    del transition_list[name]
                            continue
                        response_list[name] = result
                        if result == 'fail':
                            error_condition = True
                            logger.debug('%s failed to transition to manual' % name)
                        elif result == 'restart':
                            error_condition = True
                            logger.debug('%s restarted during transition to manual' % name)
                        elif self.get_device_error_state(name, devices_in_use):
                            error_condition = True
                            logger.debug('%s is in an error state' % name)
                        else:
                            logger.debug('%s finished transitioning to manual mode' % name)
                        # Once device has transitioned_to_manual, disconnect restart
                        # signal:
                        tab = devices_in_use[name]
                        inmain(tab.disconnect_restart_receiver, restart_function)
                        del transition_list[name]
                    
                if error_condition:                
                    self.set_status("Error in transition to manual\nExecution paused")
                                       
            except Exception:
                error_condition = True
                logger.exception("Error in shot execution. Execution paused.")
                self.set_status("Error in shot execution\nExecution paused")
                self._abort_buffered_devices(devices_in_use, restart_function)

                # Raise the error in a thread for visibility
                zprocess.raise_exception_in_thread(sys.exc_info())
                
            if error_condition:                
                # clean up the h5 file
                self.manager_paused = True
                # is this a repeat?
                with h5py.File(path, 'r') as h5_file:
                    repeat_number = h5_file.attrs.get('run repeat', 0)
                # clean the h5 file:
                temp_path = tempfilename()
                self.clean_h5_file(path, temp_path, repeat_number=repeat_number)
                try:
                    shutil.move(temp_path, path)
                except Exception:
                    msg = ('Couldn\'t delete failed run file %s, ' % path + 
                           'another process may be using it. Using alternate ' 
                           'filename for second attempt.')
                    logger.warning(msg, exc_info=True)
                    shutil.move(temp_path, path.replace('.h5','_retry.h5'))
                    path = path.replace('.h5','_retry.h5')
                
                continue
            
            ##########################################################################################################################################
            #                                                        Completion Notification                                                        #
            ########################################################################################################################################## 
            logger.info('All devices are back in static mode.')  

            self.completed_shots.put(path_to_agnostic(path))

            ##########################################################################################################################################
            #                                                        Plugin callbacks                                                                #
            ########################################################################################################################################## 
            for callback in plugins.get_callbacks('shot_complete'):
                try:
                    callback(path)
                except Exception:
                    logger.exception("Plugin callback raised an exception")

            ##########################################################################################################################################
            path = None
            self.set_status("Idle")
        logger.info('Stopping')
