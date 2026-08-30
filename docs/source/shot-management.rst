Shot Management
===============

The primary purpose of BLACS is to execute experiment shots on the lab
apparatus. BLACS does not own a queue. Runmanager owns the authoritative shot
queue and offers shots to BLACS one at a time, over ZMQ, when BLACS asks for
one. Runmanager is also responsible for forwarding completed shots on to lyse.

Before running any shot, BLACS compares the shot's connection table with the
laboratory connection table and verifies that the shot is compatible with the
current hardware configuration. Compatibility requires the shot connection
table to be a subset of the lab connection table. This ensures that old
experiments cannot be run on hardware that is no longer configured to match,
preventing damage or unexpected results.

The BLACS shot-execution controls are a pause button, an abort button, a status
display naming the running shot, a local override shot selector, and an
indicator showing whether runmanager is responding.

The queue itself lives in runmanager, along with the settings governing how
shots are compiled, what happens to a shot that does not run, and what BLACS is
given when the queue is empty. Queued shots are deleted there with the Delete
key or the row context menu, rather than with a button, and the queue is
cleared as a part of submitting a replacement batch. There is no reordering
control and no repeat control: both belonged to the BLACS-owned queue and did
not survive the move to runmanager. Anyone arriving from the upstream
documentation will go looking for them.

.. _blacs-runmanager-sync:

Synchronising with runmanager
-----------------------------

Because the queue and the hardware live in different processes, a shot can be
lost or run twice if the two disagree about who is holding it. The rules below
are what prevent that. They are stated here because neither side can be read
from the other's source, and changing one half without the other reintroduces
exactly the failures they exist to close.

**A shot is offered, then acknowledged, then run.** Runmanager hands BLACS a
shot in reply to a request. BLACS then tells runmanager it has taken it,
naming both the path runmanager offered and the path BLACS will actually run —
these differ when the shot has already been run once and BLACS makes a fresh
copy of it to re-run.

**Acknowledgement is a precondition for running the shot, not a notification
about it.** If runmanager does not confirm that it recorded the acknowledgement,
BLACS does not run the shot. It releases it and asks again. This costs one poll
cycle and no data: the shot came from runmanager in the first place, so if
runmanager cannot be reached there is no queued shot to run anyway. Running it
without a confirmed acknowledgement would run it twice, because runmanager will
offer an unacknowledged shot again.

**Acknowledgement is sent from the thread that asks for the next shot, before
the shot runs.** This ordering is what makes runmanager's rule sound: a shot
still unacknowledged when BLACS asks for another one cannot have reached BLACS,
so runmanager returns it to the head of the queue. Moving the acknowledgement
to another thread, or retrying it in the background, breaks that inference and
must not be done — a background retry races the next request, which is the
failure the ordering exists to prevent.

**BLACS does not hold a shot it could not run.** Every terminal outcome —
completion, abort, programming timeout, device error, device restart — releases
the shot. BLACS never silently re-runs a shot on resume; runmanager decides
whether a shot goes back in the queue, under its own failure policy.

**Every outcome other than completion pauses execution**, an operator's abort
included, so that decision is made before BLACS asks for another shot. Only a
completed shot leads straight on to the next request.

**Every terminal outcome is reported.** BLACS tells runmanager how each shot
turned out — ``completed``, ``aborted`` or ``failed``, with a reason. Runmanager
needs the outcome to retire the shot; a shot with no reported outcome has no
terminal state there and its failure policy never runs on it.

A BLACS that is killed, or that crashes outright, reports nothing, and the shot
it held stays marked as running in runmanager indefinitely: runmanager only
discards that record when it hands out the next shot, which will never happen.
Clearing the queue in runmanager also clears the in-flight record, and is the
way out of that state.

**Outcomes are retried until they land, and named if they never do.** Outcomes
are queued for a background thread that retries through a momentary runmanager
outage. When BLACS closes, that thread gets a bounded grace period to finish
delivering; anything still undelivered is written to the log at warning level,
naming the shot, so an operator can put runmanager's record right by hand.

Retrying outcomes in the background is safe only because of the pause rule.
Runmanager discards a shot's in-flight record when it hands out the next one,
so an outcome that arrives after BLACS has asked for another shot matches
nothing, and the failure policy never runs on it — the shot is silently not
re-queued. What prevents that is that every non-completion pauses execution, so
BLACS does not ask for another shot until an operator resumes, and the notifier
has as long as it needs. **Remove that pause and the failure policy starts
missing shots**, quietly and only sometimes. The two rules are one mechanism.

**Paths cross the boundary in shared-drive-agnostic form.** BLACS sends agnostic
paths; runmanager converts them back to local paths before looking a shot up in
its records. Both sides must agree on the ``shared_drive`` prefix in their
labconfig. If they disagree, runmanager will not recognise the shots BLACS
names, no acknowledgement will ever be recorded, and BLACS will decline every
shot it is offered.

The local override shot sits outside all of this. It is loaded from the BLACS
GUI rather than offered by runmanager, so it is never acknowledged and
runmanager has no queue record of it. Its outcome is still reported, with two
consequences worth knowing. A completed override shot **is** forwarded to lyse,
like any other completed shot, so a shot run from the BLACS GUI is still
analysed. A failed one prints a red "BLACS reported shot ... as failed" line in
runmanager's output for a shot it never queued, which is harmless but reads as
though something went wrong with the queue.

Executing a shot
----------------

Shot execution follows this pattern:

#.  When BLACS is idle it asks runmanager for the next shot. If runmanager has
    none, and a local override shot has been loaded in the BLACS GUI, BLACS runs
    that instead.
#.  BLACS checks the shot's connection table against the lab connection table.
    A shot that fails this check is rejected: runmanager is told why, so that it
    does not offer the same unusable shot again, and execution pauses. If that
    rejection does not get through, the shot stays unacknowledged and is offered
    again when BLACS next asks, to be rejected and pause again. BLACS logs the
    lost rejection rather than resending it, because resending would have to
    happen off the request thread and would break the ordering rule above.
#.  BLACS acknowledges the shot to runmanager, and proceeds only if runmanager
    confirms it recorded the acknowledgement.
#.  For each device used by the shot, BLACS sends a message to the corresponding
    device tab to program that device for hardware-timed execution. These
    messages are asynchronous, so devices program in parallel where the device
    tab's state machine allows it. During programming a device tab is in
    ``transition_to_buffered`` mode.
#.  BLACS waits until every device reports that it has entered buffered mode. If
    a device times out or reports an error, BLACS aborts the shot, returns all
    devices to manual mode, reports the failure to runmanager, releases the shot
    and pauses execution.
#.  Once all devices are ready, BLACS records the current state of the manual
    controls — these usually affect the initial values of the shot — and
    instructs the master pseudoclock to begin executing the programmed
    instructions.
#.  BLACS waits for the master pseudoclock to report that the shot has finished.
    If a device restarts or errors during the run, BLACS aborts as above.
#.  BLACS instructs every device tab to transition back to manual mode. Device
    tabs save their acquired data and reprogram the hardware for manual
    operation. If errors occur here, BLACS returns the shot file to its pre-run
    state, so the shot can be run again, and pauses execution.
#.  BLACS reports the outcome to runmanager. Runmanager, not BLACS, submits
    completed shots to lyse.
