"""Offline test-suite for the SamRonin profile generator.

Runs with zero GitHub network access and zero tokens:

    python3 -m unittest discover -s tests -v

The only "network" involved is a throwaway localhost HTTP server that
exercises pagination and retry logic for real (urllib -> local socket).
"""
import datetime as dt
import email.message
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO, "scripts", "profile.py")

_spec = importlib.util.spec_from_file_location("samronin_profile", SCRIPT)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise SystemExit(f"cannot load generator from {SCRIPT}")
gen = importlib.util.module_from_spec(_spec)
sys.modules["samronin_profile"] = gen
_spec.loader.exec_module(gen)

ORIGINAL_ASSETS = gen.ASSETS


def make_user(**over):
    u = {"login": "SamRonin", "name": "Sam Ronin", "bio": "code ronin",
         "location": "Tehran / Iran", "public_repos": 5,
         "created_at": "2025-03-01T00:00:00Z",
         "html_url": "https://github.com/SamRonin"}
    u.update(over)
    return u


def make_repo(name, **over):
    r = {"name": name, "description": f"{name} — a real project",
         "language": "TypeScript", "topics": ["react"], "fork": False,
         "archived": False, "private": False, "size": 512,
         "stargazers_count": 1, "pushed_at": "2025-10-01T00:00:00Z"}
    r.update(over)
    return r


def make_weeks(n=53):
    start = dt.date(2025, 1, 5)          # a Sunday, like GitHub's calendar
    weeks = []
    for wi in range(n):
        days = [{"date": (start + dt.timedelta(days=wi * 7 + di)).isoformat(),
                 "contributionCount": (wi + di) % 5} for di in range(7)]
        weeks.append({"contributionDays": days})
    return weeks


def build_ctx(tmp_assets):
    """Mirror main()'s pipeline with fake data — no network, no token."""
    gen.ASSETS = tmp_assets
    repos = [
        make_repo("downwise", topics=["chrome-extension", "react"],
                  description="⬇️ Smart downloader & converter | MV3 | دانلود هوشمند",
                  _langs={"TypeScript": 90000, "JavaScript": 8000}),
        make_repo("dropshrink", language="Rust", topics=["tauri", "ffmpeg"],
                  description="Fast offline compressor", _langs={"Rust": 200000}),
        make_repo("Page2LLM", description="Extract clean page text as Markdown", _langs={}),
        make_repo("profilepack", size=4, _langs={},
                  description="Synthetic test personas | QA"),
        make_repo("x" * 40, description="A repo with a very long name for truncation"),
        make_repo("a-b", description="slug twin | one"),
        make_repo("a_b", description="slug twin two"),
    ]
    user = make_user()
    ranked = gen.rank_repos(repos)
    gen.assign_slugs(ranked)
    ordered = sorted(ranked, key=gen.rank_key)
    cards = [r for r in ordered if gen.is_featurable(r)][:4] or ordered[:4]
    used = {r["name"] for r in cards}
    ctx = {"user": user, "repos": ranked, "cards": cards,
           "extra": [r for r in ordered if r["name"] not in used],
           "stats": gen.build_stats({"user": user, "total_contribs": 202}, ranked),
           "stack": gen.build_stack(ranked),
           "weeks": make_weeks(), "total_contribs": 202,
           "private_names": ["secret-project"]}
    return ctx


class GeneratorTestCase(unittest.TestCase):
    def tearDown(self):
        gen.ASSETS = ORIGINAL_ASSETS
        gen.RETRY_BASE_DELAY = 1.0


class Helpers(GeneratorTestCase):
    def test_esc(self):
        self.assertEqual(gen.esc("a<b>&c"), "a&lt;b&gt;&amp;c")
        self.assertEqual(gen.esc(None), "")

    def test_clean_text_strips_rtl_and_emoji(self):
        self.assertEqual(gen.clean_text("⬇️ Smart downloader — دانلود فایل"),
                         "Smart downloader")

    def test_clean_text_collapses_whitespace(self):
        self.assertEqual(gen.clean_text("  A   B  "), "A B")

    def test_md_cell_escapes_pipes(self):
        self.assertEqual(gen.md_cell("a | b"), "a \\| b")
        self.assertEqual(gen.md_cell(""), "—")

    def test_wrap_hard_breaks_long_words(self):
        lines = gen.wrap("A" * 200, 100, 10, 3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("…"))

    def test_wrap_normal_text(self):
        self.assertEqual(gen.wrap("one two three", 500, 10, 3), ["one two three"])

    def test_clip_word_boundary(self):
        t = gen.clip("Smart right-click downloader & converter for Chrome MV3", 30)
        self.assertTrue(t.endswith("…"))
        self.assertLessEqual(len(t), 32)
        self.assertNotIn("دانلود", gen.clip("downloader دانلود هوشمند", 30))

    def test_assign_slugs_unique_and_safe(self):
        repos = [{"name": "a-b"}, {"name": "a_b"},
                 {"name": "X" * 40}, {"name": "X" * 40 + "y"}]
        gen.assign_slugs(repos)
        slugs = [r["_slug"] for r in repos]
        self.assertEqual(len(set(slugs)), 4)
        for s in slugs:
            self.assertRegex(s, r"^[a-z0-9_]{1,32}$")

    def test_display_name_prefers_readme_title(self):
        self.assertEqual(gen.display_name({"name": "dropshrink", "_title": "DropShrink"}),
                         "DropShrink")
        self.assertEqual(gen.display_name({"name": "plain", "_title": None}), "plain")

    def test_check_leaks_case_insensitive_and_covers_summary(self):
        hits = gen.check_leaks("fine", "summary mentions SECRET-project", ["secret-project"])
        self.assertEqual(hits, ["secret-project"])
        self.assertEqual(gen.check_leaks("clean", "clean", ["secret-project"]), [])


class DerivedData(GeneratorTestCase):
    def test_build_stats_uses_public_repos_not_curated_len(self):
        data = {"user": make_user(public_repos=5), "total_contribs": 7}
        stats = gen.build_stats(data, [{"name": "a"}, {"name": "b"}])
        self.assertEqual(stats[0][0], "05")      # matches the dynamic README badge
        self.assertEqual(stats[2][0], "7")

    def test_build_stack_never_fabricates(self):
        for _, items in gen.build_stack([]):
            self.assertEqual(items, ["—"])

    def test_is_featurable(self):
        self.assertFalse(gen.is_featurable({"description": "", "size": 100}))
        self.assertTrue(gen.is_featurable({"description": "x", "size": 100}))
        self.assertFalse(gen.is_featurable({"description": "x", "size": 1}))
        self.assertTrue(gen.is_featurable({"description": "x", "size": 1,
                                           "_langs": {"Python": 10}}))

    def test_rank_repos_handles_missing_fields(self):
        ranked = gen.rank_repos([{"name": "b"},
                                 {"name": "a", "stargazers_count": 5,
                                  "pushed_at": "2025-01-01T00:00:00Z"}])
        self.assertEqual(ranked[0]["name"], "a")


class ErrorHandling(GeneratorTestCase):
    def _e(self, code, headers=None):
        h = email.message.Message()
        for k, v in (headers or {}).items():
            h[k] = v
        return urllib.error.HTTPError("http://x", code, "err", h, None)

    def test_token_missing_is_graceful(self):
        code, msg = gen.handle_fetch_error(gen.TokenMissing("no token"))
        self.assertEqual(code, 0)
        self.assertIn("::warning::", msg)

    def test_401_and_403_leave_profile_unchanged(self):
        for exc in (self._e(401), self._e(403)):
            code, msg = gen.handle_fetch_error(exc)
            self.assertEqual(code, 0)
            self.assertIn("::warning::", msg)

    def test_403_rate_limit_is_recognized(self):
        code, msg = gen.handle_fetch_error(
            self._e(403, {"X-RateLimit-Remaining": "0"}))
        self.assertEqual(code, 0)
        self.assertIn("rate limit", msg.lower())

    def test_404_fails_loudly(self):
        code, msg = gen.handle_fetch_error(self._e(404))
        self.assertEqual(code, 1)
        self.assertIn("::error::", msg)

    def test_500_fails_loudly(self):
        code, msg = gen.handle_fetch_error(self._e(500))
        self.assertEqual(code, 1)
        self.assertIn("::error::", msg)

    def test_network_failure_fails_loudly(self):
        code, msg = gen.handle_fetch_error(urllib.error.URLError("conn refused"))
        self.assertEqual(code, 1)
        self.assertIn("::error::", msg)

    def test_fetch_without_token_raises_token_missing(self):
        with self.assertRaises(gen.TokenMissing):
            gen.fetch("SamRonin", None)


class LocalHttpApi(GeneratorTestCase):
    """Real urllib calls against a localhost stand-in for api.github.com."""

    def setUp(self):
        super().setUp()
        gen.RETRY_BASE_DELAY = 0.01
        self.hits = []

    def _serve(self, do_GET):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                do_GET(outer, self)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = gen.API
        self.addCleanup(setattr, gen, "API", old)
        gen.API = f"http://127.0.0.1:{srv.server_address[1]}"

    def _json(self, req, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        req.send_response(200)
        req.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            req.send_header(k, v)
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        req.wfile.write(body)

    def test_gh_all_pages_follows_link_header(self):
        from urllib.parse import parse_qs, urlparse

        def handler(t, req):
            qs = parse_qs(urlparse(req.path).query)
            page = int(qs.get("page", ["1"])[0])
            t.hits.append(page)
            port = req.server.server_address[1]
            link = (f'<http://127.0.0.1:{port}/users/x/repos?page=2>; rel="next"'
                    if page == 1 else "")
            t._json(req, [{"name": f"repo-{page}-{i}"} for i in range(2)],
                    {"Link": link} if link else None)

        self._serve(handler)
        out = gen.gh_all_pages("/users/x/repos?per_page=100", "tok")
        self.assertEqual(len(out), 4)
        self.assertEqual(self.hits, [1, 2])

    def test_transient_500_is_retried_then_succeeds(self):
        state = {"n": 0}

        def handler(t, req):
            state["n"] += 1
            t.hits.append(state["n"])
            if state["n"] < 3:
                req.send_response(500)
                req.send_header("Content-Length", "0")
                req.end_headers()
                return
            t._json(req, {"ok": True})

        self._serve(handler)
        self.assertEqual(gen.gh("/x", "tok"), {"ok": True})
        self.assertEqual(len(self.hits), 3)

    def test_404_is_not_retried(self):
        def handler(t, req):
            t.hits.append(1)
            req.send_response(404)
            req.send_header("Content-Length", "0")
            req.end_headers()

        self._serve(handler)
        with self.assertRaises(urllib.error.HTTPError):
            gen.gh("/missing", "tok")
        self.assertEqual(len(self.hits), 1)


class EndToEndOffline(GeneratorTestCase):
    def test_full_pipeline_generates_valid_assets_and_docs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        assets = os.path.join(tmp.name, "assets")
        ctx = build_ctx(assets)

        gen.banner(2025)
        gen.divider()
        gen.typing()
        gen.footer_asset()
        gen.stats_asset(ctx["stats"])
        gen.stack_asset(ctx["stack"])
        gen.activity_asset(ctx["weeks"], ctx["total_contribs"])
        for i, r in enumerate(ctx["cards"], start=1):
            gen.card_asset(i, r)

        readme = gen.build_readme(ctx)
        summary = gen.build_summary(ctx)
        gen.validate_assets()                       # must not raise

        # every written SVG is well-formed XML
        svgs = sorted(f for f in os.listdir(assets) if f.endswith(".svg"))
        self.assertGreaterEqual(len(svgs), 9)
        for f in svgs:
            ET.parse(os.path.join(assets, f))

        # README references only assets that exist
        for ref in re.findall(r"assets/[\w./-]+\.svg", readme):
            self.assertTrue(os.path.isfile(os.path.join(tmp.name, ref)), ref)

        # no fabricated metrics survive in the terminal strip
        typing_svg = open(os.path.join(assets, "typing.svg"), encoding="utf-8").read()
        self.assertNotIn("UPTIME", typing_svg)
        self.assertNotIn("LICENSE", typing_svg)
        self.assertIn("TELEMETRY", typing_svg)

        # dynamic copyright year + no stale activity fiction
        self.assertIn(f"© {dt.date.today().year}",
                      open(os.path.join(assets, "footer.svg"), encoding="utf-8").read())
        self.assertNotIn("STEALTH",
                         open(os.path.join(assets, "activity.svg"), encoding="utf-8").read())

        # card links point at repos that exist in the data
        names = {r["name"] for r in ctx["repos"]}
        for m in re.finditer(r"https://github\.com/SamRonin/([\w.-]+)\)", readme):
            self.assertIn(m.group(1), names, m.group(1))

        # markdown tables survive pipe characters in descriptions
        self.assertIn("\\|", readme)
        # RTL text never reaches the LTR console block
        self.assertNotIn("دانلود", readme)

        # headline count matches public_repos (badge consistency)
        self.assertEqual(ctx["stats"][0][0], "05")

        # slugs are unique even for name twins
        slugs = [r["_slug"] for r in ctx["repos"]]
        self.assertEqual(len(slugs), len(set(slugs)))

        # long repo names are truncated on cards, not overflowing
        p = os.path.join(assets, "card_" + "x" * 32 + ".svg")
        if os.path.isfile(p):
            self.assertIn("…", open(p, encoding="utf-8").read())

        # summary stays valid markdown even for hostile bio/description input
        hostile_ctx = dict(ctx)
        hostile_ctx["user"] = make_user(bio="ronin\n**bold** <script>|x")
        summary2 = gen.build_summary(hostile_ctx)
        self.assertNotIn("<script>", summary2)
        self.assertNotIn("ronin\n", summary2)

        # empty calendar must not crash the activity asset
        gen.activity_asset([], 0)
        ET.parse(os.path.join(assets, "activity.svg"))


class ShippedArtifacts(unittest.TestCase):
    """Regression net over the files actually committed to the repo."""

    def test_every_svg_in_repo_is_valid_xml(self):
        bad = []
        folder = os.path.join(REPO, "assets")
        for f in sorted(os.listdir(folder)):
            if f.endswith(".svg"):
                try:
                    ET.parse(os.path.join(folder, f))
                except Exception as e:
                    bad.append(f"{f}: {e}")
        self.assertEqual(bad, [])

    def test_readme_asset_references_exist(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        missing = [ref for ref in re.findall(r"assets/[\w./-]+\.svg", readme)
                   if not os.path.isfile(os.path.join(REPO, ref))]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
