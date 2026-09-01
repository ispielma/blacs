"""How BLACS's main window divides its space.

The shot controls are a fixed strip: two buttons, a status line and the local
override selector. They belong above the device tabs, taking the height they
need and no more. They used to be one half of a vertical splitter, which gave
them a drag handle, let them be sized to anything, and -- because splitter
positions are saved with the front panel -- brought whatever they had last been
dragged to back on every start.

Asserted against the loaded window rather than main.ui read as text: what an
operator meets is the window Qt builds, and a main.ui that will not load is a
BLACS that will not start.
"""
import os
import unittest

from qtutils import UiLoader
from qtutils.qt.QtWidgets import QApplication, QSplitter, QWidget

import blacs


MAIN_UI = os.path.join(os.path.dirname(blacs.__file__), 'main.ui')

_qapplication = None


def load_main_window():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes every widget built under it down with it.
        _qapplication = QApplication([])
    return UiLoader().load(MAIN_UI)


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = load_main_window()
        # Shown, because Qt does not lay a window out until it is: without this
        # every height below reads 0 and the size assertions pass or fail for
        # reasons that have nothing to do with the layout.
        cls.ui.show()
        QApplication.instance().processEvents()

    def shot_controls(self):
        strip = self.ui.findChild(QWidget, 'verticalLayoutWidget_2')
        self.assertIsNotNone(strip, 'the shot controls strip')
        return strip

    def tabs(self):
        tabs = self.ui.findChild(QSplitter, 'tab_horizontal_splitter')
        self.assertIsNotNone(tabs, 'the device tab area')
        return tabs

    def test_the_window_loads_with_the_controls_blacs_reaches_for(self):
        # Every one of these is looked up by name at startup, so losing one is
        # a BLACS that raises on the way up rather than a window that looks odd.
        for name in (
            'shot_request_button',
            'shot_abort_button',
            'shot_status',
            'running_shot_name',
            'runmanager_online',
            'runmanager_status_label',
            'local_override_lineEdit',
            'local_override_browse_button',
        ):
            self.assertIsNotNone(
                self.ui.findChild(QWidget, name), '%s is looked up by name' % name
            )

    def test_the_shot_controls_are_not_in_a_splitter(self):
        self.assertNotIsInstance(
            self.shot_controls().parent(),
            QSplitter,
            'a splitter would give the shot controls a drag handle',
        )
        self.assertIsNone(
            self.ui.findChild(QSplitter, 'main_splitter'),
            'the splitter that used to hold them has nothing left to split',
        )

    def test_the_shot_controls_take_the_height_they_need_and_no_more(self):
        self.ui.resize(900, 700)
        QApplication.instance().processEvents()

        strip = self.shot_controls()
        self.assertAlmostEqual(
            strip.height(),
            strip.sizeHint().height(),
            delta=1,
            msg='the strip should sit at its own size hint',
        )

    def test_the_device_tabs_get_the_rest_of_the_window(self):
        strip, tabs = self.shot_controls(), self.tabs()
        heights = {}
        for window_height in (700, 1100):
            self.ui.resize(900, window_height)
            QApplication.instance().processEvents()
            heights[window_height] = (strip.height(), tabs.height())

        self.assertEqual(
            heights[700][0],
            heights[1100][0],
            'the strip does not grow with the window',
        )
        self.assertGreater(
            heights[1100][1],
            heights[700][1],
            'the device tabs take the space the window gained',
        )


if __name__ == '__main__':
    unittest.main()
