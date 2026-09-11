import unittest
import support  # noqa: F401 -- install the source path for stdlib unittest discovery
from autocd.scheduler import Window


class WindowTests(unittest.TestCase):
    def test_only_continuous_observations_count(self):
        window = Window()
        for now in (0, 10, 20):
            self.assertFalse(window.observe("a", "cfg", now, 1000 + now, 10, 30))
        self.assertTrue(window.observe("a", "cfg", 30, 1030, 10, 30))
        self.assertFalse(window.observe("b", "cfg", 40, 1040, 10, 30))
        self.assertEqual(window.since, 40)

    def test_gap_error_config_change_and_restart_reset(self):
        window = Window()
        window.observe("a", "cfg", 0, 1000, 10, 30)
        window.observe("a", "cfg", 10, 1010, 10, 30)
        self.assertFalse(window.observe("a", "cfg", 80, 1080, 10, 30))
        self.assertEqual(window.since, 80)
        self.assertFalse(window.observe("a", "new-cfg", 90, 1090, 10, 30))
        self.assertEqual(window.since, 90)
        window.reset()
        self.assertFalse(window.observe("a", "new-cfg", 120, 1120, 10, 30))
        fresh = Window()
        self.assertFalse(fresh.observe("a", "new-cfg", 150, 1150, 10, 30))

    def test_wall_clock_jumps_do_not_shorten_window(self):
        window = Window()
        window.observe("a", "cfg", 0, 1000, 10, 20)
        self.assertFalse(window.observe("a", "cfg", 10, 999999, 10, 20))
        self.assertTrue(window.observe("a", "cfg", 20, -100, 10, 20))
        self.assertEqual(window.since_utc, 1000)
