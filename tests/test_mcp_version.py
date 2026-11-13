"""MCP discovery metadata follows the installed package version."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memcal import __version__, cli, mcp_server


class TestMcpVersionMatchesPackage(unittest.TestCase):
    def test_initialize_reports_package_version(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict("os.environ", {"MEMCAL_HOME": home}):
            server = mcp_server.Server()
            try:
                response = server.handle({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            finally:
                server.conn.close()
        self.assertEqual(response["result"]["serverInfo"]["version"], __version__)


class TestMcpCommandRouting(unittest.TestCase):
    def test_cli_routes_to_module_server(self):
        with mock.patch.object(mcp_server, "main", return_value=0) as serve:
            self.assertEqual(cli.main(["mcp"]), 0)
            serve.assert_called_once_with()

    def test_explicit_home_is_scoped_to_server(self):
        import os
        observed = []
        with mock.patch.dict(os.environ, {"MEMCAL_HOME": "/tmp/original-synthetic"}), \
                mock.patch.object(mcp_server, "main", side_effect=lambda: observed.append(os.environ["MEMCAL_HOME"]) or 0):
            self.assertEqual(cli.main(["--home", "/tmp/selected-synthetic", "mcp"]), 0)
            self.assertEqual(observed, ["/tmp/selected-synthetic"])
            self.assertEqual(os.environ["MEMCAL_HOME"], "/tmp/original-synthetic")


class TestRegistryManifestContract(unittest.TestCase):
    def test_manifest_matches_package_and_launcher(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "server.json").read_text())
        self.assertEqual(manifest["version"], __version__)
        package, = manifest["packages"]
        self.assertEqual(package["version"], __version__)
        self.assertEqual(package["identifier"], "memcal")
        self.assertEqual(package["transport"], {"type": "stdio"})
        self.assertEqual(package["packageArguments"], [{"type": "positional", "value": "mcp"}])
        self.assertIn("<!-- mcp-name: " + manifest["name"] + " -->", (root / "README.md").read_text())
        self.assertEqual(cli.build_parser().parse_args(["mcp"]).func, cli.cmd_mcp)


if __name__ == "__main__": unittest.main()
