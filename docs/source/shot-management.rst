Shot Management
===============

The primary purpose of BLACS is to execute experiment shots on the lab apparatus.
Runmanager owns the authoritative shot queue and offers shots to BLACS over ZMQ
when BLACS is idle. BLACS can also load a single local override shot directly
from its GUI, either via the file picker or by dragging and dropping an HDF5
shot file onto the override control. Before accepting any shot, BLACS compares
the shot connection table with the laboratory connection table and verifies that
the shot is compatible with the current hardware configuration.

The BLACS shot-executor controls consist of pause, fallback-repeat, and abort
buttons, a status display for the currently running shot, a local override shot
selector, and a counter showing how many completed shots are still buffered for
delivery back to runmanager. Runmanager is responsible for the queued-shot UI
and for forwarding completed shots on to lyse.

Shot execution in BLACS follows this pattern:

#.  When BLACS is ready for a new run, it first checks whether a local override
    shot has been loaded. If not, it requests the next shot from runmanager.
    If runmanager is empty or temporarily unreachable, BLACS may instead create
    and run a fresh repeat of the last completed shot when fallback repeat is
    enabled.
#.  For each device used by the shot, BLACS sends a message to the corresponding
    device tab to program that device for hardware-timed execution. These
    messages are asynchronous so devices can program in parallel where possible.
#.  BLACS waits until all devices report that they have entered buffered mode.
    If a device times out or reports an error, BLACS aborts the shot, pauses
    execution, and requeues the shot locally if appropriate.
#.  Once all devices are ready, BLACS records the current manual-control state
    and instructs the master pseudoclock to begin execution.
#.  BLACS waits for the master pseudoclock to report that the shot has
    completed. If a device restarts or errors during the run, BLACS aborts the
    shot and pauses execution.
#.  After the shot completes, BLACS instructs all devices to transition back to
    manual mode. During this stage devices save acquired data and reprogram
    themselves for manual operation. If errors occur here, BLACS cleans the
    shot file back to its pre-run state, pauses execution, and prepares the
    shot for a later retry.
#.  BLACS then buffers a completion notification back to runmanager. If
    runmanager is temporarily unavailable, BLACS retries this notification in
    the background and shows the number of buffered completions in its GUI.
    Runmanager, not BLACS, handles onward submission of completed shots to lyse.
#.  Finally, BLACS updates its repeat state. If fallback repeat is enabled and
    runmanager cannot immediately provide another shot, BLACS duplicates the
    last completed shot and executes the duplicate as a fresh HDF5 file.
