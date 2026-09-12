# Working in blacs

## Tests must not invoke the application

`blacs/__main__.py` builds a `Splash` and calls `.show()` at module scope
(around line 27), and `Splash.__init__` is also what creates the
`QApplication`. `splash.hide()` runs only inside `if __name__ == '__main__'`.
So **importing `blacs.__main__` puts the startup banner on the user's screen
and leaves it there** — during a test run, during a REPL session, during
anything that is not BLACS actually starting.

This is a property of the application, not of any test, and it has been there
since `3636eba`. What keeps it from firing is the convention below, in order of
preference:

1. **Import the leaf module.** `tests/test_front_panel_settings.py` imports
   `blacs.front_panel_settings`, and `tests/test_main_window_layout.py` imports
   only the `blacs` package to find `main.ui`. Neither triggers anything. This
   is the case to copy.
2. **If the module under test cannot be reached that way**, load it by path and
   stub its dependencies in `sys.modules`, restoring them in a `finally`.
   `tests/test_plugins_compat.py` is the worked example — it exercises the real
   `blacs/plugins/__init__.py` without importing the `blacs` package at all.
3. **If a test must borrow from `__main__`** — to exercise a real method such
   as `ExperimentServer.handler` or `BLACS.finalise_quit` rather than a
   description of it — stub `labscript_utils.splash` in `sys.modules` *before*
   the import. A fake `Splash` whose `__init__`, `show`, `hide` and
   `update_text` do nothing, plus a `get_qapplication` returning `None`, is
   enough: no `QApplication` is created at all, and the real methods are still
   borrowed.

`tests/fixtures.py` already does case 3, once, on behalf of the whole suite,
and restores `sys.modules` afterwards. **Import `fixtures` before
`blacs.__main__`** and you inherit it; import `blacs.__main__` first and you
get the banner. Every test module here does the former.

A test that genuinely renders — geometry or pixel assertions — must still call
`.show()`, because Qt does not lay out or paint an unshown widget.
`tests/test_main_window_layout.py` shows the real window for exactly that
reason, and its height assertions read zero without it. Do not "fix" that by
removing the `show()`. `tests/conftest.py` renders offscreen, so a shown
window never reaches the screen; export `QT_QPA_PLATFORM` yourself if you want
to watch.

## Running the tests

From inside this repository, never from the workspace root — a workspace-root
cwd shadows the installed packages with the source directories beside it:

    python -m pytest tests -q

Nothing needs setting on the command line. `tests/conftest.py` sets
`QT_QPA_PLATFORM=offscreen`, so a test that shows a window renders to a buffer,
and `LABSCRIPT_NO_ERROR_DIALOG=1`, so `labscript_utils.excepthook` does not
spawn a tkinter window per unhandled exception — exceptions are still logged and
still reach stderr. Both use `setdefault`, so a value already in the environment
wins — `LABSCRIPT_NO_ERROR_DIALOG=0` leaves the dialog on, as do `false`, `no`,
`off` and an empty value. That has not always been true: until labscript-utils
`ae73495` ("Let LABSCRIPT_NO_ERROR_DIALOG=0 mean what it looks like") the
variable was read as a bare truth test, so `0` suppressed the dialog exactly as
`1` did. A comment elsewhere still describing that is stale.
A test that wants the dialog should assign `excepthook.NO_ERROR_DIALOG`
directly, since the environment is only consulted when that module is
imported.

Note that blacs has **no CI that runs these tests** — `.github/workflows` is
release-only. They run when someone runs them.

## More

The workspace `AGENTS.md`, one directory up, carries the longer reasoning, the
branch layout, and the conventions shared across the suite. This file exists
because work often happens inside one repository with no view of that one.
