"""Settings that have to be in place before the modules that read them.

A test run should not depend on anyone remembering an environment variable, and
a conftest is the right place for both of these: pytest imports it and nothing
else does, so a real BLACS run is unaffected and still behaves as it should.

``QT_QPA_PLATFORM`` -- running the tests is not supposed to put anything on the
screen of whoever runs them. Most of that is dealt with where it arises:
``fixtures.py`` stands in for the splash module, so importing BLACS builds no
``QApplication`` at all rather than a hidden one. The layout tests are the
exception, and they are right to be -- Qt does not lay out a widget that was
never shown, so their geometry assertions read zero unless the window really is
shown. The fix for those is not to stop showing it; rendering offscreen is what
lets them go on showing a real window without one appearing.

``LABSCRIPT_NO_ERROR_DIALOG`` -- stops ``labscript_utils.excepthook`` spawning a
tkinter window for every unhandled exception, which during a test run means one
window per failure. Exceptions are still logged and still reach stderr, so
nothing is hidden from the person running the tests.

Both use ``setdefault``, so a value already in the environment wins. Note that
for the dialog that means *unsetting* the variable, or setting it empty:
``excepthook`` reads it as ``bool(os.environ.get(...))``, so any non-empty
value -- ``'0'`` included -- suppresses the dialog. A test of the error dialog
itself would be the one reason to want it back, and there is no such test here.

``QT_QPA_PLATFORM`` must be set before qtutils imports Qt, and
``LABSCRIPT_NO_ERROR_DIALOG`` before ``labscript_utils.excepthook`` is imported,
which is why this is a conftest rather than a fixture.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LABSCRIPT_NO_ERROR_DIALOG', '1')
