"""Tier-1: classification rules — DigUp Rules.swift port."""

from __future__ import annotations

import pytest

from goldmine.ingest import rules


@pytest.mark.parametrize("ext", sorted(rules.DOC_EXTS))
def test_every_digup_doc_extension(ext):
    decision = rules.classify(f"file.{ext}", ext, code=False)
    assert decision.kind == "doc"
    assert decision.skip_reason is None


@pytest.mark.parametrize("ext", ["py", "rs", "json", "csv", "yaml", "sql", "sh"])
def test_code_exts_off_by_default(ext):
    decision = rules.classify(f"f.{ext}", ext, code=False)
    assert decision.kind is None
    assert decision.skip_reason == "code or data file"


@pytest.mark.parametrize("ext", ["py", "rs", "json", "csv", "sh"])
def test_code_exts_with_code_flag(ext):
    decision = rules.classify(f"f.{ext}", ext, code=True)
    assert decision.kind == "code"
    assert decision.skip_reason is None


def test_pdf_image_audio_video_exts():
    assert rules.classify("a.pdf", "pdf", code=False).kind == "pdf"
    assert rules.classify("a.jpg", "jpg", code=False).kind == "image"
    assert rules.classify("a.heic", "heic", code=False).kind == "image"
    assert rules.classify("a.mp3", "mp3", code=False).kind == "audio"
    assert rules.classify("a.mov", "mov", code=False).kind == "video"
    assert rules.classify("a.xyz", "xyz", code=False) == rules.Decision(None, None)


def test_unknown_type_is_reported_by_walker():
    """kind None + no skip_reason => walker reports 'unknown type'."""
    d = rules.classify("a.xyz", "xyz", code=False)
    assert d.kind is None and d.skip_reason is None


@pytest.mark.parametrize("name", [
    "server.pem", "id_ed25519", "private.key", "CERT.p12", "keystore.jks",
    ".env", "id_rsa", "secrets.txt", "credentials.json", ".netrc", ".npmrc",
    "htpasswd", "app.mobileprovision",
])
def test_secrets(name):
    assert rules.is_secret(name)


def test_secret_prefix_semantics():
    # DigUp uses hasPrefix: 'secret.txt' IS a secret, 'keynote.md' is NOT.
    assert rules.is_secret("secret.txt")
    assert not rules.is_secret("keynote.md")
    assert not rules.is_secret("environment.txt")


def test_secret_word_prefix_needs_a_boundary():
    """02 F3: word prefixes match only at a name start followed by a
    separator/dot/end. 'secretary_notes.md' is a DOCUMENT (DigUp's bare
    hasPrefix false-skips it — deliberate divergence, Rules.swift:87)."""
    assert not rules.is_secret("secretary_notes.md")
    assert not rules.is_secret("secretive.txt")
    assert not rules.is_secret("credentialing-policy.md")
    # ...but the real key-file shapes still refuse:
    assert rules.is_secret("secret.pem")            # also via ext
    assert rules.is_secret("secrets.json")
    assert rules.is_secret("secret-material.md")    # separator boundary
    assert rules.is_secret("credentials.json")
    assert rules.is_secret("credentials_training.md")  # plural + separator: refuse
    assert rules.is_secret("id_rsa_old")            # separator boundary
    assert not rules.is_secret("id_rsa2.pub")       # no boundary after id_rsa
    assert rules.is_secret(".envrc")                # dot-config: bare prefix kept


def test_minified():
    assert rules.looks_minified("app.min.js")
    assert rules.looks_minified("x.min.mjs")
    assert rules.looks_minified("s.min.css")
    assert not rules.looks_minified("app.js")
    assert not rules.looks_minified("minute.js")


def test_license():
    for name in ("LICENSE", "licence.md", "COPYING", "NOTICE.txt"):
        assert rules.is_license(name)
    assert not rules.is_license("certificates.txt")


def test_ts_vs_mpeg_ts_sniff():
    # typescript text
    d = rules.classify("x.ts", "ts", code=False, head=b"const a = 1; " * 50)
    assert d.kind is None and d.skip_reason == "code or data file"
    d = rules.classify("x.ts", "ts", code=True, head=b"const a = 1; " * 50)
    assert d.kind == "code"
    # real transport stream (188-byte packets; needs >= 389 bytes to decide)
    ts_head = b"\x47" + b"\x00" * 187
    ts_head += b"\x47" + b"\x00" * 187
    ts_head += b"\x47" + b"\x00" * 200
    d = rules.classify("x.ts", "ts", code=False, head=ts_head)
    assert d.kind == "video"
    # no head provided: falls back to code-or-skip (walker reads the head)
    d = rules.classify("x.ts", "ts", code=False)
    assert d.kind is None


def test_folder_skips():
    assert rules.folder_skip("node_modules") == "developer folder"
    assert rules.folder_skip("__pycache__") == "developer folder"
    assert rules.folder_skip("DerivedData") == "developer folder"
    assert rules.folder_skip("debug.noindex") == ".noindex folder"
    assert rules.folder_skip("src") is None
    assert rules.folder_skip("docs") is None


def test_folder_skips_build_artifacts():
    """DigUp P2 closeout A5 (DigUp Rules.swift:100-104): build-artifact folder
    names are skipped wherever they appear — no is_project_root guard on this
    set (that mechanism is separate and untouched)."""
    for name in ("build", "target", "dist", "out", ".next"):
        assert rules.folder_skip(name) == "developer folder", name


def test_project_markers():
    assert rules.is_project_root({".git", "src"})
    assert rules.is_project_root({"package.json"})
    assert rules.is_project_root({"App.xcodeproj"})
    assert rules.is_project_root({"Cargo.toml", "src"})
    assert not rules.is_project_root({"README.md", "src"})


def test_screenshot_prefix():
    assert rules.is_screenshot("Screenshot 2026-01-01 at 10.00.00.png")
    assert rules.is_screenshot("Screen Shot 2026-01-01 at 10.00.00.png")
    assert rules.is_screenshot("CleanShot 03.png")
    assert rules.is_screenshot("SCR-0412.png")
    assert not rules.is_screenshot("vacation.png")


def test_dataset_folders_heuristic():
    files = [
        ("batch", f"{i:08d}", "image") for i in range(400)
    ]  # 00000000-style machine names, 400 of them
    found = rules.dataset_folders(files, roots=set())
    assert sum(found.values()) >= 350
    # roots are never datasets
    folder = files[0][0]
    assert rules.dataset_folders(files, roots={folder}) == {}
    # camera names are never machine-named
    cam = [("f", f"IMG_{i:04d}", "image") for i in range(400)]
    assert rules.dataset_folders(cam, roots=set()) == {}


# ------------------------------------------------------ v2 quick-fixes (B6/B7)


def test_code_file_names_defined_and_used():
    """CODE_FILE_NAMES was exported in __all__ since v1 but never defined —
    a latent NameError (02 §4 G8). It exists now and classifies name-only
    code files (DigUp Rules.swift:53-56, verbatim list)."""
    assert "CODE_FILE_NAMES" in rules.__all__
    for name in ("Makefile", "GNUmakefile", "Dockerfile", "Containerfile",
                 "Justfile", "Rakefile", "Gemfile", "Podfile", "Brewfile",
                 "Procfile", "Vagrantfile", "Jenkinsfile"):
        assert name in rules.CODE_FILE_NAMES
    d = rules.classify("Makefile", "", code=True)
    assert d.kind == "code" and d.skip_reason is None
    # Dockerfile variants via prefix (DigUp: name.hasPrefix("Dockerfile"))
    d = rules.classify("Dockerfile.dev", "dev", code=True)
    assert d.kind == "code"
    # off by default: name-only code files report the code-or-skip reason
    d = rules.classify("Makefile", "", code=False)
    assert d.kind is None and d.skip_reason == "code or data file"


def test_archives_are_honestly_unsupported():
    """zip/tar/gz/7z/rar/dmg/pkg -> unsupported with the fix-it reason, not
    the vague 'unknown type' (02 §5.5 archive honesty)."""
    for ext in ("zip", "tar", "gz", "tgz", "7z", "rar", "dmg", "pkg", "jar"):
        d = rules.classify(f"backup.{ext}", ext, code=False)
        assert d.kind is None
        assert d.skip_reason == rules.ARCHIVE_REASON
        assert "archive" in d.skip_reason


def test_bundle_and_package_directories_skipped():
    """DigUp Crawler.swift:63-65 skipped packages ('app or bundle', counted
    notice). goldmine ports it as an extension set on folder_skip."""
    assert rules.folder_skip("Foo.app") == "app or bundle"
    assert rules.folder_skip("Foo.bundle") == "app or bundle"
    assert rules.folder_skip("Foo.framework") == "app or bundle"
    assert rules.folder_skip("Proj.xcodeproj") == "app or bundle"
    assert rules.folder_skip("Pod.xcworkspace") == "app or bundle"
    assert rules.folder_skip("Plug.kext") == "app or bundle"
    # plain folders (and files-with-dots) still descend
    assert rules.folder_skip("src") is None
    assert rules.folder_skip("my.docs") is None
    assert rules.folder_skip("node_modules") == "developer folder"  # unchanged


def test_eml_epub_classified_as_docs():
    assert rules.classify("mail.eml", "eml", code=False).kind == "doc"
    assert rules.classify("book.epub", "epub", code=False).kind == "doc"
