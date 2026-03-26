# Spielman Fork Merge Notes for `blacs`

This file is for agents merging new upstream `master` changes into the
revised `RunmanagerQueueSimple` BLACS code.

It is not a changelog. It is a map of the local structural changes that make
simple grep-based merging unreliable.

## Scope

This note describes the revised BLACS execution path in the current local
working tree of `RunmanagerQueueSimple`.

The main architectural change is:

- upstream BLACS owned a queue
- this fork removes BLACS-owned shot queue behavior
- runmanager owns the queue
- BLACS asks runmanager for one shot at a time
- BLACS keeps only a single local override shot as fallback

## Most Important Rename Map

When upstream changes touch these names, port them into the revised names
below instead of trying to preserve the old queue terminology.

### File / class renames

- `blacs/experiment_queue.py` -> `blacs/shot_execution.py`
- `QueueManager` -> `ShotExecutor`

### `BLACS` object / plugin registry renames

- `self.queue` -> `self.shot_executor`
- `BLACS['experiment_queue']` -> `BLACS['shot_execution']`

### Saved-state key renames

- HDF5 attr `queue_data` -> `shot_execution_data`
- `window_data["_main_window"]["_queue"]` -> `window_data["_main_window"]["_shot_execution"]`

Legacy read compatibility is still required:

- `BLACS._restore_shot_execution_data()` reads `shot_execution_data`
- if absent, it falls back to legacy `queue_data`

### UI object renames

- `queue_controls_frame` -> `shot_controls_frame`
- `queue_control_buttons_horizontalLayout` -> `shot_control_buttons_horizontalLayout`
- `queue_pause_button` -> `shot_pause_button`
- `queue_abort_button` -> `shot_abort_button`
- `queue_status_verticalLayout` -> `shot_status_verticalLayout`
- `queue_status` -> `shot_status`

### Status / log wording renames

- `Queue paused` -> `Execution paused`
- `Pause the queue` -> `Pause shot execution`
- `starting queue manager` -> `starting shot execution`
- `queue manager` -> `shot execution` or `shot executor`, depending on context

## Functions And Behaviors That Changed

These are the changes most likely to matter when replaying upstream code.

### `blacs/__main__.py`

#### `BLACS._restore_shot_execution_data(blacs_settings)`

This helper is fork-specific. It centralizes the legacy read fallback:

- use `shot_execution_data` if present
- otherwise fall back to `queue_data`
- `eval()` string values as before

If upstream adds new save-data restore logic for the old queue path, port it
here instead of reintroducing direct `queue_data` handling throughout BLACS.

#### BLACS startup wiring

Upstream queue startup logic now maps as follows:

- import `ShotExecutor` from `blacs.shot_execution`
- create `self.shot_executor`
- restore via `_restore_shot_execution_data(...)`
- expose it to plugins as `BLACS['shot_execution']`

If upstream changes plugin setup or startup order near queue creation, merge
those edits into the `shot_executor` path instead of recreating `self.queue`.

#### `ExperimentServer.process(self, h5_filepath)`

This changed semantically.

Upstream behavior:

- converted the agnostic path to local form
- pushed the file into BLACS via `process_request()`

Fork behavior:

- direct remote shot submission is rejected
- BLACS no longer accepts pushed shots from runmanager

Current return is:

- `'Error: BLACS no longer accepts direct shot submissions\\n'`

If upstream changes the old submission handler, do not blindly merge that old
push-based behavior back in.

### `blacs/shot_execution.py`

This file is the old `experiment_queue.py`, but it is no longer a queue
manager.

#### `ShotExecutor.manage(self)`

This is the main control loop and is the highest-risk merge point.

Upstream queue-era assumptions that are no longer true:

- BLACS does not maintain a list/tree/model of queued shots
- BLACS does not pop from a local queue
- BLACS does not keep a pending next shot
- BLACS does not repeat the last completed shot as a normal fallback path

Current flow is:

1. if paused, wait
2. request one agnostic shot path from `runmanager.remote.Client.queue_request_next()`
3. if runmanager gives nothing or is unavailable, use `local_override_lineEdit` if set
4. convert the chosen agnostic path to local form
5. validate/prepare it with `process_request()`
6. execute it immediately
7. on completion, go back to step 1

If upstream modifies queue-pop / queue-model / queue-tree logic inside
`experiment_queue.py`, that code probably does not belong in this fork.

#### `ShotExecutor.process_request(self, h5_filepath)`

This also changed semantically.

Upstream behavior:

- validated the file
- if valid, appended it into BLACS queue state
- returned a message string

Fork behavior:

- validates the file
- makes a `_repXXXXX.h5` rerun copy if the file already has `/data`
- returns `(path_to_run, message)`
- does not append anything to a BLACS queue

This function is still the canonical validation/rerun-preparation path.
If upstream changes the validation or rerun-copy logic, merge that logic here,
but keep the fork return contract unless the surrounding code is deliberately
being redesigned.

#### `ShotExecutor.get_save_data()` / `restore_save_data()`

Current saved state is only:

- `manager_paused`
- `last_opened_shots_folder`
- `local_override_path`

Do not reintroduce:

- `files_queued`
- queue tree/model state
- legacy queue UI restoration

#### Local override behavior

The local override is now only a persistent fallback shot selector.

Important constraints:

- selecting/editing the local override does not call `process_request()`
- the text box should continue to show the user-selected file
- rerun-copy generation happens only when the shot is actually used

If upstream touches local override handling, preserve that behavior.

### `blacs/front_panel_settings.py`

#### `get_save_data()`

Use:

- `self.blacs.shot_executor.get_save_data()`

and save it under:

- `window_data["_main_window"]["_shot_execution"]`

#### `store_front_panel_in_h5(...)`

Parameter changed:

- `save_queue_data` -> `save_shot_execution_data`

Attribute written changed:

- `dataset.attrs['shot_execution_data']`

If upstream adds saved-state fields here, merge them into the shot-execution
path, but keep the legacy read fallback only in `BLACS._restore_shot_execution_data()`.

## Plugin Merge Map

If upstream plugin code still uses old queue names, port those references
through the mappings below.

### `plugins/connection_table`

- `self.BLACS['experiment_queue']` -> `self.BLACS['shot_execution']`

### `plugins/progress_bar`

- insert into `shot_status_verticalLayout`
- use `BLACS['shot_execution'].master_pseudoclock`

### `plugins/cycle_time`

- use `BLACS['shot_execution']`
- connect to `shot_abort_button`

### `plugins/delete_repeated_shots`

This plugin still exists, but it no longer controls or reflects a BLACS-owned
queue.

Relevant local changes:

- it is attached under `shot_controls_frame`
- repeat-toggle UI coupling to the old queue controls was removed
- it still deletes `_repXXXXX.h5` files after shot completion

If upstream changes this plugin, keep it independent of any resurrected BLACS
queue UI.

## Grep Traps

These are places where upstream merge work can be missed if you search only by
the old name.

### Upstream may edit `experiment_queue.py`

In this fork, those edits probably belong in:

- `blacs/shot_execution.py`

### Upstream may edit queue save/restore code

In this fork, those edits likely belong in:

- `BLACS._restore_shot_execution_data()` in `blacs/__main__.py`
- `FrontPanelSettings.get_save_data()` in `blacs/front_panel_settings.py`
- `FrontPanelSettings.store_front_panel_in_h5()` in `blacs/front_panel_settings.py`

### Upstream may edit queue UI references in `.ui` or plugins

Search for both old and new names:

- `queue_controls_frame` / `shot_controls_frame`
- `queue_pause_button` / `shot_pause_button`
- `queue_abort_button` / `shot_abort_button`
- `queue_status` / `shot_status`
- `experiment_queue` / `shot_execution`
- `QueueManager` / `ShotExecutor`

## Quick Merge Checklist

When pulling new upstream `master` changes into this fork:

1. Check whether upstream changed `blacs/experiment_queue.py`.
   Port those changes into `blacs/shot_execution.py`.

2. Check whether upstream changed queue save/restore logic in `__main__.py` or
   `front_panel_settings.py`.
   Port them through the `shot_execution_data` path, keeping the legacy
   `queue_data` read fallback.

3. Check whether upstream changed plugin references to
   `BLACS['experiment_queue']` or old queue UI object names.
   Port those changes to `BLACS['shot_execution']` and the `shot_*` object names.

4. Check whether upstream changed `process_request()` behavior.
   Port validation/rerun-copy fixes, but do not restore enqueue semantics by
   accident.

5. Check whether upstream changed `ExperimentServer.process()`.
   Do not restore direct shot submission unless the runmanager ownership model
   is intentionally being reverted.

6. After merging, search for both old and new names before assuming a feature
   was fully ported.

## What Not To Reintroduce By Accident

These were intentionally removed from BLACS in this fork:

- BLACS-owned shot list / tree / queue widget
- add/delete/reorder queue actions
- `files_queued` save/restore state
- direct pushed-shot submission into BLACS
- queue-oriented plugin/UI naming
- processing the local override when restoring or editing its path

