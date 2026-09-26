"""Run the dependency-free Pi API stub tests as part of unittest discovery."""
import shutil
import subprocess
import unittest
from pathlib import Path


class PiCommandExtensionTests(unittest.TestCase):
    def test_runtime_bridge(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        support = subprocess.run([node, "--experimental-strip-types", "-e", ""],
                                 capture_output=True, text=True, timeout=10, check=False)
        if support.returncode:
            self.skipTest("Node TypeScript stripping is unavailable (requires Node 22.6+)")
        script = Path(__file__).with_name("test_pi_commands.mjs")
        result = subprocess.run([node, "--experimental-strip-types", "--test", str(script)],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
