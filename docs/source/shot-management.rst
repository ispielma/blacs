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

The BLACS shot-execution controls are a **Request shots** button, an **Abort**
button, a status display naming the running shot, a local override shot
selector, and an indicator showing whether runmanager is responding.

The queue itself lives in runmanager, on its Queue tab, along with the settings
governing how queued shots are compiled, whether the queue is paused, and what
BLACS is given when the queue is empty. Queued shots are deleted there with the
Delete or Backspace key or the row context menu, rather than with a button, and
the queue is emptied as part of submitting a replacement batch. There is no
reordering control and no repeat control: both belonged to the BLACS-owned
queue and did not survive the move to runmanager. Anyone arriving from the
upstream documentation will go looking for them.

.. _blacs-runmanager-sync:

Synchronising with runmanager
-----------------------------

Because the queue and the hardware live in different processes, a shot can be
lost or run twice if the two disagree about who is holding it. The rules below
are what prevent that. They are stated here because neither side can be read
from the other's source, and changing one half without the other reintroduces
exactly the failures they exist to close.

**Each application owns what it alone can decide.** Runmanager owns the queue:
its contents and order, whether it is paused, which shot is offered next, what
becomes of a shot that did not run, and submitting completed shots to lyse.
BLACS owns the apparatus: whether it is requesting shots at all, aborting the
shot in progress, the error state that stopped it, and its local override shot.
Neither reaches across. Runmanager cannot enable BLACS, clear its error,
restart a device or abort a shot; BLACS cannot pause, reorder or delete
anything in the queue. This is not merely tidiness — one BLACS is meant to
serve several runmanagers eventually, and a runmanager that could stop the
apparatus would be stopping everyone's work, not just its own.

**One exchange carries everything, and the outcome is applied first.** BLACS
makes a single call, ``queue_exchange(outcome, request_shot)``: it reports how
the shot it was last offered turned out, and asks for another, in one message.
Runmanager applies the outcome *before* it chooses what to offer, so one
exchange can retire the finished row and hand over the next one. There is no
separate acceptance handshake, because nothing leaves the queue when a shot is
offered: the row stays where it is, marked running, so a reply that goes
missing costs a poll rather than a shot.

That ordering is not an implementation detail. It is what makes the reclaim
below sound: by the time runmanager looks at the head of its queue, a row BLACS
has just reported on has already been retired or turned red, so anything still
marked running is a row this BLACS is demonstrably not executing.

**A shot_id names a queue row, not an attempt at it.** Every queued shot is
given one stable identifier when its record is made, and keeps it for the life
of the row — across a save and restore, and across every retry. Outcomes are
keyed on that identifier rather than on a filepath, which is what lets BLACS
report on a shot it re-ran under a fresh filename, and what lets runmanager
offer the same row again after a failure without matching filenames. Enqueuing
the same filepath again later makes a new row with a new identifier.

**Only completion, or an explicit deletion, removes a row.** A shot that
completed leaves the queue and is forwarded to lyse. Every other outcome —
``aborted``, ``failed`` or ``rejected`` — leaves the row exactly where it is,
at the head of the queue, turns it red, and records the reason BLACS gave in
its tooltip. The next request from BLACS is offered that same row, under the
same identifier: retry is the default, and deleting the row is the only way to
discard it. There is no Retry/Drop policy any more; the operator's choice is
between asking for shots again and deleting the row.

A shot that never got as far as BLACS is held the same way. With lazy compile,
a queued shot is compiled when BLACS asks for it, and one that fails to compile
goes red at the head with the reason rather than being dropped — a row
disappearing looks exactly like the queue draining normally, which is what a
labscript file that cannot compile used to produce. That row is the one kind
that is *not* retried: a compile that fails partway leaves data in the shot
file that stops labscript ever compiling into it, so only deleting it, which
takes the half-written file with it, moves the queue on.

**A row still marked running is offered again.** If an offer reply never
reaches BLACS, or BLACS restarts while holding the shot, the row would
otherwise sit marked running for ever with the whole queue stopped behind it.
Runmanager hands it out again instead. The inference that licenses this spans
both applications: BLACS is sequential, asks for a shot only when it is idle,
and carries the outcome of the shot it has just finished on the same exchange.
So a request that arrives without an outcome retiring this row proves BLACS is
not running it. **Asking runmanager for work from anywhere other than the shot
loop, or while a shot is under way, breaks that inference and would have one
shot handed out twice.** The rule holds for one BLACS per runmanager, which is
what this protocol is written for; a second BLACS asking the same runmanager
would be given the row the first is still executing.

**Repeating an exchange is safe, and errors are answered rather than raised.**
BLACS lets go of an outcome only once runmanager has taken it, so a lost reply
makes it send the same outcome again. A completion for a row that has already
gone changes nothing and is not analysed twice; a failure for a row already
carrying that same reason changes nothing and is not reported twice, and passes
in silence because nothing happened. A completed shot matching no row is
different, and is always reported: it is also what a row deleted while BLACS
was running it looks like, and then a shot really did run and nothing will
analyse it. Runmanager cannot tell the two apart, so it says what happened
rather than dropping it. An outcome runmanager cannot read, and any failure on
runmanager's side while it chooses what to offer, are likewise reported in its
output box, and the exchange still answers normally.
Raised, they would reach BLACS as an error indistinguishable from never having
reached runmanager at all, so BLACS would hold an outcome it had already
delivered and resend it once a second indefinitely.

**Paths cross the boundary in shared-drive-agnostic form.** Runmanager sends
agnostic paths; BLACS converts them to local paths before opening anything, and
sends agnostic paths back in outcomes and in its status. Both sides must agree
on the ``shared_drive`` prefix in their labconfig. If they disagree, the shot
runmanager offers names a file BLACS cannot open, so BLACS rejects it, stops
requesting shots and says so, and the row goes red in runmanager with that
reason — the same path any unusable shot takes. Nothing is lost, but nothing
runs either until the labconfigs are put right.

Four controls, four owners
--------------------------

Three separate controls now govern whether a shot runs, and a fourth interrupts
one that already is. A queue that is not moving is nearly always one of them,
so they are worth telling apart.

**Pause queue** (runmanager, Queue tab) stops *this* runmanager offering work —
both queued rows and the default shot it would otherwise generate. It does not
stop BLACS. The shot already under way finishes normally, and BLACS goes on
running its local override shot while the queue is paused, exactly as it does
for a runmanager with an empty queue. The queue itself is untouched, so
resuming offers the same shot at its head. Pause is saved and restored with the
queue configuration; a configuration written before the control existed opens
with the queue running rather than silently stopped.

**Request shots** (BLACS) is the gate on hardware execution, and the only one.
It always starts unchecked and is deliberately not part of BLACS's saved state,
so enabling execution is always a deliberate act at this apparatus rather than
something a restart resumes. Unchecking it stops the next request, not the shot
in hand: a whole shot happens within one pass of the shot loop, so it finishes
first. An outcome still waiting to be reported is not held back by it either,
so runmanager always learns how the shot it offered turned out. Every outcome
other than completion unchecks it and records why, and re-checking it is how an
operator acknowledges that error and asks again — one action, deliberately
checking nothing first, because the per-device check made when the next shot is
programmed is still the final authority and will stop requests again if the
problem is still there.

**BLACS** (runmanager, the destination checkbox beside Engage) decides only
whether the shots Engage compiles are put into the runmanager queue. Unticked,
they are compiled and left alone. It is not a pause, it does not reach BLACS at
all, and the shots already in the queue are unaffected by it. Its remote
methods are still called ``get_run_shots``/``set_run_shots``, from when the
checkbox was labelled *Run shot(s)*; only the label changed. The status
indicator beside it is a separate thing and stays live whatever the checkbox
says.

**Abort** (BLACS) is the only way to interrupt a shot that is running. It
belongs to the operator at the apparatus and nothing runmanager sends can reach
it. Aborting reports the shot as ``aborted``, so its row goes red and is
retried when requests are enabled again; like any other non-completed outcome
it unchecks **Request shots**.

The queue while a shot is running
---------------------------------

The row BLACS is executing stays in the queue, so the operator can see which
queued item is on the hardware. It is set apart while it is there: the queue's
first row is reserved for whichever shot has been sent to BLACS, ruled off from
the work waiting below it and coloured green while it runs, red once it has come
back not having run. It is a row rather than a caption above the table so that
it carries the same columns as everything else, and it is there whether or not
BLACS has a shot, saying so when it does not, so the queue below it never
shifts.

That the row is still in the queue means queue editing can reach it, and must
not: deleting it would delete the HDF5 file BLACS is writing into.

Deleting rows, and both *Empty queue, then add shots…* submission modes — which
clear the queue before submitting the replacement batch — therefore skip the
running row and keep its file. A selected row is identified to the queue by its
stable ``shot_id`` rather than by its position, because the queue moves on its
own: a shot finishing removes a row while an operator has one selected, and a
row number that meant one shot when the table was drawn can mean another by the
time the key is pressed. Everything else they were asked to remove is
removed, and runmanager writes one line in its output box saying why the
running shot is still there. Waiting rows and red failed rows
are deletable as normal — deleting a failed row is the only way to discard it,
and doing so exposes the next waiting row without touching BLACS. A replacement
batch is numbered around the running shot rather than over it.

A row can be left marked running when BLACS never came back to report on it —
it was killed mid-shot, or the reply was lost. It cannot be deleted in that
state, and it does not need to be: the next request from BLACS is offered that
row again under the same identifier and the state clears itself. If BLACS stays
unavailable, restarting runmanager clears it without losing the queue, because
the running mark is never written to a saved configuration and every restored
row comes back waiting; reloading the saved configuration does the same.

What runmanager can see
-----------------------

Runmanager polls BLACS's existing server for a read-only status snapshot, on
its own background thread, independently of the shot exchange and of whether
the **BLACS** destination checkbox is ticked. BLACS never pushes: runmanager
asks. The snapshot says whether BLACS is requesting shots, what it is currently
doing, the path of the shot it is running and its identifier when that shot
came from a runmanager queue, and the reason it stopped requesting shots if it
has. A path with no identifier is BLACS running its own local override shot,
and runmanager says so. The snapshot is answered without the GUI thread, so a
BLACS busy with a shot still answers.

One snapshot is shown in two places, because it answers two different
questions. Beside the **BLACS** checkbox is a light saying whether BLACS
answered at all: checking, then responding or not responding. It means exactly
what the lyse light on the row below it means and no more — a BLACS sitting
there with **Request shots** unticked, or stopped by a device error, is a
healthy link and shows as responding. Beside **Pause queue** is a line of text
saying what BLACS is doing with the queue: requesting shots, running a named
shot, not requesting shots, or stopped with the reason. That is queue
behaviour rather than link health, it belongs next to the control it is about,
and none of it is a yes or a no that a glyph could carry.

Both are informational only. Apart from answering a ``hello`` ping, and
refusing the direct shot submissions BLACS no longer accepts, ``get_status``
is the only command BLACS's server serves, and that is deliberate: enabling
requests, clearing what stopped them, restarting a device and aborting a shot
all stay with the operator standing at the apparatus. There is an architecture
guard in each repository's test suite that fails if that changes, or if the
superseded request/accept/reject/report calls come back.

Default shots and the local override shot
-----------------------------------------

Two different things run when nobody has queued any work, and only one of them
is lab work.

A **runmanager default shot** is produced by runmanager when its queue is empty
and its *When queue is empty* setting asks for one, from the labscript file
named in its *Default shot* field. It is a shot a runmanager user is running,
so it is materialised as an ordinary queue row with its own stable identifier
and follows every rule above: it is visible and green while it runs, goes red
with its reason if it does not, is retried by the next request, can be deleted,
and is removed and submitted to lyse when it completes. A failed default row
holds back the next one, because a red row is the head of the queue and is what
the next request is offered. Default rows are left out of a saved queue: their
globals were read when they were produced, and their files live in the default
directory for the day they were made.

The **local override shot** is BLACS's own, selected in the BLACS GUI. BLACS
runs it whenever an enabled **Request shots** produced no runmanager shot — the
queue is paused, empty, or still compiling, or runmanager could not be reached
or its reply could not be read. It belongs to no runmanager queue, so its
completions are reported to nobody: they reach neither runmanager nor lyse,
which is what keeps repetitions of a shot nobody submitted out of the analysis.
No queue row is created, changed or retired by one — though a runmanager can
still see that BLACS is running one: the activity line beside **Pause queue**
names the shot, and its tooltip says the shot has no identifier and so is a
BLACS local override rather than queued work. Each repetition after the first is written to its own numbered
``_repXXXXX.h5`` file, so no data is overwritten. A
local override shot BLACS cannot run still stops requests and records the
reason, because retrying an unrunnable shot once a second is no answer either —
but there is no runmanager row, so nothing is reported and no row turns red.

Executing a shot
----------------

Shot execution follows this pattern:

#.  While **Request shots** is checked and BLACS is idle, it exchanges with
    runmanager: reporting the last shot's outcome if it is still holding one,
    and asking for the next shot. If runmanager offers none — paused, empty,
    compiling, or unreachable — and a local override shot has been selected in
    the BLACS GUI, BLACS runs that instead.
#.  BLACS checks the shot's connection table against the lab connection table.
    A shot that fails this check is reported as ``rejected`` on the next
    exchange and **Request shots** is unchecked. Runmanager keeps the row, red,
    at the head of the queue with that reason in its tooltip; once the
    connection table is put right, re-checking **Request shots** offers the
    same shot again. A rejected local override shot stops requests in the same
    way but has no row to report against.
#.  BLACS sends each device tab a message to program its device for
    hardware-timed execution, in the ``start_order`` groups the shot file
    declares. The messages are asynchronous, so every device in a group
    programs in parallel; the next group is not started until the previous one
    has finished. During programming a device tab is in
    ``transition_to_buffered`` mode.
#.  BLACS waits until every device reports that it has entered buffered mode.
    If a device times out, restarts, or reports an error, or the operator
    presses **Abort**, BLACS aborts the shot, returns all devices to manual
    mode, unchecks **Request shots** with the reason, and holds the outcome for
    the next exchange.
#.  Once all devices are ready, BLACS records the current state of the manual
    controls — these usually affect the initial values of the shot — and
    instructs the master pseudoclock to begin executing the programmed
    instructions.
#.  BLACS waits for the master pseudoclock to report that the shot has
    finished. If a device restarts or errors during the run, or the operator
    presses **Abort**, BLACS aborts as above.
#.  BLACS instructs every device tab to transition back to manual mode, in the
    shot's ``stop_order`` groups. Device tabs save their acquired data and
    reprogram the hardware for manual operation. Every tab is transitioned even
    if one of them fails. If anything fails here, BLACS returns the shot file
    to its pre-run state so the shot can be run again, unchecks **Request
    shots**, and holds a ``failed`` outcome for the next exchange. A device
    reporting an error after the run and a failure saving the data are
    distinguished in the reason, because they need different attention.
#.  The outcome — ``completed`` or otherwise — is held until the next exchange,
    and is only let go of once runmanager has taken it, so a momentary outage
    cannot lose one. Runmanager, not BLACS, submits completed shots to lyse.

If BLACS is closed while holding an outcome it never delivered, that shot is
named in the log at warning level. The row is not stranded — the next BLACS to
ask that runmanager for work is offered it again under the same identifier —
but this particular run of it reached neither runmanager nor lyse and will be
run again.
