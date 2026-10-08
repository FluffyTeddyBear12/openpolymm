import unittest

class TestMetricFormatting(unittest.TestCase):
    def test_delta_formatting(self):
        val = -50.0
        delta_str = f"{val:+.2f}"
        self.assertEqual(delta_str, "-50.00")
        
        pos_val = 50.0
        pos_str = f"{pos_val:+.2f}"
        self.assertEqual(pos_str, "+50.00")

if __name__ == '__main__':
    unittest.main()
