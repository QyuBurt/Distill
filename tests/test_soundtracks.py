"""Soundtrack pulling: matching, layout and the archive.org / pasted-link providers (no network: fetches are stubbed)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from tests.helpers import IsolatedSoundtrackFiles, TempDirTest, enter

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
            res = st.find_albums([st.GameRequest("Persona 5")], providers=(st.ArchiveOrgProvider(),), pause=0)
        self.assertIn("advancedsearch.php", fetch.call_args_list[0][0][0])
        self.assertEqual(res[0].albums[0].ident, "p5")
        self.assertEqual(res[0].albums[0].detail, "Shoji Meguro")
        self.assertIs(res[0].best, res[0].albums[0])

    def test_search_runs_phrase_and_any_order_queries_merged_without_duplicates(self):
        docs = {"response": {"docs": [{"identifier": "sm64", "title": "Super Mario 64 OST"}]}}
        with self.provider_with(docs) as fetch:
            res = st.find_albums([st.GameRequest("Mario 64")], providers=(st.ArchiveOrgProvider(),), pause=0)
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
            res = st.find_albums([st.GameRequest("Persona 5")], providers=(st.ArchiveOrgProvider(),), pause=0)
        self.assertEqual([a.ident for a in res[0].albums], ["split", "single"])
        self.assertEqual([a.playable for a in res[0].albums], [2, 1])

    def test_albums_carry_their_cover_address(self):
        docs = {"response": {"docs": [{"identifier": "my item", "title": "Persona 5 Original Soundtrack"}]}}
        with self.provider_with(docs):
            res = st.find_albums([st.GameRequest("Persona 5")], providers=(st.ArchiveOrgProvider(),), pause=0)
        self.assertEqual(res[0].albums[0].art, "https://archive.org/services/img/my%20item")

    def test_poor_match_is_offered_but_not_chosen(self):
        docs = {"response": {"docs": [{"identifier": "ff8", "title": "Final Fantasy VIII Soundtrack"}]}}
        with self.provider_with(docs):
            res = st.find_albums([st.GameRequest("Final Fantasy VII")], providers=(st.ArchiveOrgProvider(),), pause=0)
        self.assertEqual(len(res[0].albums), 1)
        self.assertIsNone(res[0].best)

    def test_network_failure_is_reported_not_raised(self):
        with mock.patch.object(st, "fetch_json", side_effect=LinkError("Network error: down")):
            res = st.find_albums([st.GameRequest("X")], providers=(st.ArchiveOrgProvider(),), pause=0)
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

class KHInsiderTests(unittest.TestCase):
    SEARCH = ('<table class="albumList"><tr><td><a href="/game-soundtracks/album/persona-5"><img src="https://x/t.jpg"></a></td>'
              '<td><a href="/game-soundtracks/album/persona-5">Persona 5</a></td><td>PlayStation 4</td><td>2016</td></tr></table>')
    ALBUM = ('<h2>Persona 5</h2><div class="albumImage"><a href="https://j/big.jpg"><img src="https://j/t.jpg"></a></div>'
             '<table id="songlist"><tr><th>CD</th><th>#</th><th>Song Name</th><th>MP3</th><th>FLAC</th></tr>'
             '<tr><td>1</td><td>1.</td><td><a href="/game-soundtracks/album/persona-5/01%20A.mp3">A</a></td><td>5 MB</td><td>20 MB</td></tr></table>')
    TRACK = '<audio src="https://v/a/01 A.mp3"></audio><a href="https://v/a/01 A.flac">flac</a>'

    def test_search_page_is_read(self):
        (hit,) = st.parse_khinsider_search(self.SEARCH)
        self.assertEqual((hit["slug"], hit["title"], hit["art"]), ("persona-5", "Persona 5", "https://x/t.jpg"))

    def test_album_page_gives_flac_tracks_or_mp3_on_request(self):
        tracks, cover, _ = st.parse_khinsider_album(self.ALBUM, "persona-5")
        self.assertEqual((tracks[0].title, tracks[0].ext, tracks[0].size, cover), ("A", ".flac", 20 * 1024 ** 2, "https://j/big.jpg"))
        self.assertTrue(st.is_khinsider_page(tracks[0].url))
        self.assertEqual(st.parse_khinsider_album(self.ALBUM, "persona-5", "mp3")[0][0].ext, ".mp3")

    def test_album_title_comes_from_the_page_content_not_the_menus(self):
        page = '<div id="menu"><h2>Menu</h2></div><div id="pageContent"><h2>Persona 5</h2></div>' + self.ALBUM.split("</h2>", 1)[1]
        self.assertEqual(st.parse_khinsider_album(page, "persona-5")[2]["title"], "Persona 5")

    def test_real_world_row_markup_with_bold_headers_and_download_cells(self):
        page = ('<table id="songlist"><tr id="songlist_header"><th>&nbsp;</th><th><b>CD</b></th><th><b>#</b></th><th><b>Song Name</b></th>'
                '<th>&nbsp;</th><th><b>MP3</b></th><th><b>FLAC</b></th></tr>'
                '<tr><td></td><td align="center">1</td><td align="right">1.</td><td class="clickable-row"><a href="/game-soundtracks/album/x/01%2520Intro.mp3">Intro</a></td>'
                '<td class="clickable-row"><a>3:00</a></td><td class="clickable-row"><a>7.9 MB</a></td><td class="clickable-row"><a>28 MB</a></td>'
                '<td class="playlistDownloadSong"><a href="/game-soundtracks/album/x/01%2520Intro.mp3">Get</a></td></tr>'
                '<tr id="songlist_footer"><td></td><td></td><td></td><td><b>Total:</b></td><td><b>3:00</b></td><td><b>7.9 MB</b></td><td><b>28 MB</b></td></tr></table>')
        (track,), _cover, _info = st.parse_khinsider_album(page, "x")
        self.assertEqual((track.title, track.number, track.size, track.ext), ("Intro", 1, 28 * 1024 ** 2, ".flac"))
        self.assertIn("/game-soundtracks/album/x/01%2520Intro.mp3", track.url)  # the double-encoded name is kept as the site wrote it

    def test_a_saved_track_list_is_adjusted_for_mp3_only(self):
        page = "https://downloads.khinsider.com/game-soundtracks/album/x/01.mp3"
        saved = [st.Track(page, ".flac", "A", 1, 99, None, "", True, None)]
        self.assertEqual(st.snapshot_for_quality(saved, "best"), saved)
        (mp3,) = st.snapshot_for_quality(saved, "mp3")
        self.assertEqual((mp3.ext, mp3.lossless, mp3.size, mp3.title), (".mp3", False, None, "A"))
        self.assertEqual(saved[0].ext, ".flac")  # the saved list itself is untouched
        other = [st.Track("https://archive.org/download/x/a.flac", ".flac", "A")]
        self.assertIsNone(st.snapshot_for_quality(other, "mp3"))  # list those afresh
        self.assertIsNone(st.snapshot_for_quality([], "mp3"))

    def test_khinsider_pages_are_asked_for_like_a_browser_and_other_sites_are_left_alone(self):
        enter(self, mock.patch.object(st, "_cffi_session", lambda: None))  # the plain urllib path
        h = st.khinsider_headers("https://downloads.khinsider.com/search?search=x")
        self.assertTrue(h["User-Agent"].startswith("Mozilla/5.0"))
        self.assertIn("Accept-Language", h)
        self.assertEqual(st.khinsider_headers("https://archive.org/advancedsearch.php"), {})
        seen = {}

        class Resp:
            headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b"ok"

        with mock.patch.object(st, "http_open", lambda url, headers=None, timeout=30: seen.update(headers or {}) or Resp()):
            self.assertEqual(st.fetch_text("https://downloads.khinsider.com/x"), "ok")
        self.assertIn("User-Agent", seen)

    def test_a_cloudflare_challenge_is_recognised_and_khinsider_is_then_left_alone_for_a_while(self):
        enter(self, mock.patch.object(st, "_cffi_session", lambda: None))  # the plain urllib path
        import io
        import urllib.error

        page = b"<html><head><title>Just a moment...</title></head><body>cloudflare challenge</body></html>"
        calls = []

        def refuse(url, headers=None, timeout=30):
            calls.append(url)
            raise urllib.error.HTTPError(url, 403, "Forbidden", {"cf-mitigated": "challenge"}, io.BytesIO(page))

        st.reset_khinsider_block()
        self.addCleanup(st.reset_khinsider_block)
        with mock.patch.object(st, "http_open", refuse):
            with self.assertRaises(st.ChallengeError) as cm:
                st.fetch_text("https://downloads.khinsider.com/search?search=x")
            self.assertIn("Cloudflare", str(cm.exception))
            with self.assertRaises(st.ChallengeError):
                st.fetch_text("https://downloads.khinsider.com/search?search=y")
            self.assertEqual(len(calls), 1)  # the second was not even tried
            st.reset_khinsider_block()
            with self.assertRaises(st.ChallengeError):
                st.fetch_text("https://downloads.khinsider.com/search?search=z")
            self.assertEqual(len(calls), 2)

    def test_challenge_detection(self):
        self.assertTrue(st.is_challenge({"cf-mitigated": "challenge"}))
        self.assertTrue(st.is_challenge({}, b"<title>Just a moment...</title> cloudflare"))
        self.assertFalse(st.is_challenge({}, b"<title>Albums Published by Atlus</title>"))
        self.assertFalse(st.is_challenge({}, b"just a moment, said the cashier"))  # no Cloudflare in it

    def test_an_ordinary_403_from_khinsider_is_reported_as_a_refusal(self):
        enter(self, mock.patch.object(st, "_cffi_session", lambda: None))  # the plain urllib path
        import io
        import urllib.error

        def refuse(url, headers=None, timeout=30):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"nope"))

        st.reset_khinsider_block()
        with mock.patch.object(st, "http_open", refuse):
            with self.assertRaises(st.LinkError) as cm:
                st.fetch_text("https://downloads.khinsider.com/x")
        self.assertNotIsInstance(cm.exception, st.ChallengeError)
        self.assertIn("403", str(cm.exception))

    def test_a_real_publisher_page_is_read_as_it_is_served(self):
        # A trimmed copy of a real page (downloads.khinsider.com/game-soundtracks/publisher/atlus, saved by the probe).
        page = (Path(__file__).parent / "fixtures" / "khinsider" / "publisher_atlus_sample.html").read_text(encoding="utf-8")
        albums = st.parse_khinsider_search(page)
        by_slug = {a["slug"]: a for a in albums}
        first = by_slug["101-in-1-party-megamix-wii-gamerip"]
        self.assertEqual((first["title"], first["detail"]), ("101-in-1 Party Megamix", "Wii \u00b7 Gamerip \u00b7 2010"))
        self.assertTrue(first["art"].startswith("https://lambda.vgmtreasurechest.com/soundtracks/101-in-1-party-megamix-wii-gamerip/thumbs_small/"))
        self.assertEqual(by_slug["13-sentinels-aegis-rim-switch-gamerip-2022"]["detail"].split(" \u00b7 ")[0], "Switch")
        self.assertEqual(len(albums), len(by_slug))  # nothing twice, though the page lists its top albums too

    def test_with_curl_cffi_installed_khinsider_is_fetched_through_it(self):
        class Resp:
            status_code, headers, content = 200, {}, b"<html>albumList</html>"

        class Session:
            def __init__(self): self.calls = []
            def get(self, url, headers=None, timeout=30):
                self.calls.append((url, headers))
                return Resp()

        session = Session()
        st.reset_khinsider_block()
        with mock.patch.object(st, "_cffi_session", lambda: session), \
                mock.patch.object(st, "http_open", side_effect=AssertionError("plain urllib must not be used")):
            self.assertEqual(st.fetch_text("https://downloads.khinsider.com/search?search=x"), "<html>albumList</html>")
        self.assertEqual(len(session.calls), 1)

    def test_curl_cffi_still_challenged_says_so_and_waits(self):
        class Resp:
            status_code = 403
            headers = {"cf-mitigated": "challenge"}
            content = b"<title>Just a moment...</title> cloudflare"

        class Session:
            calls = 0
            def get(self, url, headers=None, timeout=30):
                Session.calls += 1
                return Resp()

        st.reset_khinsider_block()
        self.addCleanup(st.reset_khinsider_block)
        with mock.patch.object(st, "_cffi_session", lambda: Session()):
            for _ in range(2):
                with self.assertRaises(st.ChallengeError) as cm:
                    st.fetch_text("https://downloads.khinsider.com/x")
            self.assertIn("still refused", str(cm.exception))
            self.assertEqual(Session.calls, 1)  # the second was not tried

    def test_without_curl_cffi_the_message_says_how_to_get_it(self):
        self.assertIn("pip install curl_cffi", st.CHALLENGE_MESSAGE)

    def test_track_page_gives_the_file_address(self):
        self.assertEqual(st.parse_khinsider_track(self.TRACK), "https://v/a/01%20A.flac")
        self.assertEqual(st.parse_khinsider_track(self.TRACK, "mp3"), "https://v/a/01%20A.mp3")
        self.assertIsNone(st.parse_khinsider_track("<html></html>"))

    def test_files_are_resolved_and_plain_urls_pass_through(self):
        page = "https://downloads.khinsider.com/game-soundtracks/album/x/01.mp3"
        files = [st.RemoteFile("G/soundtrack/01 - A.mp3", page, 5), st.RemoteFile("G/soundtrack/02 - B.mp3", "https://z/2.mp3", 5)]
        with mock.patch.object(st, "fetch_text", lambda url, timeout=30: self.TRACK):
            st._RESOLVED.clear()
            out, problems = st.resolve_files(files)
        self.assertEqual((out[0].url, out[1].url, problems), ("https://v/a/01%20A.flac", "https://z/2.mp3", []))


class GameMatchingTests(unittest.TestCase):
    def test_platform_names_are_compared_by_meaning(self):
        self.assertTrue(st.same_platform("Sony PlayStation 4", "PlayStation 4"))
        self.assertTrue(st.same_platform("PS4", "PlayStation 4"))
        self.assertTrue(st.same_platform("Super Nintendo Entertainment System", "SNES"))
        self.assertTrue(st.same_platform("Nintendo Switch", "Switch"))
        self.assertTrue(st.same_platform("Sega Genesis", "Mega Drive"))
        self.assertFalse(st.same_platform("PlayStation 2", "PlayStation 4"))
        self.assertFalse(st.same_platform("Super Nintendo", "Nintendo 64"))
        self.assertFalse(st.same_platform("", "PC"))

    def test_albums_for_the_games_platform_and_year_move_up_but_a_poor_title_does_not_win(self):
        wrong = st.Album("khinsider", "a", "Persona 5", "PC \u00b7 2017", 0.95)
        right = st.Album("khinsider", "b", "Persona 5", "PlayStation 4 \u00b7 2016", 0.90)
        poor = st.Album("khinsider", "c", "Persona 3", "PlayStation 4 \u00b7 2016", 0.40)
        ranked = st.rank_for_game([wrong, right, poor], "Sony PlayStation 4", "2016")
        self.assertEqual([a.ident for a in ranked], ["b", "a", "c"])
        self.assertEqual([a.ident for a in st.rank_for_game([wrong, right], "", "")], ["a", "b"])  # no hint: as found

    def test_detail_line_is_split(self):
        self.assertEqual(st.split_detail("PlayStation 4 \u00b7 Gamerip \u00b7 2016"), ("PlayStation 4", 2016))

    def test_titles_of_the_same_release_share_a_merge_key(self):
        self.assertEqual(st.album_merge_key("Celeste Original Soundtrack"), st.album_merge_key("Celeste"))
        self.assertNotEqual(st.album_merge_key("Celeste"), st.album_merge_key("Celeste 2"))


class LocalAlbumTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def touch(self, *parts):
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
        return p

    def test_standard_and_romm_folders_are_found_as_albums(self):
        self.touch("Soundtracks", "Celeste", "01 - Intro.flac")
        self.touch("Soundtracks", "Celeste", "cover.jpg")
        self.touch("Soundtracks", "Empty", "notes.txt")
        self.touch("games", "SNES", "Chrono Trigger", "soundtrack", "01 - Prelude.mp3")
        self.touch("games", "SNES", "Chrono Trigger", "Chrono Trigger.sfc")
        albums = st.scan_local_albums(self.root, self.root / "games")
        self.assertEqual([a.title for a in albums], ["Celeste", "Chrono Trigger"])
        self.assertTrue(all(a.provider == "local" for a in albums))
        self.assertTrue(albums[0].art.endswith("cover.jpg"))
        self.assertEqual(albums[1].art, "")

    def test_no_double_listing_when_the_games_folder_holds_the_standard_one(self):
        self.touch("Soundtracks", "Celeste", "01 - A.mp3")
        self.assertEqual(len(st.scan_local_albums(self.root, self.root)), 1)

    def test_local_tracks_have_titles_numbers_discs_and_play_from_their_path(self):
        self.touch("Soundtracks", "G", "Disc 1", "01 - First.flac")
        self.touch("Soundtracks", "G", "Disc 2", "01 - Second.flac")
        (album,) = st.scan_local_albums(self.root)
        files = st.files_for("G", album)
        self.assertEqual(len(files), 2)
        self.assertTrue(all(st.is_local_path(f.url) for f in files))
        self.assertFalse(st.is_local_path("https://x/a.mp3"))
        tracks = st.LocalProvider().tracks(album)
        self.assertEqual([(t.number, t.title, t.disc) for t in tracks], [(1, "First", 1), (1, "Second", 2)])


class ProviderFailureTests(IsolatedSoundtrackFiles):
    class Broken:
        name = "broken"

        def search(self, game):
            raise st.LinkError("khinsider refused the request (HTTP 403)")

        def tracks(self, album, prefer="best"):
            raise st.LinkError("nope")

    class Working:
        name = "working"

        def search(self, game):
            return [st.Album("working", "p5", "Persona 5", "", 1.0)]

        def tracks(self, album, prefer="best"):
            return [st.Track("https://x/1.mp3", ".mp3", "A", 1)]

    def test_one_source_failing_still_gives_the_others_albums_and_says_so_and_is_not_cached(self):
        (res,) = st.find_albums([st.GameRequest("Persona 5")], providers=(self.Broken(), self.Working()), pause=0)
        self.assertEqual([a.ident for a in res.albums], ["p5"])
        self.assertIn("403", res.error)
        self.assertEqual(st._load_cache(), {})  # nothing remembered
        (again,) = st.find_albums([st.GameRequest("Persona 5")], providers=(self.Working(),), pause=0)
        self.assertEqual(again.error, "")  # asked afresh once the source is back


class RealKHInsiderPagesTests(unittest.TestCase):
    """The parsers against pages saved from the live site (tests/fixtures/khinsider)."""

    @staticmethod
    def page(name):
        return (Path(__file__).parent / "fixtures" / "khinsider" / name).read_text(encoding="utf-8")

    def test_search_results(self):
        rows = st.parse_khinsider_search(self.page("search_persona_5.html"))
        self.assertEqual(len(rows), 12)
        first = rows[0]
        self.assertEqual((first["slug"], first["title"], first["detail"]), ("metagroove-2020", "METAGROOVE", "PS3, PS4 \u00b7 Arrangement \u00b7 2020"))
        self.assertTrue(first["art"].startswith("https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/thumbs_small/"))
        self.assertEqual(st.split_detail(first["detail"]), ("PS3, PS4", 2020))

    def test_album_page_tracks_durations_sizes_and_info(self):
        tracks, cover, info = st.parse_khinsider_album(self.page("album_metagroove.html"), "metagroove-2020")
        self.assertEqual(len(tracks), 13)
        t = tracks[0]
        self.assertEqual((t.number, t.title, t.seconds, t.ext, t.lossless), (1, "Life Will Change", 320, ".flac", True))
        self.assertEqual(t.size, 37790679)  # 36.04 MB, the FLAC column
        self.assertIn("/game-soundtracks/album/metagroove-2020/01.%2520Life%2520Will%2520Change.mp3", t.url)
        self.assertEqual(st.fmt_total(sum(x.seconds or 0 for x in tracks)), "1 h 19 min")  # the page's own total: 1h 19m
        self.assertEqual(cover, "https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/cover.jpg")
        self.assertEqual((info["title"], info["platforms"], info["year"], info["publisher"], info["type"]),
                         ("METAGROOVE", "PS3, PS4", "2020", "J-Music Ensemble", "Arrangement"))
        self.assertEqual(len(info["related"]), 12)
        self.assertEqual(info["related"][1]["slug"], "persona-5")

    def test_mp3_only_picks_the_mp3_column(self):
        mp3 = st.parse_khinsider_album(self.page("album_metagroove.html"), "metagroove-2020", "mp3")[0][0]
        self.assertEqual((mp3.ext, mp3.size), (".mp3", int(10.47 * 1024 * 1024)))

    def test_track_page_file_addresses(self):
        page = self.page("track_metagroove.html")
        self.assertEqual(st.parse_khinsider_track(page),
                         "https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/xbqytxht/01.%20Life%20Will%20Change.flac")
        self.assertTrue(st.parse_khinsider_track(page, "mp3").endswith("01.%20Life%20Will%20Change.mp3"))

    def test_ranked_list(self):
        top = st.parse_khinsider_toplist(self.page("top40.html"))
        self.assertEqual(len(top), 40)
        self.assertEqual((top[0]["rank"], top[0]["slug"]), (1, "minecraft"))
        self.assertEqual(top[1]["rank"], 2)

    def test_latest_page_and_its_page_counter(self):
        page = self.page("home_latest.html")
        self.assertEqual(len(st.parse_khinsider_search(page)), 15)
        self.assertEqual(st.parse_khinsider_pages(page), (1, 620))
        self.assertEqual(st.parse_khinsider_pages("<html></html>"), (1, 1))

    def test_console_list(self):
        consoles = st.parse_khinsider_consoles(self.page("console_list.html"))
        self.assertEqual(len(consoles), 69)
        self.assertEqual(consoles[0], ("3do", "3DO", 173))
        self.assertIn(("nintendo-3ds", "3DS", 1807), consoles)

    def test_cover_tiles(self):
        tiles = st.parse_khinsider_tiles(self.page("album_metagroove.html"), after="People who viewed this also viewed")
        self.assertEqual(len(tiles), 12)
        self.assertEqual(tiles[0]["title"], "Persona 5 Royal - The Complete Soundtrack (2016)")
        self.assertEqual(st.parse_khinsider_tiles("<html></html>", after="nothing"), [])


class CoverQualityTests(unittest.TestCase):
    """The covers on khinsider's lists are tiny thumbnails; the same picture is fetched sharper where there is one."""
    SMALL = "https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/thumbs_small/cover.jpg"
    MEDIUM = "https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/thumbs/cover.jpg"
    FULL = "https://lambda.vgmtreasurechest.com/soundtracks/metagroove-2020/cover.jpg"
    JPEG = b"\xff\xd8\xff" + bytes(32)
    PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)

    def test_a_list_thumbnail_is_tried_full_size_then_medium_then_as_it_is(self):
        self.assertEqual(st.cover_candidates(self.SMALL), [self.FULL, self.MEDIUM, self.SMALL])

    def test_a_medium_one_is_tried_full_size_then_as_it_is(self):
        self.assertEqual(st.cover_candidates(self.MEDIUM), [self.FULL, self.MEDIUM])

    def test_the_name_and_any_query_are_kept(self):
        got = st.cover_candidates("https://x.example/soundtracks/a-b/thumbs_small/00%20Front.jpg?v=2")
        self.assertEqual(got[0], "https://x.example/soundtracks/a-b/00%20Front.jpg?v=2")

    def test_other_addresses_are_left_alone(self):
        for url in (self.FULL, "https://archive.org/services/img/some-item", "C:/Music/Game/cover.jpg", "/home/me/cover.png"):
            self.assertEqual(st.cover_candidates(url), [url])
        self.assertEqual(st.cover_candidates(""), [])

    def test_the_sharpest_picture_is_the_one_returned(self):
        asked = []

        def reader(url, limit):
            asked.append(url)
            return self.JPEG

        self.assertEqual(st.fetch_cover(self.SMALL, reader=reader), self.JPEG)
        self.assertEqual(asked, [self.FULL])  # (nothing more was fetched)

    def test_a_missing_or_oversize_sharper_picture_falls_back_a_step_at_a_time(self):
        def reader(url, limit):
            if url == self.FULL:
                raise st.CoverTooBig(url)
            if url == self.MEDIUM:
                raise OSError("404")
            return self.PNG

        self.assertEqual(st.fetch_cover(self.SMALL, reader=reader), self.PNG)  # (the thumbnail the list gave, as before)

    def test_a_sharper_address_that_is_not_a_picture_is_passed_over(self):
        def reader(url, limit):
            return b"<html>not found</html>" if url == self.FULL else self.JPEG

        self.assertEqual(st.fetch_cover(self.SMALL, reader=reader), self.JPEG)

    def test_the_address_given_failing_is_an_error_and_too_big_is_no_cover(self):
        def broken(url, limit):
            raise OSError("down")

        def huge(url, limit):
            raise st.CoverTooBig(url)

        with self.assertRaises(OSError):
            st.fetch_cover(self.FULL, reader=broken)
        self.assertEqual(st.fetch_cover(self.FULL, reader=huge), b"")

    def test_a_big_cover_is_refused_from_its_header_without_downloading_it(self):
        class Resp:
            headers = {"Content-Length": str(st.COVER_MAX_BYTES + 1)}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                raise AssertionError("the body must not be downloaded")

        with mock.patch.object(st, "http_open", lambda url, timeout=30: Resp()):
            with self.assertRaises(st.CoverTooBig):
                st.read_cover(self.FULL)

    def test_a_cover_on_this_computer_is_read_as_it_is(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cover.jpg"
            path.write_bytes(self.JPEG)
            self.assertEqual(st.fetch_cover(str(path)), self.JPEG)

    def test_the_picture_type_comes_from_the_picture(self):
        self.assertEqual((st.image_mime(self.JPEG), st.image_mime(self.PNG), st.image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), st.image_mime(b"<html>")),
                         ("image/jpeg", "image/png", "image/webp", ""))


class DurationTests(unittest.TestCase):
    def test_lengths_are_read_and_shown(self):
        self.assertEqual((st.to_seconds("215.42"), st.to_seconds("3:35"), st.to_seconds("1:02:03"), st.to_seconds(215)), (215, 215, 3723, 215))
        self.assertEqual((st.to_seconds(None), st.to_seconds(""), st.to_seconds("n/a"), st.to_seconds("0")), (None, None, None, None))
        self.assertEqual((st.fmt_clock(320), st.fmt_clock(3723), st.fmt_clock(None), st.fmt_clock(0)), ("5:20", "1:02:03", "", ""))
        self.assertEqual((st.fmt_total(834), st.fmt_total(4740), st.fmt_total(45), st.fmt_total(None)), ("13 min 54 s", "1 h 19 min", "45 s", ""))

    def test_a_tracks_length_travels_to_its_file(self):
        (f,) = st.layout("G", [st.Track("https://x/1.mp3", ".mp3", "A", 1, 5, None, "", False, None, 215)])
        self.assertEqual(f.seconds, 215)


class PlaylistFileAndBrowseTests(unittest.TestCase):
    def test_an_m3u_has_a_line_of_info_and_the_location_per_track(self):
        text = st.m3u_text([("Persona 5 - Life Will Change", 320, "https://x/1.flac"), ("A\nB", None, "C:\\music\\2.mp3")])
        self.assertEqual(text.splitlines(), ["#EXTM3U", "#EXTINF:320,Persona 5 - Life Will Change", "https://x/1.flac",
                                             "#EXTINF:-1,A B", "C:\\music\\2.mp3"])

    def test_browse_addresses(self):
        self.assertEqual(st.browse_path("platform", "nintendo-switch"), "/game-soundtracks/nintendo-switch")
        self.assertEqual(st.browse_path("year", "2016"), "/game-soundtracks/year/2016")
        self.assertEqual((st.browse_path("letter", "a"), st.browse_path("letter", "0-9"), st.browse_path("letter", "#")),
                         ("/game-soundtracks/browse/A", "/game-soundtracks/browse/0-9", "/game-soundtracks/browse/0-9"))
        with self.assertRaises(ValueError):
            st.browse_path("colour", "red")

    def test_real_search_results_rank_the_games_own_titles_first(self):
        page = (Path(__file__).parent / "fixtures" / "khinsider" / "search_persona_5.html").read_text(encoding="utf-8")
        albums = [st.Album("khinsider", d["slug"], d["title"], d["detail"], max(st.match_score("Persona 5", d["title"]), 0.1), d["art"])
                  for d in st.parse_khinsider_search(page)]
        ranked = sorted(albums, key=lambda a: -a.score)
        self.assertTrue(all(a.title.startswith("Persona 5") for a in ranked[:5]))
        self.assertNotEqual(ranked[0].title, "METAGROOVE")  # the site lists that one first
        hinted = st.rank_for_game(ranked, "Sony PlayStation 4", "2016")
        self.assertEqual(hinted[0].title, "Persona 5 -PS4 Edition-")

    def test_a_queue_items_length_survives_a_restart(self):
        from distill.mediaqueue import PlayQueue, QueueItem
        q = PlayQueue()
        q.set([QueueItem("t", "A", "u", "", "khinsider", "x", 0, "PC", 215)], 0)
        again = PlayQueue()
        again.restore(q.state())
        self.assertEqual(again.items[0].seconds, 215)
        again.restore({"items": [["t", "A", "u", "", "khinsider", "x", 0, "PC"]], "order": [0]})  # saved before lengths existed
        self.assertEqual(again.items[0].seconds, 0)
