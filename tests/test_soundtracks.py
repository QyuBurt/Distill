"""Soundtrack pulling: matching, layout and the archive.org / pasted-link providers (no network: fetches are stubbed)."""
from __future__ import annotations

import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from tests.helpers import IsolatedSoundtrackFiles, TempDirTest

from distill import soundtracks as st
from distill.downloads import LinkError, RemoteFile


def meta(*files):
    return {"files": [dict(f) for f in files]}


class MatchingTests(unittest.TestCase):
    def test_exact_and_noisy_titles_match(self):
        self.assertGreaterEqual(st.match_score("Persona 5 (USA)", "Persona 5 Original Soundtrack"), st.MATCH_MIN)
        self.assertGreaterEqual(st.match_score("The Legend of Zelda", "Legend of Zelda - OST"), st.MATCH_MIN)

    def test_similar_numbered_titles_do_not_match(self):
        self.assertLess(st.match_score("Final Fantasy VII", "Final Fantasy VIII Original Soundtrack"), st.MATCH_MIN)
        self.assertLess(st.match_score("Persona 4", "Persona 5 Original Soundtrack"), st.MATCH_MIN)

    def test_contiguous_phrase_beats_scattered_words(self):
        sm64 = st.match_score("Mario 64", "Super Mario 64 Original Soundtrack")
        kart = st.match_score("Mario 64", "Mario Kart 64 Original Soundtrack")
        self.assertGreaterEqual(sm64, st.MATCH_MIN)
        self.assertLess(kart, st.MATCH_MIN)
        self.assertGreater(sm64, kart)

    def test_capitalization_never_matters(self):
        self.assertEqual(st.match_score("MARIO 64", "super MARIO 64 ost"), st.match_score("mario 64", "Super Mario 64 OST"))

    def test_unrelated_is_zero_ish(self):
        self.assertLess(st.match_score("Metroid Prime", "Piano Favourites"), 0.3)

    def test_search_name_drops_tags(self):
        self.assertEqual(st.search_name("Resident Evil 2 (USA) (Disc 1) [!]"), "Resident Evil 2")


class ParsingTests(TempDirTest):
    def test_game_lines(self):
        reqs = st.parse_game_lines("# list\nPersona 5\n\nChrono Trigger = https://archive.org/details/x\npersona 5\nhttps://nameless")
        self.assertEqual([(r.name, r.link) for r in reqs],
                         [("Persona 5", None), ("Chrono Trigger", "https://archive.org/details/x")])

    def test_names_from_folder(self):
        for n in ("Zelda", "Metroid", ".hidden"):
            (self.tmp / n).mkdir()
        (self.tmp / "file.txt").write_text("x")
        self.assertEqual(st.game_names_from_folder(self.tmp), ["Metroid", "Zelda"])


class LayoutTests(unittest.TestCase):
    def test_folder_and_numbering(self):
        tracks = [st.Track("u2", ".mp3", "Second: Theme?", 2), st.Track("u1", ".mp3", "Opening", 1)]
        files = st.layout("Game (USA)", tracks)
        self.assertEqual([f.name for f in files],
                         ["Game (USA)/soundtrack/01 - Opening.mp3", "Game (USA)/soundtrack/02 - Second_ Theme_.mp3"])
        self.assertEqual([f.url for f in files], ["u1", "u2"])

    def test_unnumbered_tracks_are_numbered_by_position(self):
        files = st.layout("G", [st.Track("a", ".flac", "A"), st.Track("b", ".flac", "B")])
        self.assertEqual([f.name.rsplit("/", 1)[1] for f in files], ["01 - A.flac", "02 - B.flac"])

    def test_discs_stay_in_their_own_folders_and_duplicates_get_suffixes(self):
        tracks = [st.Track("a", ".mp3", "Intro", 1, sub="CD1/"), st.Track("b", ".mp3", "Intro", 1, sub="CD2/"),
                  st.Track("c", ".mp3", "Intro", 1, sub="CD2/")]
        names = [f.name for f in st.layout("G", tracks)]
        self.assertIn("G/soundtrack/CD1/01 - Intro.mp3", names)
        self.assertIn("G/soundtrack/CD2/01 - Intro.mp3", names)
        self.assertIn("G/soundtrack/CD2/01 - Intro (2).mp3", names)

    def test_tracks_are_ordered_by_disc_then_track_and_discs_get_their_own_folders(self):
        tracks = [st.Track(f"u{d}{n}", ".flac", f"T{d}.{n}", n, disc=d) for n in (1, 2) for d in (4, 2, 1, 3)]
        names = [f.name.split("/", 2)[2] for f in st.layout("SMO", tracks)]
        self.assertEqual(names, [f"Disc {d}/0{n} - T{d}.{n}.flac" for d in (1, 2, 3, 4) for n in (1, 2)])

    def test_a_single_disc_needs_no_disc_folder_and_disc_ten_follows_disc_nine(self):
        one = st.layout("G", [st.Track("a", ".mp3", "A", 2, disc=2), st.Track("b", ".mp3", "B", 1, disc=2)])
        self.assertEqual([f.name.split("/", 2)[2] for f in one], ["01 - B.mp3", "02 - A.mp3"])
        folders = [st.Track("a", ".mp3", "A", 1, sub="Disc 10/"), st.Track("b", ".mp3", "B", 1, sub="Disc 9/")]
        self.assertEqual([f.url for f in st.layout("G", folders)], ["b", "a"])

    def test_disc_is_read_from_the_file_name_or_tags(self):
        self.assertEqual(st.split_disc("Disc 2 - 05 Boss"), (2, "05 Boss"))
        self.assertEqual(st.split_disc("d3t07 Boss"), (3, "07 Boss"))
        self.assertEqual(st.split_disc("1-05 Boss"), (1, "05 Boss"))
        self.assertEqual(st.split_disc("01 - Boss"), (None, "01 - Boss"))
        self.assertEqual(st._disc_and_track("2-05"), (2, 5))
        self.assertEqual(st._disc_and_track("5/22"), (None, 5))

    def test_escaping_names_stay_inside_the_game_folder(self):
        f = st.layout("../evil", [st.Track("a", ".mp3", "../../x", 1)])[0]
        self.assertNotIn("..", f.name.split("/"))
        self.assertEqual(len(f.name.split("/")), 3)

    def test_tidy_title(self):
        self.assertEqual(st.tidy_title("01 - Main Theme"), (1, "Main Theme"))
        self.assertEqual(st.tidy_title("Track 07. Boss"), (7, "Boss"))
        self.assertEqual(st.tidy_title("1942 Theme"), (None, "1942 Theme"))


class ArchiveProviderTests(IsolatedSoundtrackFiles):
    ITEM = meta(
        {"name": "01 Opening.flac", "source": "original", "format": "Flac", "track": "1", "title": "Opening", "size": "10"},
        {"name": "01 Opening.mp3", "source": "derivative", "format": "VBR MP3", "size": "3"},
        {"name": "01 Opening_64kb.mp3", "source": "derivative", "format": "64Kbps MP3"},
        {"name": "02 Battle.mp3", "source": "original", "format": "MP3", "title": "Battle", "track": "2/12"},
        {"name": "02 Battle.ogg", "source": "derivative", "format": "Ogg Vorbis"},
        {"name": "cover.jpg", "source": "original", "format": "JPEG"},
        {"name": "__ia_thumb.jpg", "source": "original", "format": "Item Image"},
    )

    def provider_with(self, payload):
        return mock.patch.object(st, "fetch_json", return_value=payload)

    def test_search_scores_and_orders_candidates(self):
        docs = {"response": {"docs": [
            {"identifier": "wrong", "title": "Piano Favourites"},
            {"identifier": "p5", "title": "Persona 5 Original Soundtrack", "creator": ["Shoji Meguro"]},
            {"identifier": "", "title": "broken"}]}}
        with self.provider_with(docs) as fetch:
            res = st.find_albums([st.GameRequest("Persona 5")], pause=0)
        self.assertIn("advancedsearch.php", fetch.call_args_list[0][0][0])
        self.assertEqual(res[0].albums[0].ident, "p5")
        self.assertEqual(res[0].albums[0].detail, "Shoji Meguro")
        self.assertIs(res[0].best, res[0].albums[0])

    def test_search_runs_phrase_and_any_order_queries_merged_without_duplicates(self):
        docs = {"response": {"docs": [{"identifier": "sm64", "title": "Super Mario 64 OST"}]}}
        with self.provider_with(docs) as fetch:
            res = st.find_albums([st.GameRequest("Mario 64")], pause=0)
        urls = [c[0][0] for c in fetch.call_args_list if "advancedsearch" in c[0][0]]
        self.assertEqual(len(urls), 2)
        self.assertIn("%22Mario+64%22", urls[0])
        self.assertIn("Mario+AND+64", urls[1])
        self.assertIn("rows=50", urls[0])
        self.assertEqual([a.ident for a in res[0].albums], ["sm64"])  # same item from both queries, kept once

    def test_albums_with_separate_tracks_are_favoured(self):
        one_file = meta({"name": "all.mp3", "source": "original", "format": "MP3", "title": "Full album"})
        split = meta({"name": "01 A.mp3", "source": "original", "format": "MP3"},
                     {"name": "02 B.mp3", "source": "original", "format": "MP3"})
        items = {"single": one_file, "split": split}

        def fake(url, timeout=30):
            if "advancedsearch" in url:
                return {"response": {"docs": [{"identifier": "single", "title": "Persona 5 Soundtrack"},
                                              {"identifier": "split", "title": "Persona 5 Original Soundtrack"}]}}
            return next(v for k, v in items.items() if f"/metadata/{k}" in url)

        with mock.patch.object(st, "fetch_json", side_effect=fake):
            res = st.find_albums([st.GameRequest("Persona 5")], pause=0)
        self.assertEqual([a.ident for a in res[0].albums], ["split", "single"])
        self.assertEqual([a.playable for a in res[0].albums], [2, 1])

    def test_albums_carry_their_cover_address(self):
        docs = {"response": {"docs": [{"identifier": "my item", "title": "Persona 5 Original Soundtrack"}]}}
        with self.provider_with(docs):
            res = st.find_albums([st.GameRequest("Persona 5")], pause=0)
        self.assertEqual(res[0].albums[0].art, "https://archive.org/services/img/my%20item")

    def test_poor_match_is_offered_but_not_chosen(self):
        docs = {"response": {"docs": [{"identifier": "ff8", "title": "Final Fantasy VIII Soundtrack"}]}}
        with self.provider_with(docs):
            res = st.find_albums([st.GameRequest("Final Fantasy VII")], pause=0)
        self.assertEqual(len(res[0].albums), 1)
        self.assertIsNone(res[0].best)

    def test_network_failure_is_reported_not_raised(self):
        with mock.patch.object(st, "fetch_json", side_effect=LinkError("Network error: down")):
            res = st.find_albums([st.GameRequest("X")], pause=0)
        self.assertIn("Network error", res[0].error)

    def test_best_quality_prefers_flac_then_original_mp3(self):
        album = st.Album("archive.org", "item", "t")
        with self.provider_with(self.ITEM):
            tracks = st.ArchiveOrgProvider().tracks(album)
        self.assertEqual(sorted((t.ext, t.title, t.number) for t in tracks),
                         [(".flac", "Opening", 1), (".mp3", "Battle", 2)])
        self.assertTrue(next(t for t in tracks if t.ext == ".flac").lossless)

    def test_discs_in_one_flat_item_come_out_in_disc_order(self):
        album = st.Album("archive.org", "item", "t")
        item = meta(*({"name": f"Disc {d} - {n:02d} Song {d}.{n}.flac", "source": "original", "format": "Flac", "track": str(n)}
                      for n in (2, 1) for d in (2, 1)))
        with self.provider_with(item):
            tracks = st.ArchiveOrgProvider().tracks(album)
        got = [f.name.split("/", 2)[2] for f in st.layout("G", tracks)]
        self.assertEqual(got, ["Disc 1/01 - Song 1.1.flac", "Disc 1/02 - Song 1.2.flac",
                               "Disc 2/01 - Song 2.1.flac", "Disc 2/02 - Song 2.2.flac"])

    def test_mp3_preference(self):
        album = st.Album("archive.org", "item", "t")
        with self.provider_with(self.ITEM):
            tracks = st.ArchiveOrgProvider().tracks(album, prefer="mp3")
        self.assertEqual({t.ext for t in tracks}, {".mp3"})
        self.assertEqual(sorted(t.title for t in tracks), ["Battle", "Opening"])

    def test_files_for_builds_the_game_folder(self):
        album = st.Album("archive.org", "my item", "t")
        with self.provider_with(self.ITEM):
            files = st.files_for("Persona 5 (USA)", album)
        self.assertEqual([f.name for f in files],
                         ["Persona 5 (USA)/soundtrack/01 - Opening.flac", "Persona 5 (USA)/soundtrack/02 - Battle.mp3"])
        self.assertTrue(files[0].url.startswith("https://archive.org/download/my%20item/"))
        self.assertTrue(all(f.group == "" for f in files))  # 'Folder per item' must not add a second folder

    def test_album_gets_the_items_own_cover_file_over_the_generic_image(self):
        album = st.Album("archive.org", "my item", "t", art="https://archive.org/services/img/my%20item")
        item = meta({"name": "01 A.mp3", "source": "original", "format": "MP3"},
                    {"name": "__ia_thumb.jpg", "source": "original", "format": "Item Tile"},
                    {"name": "Scans/Front Cover.jpg", "source": "original", "format": "JPEG", "size": "120000"})
        with self.provider_with(item):
            st.ArchiveOrgProvider().tracks(album)
        self.assertEqual(album.art, "https://archive.org/download/my%20item/Scans/Front%20Cover.jpg")

    def test_thumbnail_file_is_used_when_there_is_no_cover_file_and_generic_image_when_neither(self):
        thumb = st.Album("archive.org", "x", "t", art="generic")
        with self.provider_with(meta({"name": "01 A.mp3"}, {"name": "__ia_thumb.jpg"}, {"name": "back.jpg"})):
            st.ArchiveOrgProvider().tracks(thumb)
        self.assertEqual(thumb.art, "https://archive.org/download/x/__ia_thumb.jpg")
        bare = st.Album("archive.org", "x", "t", art="generic")
        with self.provider_with(meta({"name": "01 A.mp3"}, {"name": "screenshot 3.png"})):
            st.ArchiveOrgProvider().tracks(bare)
        self.assertEqual(bare.art, "generic")

    def test_pick_cover_rules(self):
        pick = st.pick_cover
        self.assertEqual(pick([("cover.jpg", 10), ("front.png", 10)]), "cover.jpg")
        self.assertEqual(pick([("__ia_thumb.jpg", 5), ("x_itemimage.jpg", 5)]), "x_itemimage.jpg")
        self.assertEqual(pick([("cover.jpg", st.COVER_MAX_BYTES + 1), ("__ia_thumb.jpg", 5)]), "__ia_thumb.jpg")
        self.assertIsNone(pick([("back cover.jpg", 1), ("cd1.png", 1), ("Disc 2.jpg", 1), ("booklet.jpg", 1)]))
        self.assertIsNone(pick([("track01.mp3", 1), ("notes.txt", 1), ("page 4.jpg", 1)]))
        self.assertEqual(pick([("Album.JPG", None)]), "Album.JPG")

    def test_item_without_audio_is_an_error(self):
        with self.provider_with(meta({"name": "readme.txt"})):
            with self.assertRaises(LinkError):
                st.ArchiveOrgProvider().tracks(st.Album("archive.org", "x", "t"))


class LinkProviderTests(unittest.TestCase):
    def test_pasted_link_is_refiled_under_the_game(self):
        listing = [RemoteFile("Disc 1/01 - Intro.mp3", "https://h/1", 5, "item"),
                   RemoteFile("Disc 1/02 - Town.mp3", "https://h/2", 6, "item"),
                   RemoteFile("art/cover.jpg", "https://h/3", 1, "item"),
                   RemoteFile("meta.xml", "https://h/4", 1, "item", extra=True)]
        provider = st.LinkProvider(resolver=lambda text: (listing, []))
        tracks = provider.tracks(provider.album_for("https://example.org/ost/"))
        names = [f.name for f in st.layout("Chrono Trigger", tracks)]
        self.assertEqual(names, ["Chrono Trigger/soundtrack/Disc 1/01 - Intro.mp3",
                                 "Chrono Trigger/soundtrack/Disc 1/02 - Town.mp3"])

    def test_pasted_archive_link_gets_the_items_cover_other_links_none(self):
        provider = st.LinkProvider()
        self.assertEqual(provider.album_for("https://archive.org/details/some-ost").art,
                         "https://archive.org/services/img/some-ost")
        self.assertEqual(provider.album_for("https://example.org/music/").art, "")
        self.assertEqual(provider.album_for("https://archive.org/").art, "")  # not an item link: no art, no error

    def test_pinned_link_skips_search(self):
        with mock.patch.object(st, "fetch_json", side_effect=AssertionError("searched")):
            res = st.find_albums([st.GameRequest("Chrono Trigger", "https://example.org/x")], pause=0)
        self.assertEqual(res[0].albums[0].provider, "link")
        self.assertIsNotNone(res[0].best)

    def test_pasted_link_uses_a_cover_image_found_at_it(self):
        listing = [RemoteFile("01 - Intro.mp3", "https://h/1", 5, "item"),
                   RemoteFile("art/cover.jpg", "https://h/cover", 9, "item"),
                   RemoteFile("art/back.jpg", "https://h/back", 9, "item")]
        provider = st.LinkProvider(resolver=lambda text: (listing, []))
        album = provider.album_for("https://example.org/ost/")
        provider.tracks(album)
        self.assertEqual(album.art, "https://h/cover")

    def test_link_with_no_audio(self):
        provider = st.LinkProvider(resolver=lambda text: ([RemoteFile("a.txt", "u", 1)], []))
        with self.assertRaises(LinkError):
            provider.tracks(provider.album_for("x"))


class AlreadyThereTests(TempDirTest):
    def test_counts_audio_in_the_game_soundtrack_folder(self):
        self.write("Soundtracks/Game/01 - A.mp3")
        self.write("Soundtracks/Game/CD2/02 - B.flac")
        self.write("Soundtracks/Game/cover.jpg")
        self.assertEqual(st.already_there(self.tmp, "Game"), 2)
        self.assertEqual(st.already_there(self.tmp, "Other"), 0)
        self.write("snes/Zelda/z.sfc")
        self.write("snes/Zelda/soundtrack/01.flac")
        self.assertEqual(st.already_there(self.tmp, "Zelda", st.SAVE_ROMM), 1)


class SavePlanTests(IsolatedSoundtrackFiles, TempDirTest):
    def files(self, game="Chrono Trigger"):
        return [(game, [RemoteFile(f"{game}/soundtrack/01 - A.flac", "u1", 1), RemoteFile(f"{game}/soundtrack/CD2/02 - B.flac", "u2", 1)])]

    def test_standard_layout(self):
        plan = st.plan_save(self.files(), st.SAVE_STANDARD, self.tmp)
        self.assertEqual([f.name for f in plan.files],
                         ["Soundtracks/Chrono Trigger/01 - A.flac", "Soundtracks/Chrono Trigger/CD2/02 - B.flac"])
        self.assertEqual(plan.skipped, [])

    def test_romm_game_in_its_own_folder_beside_the_game(self):
        self.write("snes/Chrono Trigger/Chrono Trigger.sfc")
        plan = st.plan_save(self.files(), st.SAVE_ROMM, self.tmp)
        self.assertEqual(plan.files[0].name, "snes/Chrono Trigger/soundtrack/01 - A.flac")
        self.assertEqual(plan.moves, {})

    def test_romm_loose_file_gets_a_folder_first(self):
        game = self.write("snes/Chrono Trigger (USA).sfc")
        plan = st.plan_save(self.files("Chrono Trigger"), st.SAVE_ROMM, self.tmp)
        self.assertEqual(plan.files[0].name, "snes/Chrono Trigger (USA)/soundtrack/01 - A.flac")
        self.assertEqual(plan.moves["Chrono Trigger"],
                         [("move", str(game), str(self.tmp / "snes/Chrono Trigger (USA)/Chrono Trigger (USA).sfc"))])
        self.assertTrue(game.exists())  # planning never touches the disk

    def test_romm_no_match_and_ambiguous_are_skipped(self):
        self.write("snes/Chrono Trigger/a.sfc")
        self.write("gba/Chrono Trigger/a.gba")
        plan = st.plan_save(self.files() + self.files("Other"), st.SAVE_ROMM, self.tmp)
        self.assertEqual(plan.files, [])
        self.assertEqual(sorted(g for g, _ in plan.skipped), ["Chrono Trigger", "Other"])
        self.assertEqual(len(plan.ambiguous["Chrono Trigger"]), 2)
        picked = st.plan_save(self.files(), st.SAVE_ROMM, self.tmp, picks={"Chrono Trigger": plan.ambiguous["Chrono Trigger"][1]})
        self.assertEqual(len(picked.files), 2)

    def test_scan_treats_platform_folders_as_containers_and_groups_cue_bin(self):
        self.write("snes/Game A/a.sfc")
        self.write("psx/Game B.cue", 'FILE "Game B.bin" BINARY\n')
        self.write("psx/Game B.bin")
        self.write("psx/readme.txt")
        self.assertEqual(st.game_names_from_folder(self.tmp), ["Game A", "Game B"])
        b = next(e for e in st.scan_games(self.tmp) if e.key == "game b")
        self.assertEqual(sorted(p.name for p in b.files), ["Game B.bin", "Game B.cue"])


class AliasParsingTests(unittest.TestCase):
    def test_other_name_link_and_both(self):
        reqs = st.parse_game_lines(
            "Plain\nSuper Mario 64 = Mario 64 OST\nZelda = https://archive.org/details/x?a=b\n"
            "Chrono = Chrono Trigger OST = https://archive.org/details/y?q=1")
        self.assertEqual([(r.name, r.search, r.link) for r in reqs], [
            ("Plain", None, None),
            ("Super Mario 64", "Mario 64 OST", None),
            ("Zelda", None, "https://archive.org/details/x?a=b"),
            ("Chrono", "Chrono Trigger OST", "https://archive.org/details/y?q=1"),
        ])

    def test_search_uses_the_other_name_but_keeps_the_folder_name(self):
        seen = []

        class P:
            name = "fake"

            def search(self, game):
                seen.append(game)
                return [st.Album("fake", "i", "Mario 64 OST", "", 0.9)]

            def tracks(self, album, prefer="best"):
                return []

        with mock.patch.object(st, "CACHE_FILE", st.CACHE_FILE):
            res = st.find_albums(st.parse_game_lines("Super Mario 64 = Mario 64 OST"), providers=[P()], pause=0,
                                 use_cache=False)
        self.assertEqual(seen, ["Mario 64 OST"])
        self.assertEqual(res[0].request.name, "Super Mario 64")


class SearchCacheTests(IsolatedSoundtrackFiles):
    class Counting:
        name = "fake"

        def __init__(self):
            self.searches = 0

        def search(self, game):
            self.searches += 1
            return [st.Album("fake", "id", "Persona 5 OST", "x", 0.95)]

        def tracks(self, album, prefer="best"):
            return [st.Track("01 A.flac", "u", 1), st.Track("02 B.flac", "u2", 1)]

    def test_second_search_is_remembered_and_fresh_bypasses_it(self):
        p = self.Counting()
        reqs = [st.GameRequest("Persona 5")]
        st.find_albums(reqs, providers=[p], pause=0)
        res = st.find_albums(reqs, providers=[p], pause=0)
        self.assertEqual(p.searches, 1)
        self.assertEqual(res[0].albums[0].ident, "id")
        self.assertEqual(res[0].albums[0].playable, 2)
        st.find_albums(reqs, providers=[p], pause=0, use_cache=False)
        self.assertEqual(p.searches, 2)

    def test_old_entries_expire(self):
        p = self.Counting()
        reqs = [st.GameRequest("Persona 5")]
        st.find_albums(reqs, providers=[p], pause=0)
        with mock.patch.object(st.time, "time", return_value=st.time.time() + 4 * 86400):
            st.find_albums(reqs, providers=[p], pause=0)
        self.assertEqual(p.searches, 2)


class UndoSaveTests(IsolatedSoundtrackFiles, TempDirTest):
    def test_undo_removes_only_what_the_save_made_and_empty_folders(self):
        kept = self.write("Soundtracks/Old Game/01.flac")
        new1 = self.write("Soundtracks/New Game/CD1/01.flac")
        part = self.write("Soundtracks/New Game/CD1/02.flac.part")
        new2 = self.tmp / "Soundtracks/New Game/CD1/02.flac"
        st.record_save([new1, new2], self.tmp, moved=False)
        self.assertEqual(st.saves_depth(), 1)
        removed, restored, problems = st.undo_last_save()
        self.assertEqual((removed, restored, problems), (1, 0, []))
        self.assertFalse(new1.exists() or part.exists())
        self.assertFalse((self.tmp / "Soundtracks/New Game").exists())  # emptied folders go too
        self.assertTrue(kept.exists() and self.tmp.exists())
        self.assertIsNone(st.undo_last_save())

    def test_undo_puts_back_moved_games(self):
        with mock.patch("distill.organize.undo_last", return_value=(2, [])) as undo:
            st.record_save([], self.tmp, moved=True)
            self.assertEqual(st.undo_last_save(), (0, 2, []))
        undo.assert_called_once_with("soundtrack")


class TagTests(TempDirTest):
    def test_parse_track_name_and_disc(self):
        from distill import soundtrack_tags as tags
        from pathlib import Path

        self.assertEqual(tags.parse_track(Path("x/CD2/05 - Boss Battle.flac")), (5, "Boss Battle", 2))
        self.assertEqual(tags.parse_track(Path("x/Title only.mp3")), (None, "Title only", None))

    def test_only_missing_tags_are_filled(self):
        from distill import soundtrack_tags as tags

        saved = []

        class Easy(dict):
            tags = object()

            def save(self):
                saved.append(dict(self))

        audio = Easy(title=["Keep me"])
        fake = mock.Mock()
        fake.File.return_value = audio
        path = self.write("Game/soundtrack/03 - Theme.flac")
        with mock.patch.object(tags, "_mutagen", return_value=fake):
            out = tags.tag_file(path, tags.TagInfo("The Album", "The Artist", total=10))
        self.assertEqual(out, "tagged")
        self.assertEqual(audio["title"], ["Keep me"])  # never overwritten
        self.assertEqual(audio["album"], ["The Album"])
        self.assertEqual(audio["tracknumber"], ["3/10"])
        self.assertEqual(len(saved), 1)

    def test_without_mutagen_nothing_happens(self):
        from distill import soundtrack_tags as tags

        path = self.write("a/01 - A.flac")
        with mock.patch.object(tags, "_mutagen", return_value=None):
            self.assertIn("mutagen", tags.tag_file(path, tags.TagInfo("x")))

    def test_cover_file_is_not_overwritten(self):
        from distill import soundtrack_tags as tags

        self.write("a/cover.png", b"mine")
        self.assertIsNone(tags.save_cover_file(self.tmp / "a", b"new", "image/jpeg"))
        self.assertEqual((self.tmp / "b").exists(), False)


class TooltipWrapTests(unittest.TestCase):
    def test_long_lines_wrap_and_short_or_aligned_lines_stay(self):
        from distill.tooltips import wrap_tooltip

        long = "Fills in missing title, album, artist, track and disc numbers and the cover so players show them."
        out = wrap_tooltip(long, 40)
        self.assertTrue(all(len(line) <= 40 for line in out.split("\n")))
        self.assertEqual(" ".join(out.split()), long)
        aligned = "RomM:  <Games>/<Game>\n                 /soundtrack/01.flac"
        self.assertEqual(wrap_tooltip(aligned, 30), aligned)

    def test_html_output_keeps_angle_brackets_and_breaks(self):
        from distill.tooltips import looks_like_html, tooltip_html

        out = tooltip_html("<Save to>/Soundtracks/<Game>\nnext", 56)
        self.assertIn("&lt;Save to&gt;", out)
        self.assertIn("\nnext", out)
        self.assertFalse(looks_like_html("<Save to>/x"))
        self.assertTrue(looks_like_html("<b>Bold</b> tip"))


if __name__ == "__main__":
    unittest.main()
