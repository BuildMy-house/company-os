import json, os, sys, tempfile, unittest

from company_ops import substack_bridge as sb

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_substack_mcp.py")
ENV = {"SUBSTACK_PUBLICATION_URL": "https://x.substack.com", "SUBSTACK_SESSION_TOKEN": "cookie",
       "SUBSTACK_USER_ID": "1", "PATH": os.environ.get("PATH", "")}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.mktemp()
        self.mode = "ok"

    def call(self, tool, args=None, **env):
        return sb.call_tool(tool, args or {}, {**ENV, **env}, server_cmd=[sys.executable, FAKE, self.log, self.mode])

    def test_subscriber_level_and_other_tools_refused(self):
        for tool in ("export_subscribers", "list_subscribers", "get_analytics", "upload_image",
                     "get_reader_feed", "comment_on_post", "delete_draft", "set_post_body"):
            with self.assertRaises(sb.NotAllowed, msg=tool):
                self.call(tool)

    def test_allowlist_is_exactly_the_intended_tools(self):
        self.assertEqual(sb.ALLOWED_TOOLS, {"get_publication_stats", "get_post_stats", "list_posts",
                                            "create_draft_post", "publish_draft"})

    def test_publish_never_sends_email(self):
        self.call("publish_draft", {"draft_id": 5, "send": True})
        self.assertEqual(json.load(open(self.log))["arguments"], {"draft_id": 5, "send": False})

    def test_read_call_returns_json_and_child_env_is_minimal(self):
        out = self.call("get_publication_stats", DATABASE_URL="postgres://secret", SOME_SECRET="x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["env_leak"], [])  # DSNs/other secrets are not forwarded to the child

    def test_auth_error_is_auth_failed(self):
        self.mode = "auth"
        with self.assertRaises(sb.AuthFailed):
            self.call("get_post_stats")

    def test_missing_env_raises(self):
        with self.assertRaises(RuntimeError):
            sb.call_tool("get_post_stats", {}, {"PATH": "/usr/bin"}, server_cmd=[sys.executable, FAKE])


if __name__ == "__main__":
    unittest.main()
