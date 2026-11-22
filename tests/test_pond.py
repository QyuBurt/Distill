import math
import random
import tempfile
import unittest
import wave
from pathlib import Path

from distill import pondmath as pm
from distill import pondsound as ps


class RippleTests(unittest.TestCase):
    def test_a_ring_crosses_a_point_once_and_weakens_with_distance(self):
        r = pm.Ripple(0, 0, 0.0)
        d = 100.0
        t = d / pm.SPEED
        self.assertGreater(r.passes(d, 0, t - 0.01, t + 0.01), 0.0)
        self.assertEqual(r.passes(d, 0, t + 0.01, t + 0.5), 0.0)  # already past
        self.assertEqual(r.passes(d, 0, 0.0, t - 0.01), 0.0)  # not there yet
        near, far = r.passes(50, 0, 0, 1), r.passes(500, 0, 0, 3)
        self.assertGreater(near, far)

    def test_a_faint_ripple_is_not_noticed(self):
        r = pm.Ripple(0, 0, 0.0, power=0.1)
        self.assertEqual(r.passes(10, 0, 0, 1), 0.0)

    def test_it_dies_away(self):
        r = pm.Ripple(0, 0, 0.0)
        self.assertTrue(r.alive(0.5))
        self.assertFalse(r.alive(pm.RIPPLE_LIFE + 0.1))


class ClockTests(unittest.TestCase):
    def test_quantise_lands_on_the_grid_and_never_earlier(self):
        c = pm.Clock(bpm=120.0)  # a beat is 0.5 s, a step 0.25 s
        self.assertAlmostEqual(c.quantise(0.01), 0.25)
        self.assertAlmostEqual(c.quantise(0.25), 0.25)
        self.assertAlmostEqual(c.quantise(0.26), 0.5)
        self.assertGreaterEqual(c.quantise(3.1415), 3.1415 - 1e-6)

    def test_beats_in_a_span_and_the_bar_start(self):
        c = pm.Clock(bpm=120.0)
        got = c.beats(0.0, 2.1)
        self.assertEqual([round(t, 2) for t, _ in got], [0.5, 1.0, 1.5, 2.0])
        self.assertEqual([bar for _t, bar in got], [False, False, False, True])

    def test_a_new_tempo_restarts_the_bar_where_it_is(self):
        c = pm.Clock(bpm=90.0)
        c.set_bpm(120.0, 10.0)
        self.assertAlmostEqual(c.quantise(10.0), 10.0)
        self.assertEqual(c.bpm, 120.0)
        c.set_bpm(999, 11.0)
        self.assertEqual(c.bpm, pm.BPM_RANGE[1])


class ScoreTests(unittest.TestCase):
    def test_notes_come_out_in_time_order_once(self):
        s = pm.Score()
        s.add(2.0, "a", 1.0)
        s.add(1.0, "b", 1.0)
        self.assertEqual([k for k, _v, _w in s.due(1.5)], ["b"])
        self.assertEqual([k for k, _v, _w in s.due(5)], ["a"])
        self.assertEqual(s.due(9), [])

    def test_the_same_note_at_the_same_moment_is_one_note(self):
        s = pm.Score()
        self.assertTrue(s.add(1.0, "kick", 0.5))
        self.assertFalse(s.add(1.0, "kick", 0.9))
        self.assertEqual(len(s), 1)

    def test_it_holds_only_so_many(self):
        s = pm.Score()
        for i in range(pm.MAX_PENDING + 20):
            s.add(i, "x", 1.0)
        self.assertEqual(len(s), pm.MAX_PENDING)


class PitchTests(unittest.TestCase):
    def test_higher_on_the_water_is_a_higher_note(self):
        self.assertGreater(pm.height_degree(10, 600), pm.height_degree(590, 600))
        self.assertEqual(pm.height_degree(-50, 600), pm.DEGREES - 1)
        self.assertEqual(pm.height_degree(9999, 600), 0)

    def test_every_note_is_in_the_scale(self):
        for timbre, root in pm.TIMBRES.items():
            for d in range(pm.DEGREES):
                self.assertIn((pm.midi_of(timbre, d) - root) % 12, pm.PENT)
        self.assertAlmostEqual(pm.frequency(69), 440.0)


class PondTests(unittest.TestCase):
    def pond(self):
        p = pm.Pond(seed=3)
        p.resize(900, 600)
        p.snap = False
        return p

    def run_for(self, p, seconds, start=0.0, fps=60):
        notes, t = [], start
        while t < start + seconds:
            t += 1 / fps
            notes += p.update(t)
        return notes

    def test_a_ripple_sets_off_the_animal_it_reaches(self):
        p = self.pond()
        (c,) = p.spawn("crab", 400, 564)
        c.drum = "snare"
        p.add_ripple(300, 564, 0.0)
        self.assertEqual([k for k, _v in self.run_for(p, 1.0)], ["snare"])

    def test_each_kind_answers_in_its_own_voice(self):
        voices = {"jelly": "bell", "minnow": "blip", "tadpole": "pluck", "snail": "bass"}
        for kind, voice in voices.items():
            p = self.pond()
            made = p.spawn(kind, 450, 300 if kind != "snail" else 480)
            for c in made:
                c.burst = 0
            p.add_ripple(400, made[0].y, 0.0)
            keys = {k.split(":")[0] for k, _v in self.run_for(p, 0.6)}
            self.assertIn(voice, keys, kind)

    def test_a_school_is_several_and_goes_together(self):
        p = self.pond()
        self.assertEqual(len(p.spawn("minnow", 300, 300)), pm.SCHOOL)
        p.spawn("jelly", 700, 200)
        self.assertTrue(p.remove_at(p.creatures[0].x, p.creatures[0].y))
        self.assertEqual([c.kind for c in p.creatures], ["jelly"])
        self.assertFalse(p.remove_at(5, 5))

    def test_an_animal_does_not_answer_twice_in_a_row(self):
        p = self.pond()
        (c,) = p.spawn("crab", 400, 564)
        p.add_ripple(380, 564, 0.0)
        p.add_ripple(380, 564, 0.0)
        self.assertEqual(len(self.run_for(p, 0.5)), 1)

    def test_animals_answering_each_other_comes_to_an_end(self):
        p = self.pond()
        for x in (300, 360, 420):
            p.spawn("tadpole", x, 300)
        for c in p.creatures:
            c.speed = 0
        p.add_ripple(330, 300, 0.0)
        self.run_for(p, 8.0)
        self.assertEqual(p.ripples, [])  # everything has died away
        self.assertEqual(len(p.score), 0)

    def test_snapped_notes_wait_for_the_grid(self):
        p = self.pond()
        p.snap = True
        p.set_bpm(120, 0.0)
        (c,) = p.spawn("crab", 400, 564)
        p.add_ripple(380, 564, 0.0)
        # the ring reaches the crab at about 0.08 s; the next eighth-note step is at 0.25 s
        early = self.run_for(p, 0.2)
        self.assertEqual(early, [])
        late = self.run_for(p, 0.2, start=0.2)
        self.assertEqual(len(late), 1)

    def test_the_tide_makes_a_ripple_on_every_beat(self):
        p = self.pond()
        p.tide = True
        p.set_bpm(120, 0.0)
        self.run_for(p, 2.05)
        self.assertEqual(len(p.ripples), 4)  # beats at 0.5, 1.0, 1.5, 2.0 (all within their life)

    def test_animals_stay_in_the_water(self):
        p = self.pond()
        for kind in pm.KINDS:
            p.spawn(kind)
        self.run_for(p, 30.0)
        for c in p.creatures:
            self.assertTrue(0 <= c.x <= p.w and 0 <= c.y <= p.h, (c.kind, c.x, c.y))

    def test_clear_empties_the_pond(self):
        p = self.pond()
        p.spawn("jelly")
        p.add_ripple(1, 1, 0.0)
        p.clear()
        self.assertEqual((p.creatures, p.ripples, len(p.score)), ([], [], 0))


class SoundTests(unittest.TestCase):
    def test_every_sound_renders_inside_range_and_the_same_each_time(self):
        for key in ps.all_keys():
            pcm = ps.render(key)
            self.assertGreater(len(pcm), 1000, key)
            self.assertEqual(pcm, ps.render(key), key)
        self.assertEqual(len(ps.all_keys()), 4 * pm.DEGREES + 3)

    def test_unknown_sound_is_an_error(self):
        with self.assertRaises(ValueError):
            ps.render("gong:1")

    def test_wav_files_are_written_once(self):
        folder = Path(tempfile.mkdtemp()) / "pond"
        files = ps.write_all(folder)
        self.assertEqual(len(files), len(ps.all_keys()))
        with wave.open(str(files[0])) as f:
            self.assertEqual((f.getnchannels(), f.getsampwidth(), f.getframerate()), (1, 2, ps.RATE))
        stamp = files[0].stat().st_mtime_ns
        ps.write_all(folder)
        self.assertEqual(files[0].stat().st_mtime_ns, stamp)


if __name__ == "__main__":
    unittest.main()
