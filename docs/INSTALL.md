# Install Goldmine

The standalone package targets **macOS 14 or newer on Apple Silicon**. It contains
Python 3.12, NumPy, the exact MCP SDK and dependencies, the pinned llama-server,
and the OCR and media helpers. You need no Python, Homebrew, compiler or virtual
environment. Core setup prepares the SHA256-pinned 310 MB embedding model and
runs an actual embedding before reporting success.

**Availability:** this is a local candidate installer. A public standalone release
has not been published. The release workflow prepares a **draft**; the owner must
review and publish it before the public download command works. Package version
0.2.0 remains development; installation evidence does not certify search quality.

## Install a prepared local package

Put `install.sh`, `goldmine-macos-arm64.tar.gz` and its `SHA256SUMS` in one folder.
Run the installer; it discovers the archive and verifies its checksum automatically:

```sh
sh install.sh
```

For a fully offline install, add `--model /absolute/path/embeddinggemma-2-Q8_0.gguf`.
The installer verifies and copies this file into the model directory; it does not
depend on the original file remaining there. Add `--skip-model` instead for an
explicitly keyword-only installation, with no model download. A missing or
incorrect archive checksum refuses installation before extraction.
For files in separate locations, supply `--archive /absolute/path/to/archive.tar.gz
--sha256 <the-matching-hash-from-SHA256SUMS>` explicitly.

Setup prints a command you can run **in the current terminal**:

```sh
"$HOME/.local/bin/goldmine" init
```

Choose your folders in the guided setup, then use `goldmine search 'your query'`.
Only chosen folders are indexed. Installation itself never indexes files or
starts background services. The installer adds an idempotent PATH block to the
current shell's `.zshrc` (respecting `ZDOTDIR`) or `.bash_profile`; a preexisting
file is backed up as `*.before-goldmine`. New terminals can use `goldmine` directly.
Use `--no-path` to leave shell profiles untouched and keep using the absolute path.

## Public install after the first release is published

This command is reserved for the first **published** standalone release:

```sh
curl -fsSL https://github.com/sixtycat2000-ctrl/goldmine/releases/latest/download/install.sh | sh
```

The installer downloads only from the repository's HTTPS release assets, verifies
the archive against that release's `SHA256SUMS`, and verifies the complete payload
again before activation. Checksums detect corruption; they are not publisher
signatures or Apple notarization. To select a release, use e.g.
`GOLDMINE_VERSION=v0.2.0 sh install.sh` with an actually published matching tag.

## What works immediately

Text, Markdown and source-code keyword and semantic search, Vision OCR, and the
four read-only MCP tools are included. Run `goldmine doctor` to see capabilities.
The media helper is included; visual search additionally needs the explicitly
installed mmproj model. PDF text extraction needs Poppler; audio/video preparation
needs ffmpeg, and speech transcription needs whisper-cli and pinned speech/VAD
models. These optional tools are not silently installed. `goldmine init --media`
offers the speech models; the corresponding tools must also be available.

For MCP, use the installed absolute executable in your client's stdio configuration,
with arguments `mcp --db /absolute/path/to/library.sqlite`. No extra pip install is
needed. See [MCP](MCP.md) for the full client configuration and read-only contract.

## Upgrade, uninstall and locations

Run the same installer with a newer archive and its matching checksum. Each
payload is immutable and identified by its manifest SHA; activation changes one
`current` symlink only after validation, model preparation and engine probing.
A failed preparation keeps the previous activation. Old payloads remain available
for processes already using them. Concurrent installers refuse to overlap.

```sh
sh install.sh --uninstall
```

Uninstall removes managed commands, payloads and the exact owned PATH block.
**Indexes, models and configuration are retained**, along with shell-profile
backups. Foreign or modified launchers are refused instead of overwritten.

| Item | Default location |
|---|---|
| Versioned application/runtime | `~/.local/share/goldmine/releases/` |
| Active version | `~/.local/share/goldmine/current` |
| Command | `~/.local/bin/goldmine` |
| Indexes, models and native links | `~/Library/Application Support/goldmine/` |
| Configuration | `~/.config/goldmine/config.toml` |

`GOLDMINE_INSTALL_DIR` and `GOLDMINE_BIN_DIR` override application/command locations.
Existing `GOLDMINE_HOME`, `GOLDMINE_CONFIG` and configured core home are respected.
Use the same values for subsequent upgrades and uninstall. Existing explicit
backend/model settings retain their normal priority. If a previous manual install
owns a conflicting command or native binary, move that file aside yourself before
using this installer; it will not replace an unowned file.

Developers can still [install from source](../README.md#development-install) or
[build the actual standalone package](BUILDING.md#standalone-application-package).
