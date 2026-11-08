import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from architect_harness.core import GraphService, HarnessError
from architect_harness.observations import origin, probe_http


class ProbeBoundaryTests(unittest.TestCase):
    def test_origin_normalization_and_credentials(self):
        self.assertEqual(origin("https://EXAMPLE.com/path"), "https://example.com:443")
        self.assertEqual(origin("http://[::1]:8080/path"), "http://[::1]:8080")
        for value in ["file:///tmp/x", "https://user:password@example.com", "http://host:bad"]:
            with self.assertRaises(HarnessError):
                origin(value)

    def test_unapproved_origin_never_opens_network(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GraphService(str(Path(directory) / "graph.sqlite"))
            service.initialize("p", "P")
            with patch("urllib.request.build_opener") as opener:
                with self.assertRaises(HarnessError):
                    probe_http(service, "https://example.com", [], ["missing"])
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
