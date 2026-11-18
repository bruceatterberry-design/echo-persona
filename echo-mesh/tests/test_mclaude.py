"""mclaude (the launcher) and meshmcp (the mesh tools Claude gets): the decision table, the protocol, and the safety refusals. No Claude, no live Mesh."""
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from unittest import mock
TOOLS = os.path.join(os.path.dirname(__file__), "..", "tools")
sys.path.insert(0, TOOLS)
import meshmcp as X

_l = importlib.machinery.SourceFileLoader("mclaude_launcher", os.path.join(TOOLS, "mclaude"))
_s = importlib.util.spec_from_loader("mclaude_launcher", _l)
L = importlib.util.module_from_spec(_s); _l.exec_module(L)


def call(name, args):
    r = X.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]["content"][0]["text"], r["result"]["isError"]


class T(unittest.TestCase):
    def test_the_three_cases_and_both_down(self):
        self.assertEqual(L.decide(True, True), "claude+mesh")        # Claude with the mesh helping
        self.assertEqual(L.decide(True, False), "claude")            # mesh down: Claude alone
        self.assertEqual(L.decide(False, True), "mesh")              # Claude down: the mesh takes over
        self.assertEqual(L.decide(False, False), "none")
        self.assertEqual(L.decide(True, True, no_mesh=True), "claude")
        self.assertEqual(L.decide(True, True, mesh_only=True), "mesh")
        self.assertEqual(L.decide(False, True, no_mesh=True), "none")

    def test_project_tests_come_from_the_flag_or_the_projects_own_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(L.project_tests(d, None))
            open(f"{d}/.mclaude.json", "w").write(json.dumps({"tests": "make test"}))
            self.assertEqual(L.project_tests(d, None), "make test"); self.assertEqual(L.project_tests(d, "pytest -q"), "pytest -q")

    def test_mcp_config_names_the_mesh_server_and_holds_no_token(self):
        with mock.patch.object(L.os.path, "expanduser", lambda p: p.replace("~", tempfile.gettempdir())):
            path = L.write_mcp_config("/proj", "make test")
        cfg = json.load(open(path))["mcpServers"]["mesh"]
        self.assertTrue(cfg["args"][0].endswith("meshmcp.py")); self.assertEqual(cfg["env"], {"MESHMCP_ROOT": "/proj", "MESHMCP_TESTS": "make test"})
        self.assertNotIn("admin", json.dumps(cfg).lower())

    def test_the_prompt_tells_claude_the_rules(self):
        for w in ("mesh_try_patch", "DRAFT", "NOTHING real is changed", "carry on alone"):
            self.assertIn(w, L.SYSTEM_PROMPT)

    def test_protocol_lists_four_tools_with_schemas(self):
        r = X.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        tools = X.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), ["mesh_ask_all", "mesh_fanout", "mesh_status", "mesh_try_patch"])
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))
        self.assertIsNone(X.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIn("error", X.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}))

    def test_when_the_mesh_is_down_every_tool_says_so_in_one_line(self):
        with mock.patch.object(X, "mesh_up", return_value=([], "URLError")):
            for name, args in (("mesh_status", {}), ("mesh_try_patch", {"task": "t", "files": ["a.py"]}), ("mesh_fanout", {"prompt": "{item}", "items": ["x"]}), ("mesh_ask_all", {"question": "q"})):
                text, err = call(name, args)
                self.assertIn("not available", text); self.assertIn("yourself", text); self.assertFalse(err)

    def test_secrets_and_outside_files_are_refused_before_anything_is_sent(self):
        node = {"id": "n-1", "name": "pc", "online": True, "inventory": {"ollama": {"models": ["qwen2.5-coder:3b"]}}}
        with mock.patch.object(X, "mesh_up", return_value=([node], None)), mock.patch.object(X.F, "api", side_effect=AssertionError("must not be called")):
            self.assertIn("secret", call("mesh_fanout", {"prompt": "x {item}", "items": ["password = hunter2hunter2"]})[0])
            self.assertIn("secret", call("mesh_ask_all", {"question": "my key is -----BEGIN RSA PRIVATE KEY-----"})[0])
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MESHMCP_ROOT": d}):
                self.assertIn("Refused", call("mesh_try_patch", {"task": "t", "files": [".env"]})[0])
                self.assertIn("Refused", call("mesh_try_patch", {"task": "t", "files": ["../../etc/passwd"]})[0])
                self.assertIn("No such file", call("mesh_try_patch", {"task": "t", "files": ["nope.py"]})[0])
                self.assertIn("Give a task", call("mesh_try_patch", {"task": "", "files": ["a.py"]})[0])

    def test_a_partial_result_is_labelled_as_a_head_start_not_a_pass(self):
        node = {"id": "n-1", "name": "pc", "online": True, "inventory": {"ollama": {"models": ["qwen2.5-coder:3b"]}}}
        fake = {"ok": False, "note": "1 test(s) still fail", "files": {}, "diff": "", "speed": {}, "partial": {"files": {"a.py": "x"}, "diff": "+x\n"}}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MESHMCP_ROOT": d}), mock.patch.object(X, "mesh_up", return_value=([node], None)), \
                mock.patch.object(X.M, "run_task", return_value=fake), mock.patch.object(X.M, "speed_report", return_value={"wall_s": 5, "mesh_nodes": {}, "mesh_answered": 0, "mesh_attempts": 0}):
            open(f"{d}/a.py", "w").write("old")
            text, err = call("mesh_try_patch", {"task": "t", "files": ["a.py"]})
        self.assertIn("NOT PASSING YET", text); self.assertIn("HEAD START", text); self.assertNotIn("PASSING CANDIDATE", text)

    def test_a_tool_that_crashes_returns_an_error_text_not_a_dead_server(self):
        with mock.patch.dict(X.TOOLS, {"mesh_status": (lambda a: 1 / 0, "d", {})}):
            text, err = call("mesh_status", {})
        self.assertTrue(err); self.assertIn("yourself", text)

    def test_the_test_command_cannot_be_chosen_by_a_tool_call(self):
        props = X.TOOLS["mesh_try_patch"][2]
        self.assertNotIn("tests", props); self.assertNotIn("command", props)


if __name__ == "__main__":
    unittest.main()
