"""The shared file-system primitives in system.py, and the chunked CRC that uses them."""
from __future__ import annotations

import threading
import unittest
import zlib
from unittest import mock

from distill import formats, models, organize, system

from tests.helpers import TempDirTest


class TreeTests(TempDirTest):
    def make(self):
        (self.tmp / "a" / "b").mkdir(parents=True)
        (self.tmp / "top.bin").write_bytes(b"x" * 10)
        (self.tmp / "a" / "Mid.BIN").write_bytes(b"x" * 20)
        (self.tmp / "a" / "b" / "deep.bin").write_bytes(b"x" * 30)

    def test_iter_files_lists_relative_posix_names_and_sizes(self):
        self.make()
        self.assertEqual(dict(system.iter_files(self.tmp)), {"top.bin": 10, "a/Mid.BIN": 20, "a/b/deep.bin": 30})

    def test_tree_size_of_file_folder_and_nothing(self):
        self.make()
        self.assertEqual(system.tree_size(self.tmp), 60)
        self.assertEqual(system.tree_size(self.tmp / "top.bin"), 10)
        self.assertEqual(system.tree_size(self.tmp / "missing"), 0)
        self.assertEqual(models.path_bytes(self.tmp), 60)
        self.assertEqual(system._tree_size(None), -1)

    def test_manifest_keys_are_lowercase(self):
        self.make()
        self.assertEqual(formats.tree_manifest(self.tmp), {"top.bin": 10, "a/mid.bin": 20, "a/b/deep.bin": 30})

    def test_a_file_that_vanishes_mid_scan_is_ignored(self):
        self.make()
        real = system.os.scandir

        class Flaky:
            def __init__(self, entry): self.e = entry
            name, path = property(lambda s: s.e.name), property(lambda s: s.e.path)
            def is_dir(self, **kw): return self.e.is_dir(**kw)
            def is_file(self, **kw): return self.e.is_file(**kw)
            def stat(self):
                if self.e.name == "top.bin":
                    raise FileNotFoundError
                return self.e.stat()

        def scandir(p):
            ctx = real(p)
            class Wrap:
                def __enter__(s): return (Flaky(e) for e in ctx.__enter__())
                def __exit__(s, *a): return ctx.__exit__(*a)
            return Wrap()
        with mock.patch.object(system.os, "scandir", scandir):
            self.assertEqual(system.tree_size(self.tmp), 50)

    def test_unreadable_folder_is_skipped(self):
        self.make()
        with mock.patch.object(system.os, "scandir", side_effect=PermissionError):
            self.assertEqual(system.tree_size(self.tmp), 0)


class SmallHelperTests(TempDirTest):
    def test_read_head_reads_only_what_it_is_asked_for(self):
        f = self.tmp / "big.bin"
        f.write_bytes(b"abcdef" * 1000)
        self.assertEqual(system.read_head(f, 4), b"abcd")
        self.assertEqual(system.read_head(f, 10**9), f.read_bytes())
        with self.assertRaises(OSError):
            system.read_head(self.tmp / "nope", 4)

    def test_free_bytes_asks_the_nearest_existing_folder(self):
        free = system.free_bytes(self.tmp / "not" / "made" / "yet")
        self.assertIsInstance(free, int)
        self.assertGreater(free, 0)

    def test_free_bytes_is_none_when_nothing_answers(self):
        with mock.patch.object(system.shutil, "disk_usage", side_effect=OSError):
            self.assertIsNone(system.free_bytes(self.tmp))


class CrcTests(TempDirTest):
    def test_crc_with_and_without_skip(self):
        f = self.tmp / "rom.bin"
        data = bytes(range(256)) * 50
        f.write_bytes(data)
        stop = threading.Event()
        self.assertEqual(organize._crc32_hex(f, stop), f"{zlib.crc32(data):08x}")
        self.assertEqual(organize._crc32_hex(f, stop, skip=512), f"{zlib.crc32(data[512:]):08x}")

    def test_crc_across_chunks_matches(self):
        f = self.tmp / "big.bin"
        data = b"0123456789" * (1 << 20)  # 10 MB > two 4 MB chunks
        f.write_bytes(data)
        self.assertEqual(organize._crc32_hex(f, threading.Event()), f"{zlib.crc32(data):08x}")

    def test_stop_interrupts_the_skip_variant_too(self):
        f = self.tmp / "rom.bin"
        f.write_bytes(b"x" * 1000)
        stop = threading.Event()
        stop.set()
        self.assertIsNone(organize._crc32_hex(f, stop, skip=512))

    def test_unreadable_is_none(self):
        self.assertIsNone(organize._crc32_hex(self.tmp / "nope", threading.Event()))


if __name__ == "__main__":
    unittest.main()
