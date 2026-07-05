import unittest

import autoresume


class TestModule(unittest.TestCase):
    def test_module_constants_exist(self):
        self.assertEqual(autoresume.MAX_ATTEMPTS, 3)
        self.assertIn("ignore this message", autoresume.MESSAGE)
