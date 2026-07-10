import unittest

import pm_mesh


class SmokeTest(unittest.TestCase):
    def test_package_imports(self):
        self.assertTrue(hasattr(pm_mesh, "__version__"))


if __name__ == "__main__":
    unittest.main()
