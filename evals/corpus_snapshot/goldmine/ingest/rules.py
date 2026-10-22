"""Classification rules — DigUp Rules.swift port, extension-set based (D6).

DigUp asked UniformTypeIdentifiers what a file is; goldmine uses extension sets
plus magic sniffing (util.sniff_image / util.is_mpeg_ts) so the whole walker works
from stdlib alone. The DigUp sets are ported verbatim; image/audio/video get
explicit extension lists where DigUp relied on UTType conformance.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "Decision",
    "DOC_EXTS", "CODE_EXTS", "PDF_EXTS", "IMAGE_EXTS", "AUDIO_EXTS", "VIDEO_EXTS",
    "SECRET_EXTS", "SECRET_PREFIXES", "MINIFIED_SUFFIXES", "LICENSE_PREFIXES",
    "SKIP_FOLDERS", "PROJECT_MARKERS", "PROJECT_BUNDLE_EXTS", "CODE_FILE_NAMES",
    "ARCHIVE_EXTS", "ARCHIVE_REASON", "PACKAGE_DIR_EXTS",
    "SCREENSHOT_PREFIXES", "SCREENSHOT_XATTR", "TS_EXTS",
    "classify", "is_secret", "looks_minified", "is_license", "is_screenshot",
    "set_screenshot_xattr",
    "folder_skip", "looks_machine_named", "dataset_folders",
]

# -- extension sets -------------------------------------------------------------

#: DigUp doc extensions plus eml/epub and reStructuredText through plain text.
DOC_EXTS = {"txt", "text", "rst", "md", "markdown", "rtf", "docx", "doc", "odt",
            "html", "htm", "eml", "epub"}

#: Archives — NEVER content-extracted (02 §5.5, documented decision: zip-bomb
#: surface, and the useful contents are usually findable by path). The walker
#: records them as unsupported with a fix-it reason instead of "unknown type".
ARCHIVE_EXTS = {
    "zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar",
    "dmg", "pkg", "iso", "jar", "war", "ear",
}
ARCHIVE_REASON = "archive — extract to a folder and ingest that"

#: Source code and data files: off by default (ripgrep searches them better and
#: they flood an index) — DigUp codeExtensions, verbatim.
CODE_EXTS = {
    "py", "ipynb", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "m", "mm", "swift", "tsx",
    "cts", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts", "scala", "rb",
    "php", "cs", "fs", "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "r", "jl",
    "dart", "sql", "vue", "svelte", "css", "scss", "sass", "less", "json", "jsonl",
    "yaml", "yml", "toml", "xml", "plist", "gradle", "cmake", "mk", "lock", "log",
    "csv", "tsv", "ini", "cfg", "conf", "proto", "graphql", "tf", "nix", "zig", "hs",
    "ex", "exs", "erl", "clj", "elm", "ml", "vim", "el", "asm", "metal", "glsl",
    "hlsl", "wgsl", "cu", "pyi", "pyc",
}

# DigUp used UTType conformance for these; goldmine ports them as explicit sets.
PDF_EXTS = {"pdf"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "tiff", "tif", "bmp", "avif"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "aiff", "aif", "wma", "caf"}
VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "mpg", "mpeg", "wmv", "flv"}

#: .ts / .mts / .m2ts are usually TypeScript; only a real transport stream is video.
TS_EXTS = {"ts", "mts", "m2ts"}

SECRET_EXTS = {
    "pem", "key", "p12", "pfx", "keystore", "jks", "ppk", "kdbx", "gpg", "asc",
    "crt", "cer", "der", "p8", "mobileprovision",
}
#: Word-style prefixes: matched only at the name start followed by a
#: separator/dot/end, so "secret.pem" is refused but "secretary_notes.md"
#: indexes. DELIBERATE DIVERGENCE from DigUp Rules.swift:87, whose bare
#: hasPrefix false-skips "secretary_notes.md" as a key file; plurals are
#: listed explicitly so "secrets.json"/"credentials.json" stay refused.
#: Dot-config names (".env" …) keep the bare startswith — they are already
#: delimiter-delimited and ".envrc" (direnv) must stay refused.
SECRET_WORD_PREFIXES = (
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "secret", "secrets", "credential", "credentials", "htpasswd",
)
SECRET_NAME_PREFIXES = (".env", ".netrc", ".npmrc", ".pypirc", ".htpasswd")
#: exported since v1 (02 §4); now the union of both prefix families
SECRET_PREFIXES = SECRET_WORD_PREFIXES + SECRET_NAME_PREFIXES
_SECRET_BOUNDARY = "._- "

MINIFIED_SUFFIXES = (".min.js", ".min.mjs", ".min.css")
LICENSE_PREFIXES = ("license", "licence", "copying", "notice")

#: Code known by its name alone (DigUp Rules.swift codeFileNames, verbatim —
#: "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
#: "Justfile", "justfile", "Rakefile", "Gemfile", "Podfile", "Brewfile",
#: "Procfile", "Vagrantfile", "Jenkinsfile"). Exported in __all__ since v1
#: but never defined — a latent NameError/ImportError, fixed in v2 (02 §4 G8).
CODE_FILE_NAMES = frozenset({
    "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
    "Justfile", "justfile", "Rakefile", "Gemfile", "Podfile", "Brewfile",
    "Procfile", "Vagrantfile", "Jenkinsfile",
})

#: Directory suffixes macOS treats as packages/bundles. DigUp skipped these
#: via FileManager isPackage with reason "app or bundle" (Crawler.swift:63-65);
#: stdlib Python has no isPackage, so the extension set is the port.
PACKAGE_DIR_EXTS = (
    "app", "bundle", "framework", "kext", "xcodeproj", "xcworkspace",
    "appex", "xctest", "plugin", "prefpane", "qlgenerator", "action",
    "definition", "docset", "workflow", "xpc", "pkg", "mpkg",
)

#: Folders never worth walking into (DigUp skippedFolderNames, verbatim).
SKIP_FOLDERS = {
    "node_modules", "bower_components", "venv", "site-packages", "__pycache__",
    "DerivedData", "Pods", "Carthage",
}

#: Any of these in a folder makes it a code project (DigUp projectMarkers).
PROJECT_MARKERS = (
    ".git", "package.json", "Package.swift", "pyproject.toml", "setup.py",
    "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt",
)
PROJECT_BUNDLE_EXTS = {"xcodeproj", "xcworkspace"}

SCREENSHOT_PREFIXES = ("Screenshot", "Screen Shot", "CleanShot", "SCR-")

MACHINE_NAMED_1 = re.compile(r"^[0-9a-f]{8,}([-_][0-9a-f]{4,})*$")
MACHINE_NAMED_2 = re.compile(r"^[a-z]{0,16}[-_. ]?\d{3,}([-_.]\d+)*$")
CAMERA_PREFIXES = (
    "img", "dsc", "pxl", "mvimg", "vid", "gopr", "dji", "photo", "whatsapp", "screenshot",
)


@dataclass
class Decision:
    """kind ∈ doc|pdf|code|image|audio|video|webpage|None; skip_reason explains
    an intentional not-read (unsupported rows). Both None => ignore silently? No:
    the walker reports unknown types as unsupported with reason 'unknown type'."""

    kind: str | None
    skip_reason: str | None


def is_secret(name: str) -> bool:
    """Keys, certificates, password and credential files: never read, anywhere.

    Word prefixes need a boundary after them (separator/dot/end-of-name):
    "secret.pem"/"secrets.json" are secrets, "secretary_notes.md" is a document
    (DigUp's bare hasPrefix swallows it — see SECRET_WORD_PREFIXES above).
    """
    lower = name.lower()
    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
    if ext in SECRET_EXTS and "." in lower:
        return True
    if lower.startswith(SECRET_NAME_PREFIXES):
        return True
    for prefix in SECRET_WORD_PREFIXES:
        if lower.startswith(prefix):
            rest = lower[len(prefix):]
            if rest == "" or rest[0] in _SECRET_BOUNDARY:
                return True
    return False


def looks_minified(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(MINIFIED_SUFFIXES)


def is_license(name: str) -> bool:
    lower = name.lower()
    return lower.startswith(LICENSE_PREFIXES)


#: macOS tags real screenshots with this xattr (Spotlight/Quartz); DigUp read
#: it first and fell back to name prefixes (Rules.swift:139-144). v2 adopts
#: the same order (02 §5.2 Scope A) via stdlib ctypes — no pyobjc.
SCREENSHOT_XATTR = "com.apple.metadata:kMDItemIsScreenCapture"


def _xattr_is_screenshot(path) -> bool:
    """True when the screen-capture xattr exists and is truthy. stdlib ctypes
    getxattr(3) — macOS-only; absent attr, non-macOS, or any error -> False
    (the name-prefix fallback decides those)."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.dylib", use_errno=True
        )
        libc.getxattr.restype = ctypes.c_ssize_t
        libc.getxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        cpath = os.fsencode(str(path))
        cname = SCREENSHOT_XATTR.encode("ascii")
        size = libc.getxattr(cpath, cname, None, 0, 0, 0)
        if size <= 0:
            return False
        buf = ctypes.create_string_buffer(size)
        got = libc.getxattr(cpath, cname, buf, size, 0, 0)
        if got <= 0:
            return False
        value = bytes(buf.raw[:got]).strip(b"\x00 \t\r\n")
        return value not in (b"", b"0")
    except Exception:  # non-macOS, ctypes unavailable, EINVAL — never a crash
        return False


def set_screenshot_xattr(path, value: bytes = b"1") -> bool:
    """Test/fixture helper: setxattr(3) the screen-capture marker. Returns
    False where setxattr is unavailable (non-macOS) so tests can skip."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.dylib", use_errno=True
        )
        libc.setxattr.restype = ctypes.c_int
        libc.setxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        rc = libc.setxattr(
            os.fsencode(str(path)), SCREENSHOT_XATTR.encode("ascii"),
            value, len(value), 0, 0,
        )
        return rc == 0
    except Exception:  # pragma: no cover - non-macOS
        return False


def is_screenshot(path, name: str | None = None) -> bool:
    """Screenshot detection (v2 02 §5.2): xattr
    `com.apple.metadata:kMDItemIsScreenCapture` FIRST (DigUp Rules.swift:140-144,
    via stdlib ctypes), name prefixes as fallback (the v1 heuristic).

    `path` may be a Path or a path string (xattr probed only when the file
    exists) or a bare filename (prefix fallback only)."""
    from pathlib import Path as _Path

    if name is None:
        text = str(path)
        name = text.rsplit("/", 1)[-1]
    if isinstance(path, _Path) or "/" in str(path):
        try:
            import os as _os

            if _os.path.exists(str(path)) and _xattr_is_screenshot(path):
                return True
        except Exception:  # pragma: no cover - belt and braces
            pass
    return any(name.startswith(p) for p in SCREENSHOT_PREFIXES)


def folder_skip(name: str) -> str | None:
    """None = descend. 'developer folder' / '.noindex folder' /
    'app or bundle' otherwise (bundle skip: DigUp Crawler.swift:63-65 —
    directory packages are counted, never descended into)."""
    if name in SKIP_FOLDERS:
        return "developer folder"
    if name.endswith(".noindex"):
        return ".noindex folder"
    if "." in name and name.rsplit(".", 1)[-1].lower() in PACKAGE_DIR_EXTS:
        return "app or bundle"
    return None


def is_project_root(names: set[str]) -> bool:
    """True when a directory listing contains any project marker."""
    if any(marker in names for marker in PROJECT_MARKERS):
        return True
    return any(n.rsplit(".", 1)[-1] in PROJECT_BUNDLE_EXTS and "." in n for n in names)


def classify(name: str, ext: str, *, code: bool, head: bytes | None = None) -> Decision:
    """Decide kind / skip reason for one file.

    `head`: optional first bytes — required to sniff .ts/.mts/.m2ts files
    (MPEG-TS transport stream => video; else code-or-skip). Extends the
    MODULE_MAP signature compatibly (keyword-only, default None).
    """
    ext = ext.lower()

    if ext in TS_EXTS:
        if head is not None and _mpeg_ts_head(head):
            return Decision(kind="video", skip_reason=None)
        return Decision(kind="code" if code else None, skip_reason=None if code else "code or data file")

    if ext in DOC_EXTS:
        return Decision(kind="doc", skip_reason=None)
    if ext in PDF_EXTS:
        return Decision(kind="pdf", skip_reason=None)
    if ext in IMAGE_EXTS:
        return Decision(kind="image", skip_reason=None)
    if ext in AUDIO_EXTS:
        return Decision(kind="audio", skip_reason=None)
    if ext in VIDEO_EXTS:
        return Decision(kind="video", skip_reason=None)
    if ext in CODE_EXTS:
        return Decision(kind="code" if code else None, skip_reason=None if code else "code or data file")
    if name in CODE_FILE_NAMES or name.startswith("Dockerfile"):
        # code known by its name alone (DigUp codeFileNames + Dockerfile prefix)
        return Decision(kind="code" if code else None, skip_reason=None if code else "code or data file")
    if ext in ARCHIVE_EXTS:
        # honesty, not scope: archives are unsupported with a fix-it reason
        # instead of the vague "unknown type" (02 §5.5)
        return Decision(kind=None, skip_reason=ARCHIVE_REASON)
    return Decision(kind=None, skip_reason=None)


def _mpeg_ts_head(head: bytes) -> bool:
    from ..util import is_mpeg_ts

    return is_mpeg_ts(head)


def looks_machine_named(stem: str) -> bool:
    """Machine-made names like 'clip_000123' but not camera names like IMG_1234."""
    name = stem.lower()
    if name.startswith(CAMERA_PREFIXES):
        return False
    return bool(MACHINE_NAMED_1.match(name) or MACHINE_NAMED_2.match(name))


def dataset_folders(
    files: list[tuple[str, str, str]], roots: set[str], minimum: int = 300
) -> dict[str, int]:
    """[stretch] Folders with hundreds of same-kind machine-named media files:
    training data, not your stuff. DigUp datasetFolders port; roots you pick
    yourself are never datasets."""
    groups: dict[tuple[str, str], list[str]] = {}
    for folder, stem, kind in files:
        if kind not in ("image", "audio", "video") or folder in roots:
            continue
        groups.setdefault((folder, kind), []).append(stem)
    datasets: dict[str, int] = {}
    for (folder, _kind), stems in groups.items():
        if len(stems) < minimum:
            continue
        machine = sum(1 for s in stems if looks_machine_named(s))
        if machine / len(stems) >= 0.7:
            datasets[folder] = datasets.get(folder, 0) + len(stems)
    return datasets
