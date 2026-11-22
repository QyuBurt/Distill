import unittest

from distill.ui.theme import T


class _Gone:
    """Stands in for a widget whose Qt object was deleted while its Python wrapper is still alive."""

    def isAncestorOf(self, _w):
        raise RuntimeError("Internal C++ object (QFrame) already deleted.")


class FlushTests(unittest.TestCase):
    def test_flush_skips_a_widget_whose_qt_object_is_gone(self):
        gone, root = _Gone(), object()
        T._bound[id(gone)] = (lambda: gone, lambda: None)
        T._stale.add(id(gone))
        self.addCleanup(T._bound.pop, id(gone), None)
        self.addCleanup(T._stale.discard, id(gone))
        T.flush(root)  # used to raise RuntimeError and show the "Something unexpected went wrong" box when switching pages
        self.assertNotIn(id(gone), T._stale)


if __name__ == "__main__":
    unittest.main()
