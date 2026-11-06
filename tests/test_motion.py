"""motion.set_visible: the space opens and closes and the widget fades, and every limit comes back exactly.

Skipped when PySide6 isn't installed. Animations are stepped by hand (setCurrentTime) so nothing waits on a clock."""
from __future__ import annotations

import os
import sys
import unittest

try:
    import PySide6  # noqa: F401
    HAVE_QT = not os.environ.get("DISTILL_SKIP_UI_TESTS")  # set by build_exe.bat and CI where window tests can't run reliably
except ImportError:  # pragma: no cover
    HAVE_QT = False

if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FULL = 16777215


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class SetVisibleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QHBoxLayout, QPushButton, QProgressBar, QWidget

        from distill.ui import motion

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.win = QWidget()
        lay = QHBoxLayout(self.win)
        self.anchor = QPushButton("anchor")
        self.button = QPushButton("A button with a label")
        self.bar = QProgressBar()
        self.bar.setFixedHeight(12)
        for w in (self.anchor, self.button, self.bar):
            lay.addWidget(w)
        self.win.show()
        self.addCleanup(self.win.close)
        self.app.processEvents()

    def finish(self, widget):
        anim = widget._presence.anim
        anim.setCurrentTime(anim.duration())

    def test_hiding_then_showing_a_button_folds_the_row_and_restores_its_limits(self):
        m, b = self.motion, self.button
        m.set_visible(b, False, axis="width")
        self.assertTrue(b.isVisible(), "still on screen while it folds")
        self.finish(b)
        self.assertFalse(b.isVisible())
        self.assertEqual((b.minimumWidth(), b.maximumWidth()), (0, FULL))
        self.assertIsNone(b.graphicsEffect())
        m.set_visible(b, True, axis="width")
        self.assertTrue(b.isVisible())
        self.assertEqual(b.maximumWidth(), 0, "starts from nothing")
        self.finish(b)
        self.assertEqual((b.minimumWidth(), b.maximumWidth()), (0, FULL))
        self.assertIsNone(b.graphicsEffect())

    def test_a_fixed_height_bar_keeps_exactly_its_height(self):
        m, bar = self.motion, self.bar
        m.set_visible(bar, False, axis="height")
        self.finish(bar)
        self.assertFalse(bar.isVisible())
        m.set_visible(bar, True, axis="height")
        self.finish(bar)
        self.assertTrue(bar.isVisible())
        self.assertEqual((bar.minimumHeight(), bar.maximumHeight()), (12, 12))

    def test_repeating_the_same_answer_does_nothing(self):
        m, b = self.motion, self.button
        m.set_visible(b, False, axis="width")
        anim = b._presence.anim
        anim.setCurrentTime(anim.duration() // 2)
        width = b.maximumWidth()
        m.set_visible(b, False, axis="width")
        self.assertEqual(b.maximumWidth(), width)
        self.assertEqual(anim.currentTime(), anim.duration() // 2)

    def test_changing_its_mind_mid_way_reverses_and_still_ends_clean(self):
        m, b = self.motion, self.button
        m.set_visible(b, False, axis="width")
        anim = b._presence.anim
        anim.setCurrentTime(anim.duration() // 2)
        m.set_visible(b, True, axis="width")
        self.finish(b)
        self.assertTrue(b.isVisible())
        self.assertEqual((b.minimumWidth(), b.maximumWidth()), (0, FULL))
        self.assertIsNone(b.graphicsEffect())

    def test_reduced_motion_is_instant(self):
        m, b = self.motion, self.button
        m.Motion.reduce = True
        m.set_visible(b, False, axis="width")
        self.assertFalse(b.isVisible())
        self.assertFalse(hasattr(b, "_presence"))
        m.set_visible(b, True, axis="width")
        self.assertTrue(b.isVisible())

    def test_animate_false_snaps_even_mid_animation(self):
        m, b = self.motion, self.button
        m.set_visible(b, False, axis="width")
        m.set_visible(b, True, axis="width", animate=False)
        self.assertTrue(b.isVisible())
        self.assertEqual((b.minimumWidth(), b.maximumWidth()), (0, FULL))
        self.assertIsNone(b.graphicsEffect())

    def test_a_widget_on_a_page_that_is_not_showing_changes_at_once(self):
        from PySide6.QtWidgets import QPushButton, QWidget

        hidden_page = QWidget()  # never shown
        child = QPushButton("x", hidden_page)
        self.motion.set_visible(child, False, axis="width")
        self.assertFalse(child.isVisibleTo(hidden_page))
        self.assertFalse(hasattr(child, "_presence"))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class SlideTests(unittest.TestCase):
    """slide(): children are clipped, never squeezed (the doubled-text artifact), and limits come back exactly."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from distill.ui import motion

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.win = QWidget()
        self.win.resize(420, 600)
        outer = QVBoxLayout(self.win)
        self.card = QWidget()
        lay = QVBoxLayout(self.card)
        self.labels = []
        for i in range(4):
            label = QLabel(("A wrapped line of text that is long enough to need more than one row. " * 3) + str(i))
            label.setWordWrap(True)
            lay.addWidget(label)
            self.labels.append(label)
        self.header = QWidget()
        self.header.setFixedHeight(48)
        outer.addWidget(self.header)
        outer.addWidget(self.card)
        outer.addStretch(1)
        self.win.show()
        self.addCleanup(self.win.close)
        self.app.processEvents()

    def test_children_keep_their_size_while_the_card_folds(self):
        before = [lbl.geometry() for lbl in self.labels]
        self.motion.slide(self.card, False)
        anim = self.card._slide_anim
        anim.setCurrentTime(anim.duration() // 2)
        self.app.processEvents()
        self.assertLess(self.card.height(), before[-1].bottom(), "the card really is part-way")
        self.assertEqual([lbl.geometry() for lbl in self.labels], before, "squeezed children overprint each other")

    def test_children_keep_their_size_while_the_card_opens(self):
        self.motion.slide(self.card, False)
        anim = self.card._slide_anim
        anim.setCurrentTime(anim.duration())
        self.app.processEvents()
        self.assertFalse(self.card.isVisible())
        self.motion.slide(self.card, True)
        anim = self.card._slide_anim
        anim.setCurrentTime(anim.duration() // 2)
        self.app.processEvents()
        heights = [lbl.height() for lbl in self.labels]
        anim.setCurrentTime(anim.duration())
        self.app.processEvents()
        self.assertEqual(heights, [lbl.height() for lbl in self.labels], "they should open at their final size")

    def test_layout_is_live_again_and_limits_restored_afterwards(self):
        self.motion.slide(self.card, False)
        self.assertFalse(self.card.layout().isEnabled())
        anim = self.card._slide_anim
        anim.setCurrentTime(anim.duration())
        self.assertTrue(self.card.layout().isEnabled())
        self.assertFalse(self.card.isVisible())
        self.assertEqual((self.card.minimumHeight(), self.card.maximumHeight()), (0, FULL))

    def test_a_fixed_height_header_stays_fixed(self):
        self.motion.slide(self.header, False)
        anim = self.header._slide_anim
        anim.setCurrentTime(anim.duration())
        self.motion.slide(self.header, True)
        anim = self.header._slide_anim
        anim.setCurrentTime(anim.duration())
        self.assertEqual((self.header.minimumHeight(), self.header.maximumHeight()), (48, 48))

    def test_open_on_something_already_open_resizes_it(self):
        self.labels[3].hide()
        self.motion.slide(self.card, True)
        anim = self.card._slide_anim
        self.assertLess(anim.endValue(), anim.startValue())
        anim.setCurrentTime(anim.duration())
        self.assertEqual((self.card.minimumHeight(), self.card.maximumHeight()), (0, FULL))

    def test_reduced_motion_is_instant_and_never_freezes(self):
        self.motion.Motion.reduce = True
        self.motion.slide(self.card, False)
        self.assertFalse(self.card.isVisible())
        self.assertTrue(self.card.layout().isEnabled())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class BounceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QListWidget

        from distill.ui import motion

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.view = QListWidget()
        for i in range(80):  # plenty to scroll, so the top and the bottom are real ends
            self.view.addItem(f"item {i}")
        self.view.resize(300, 300)
        self.view.show()
        self.addCleanup(self.view.close)
        self.app.processEvents()
        self.wheel = motion.smooth_scroll(self.view, bounce=True)

    def turn(self, delta):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        pos = QPointF(20, 20)
        ev = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        return self.wheel.eventFilter(self.view.viewport(), ev)

    def test_curve_rises_with_resistance_and_is_capped(self):
        m = self.motion
        offsets = [m.bump_offset(t) for t in (0, 3, 7, 14, 28, 1000)]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(offsets[0], 0)
        self.assertEqual(offsets[-1], m.BUMP_MAX)
        self.assertGreater(offsets[2] - offsets[1], (offsets[4] - offsets[3]) * 4 / 14)

    def test_scrolling_up_at_the_top_pulls_the_content_down_and_it_springs_back(self):
        home = self.view.viewport().pos()
        self.assertFalse(self.turn(120), "the wheel is still passed on")
        self.assertGreater(self.view.viewport().y(), home.y())
        self.assertLessEqual(self.view.viewport().y() - home.y(), self.motion.BUMP_MAX)
        self.wheel._settle()
        self.wheel._spring.setCurrentTime(self.wheel._spring.duration())
        self.assertEqual(self.view.viewport().pos(), home, "it settles exactly back into place")

    def test_scrolling_down_at_the_bottom_pulls_it_up(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        home = self.view.viewport().pos()
        self.turn(-120)
        self.assertLess(self.view.viewport().y(), home.y())

    def test_a_long_spin_never_pulls_past_the_cap(self):
        home = self.view.viewport().pos()
        for _ in range(40):
            self.turn(120)
        self.assertLessEqual(self.view.viewport().y() - home.y(), self.motion.BUMP_MAX)

    def test_a_view_with_nothing_to_scroll_does_not_bounce(self):
        from PySide6.QtWidgets import QListWidget

        short = QListWidget()
        short.addItem("only one")
        short.resize(300, 300)
        short.show()
        self.addCleanup(short.close)
        self.app.processEvents()
        wheel = self.motion.smooth_scroll(short, bounce=True)
        home = short.viewport().pos()
        wheel._pull(120)
        wheel._pull(-120)
        self.assertEqual(short.viewport().pos(), home)

    def test_it_is_off_unless_asked_for(self):
        from distill.ui import motion

        plain = motion.smooth_scroll(self.view)
        self.assertFalse(plain.bounce)

    def test_reduced_motion_never_moves_the_view(self):
        self.motion.Motion.reduce = True
        home = self.view.viewport().pos()
        self.turn(120)
        self.assertEqual(self.view.viewport().pos(), home)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class StatusLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_text_is_set_whatever_the_animation_does(self):
        from distill.ui.widgets import hint, StatusLabel

        label = hint("", live=True)
        self.assertIsInstance(label, StatusLabel)
        for text in ("one", "two", "two", "three"):  # includes a repeat and rapid changes
            label.setText(text)
            self.assertEqual(label.text(), text)

    def test_plain_hints_are_ordinary_labels(self):
        from distill.ui.widgets import hint, StatusLabel

        self.assertNotIsInstance(hint("x"), StatusLabel)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class RowEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

        from distill.ui import motion
        from distill.ui.widgets import RevealDelegate

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.tree = QTreeWidget()
        for i in range(4):
            self.tree.addTopLevelItem(QTreeWidgetItem([f"row {i}"]))
        self.reveal = RevealDelegate(self.tree)
        self.tree.setItemDelegate(self.reveal)
        self.tree.show()
        self.addCleanup(self.tree.close)
        self.app.processEvents()

    def test_fade_out_runs_the_removal_after_the_fade(self):
        done = []
        self.reveal.fade_out([0, 1], lambda: done.append(True))
        self.assertEqual(done, [], "not yet: the rows are still fading")
        anim = self.reveal._leaving[0]
        anim.setCurrentTime(anim.duration())
        self.assertEqual(done, [True])
        self.assertEqual(self.reveal._alpha, {}, "nothing is left half-faded")

    def test_fade_out_with_nothing_to_fade_or_no_motion_removes_at_once(self):
        done = []
        self.reveal.fade_out([], lambda: done.append("empty"))
        self.motion.Motion.reduce = True
        self.reveal.fade_out([0], lambda: done.append("reduced"))
        self.assertEqual(done, ["empty", "reduced"])

    def test_a_second_removal_finishes_the_first_instead_of_losing_it(self):
        done = []
        self.reveal.fade_out([0], lambda: done.append("first"))
        self.reveal.fade_out([1], lambda: done.append("second"))
        self.assertEqual(done, ["first"])
        anim = self.reveal._leaving[0]
        anim.setCurrentTime(anim.duration())
        self.assertEqual(done, ["first", "second"])

    def test_flash_fades_and_clears(self):
        from PySide6.QtGui import QColor

        self.reveal.flash(2, QColor("#3fb950"))
        self.assertIn(2, self.reveal._flash)
        anim = self.reveal._flash_anims[2]
        anim.setCurrentTime(anim.duration())
        self.assertNotIn(2, self.reveal._flash)
        self.assertNotIn(2, self.reveal._flash_anims)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class PinnedPageTests(unittest.TestCase):
    """A tool page: cards in a SizedStack that is pinned to the page's height, and another card outside the stack
    beneath it. Every painted frame of a fold must have the card, the pinned stack and the card below in agreement."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from distill.ui import motion
        from distill.ui.widgets import Expander, SizedStack

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.win = QWidget()
        column = QVBoxLayout(self.win)
        column.setSpacing(12)
        self.stack = SizedStack()
        self.page = QWidget()
        self.page.setProperty("compact", True)
        page_lay = QVBoxLayout(self.page)
        page_lay.setSpacing(12)
        self.card = Expander("CHD Settings", expanded=False)
        for i in range(3):
            label = QLabel("A setting with a description long enough to wrap onto a second line when narrow. " * 2 + str(i))
            label.setWordWrap(True)
            self.card.add(label)
        self.other = Expander("Workflow", expanded=False)
        page_lay.addWidget(self.other)
        page_lay.addWidget(self.card)
        self.stack.addWidget(self.page)
        column.addWidget(self.stack)
        self.below = Expander("Performance", expanded=False)  # outside the stack, like the Performance card
        column.addWidget(self.below)
        column.addStretch(1)
        self.win.resize(520, 760)
        self.win.show()
        self.addCleanup(self.win.close)
        self.app.processEvents()

    def step(self, fraction):
        """Move the fold to `fraction` of the way - and look at once, without letting any posted layout event run."""
        anim = self.card.body._slide_anim
        anim.setCurrentTime(round(anim.duration() * fraction))

    def agree(self):
        card, stack, below, page = self.card, self.stack, self.below, self.page
        need = page.layout().totalHeightForWidth(page.width())
        self.assertEqual(stack.height(), need, "the stack must be pinned to what the page needs, this very frame")
        self.assertLessEqual(card.geometry().bottom() + 1, page.height(), "the card must fit inside the page")
        self.assertGreaterEqual(below.geometry().top(), stack.geometry().bottom(),
                                "the card below must not overlap the stack")
        self.assertLessEqual(card.body.geometry().bottom() + 1, card.height(), "the content must stay inside its card")
        slack = card.height() - card.header.height() - (card.divider.height() if card.divider.isVisible() else 0)
        self.assertEqual(card.body.height(), slack, "no hollow space under the header: the card is exactly its content")

    def test_expanding_keeps_card_stack_and_neighbour_in_agreement_every_frame(self):
        self.card.header.setChecked(True)
        for fraction in (0.0, 0.15, 0.3, 0.5, 0.75, 1.0):
            with self.subTest(fraction=fraction):
                self.step(fraction)
                self.agree()

    def test_collapsing_keeps_them_in_agreement_every_frame(self):
        self.card.header.setChecked(True)
        self.step(1.0)
        self.app.processEvents()
        self.card.header.setChecked(False)
        for fraction in (0.0, 0.15, 0.3, 0.5, 0.75, 1.0):
            with self.subTest(fraction=fraction):
                self.step(fraction)
                self.agree()

    def test_reversing_half_way_still_agrees(self):
        self.card.header.setChecked(True)
        self.step(0.5)
        self.card.header.setChecked(False)
        for fraction in (0.0, 0.4, 1.0):
            with self.subTest(fraction=fraction):
                self.step(fraction)
                self.agree()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class ScrollAnchorTests(unittest.TestCase):
    """Closing a card on a page scrolled to its end must not scroll the page under it (everything above, and the
    header just clicked, slid down by the card's height - measured at 75 px in a recording)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

        from distill.ui import motion
        from distill.ui.widgets import Expander

        self.motion = motion
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self.cards = []
        for i in range(4):
            card = Expander(f"Card {i}", expanded=True)
            for j in range(3):
                label = QLabel("A setting with a description long enough to wrap onto a second line. " * 2)
                label.setWordWrap(True)
                card.add(label)
            lay.addWidget(card)
            self.cards.append(card)
        lay.addStretch(1)  # as on the real pages: the column absorbs spare height instead of stretching the cards
        self.inner = inner
        self.area.setWidget(inner)
        self.area.resize(520, 360)
        self.area.show()
        self.addCleanup(self.area.close)
        self.app.processEvents()
        self.bar = self.area.verticalScrollBar()
        self.bar.setValue(self.bar.maximum())  # scrolled to the end
        self.app.processEvents()

    def header_y(self, card):
        from PySide6.QtCore import QPoint

        return card.header.mapTo(self.area.viewport(), QPoint(0, 0)).y()

    def step(self, card, fraction):
        anim = card.body._slide_anim
        anim.setCurrentTime(round(anim.duration() * fraction))

    def test_closing_a_card_leaves_the_page_where_it_is(self):
        card = self.cards[1]
        value, y_above, y_clicked = self.bar.value(), self.header_y(self.cards[0]), self.header_y(card)
        self.assertGreater(value, 0, "the page must really be scrolled for this to mean anything")
        card.header.setChecked(False)
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(fraction=fraction):
                self.step(card, fraction)
                self.assertEqual(self.bar.value(), value, "the scroll position must not move during the fold")
                self.assertEqual(self.header_y(self.cards[0]), y_above, "the card above must not move")
                self.assertEqual(self.header_y(card), y_clicked, "the header that was clicked must stay under the cursor")

    def test_the_hold_lets_go_once_the_user_scrolls_into_the_real_range(self):
        card = self.cards[1]
        card.header.setChecked(False)
        self.step(card, 1.0)
        held = getattr(self.area, "_scroll_hold", None)
        self.assertIsNotNone(held, "scrolled past what the shorter page offers: kept, so nothing jumps")
        self.assertGreater(self.inner.minimumHeight(), 0)
        self.bar.setValue(0)
        self.assertIsNone(getattr(self.area, "_scroll_hold", None))
        self.assertEqual(self.inner.minimumHeight(), 0, "the page is its real height again")

    def test_a_page_that_was_not_at_the_end_holds_nothing_afterwards(self):
        self.bar.setValue(2)
        card = self.cards[3]
        card.header.setChecked(False)
        self.step(card, 1.0)
        self.assertIsNone(getattr(self.area, "_scroll_hold", None))
        self.assertEqual(self.inner.minimumHeight(), 0)

    def test_changing_page_drops_the_hold_at_once(self):
        card = self.cards[1]
        card.header.setChecked(False)
        self.step(card, 1.0)
        self.motion.release_scroll_hold(card)
        self.assertIsNone(getattr(self.area, "_scroll_hold", None))
        self.assertEqual(self.inner.minimumHeight(), 0)

    def test_opening_a_card_does_not_move_the_page(self):
        card = self.cards[1]
        card.header.setChecked(False)
        self.step(card, 1.0)
        self.bar.setValue(0)
        self.app.processEvents()
        value, y_clicked = self.bar.value(), self.header_y(card)
        card.header.setChecked(True)
        for fraction in (0.0, 0.5, 1.0):
            with self.subTest(fraction=fraction):
                self.step(card, fraction)
                self.assertEqual(self.bar.value(), value)
                self.assertEqual(self.header_y(card), y_clicked)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class SmoothEverywhereTests(unittest.TestCase):
    """Every scroll surface gets the glide when it first appears - except the ones it would break."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from distill.ui import motion

        self.motion = motion
        self.attach = motion._ScrollEverywhere()

    def shown(self, view):
        from PySide6.QtGui import QShowEvent

        view.show()
        self.addCleanup(view.close)
        self.attach.eventFilter(view, QShowEvent())
        return view

    def test_lists_trees_tables_pages_and_browsers_get_it(self):
        from PySide6.QtWidgets import QListWidget, QScrollArea, QTableWidget, QTextBrowser, QTreeWidget

        for make in (QListWidget, QTreeWidget, QTableWidget, QScrollArea, QTextBrowser):
            with self.subTest(view=make.__name__):
                view = self.shown(make())
                self.assertIsNotNone(getattr(view, "_smooth_wheel", None))
                self.assertTrue(view._smooth_wheel.bounce)

    def test_item_views_are_switched_to_pixel_scrolling(self):
        from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

        view = self.shown(QTreeWidget())
        self.assertEqual(view.verticalScrollMode(), QAbstractItemView.ScrollMode.ScrollPerPixel)

    def test_a_line_scrolling_log_is_left_alone(self):
        from PySide6.QtWidgets import QPlainTextEdit

        self.assertIsNone(getattr(self.shown(QPlainTextEdit()), "_smooth_wheel", None))

    def test_headers_are_left_alone(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHeaderView

        self.assertFalse(self.motion.wants_smooth_scroll(QHeaderView(Qt.Orientation.Horizontal)))

    def test_popups_such_as_a_combo_box_list_are_left_alone(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QListWidget

        popup = QListWidget()
        popup.setWindowFlags(Qt.WindowType.Popup)
        self.assertFalse(self.motion.wants_smooth_scroll(popup))

    def test_it_is_attached_once_and_asking_again_returns_the_same_one(self):
        from PySide6.QtGui import QShowEvent
        from PySide6.QtWidgets import QListWidget

        view = self.shown(QListWidget())
        first = view._smooth_wheel
        self.attach.eventFilter(view, QShowEvent())
        self.assertIs(view._smooth_wheel, first)
        self.assertIs(self.motion.smooth_scroll(view), first)


if __name__ == "__main__":
    unittest.main()
