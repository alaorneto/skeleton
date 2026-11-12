"""Regras puras: SemVer, leitura de texto, configuração, arquivos de versão e changelog."""

import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.config import Config, ConfigError
from devflow.plan import validate_analysis
from devflow.release import ReleaseError, rewrite, update_changelog
from devflow.semver import Version, effective_level, max_level, next_prerelease, next_version
from devflow.text import (
    breaking_notes,
    checkboxes,
    marked_value,
    meaningful,
    parse_conventional,
    referenced_issues,
    sections,
    slugify,
)

V = Version.parse


class SemverTest(unittest.TestCase):
    def test_parse_and_print_round_trip(self):
        for text in ("0.5.0", "1.2.3-rc.1", "1.0.0-alpha.beta+build.5", "10.20.30"):
            self.assertEqual(str(V(text)), text)
        for text in ("1.2", "v1.2.3", "01.2.3", "1.2.3-", "1.2.3-01", "abc", ""):
            self.assertIsNone(Version.try_parse(text), text)

    def test_precedence_follows_the_specification(self):
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.1.0",
            "2.0.0",
        ]
        self.assertEqual([str(v) for v in sorted(map(V, reversed(ordered)), key=Version.key)], ordered)
        self.assertEqual(V("1.0.0+a").key(), V("1.0.0+b").key())

    def test_next_number_for_each_level(self):
        self.assertEqual(str(next_version(V("1.2.3"), "patch")), "1.2.4")
        self.assertEqual(str(next_version(V("1.2.3"), "minor")), "1.3.0")
        self.assertEqual(str(next_version(V("1.2.3"), "major")), "2.0.0")
        self.assertEqual(str(next_version(V("1.3.0-rc.2"), "patch")), "1.3.1")
        with self.assertRaises(ValueError):
            next_version(V("1.2.3"), "none")

    def test_zero_major_keeps_breaking_changes_in_the_minor(self):
        self.assertEqual(effective_level(V("0.5.0"), "major"), "minor")
        self.assertEqual(str(next_version(V("0.5.2"), "major")), "0.6.0")
        self.assertEqual(str(next_version(V("0.5.2"), "minor")), "0.6.0")
        self.assertEqual(str(next_version(V("0.5.2"), "patch")), "0.5.3")
        self.assertEqual(str(next_version(V("0.5.2"), "major", "strict")), "1.0.0")
        self.assertEqual(effective_level(V("1.0.0"), "major"), "major")

    def test_the_highest_level_wins(self):
        self.assertEqual(max_level(["none", "patch", "minor", "patch"]), "minor")
        self.assertEqual(max_level([]), "none")

    def test_prereleases_count_per_target_and_channel(self):
        existing = [V("1.3.0-rc.1"), V("1.3.0-rc.2"), V("1.3.0-beta.7"), V("1.4.0-rc.9")]
        self.assertEqual(str(next_prerelease(V("1.3.0"), "rc", existing)), "1.3.0-rc.3")
        self.assertEqual(str(next_prerelease(V("2.0.0"), "rc", existing)), "2.0.0-rc.1")
        with self.assertRaises(ValueError):
            next_prerelease(V("1.3.0"), "r c", [])

    def test_pep440_form(self):
        self.assertEqual(V("1.3.0-rc.1").pep440(), "1.3.0rc1")
        self.assertEqual(V("1.3.0-alpha.2").pep440(), "1.3.0a2")
        self.assertEqual(V("1.3.0").pep440(), "1.3.0")
        self.assertEqual(V("1.3.0-nightly.5").pep440(), "1.3.0-nightly.5")


class TextTest(unittest.TestCase):
    def test_closing_keywords_are_told_apart_from_references(self):
        body = "Closes #1\nRefs #2\nfixes acme/demo#3\nPart of #4\nResolves other/repo#9"
        self.assertEqual(referenced_issues(body, "acme/demo"), [1, 2, 3, 4])
        self.assertEqual(referenced_issues(body, "acme/demo", closing_only=True), [1, 3])
        self.assertEqual(referenced_issues("feat: x (#7)", closing_only=True), [])

    def test_sections_accept_both_heading_levels_and_skip_code_fences(self):
        body = "## Resumo\ntexto\n```\n## falso\n```\n### Critérios de aceite\n- [x] a\n- [ ] b\n"
        found = sections(body)
        self.assertEqual(list(found), ["resumo", "critérios de aceite"])
        self.assertIn("## falso", found["resumo"])
        self.assertEqual(checkboxes(found["critérios de aceite"]), [(True, "a"), (False, "b")])

    def test_template_leftovers_are_not_content(self):
        self.assertEqual(meaningful("<!-- dica -->\n- \n-\n- [ ] \n_No response_\n"), "")
        self.assertEqual(meaningful("<!-- dica -->\n- `make test` — ok"), "- `make test` — ok")
        self.assertEqual(
            marked_value("- Motivo da sobreposição: só muda o padrão", "motivo da sobreposição"), "só muda o padrão"
        )
        self.assertEqual(marked_value("- Motivo da sobreposição:", "Motivo da sobreposição"), "")

    def test_conventional_commits_and_breaking_markers(self):
        parsed = parse_conventional("feat(api)!: drop v1")
        self.assertEqual(
            (parsed.type, parsed.scope, parsed.breaking, parsed.description), ("feat", "api", True, "drop v1")
        )
        self.assertIsNone(parse_conventional("Merge branch 'main'"))
        self.assertEqual(breaking_notes("fix: x\n\nBREAKING CHANGE: a virou b\nBREAKING-CHANGE: c"), ["a virou b", "c"])
        self.assertEqual(breaking_notes("<!-- BREAKING CHANGE: exemplo do modelo -->"), [])

    def test_slug(self):
        self.assertEqual(slugify("Corrigir a origem pública do e-mail!"), "corrigir-a-origem-publica-do-e-mail")
        self.assertLessEqual(len(slugify("x" * 200)), 40)
        self.assertEqual(slugify("???"), "trabalho")


class ConfigTest(unittest.TestCase):
    def test_defaults_are_valid_and_expose_the_labels(self):
        cfg = Config()
        self.assertEqual(cfg.type_for_prefix("fix"), "bug")
        self.assertEqual(cfg.type_level("feature"), "minor")
        for label in ("bug", "epic", "release", "no-release", "breaking-change", "semver:major"):
            self.assertIn(label, cfg.all_labels())

    def test_project_values_override_key_by_key_but_types_replace_whole(self):
        cfg = Config({"versioning": {"tag_prefix": "rel-"}, "types": {"task": {"prefixes": ["task"]}}})
        self.assertEqual(cfg.tag_prefix, "rel-")
        self.assertEqual(cfg.versioning["zero_major"], "breaking-is-minor")
        self.assertEqual(cfg.type_labels, ["task"])
        self.assertNotIn("release", Config({"versioning": {"enabled": False}}).all_labels())

    def test_invalid_values_name_the_key(self):
        cases = {
            "types.bug.level": {"types": {"bug": {"prefixes": ["fix"], "level": "huge"}}},
            "versioning.files[0]": {"versioning": {"files": [{"path": "a", "search": "x", "regex": "y"}]}},
            "versioning.files[0].replace": {"versioning": {"files": [{"path": "a", "regex": "y"}]}},
            "versioning.initial_version": {"versioning": {"initial_version": "1.0"}},
            "versioning.floors[0].regex": {"versioning": {"floors": [{"globs": ["*"], "regex": "nogroup"}]}},
            "release.mode": {"release": {"mode": "magic"}},
            "release.commit_message": {"release": {"commit_message": "release"}},
            "pr.severity.title": {"pr": {"severity": {"title": "fatal"}}},
            "schema": {"schema": 2},
        }
        for key, data in cases.items():
            with self.subTest(key), self.assertRaises(ConfigError) as raised:
                Config(data)
            self.assertIn(f"`{key}`", str(raised.exception))

    def test_release_commit_is_recognised_with_and_without_squash_suffix(self):
        pattern = Config().release_subject_pattern()
        self.assertEqual(pattern.match("chore(release): v1.2.3")["tag"], "v1.2.3")
        self.assertEqual(pattern.match("chore(release): v1.2.3 (#45)")["tag"], "v1.2.3")
        self.assertIsNone(pattern.match('Revert "chore(release): v1.2.3"'))
        self.assertIsNone(pattern.match("chore(release): v1.2.3 and more"))


class VersionFilesTest(unittest.TestCase):
    def test_literal_search_replaces_the_current_version(self):
        entry = {"path": "p", "search": 'version = "{current}"'}
        self.assertEqual(
            rewrite('name = "x"\nversion = "1.2.0"\n', entry, V("1.2.0"), V("1.3.0")), 'name = "x"\nversion = "1.3.0"\n'
        )

    def test_the_occurrence_count_is_enforced(self):
        entry = {"path": "p", "search": 'version = "{current}"'}
        for text in ('version = "9.9.9"\n', 'version = "1.2.0"\nversion = "1.2.0"\n'):
            with self.assertRaises(ReleaseError):
                rewrite(text, entry, V("1.2.0"), V("1.3.0"))
        twice = {**entry, "count": 2}
        self.assertEqual(
            rewrite('version = "1.2.0"\nversion = "1.2.0"\n', twice, V("1.2.0"), V("1.3.0")).count("1.3.0"), 2
        )

    def test_regex_entries_and_derived_tokens(self):
        entry = {
            "path": "m",
            "regex": r'"core": ">=\d+\.\d+\.\d+,<\d+\.\d+\.\d+"',
            "replace": '"core": ">={new_major}.{new_minor}.0,<{new_next_minor}"',
        }
        text = '{"core": ">=0.5.0,<0.6.0"}'
        self.assertEqual(rewrite(text, entry, V("0.5.0"), V("0.5.1")), text)
        self.assertEqual(rewrite(text, entry, V("0.5.1"), V("0.6.0")), '{"core": ">=0.6.0,<0.7.0"}')
        pep = {"path": "p", "search": 'version = "{current_pep440}"'}
        self.assertEqual(rewrite('version = "1.3.0rc1"', pep, V("1.3.0-rc.1"), V("1.3.0")), 'version = "1.3.0"')

    def test_replacement_text_is_literal(self):
        entry = {"path": "p", "regex": r"VERSION=\S+", "replace": r"VERSION={new}\1"}
        self.assertEqual(rewrite("VERSION=1.0.0", entry, V("1.0.0"), V("1.0.1")), r"VERSION=1.0.1\1")


class ChangelogFileTest(unittest.TestCase):
    cfg = Config()

    def test_first_release_creates_the_file_with_its_header(self):
        text = update_changelog(None, "1.0.0", "## [1.0.0] — 2030-01-01", "### Correções\n\n- a", self.cfg)
        self.assertTrue(text.startswith("# Changelog\n\n"))
        self.assertTrue(text.endswith("## [1.0.0] — 2030-01-01\n\n### Correções\n\n- a\n"))

    def test_new_versions_go_on_top_and_rewriting_one_is_idempotent(self):
        first = update_changelog(None, "1.0.0", "## [1.0.0] — d", "- a", self.cfg)
        second = update_changelog(first, "1.1.0", "## [1.1.0](url) — d", "- b", self.cfg)
        self.assertLess(second.index("## [1.1.0]"), second.index("## [1.0.0]"))
        self.assertEqual(update_changelog(second, "1.1.0", "## [1.1.0](url) — d", "- b", self.cfg), second)
        replaced = update_changelog(second, "1.1.0", "## [1.1.0](url) — d", "- c", self.cfg)
        self.assertIn("- c", replaced)
        self.assertNotIn("- b", replaced)
        self.assertIn("- a", replaced)
        self.assertEqual(replaced.count("## ["), 2)

    def test_a_version_is_not_confused_with_one_that_starts_the_same(self):
        text = update_changelog(None, "1.1.10", "## [1.1.10] — d", "- ten", self.cfg)
        text = update_changelog(text, "1.1.1", "## [1.1.1] — d", "- one", self.cfg)
        self.assertIn("- ten", text)
        self.assertEqual(text.count("## ["), 2)


class AnalysisFormatTest(unittest.TestCase):
    def test_level_is_recomputed_from_the_changes(self):
        data = validate_analysis(
            {
                "schema": 1,
                "level": "patch",
                "changes": [
                    {"pr": 3, "level": "minor", "summary": "x"},
                    {"commit": "abcdef1", "level": "major", "summary": "y"},
                ],
            }
        )
        self.assertEqual(data["level"], "major")
        self.assertTrue(data["changes"][1]["breaking"])

    def test_malformed_input_is_refused(self):
        for bad in (
            [],
            {"schema": 2, "changes": []},
            {"schema": 1, "changes": [{"level": "patch", "summary": "x"}]},
            {"schema": 1, "changes": [{"pr": True, "level": "patch", "summary": "x"}]},
            {"schema": 1, "changes": [{"pr": 1, "level": "patch", "summary": " "}]},
            {"schema": 1, "changes": [{"pr": 1, "level": "patch", "summary": "x", "evidence": "y"}]},
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                validate_analysis(bad)


if __name__ == "__main__":
    unittest.main()
