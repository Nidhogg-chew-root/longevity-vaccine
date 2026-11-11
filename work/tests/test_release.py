"""The release packager (work\\scripts\\release.py).

Two properties carry the weight:

  1. **Reproducibility.** The same inputs must produce a byte-identical zip, or the manifest's
     sha256 changes on every rebuild and nobody can tell a real release from a repack.
  2. **Refusal.** The packager must refuse to build something unshippable -- a failing L1 lock, a
     version that disagrees with version.py, or a bundled Python that still carries the
     developer-only packages that make up most of the 428 MB payload. All three are invisible in
     a working tree and would only be discovered by a player.
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from apply_patches import _yaml_load  # the production loader (libyaml)

_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parent.parent
_SCRIPTS = _REPO / "work" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import release as rel  # noqa: E402
import version as ver  # noqa: E402

_PY = _REPO / "tools" / "python" / "python.exe"
pytestmark = pytest.mark.skipif(not _PY.exists(), reason="bundled interpreter not present")


def _run(*args, expect_fail: bool = False) -> subprocess.CompletedProcess:
    # NEVER let a test write to release.py's DEFAULT output directory. One that did left stale
    # release output in the repo, built before the runtime import-closure fix -- so it held an
    # interpreter that could not resolve a hostname -- sitting in the most obvious place anyone
    # would go looking for something to hand a tester.
    assert "--out" in args, "release.py tests must pass --out; the default writes into dist/"
    p = subprocess.run([str(_PY), str(_SCRIPTS / "release.py"), *args],
                       capture_output=True, text=True, timeout=600)
    if expect_fail:
        assert p.returncode != 0, f"expected failure, got 0:\n{p.stdout}{p.stderr}"
    else:
        assert p.returncode == 0, f"expected success:\n{p.stdout}{p.stderr}"
    return p


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """One dev build (no Python runtime) shared by the read-only tests."""
    out = tmp_path_factory.mktemp("rel")
    _run("--skip-gate", "--no-python", "--modules", "base", "--out", str(out))
    return out / ver.VERSION


# ---------------------------------------------------------------- reproducibility

def test_the_zip_is_byte_for_byte_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _run("--skip-gate", "--no-python", "--modules", "base", "--out", str(a))
    _run("--skip-gate", "--no-python", "--modules", "base", "--out", str(b))
    za = (a / ver.VERSION / rel.archive_name("base", ver.VERSION)).read_bytes()
    zb = (b / ver.VERSION / rel.archive_name("base", ver.VERSION)).read_bytes()
    assert za == zb, "two builds of identical inputs differ -- the zip is not deterministic"


def test_every_entry_has_the_fixed_timestamp(built):
    z = zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION))
    for info in z.infolist():
        assert info.date_time == rel._ZIP_EPOCH, f"{info.filename} carries a host timestamp"


def test_the_player_facing_archive_is_named_for_the_product():
    """Pinned as a literal on purpose.

    Every other test derives the file name from archive_name(), which is what keeps them from
    drifting -- but it also means a silent rename would leave the whole suite green while the
    manifest pointed at a file nobody recognises. This is the one place the name is a decision.
    """
    assert rel.archive_name("base", "1.2.3") == "LongevityVaccine_1.2.3.zip"
    assert rel.archive_name("movies-hd", "1.2.3") == "LV-movies-hd-1.2.3.zip"
    assert rel.archive_name("portraits-hd", "1.2.3") == "LV-portraits-hd-1.2.3.zip"


def test_entries_are_sorted(built):
    names = [i.filename for i in zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION)).infolist()]
    assert names == sorted(names), "unsorted entries make the archive order filesystem-dependent"


# ---------------------------------------------------------------- refusal

def test_the_archive_carries_both_interpreters_when_staged(tmp_path):
    """tools/python (3.12) AND tools/python38 (Windows 7 / 8): a release without the second leaves every
    Windows 7 player with an installer that cannot start (audit 2026-10-31). Skipped until both are staged;
    runtime_root() refuses when only one is."""
    if not (rel.RUNTIME_DIR / "python.exe").is_file():
        pytest.skip("3.12 runtime not staged")
    if not (rel.RUNTIME38_DIR / "python.exe").is_file():
        pytest.skip("runtime38 not staged (fetch_python38.py + build_runtime.py)")
    names = {dst for _src, dst in rel.collect("base", include_python=True)}
    for required in ("tools/python/python.exe", "tools/python38/python.exe", "tools/python38/python38.zip",
                     "tools/python38/python38._pth", "tools/python38/ucrtbase.dll", "tools/python38/LICENSE.txt",
                     "tools/python38/PROVENANCE.txt", "tools/python38/Lib/site-packages/pefile.py",
                     "tools/python38/Lib/site-packages/yaml/parser.py"):
        assert required in names, required
    assert not [n for n in names if n.startswith("tools/python38/") and n.endswith(".pyd")
                and "/Lib/site-packages/" in n], "a cp312 extension rode into the 3.8 runtime"
    assert not [n for n in names if n.endswith((".staged-at", ".verified-at"))]


def test_runtime_root_refuses_without_the_38_runtime(tmp_path, monkeypatch):
    have = tmp_path / "rt"
    have.mkdir()
    (have / "python.exe").write_bytes(b"MZ")
    monkeypatch.setattr(rel, "RUNTIMES", ((have, "tools/python"), (tmp_path / "rt38", "tools/python38")))
    with pytest.raises(FileNotFoundError) as e:
        rel.runtime_root()
    assert "tools/python38" in str(e.value) and "fetch_python38.py" in str(e.value)


def test_runtime_stale_names_the_runtime_with_the_problem(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / ".staged-at").write_text("9999999999", encoding="ascii")
        (d / ".verified-at").write_text("9999999999", encoding="ascii")
    (b / ".verified-at").unlink()
    monkeypatch.setattr(rel, "RUNTIMES", ((a, "tools/python"), (b, "tools/python38")))
    assert rel.runtime_stale() == "tools/python38: the staged runtime was never verified (run build_runtime.py --verify)"
    (b / ".verified-at").write_text("9999999999", encoding="ascii")
    assert rel.runtime_stale() == ""


def test_the_staged_runtime_carries_no_developer_packages():
    """The trim happened, so this asserts the property rather than the defect.

    It used to assert that the UNTRIMMED tools/python was detected, with a note saying to rewrite
    it once the trim landed -- which is exactly what fired. The check itself also moved: it looks
    at the STAGED runtime now, not the developer's interpreter, because what matters is what ends
    up in the archive, not what the developer happens to have installed.
    """
    if not (rel.RUNTIME_DIR / "python.exe").is_file():
        pytest.skip("no staged runtime; run build_runtime.py first")
    assert rel.check_dev_only_packages() == []


def test_the_dev_package_detector_still_catches_one(tmp_path):
    """Assert the detector WORKS, not merely that it returns nothing on a clean tree.

    A check that is only ever run against a tree with nothing to find is indistinguishable from a
    check that is broken, so plant an offender and require it to be found -- and require a
    SHIPPED dependency sitting beside it not to be.
    """
    fake = tmp_path / "runtime"
    for name in ("frida", "numpy", "yaml"):
        (fake / "Lib" / "site-packages" / name).mkdir(parents=True)
    (fake / "Lib" / "site-packages" / "pefile.py").write_text("", encoding="ascii")
    found = rel.check_dev_only_packages(fake)
    assert "frida" in found and "numpy" in found
    assert "yaml" not in found and "pefile.py" not in found, \
        "pyyaml and pefile are SHIPPED dependencies, not developer ones"


def test_allow_untrimmed_is_an_explicit_opt_in(tmp_path):
    """The escape hatch must exist (the trim is a later phase) but must warn loudly."""
    p = _run("--skip-gate", "--no-python", "--allow-untrimmed", "--modules", "base",
             "--out", str(tmp_path))
    # --no-python means the offenders are not packaged, so no warning is expected here; the
    # point is only that the flag is accepted and does not refuse.
    assert "REFUSING" not in p.stdout + p.stderr


def test_refuses_a_version_that_disagrees_with_version_py(tmp_path):
    p = _run("--skip-gate", "--no-python", "--version", "9.9.9", "--out", str(tmp_path),
             expect_fail=True)
    out = p.stdout + p.stderr
    assert "disagrees with" in out
    assert "version.py" in out


def test_refuses_a_malformed_version(tmp_path):
    p = _run("--skip-gate", "--no-python", "--version", "1.0", "--out", str(tmp_path),
             expect_fail=True)
    assert "MAJOR.MINOR.PATCH" in p.stdout + p.stderr


def test_refuses_an_unknown_module(tmp_path):
    p = _run("--skip-gate", "--no-python", "--modules", "nope", "--out", str(tmp_path),
             expect_fail=True)
    assert "unknown module" in p.stdout + p.stderr


def test_base_is_required(tmp_path):
    """movies-hd alone is not a release."""
    p = _run("--skip-gate", "--no-python", "--modules", "portraits-hd", "--out", str(tmp_path),
             expect_fail=True)
    assert "base module is required" in p.stdout + p.stderr


# ---------------------------------------------------------------- archive shape

def test_the_archive_has_the_one_payload_layout(built):
    """FLAT, and with no `work` folder anywhere.

    `work` is a checkout's directory -- it also holds tests, screenshots and notes -- and means
    nothing to a player. The archive and a resident updater share this one layout; the checkout
    is merely where the files come from, and MODULE_SPEC is the only place the two are related.
    """
    names = set(zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION)).namelist())
    for required in ("INSTALL.bat", "scripts/run-install.bat", "scripts/install.py",
                     "scripts/apply_patches.py", "scripts/config.py", "scripts/version.py",
                     "scripts/installer_exit.py", "patches/patches.yaml",
                     "src/uimod/build/Release/uimod.dll", "art/longevity_sm.pcx", "CHANGELOG.txt"):
        assert required in names, f"missing {required}"
    assert "CHANGELOG.md" not in names, "the changelog ships as .txt, which Notepad opens"
    # CRLF on every checkout (.gitattributes): Notepad before Windows 10 1809 shows LF text as one line.
    attrs = (_REPO / ".gitattributes").read_text(encoding="utf-8").split("\n")
    assert "work/CHANGELOG.md text eol=crlf" in [a.strip() for a in attrs]
    assert not [n for n in names if n.split("/")[0] == "work"], \
        "the checkout's `work` folder reached the archive"
    assert sum(1 for n in names if n.startswith("src/uimod/third_party/ffmpeg/bin/")) == 5


def test_root_launcher_is_present_crlf_and_guards_itself(built):
    z = zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION))
    data = z.read("INSTALL.bat")
    assert data.count(b"\r\n") == data.count(b"\n"), "INSTALL.bat must be CRLF for cmd"
    text = data.decode("ascii")
    # A shim that silently does nothing on an incomplete extract would defeat the installer's
    # exit guarantee before Python is ever reached.
    assert "if not exist" in text and "pause" in text
    assert "INSTALLATION FAILED" in text


def test_the_linux_and_macos_launcher_ships_executable_and_lf(built):
    """INSTALL.sh is the Wine entry point: at the archive root beside INSTALL.bat, LF (bash reads a CRLF line's
    \\r as part of the command), and executable once extracted -- unzip and macOS Archive Utility take the mode
    from a Unix-made entry only."""
    z = zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION))
    info = z.getinfo("INSTALL.sh")
    data = z.read(info)
    assert data.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in data
    assert info.create_system == 3 and (info.external_attr >> 16) == 0o100755
    assert "call" in data.decode("utf-8") and "INSTALL.bat" in data.decode("utf-8")
    others = [i for i in z.infolist() if i.filename != "INSTALL.sh"]
    assert all(i.external_attr >> 16 == 0o644 and i.create_system == 0 for i in others)
    attrs = [a.strip() for a in (_REPO / ".gitattributes").read_text(encoding="utf-8").split("\n")]
    assert "*.sh text eol=lf" in attrs


def test_no_pycache_or_dev_tooling_in_the_archive(built):
    names = zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION)).namelist()
    assert not [n for n in names if "__pycache__" in n]
    assert not [n for n in names if "/tests/" in n or n.startswith("tests/")]


def test_the_signer_is_never_shipped(built):
    """The private-key machinery must not reach a player's disk. The previous check only asserted
    that the VERIFIER contains no `def sign` -- adding sign_manifest.py to the base module would
    have shipped the signer with every test still green."""
    names = zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION)).namelist()
    for forbidden in ("sign_manifest.py", "release.py", "hash_hd_pack.py"):
        assert not [n for n in names if n.endswith(forbidden)], f"{forbidden} is in the archive"
    # ...and it is absent from the spec itself, so a future edit to MODULE_SPEC trips this too.
    spec_files = [dst for _src, dst in rel.MODULE_SPEC["base"]["files"]]
    assert not [f for f in spec_files if "sign_manifest" in f]


def test_the_runtime_modules_the_installer_needs_are_shipped(built):
    """The counterpart: the verifier and the update machinery MUST be present, or an installed
    copy cannot check a signature at all."""
    names = set(zipfile.ZipFile(built / rel.archive_name("base", ver.VERSION)).namelist())
    for required in ("ed25519_verify.py", "manifest.py", "fetch.py", "preflight.py",
                     "transact.py", "receipt.py", "bootstrap.py",
                     "patch_helptext.py",      # the Datalinks text patch install.py applies on every run
                     "roots.pem"):             # the public roots fetch.py adds to the Windows store (Windows 7)
        assert f"scripts/{required}" in names, f"{required} is missing from the base module"


# ---------------------------------------------------------------- manifest

@pytest.fixture(scope="module")
def manifest(built) -> dict:
    return json.loads((built / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_records_the_kill_switch_fields(manifest):
    """These must exist in 1.0.0 -- a field absent from the first release cannot help the
    installs that later need it."""
    for k in ("protocol", "manual_install_required", "manual_install_reason",
              "unsupported_versions", "unsupported_targets"):
        assert k in manifest, f"missing {k}"
    assert manifest["manual_install_required"] is False
    assert manifest["unsupported_versions"] == []


def test_manifest_hash_and_size_match_the_artifact(built, manifest):
    z = built / rel.archive_name("base", ver.VERSION)
    assert manifest["modules"]["base"]["sha256"] == rel.sha256_file(z)
    assert manifest["modules"]["base"]["size"] == z.stat().st_size


def test_manifest_targets_come_from_patches_yaml(manifest):
    """So the shipped base/patched MD5s cannot drift from what the patcher applies."""
    spec = _yaml_load((_REPO / "work" / "patches" / "patches.yaml").read_text(encoding="utf-8"))
    t = spec["targets"]["pracx"]
    m = manifest["targets"]["pracx"]
    assert m["base_md5"] == t["base_md5"]
    assert m["patched_md5"] == t["patched_md5"] == spec["target"]["patched_md5"]


def test_manifest_excludes_unmeasured_and_dev_only_targets(manifest):
    """`vanilla` was dropped from support (2026-09-21); publishing it would advertise a configuration
    we cannot patch. `pracx_thinker` ships since 2026-10-28 (the folder-detected overlay of pracx)."""
    assert set(manifest["targets"]) == {"pracx", "pracx_thinker"}


def test_manifest_url_points_at_the_release_asset(manifest):
    url = manifest["modules"]["base"]["url"]
    assert url.endswith(rel.archive_name("base", ver.VERSION))
    assert ver.VERSION in url


def test_manifest_version_matches_version_py(manifest):
    assert manifest["latest"] == ver.VERSION


# ---------------------------------------------------------------- independent pack versions

def _fake_pack(out: Path, name: str, mver: str) -> Path:
    """A stand-in pack archive where release.py would have put a real one. Building the real
    movies-hd is 1.3 GB at DEFLATE 9; the reuse path only needs a file with the right name."""
    p = out / "modules" / rel.archive_name(name, mver)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"{name}/x.bin", b"x")
        if name == "portraits-hd":
            # a reused portrait archive must carry the artist's credit and confirmed licence word for word
            z.writestr("portraits/hd/PROVENANCE.txt",
                       (rel._REPO / "work" / "art" / "portraits-hd-PROVENANCE.txt").read_bytes())
    return p


def test_pack_archives_are_named_for_the_pack_version_not_the_product():
    assert ver.module_version("base") == ver.VERSION
    for name, mver in ver.MODULE_VERSIONS.items():
        assert rel.archive_name(name, mver) == f"LV-{name}-{mver}.zip"
    with pytest.raises(KeyError):
        ver.module_version("nope")


def test_a_product_build_carries_existing_packs_unchanged(tmp_path):
    """A bare run is a product release: the manifest lists every pack that has an archive, at
    the pack's own version, with the pack's own URL -- and never re-packs it."""
    mver = ver.MODULE_VERSIONS["movies-hd"]
    fake = _fake_pack(tmp_path, "movies-hd", mver)
    before = fake.read_bytes()
    p = _run("--skip-gate", "--no-python", "--out", str(tmp_path))
    m = json.loads((tmp_path / ver.VERSION / "manifest.json").read_text(encoding="utf-8"))
    assert set(m["modules"]) == {"base", "movies-hd"}, m["modules"].keys()
    assert m["modules"]["movies-hd"]["version"] == mver
    assert m["modules"]["movies-hd"]["sha256"] == rel.sha256_file(fake)
    assert m["modules"]["movies-hd"]["url"].endswith(
        f"/movies-hd-v{mver}/{rel.archive_name('movies-hd', mver)}")
    assert m["modules"]["base"]["url"].endswith(
        f"/v{ver.VERSION}/{rel.archive_name('base', ver.VERSION)}")
    assert fake.read_bytes() == before, "an unnamed pack must be reused, not rebuilt"
    assert "reused" in p.stdout


def test_a_pack_only_build_reuses_the_product_archive(tmp_path):
    """`--modules <pack>` is a pack release: base is not rebuilt, but it IS in the manifest."""
    _run("--skip-gate", "--no-python", "--out", str(tmp_path))
    base = tmp_path / ver.VERSION / rel.archive_name("base", ver.VERSION)
    stamp = base.stat().st_mtime_ns
    mver = ver.MODULE_VERSIONS["portraits-hd"]
    fake = _fake_pack(tmp_path, "portraits-hd", mver)
    # portraits-hd has no source on this machine; naming it with an existing archive reuses it.
    p = _run("--skip-gate", "--no-python", "--modules", "portraits-hd", "--out", str(tmp_path))
    m = json.loads((tmp_path / ver.VERSION / "manifest.json").read_text(encoding="utf-8"))
    assert set(m["modules"]) == {"base", "portraits-hd"}
    assert m["modules"]["base"]["version"] == ver.VERSION
    assert m["modules"]["portraits-hd"]["sha256"] == rel.sha256_file(fake)
    assert base.stat().st_mtime_ns == stamp, "base must not be rebuilt on a pack release"
    assert "base" in p.stdout and "reused" in p.stdout


def test_a_pack_release_with_no_product_archive_is_refused(tmp_path):
    _fake_pack(tmp_path, "movies-hd", ver.MODULE_VERSIONS["movies-hd"])
    p = _run("--skip-gate", "--no-python", "--modules", "movies-hd", "--out", str(tmp_path),
             expect_fail=True)
    assert "base module is required" in p.stdout + p.stderr


def test_manifest_min_upgradable_from_does_not_strand_existing_installs(manifest):
    """It used to equal `latest`, which told every existing install it was too old to update."""
    assert manifest["min_upgradable_from"] == ver.MIN_UPGRADABLE_FROM
    assert ver.at_least(ver.VERSION, ver.MIN_UPGRADABLE_FROM)


def test_a_no_python_base_is_never_reused_into_a_real_release(tmp_path):
    """A dev build's archive is not installable; a later pack-only run must not sign a manifest
    pointing at it."""
    _run("--skip-gate", "--no-python", "--out", str(tmp_path))
    base = tmp_path / ver.VERSION / rel.archive_name("base", ver.VERSION)
    assert (base.parent / (base.name + ".no-python")).is_file()
    _fake_pack(tmp_path, "portraits-hd", ver.MODULE_VERSIONS["portraits-hd"])
    p = _run("--skip-gate", "--modules", "portraits-hd", "--out", str(tmp_path),
             expect_fail=True)
    assert "REFUSING to reuse" in p.stdout + p.stderr
    # A second --no-python run may still reuse it (same kind of build).
    _run("--skip-gate", "--no-python", "--modules", "portraits-hd", "--out", str(tmp_path))


def test_module_versions_and_module_spec_name_the_same_packs():
    """A pack missing from either table would be a KeyError at build time or a pack the
    manifest could never carry."""
    assert set(ver.MODULE_VERSIONS) == set(rel.MODULE_SPEC) - {"base"}
    assert "base" not in ver.MODULE_VERSIONS


def test_build_manifest_uses_each_modules_own_version():
    arts = {"base": {"version": "1.0.0", "sha256": "a" * 64, "size": 1},
            "movies-hd": {"version": "2.5.0", "sha256": "b" * 64, "size": 2}}
    m = rel.build_manifest("1.0.0", arts, "https://h/v{version}", "https://h/{module}-v{version}")
    assert m["latest"] == "1.0.0"
    assert m["modules"]["movies-hd"]["version"] == "2.5.0"
    assert m["modules"]["movies-hd"]["url"] == "https://h/movies-hd-v2.5.0/LV-movies-hd-2.5.0.zip"
    assert m["modules"]["base"]["url"] == "https://h/v1.0.0/LongevityVaccine_1.0.0.zip"
    assert m["modules"]["base"]["required"] is True
    assert m["modules"]["movies-hd"]["required"] is False


def test_manifest_is_not_claimed_to_be_signed(built):
    """An unsigned build must not leave anything that looks like a signature."""
    assert not (built / "manifest.json.sig").exists()


# ---------------------------------------------------------------- signing

import manifest as mfmod        # noqa: E402
import sign_manifest as sm      # noqa: E402


def _client_trusting(tmp_path, pubkey_hex: str) -> dict:
    """An environment in which release.py believes shipped clients trust `pubkey_hex`.

    A real release key is compiled in now, so a test cannot sign with a throwaway key without
    release.py correctly refusing -- the refusal IS the feature. Every signing test therefore
    states which key the imagined client trusts, rather than relying on the build default.
    """
    import os
    shim = tmp_path / f"shim-{abs(hash(pubkey_hex)) % 10 ** 8}"
    shim.mkdir(exist_ok=True)
    (shim / "usercustomize.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{_SCRIPTS}')\n"
        "import manifest\n"
        f"manifest.PUBLIC_KEY_HEX = '{pubkey_hex}'\n", encoding="ascii")
    return dict(os.environ, PYTHONPATH=str(shim))


def _run_env(env, *args, expect_fail: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run([str(_PY), str(_SCRIPTS / "release.py"), *args],
                       capture_output=True, text=True, timeout=600, env=env)
    out = p.stdout + p.stderr
    if expect_fail:
        assert p.returncode != 0, f"expected failure, got 0:\n{out}"
    else:
        assert p.returncode == 0, f"expected success:\n{out}"
    return p


def test_the_release_key_is_the_one_clients_trust():
    """The compiled-in anchor must be exactly the public half of the release signing key.

    Pinned as a literal: a silent edit to PUBLIC_KEY_HEX makes every shipped client reject every
    future manifest, and nothing else in the suite would notice.
    """
    assert mfmod.PUBLIC_KEY_HEX == \
        "f429f9684395252e268fc04ca5f2a7e016558cca78ec9d7acaad9adf7205ca9b"


def test_signing_produces_a_verifiable_pair(tmp_path):
    key = tmp_path / "k.key"
    sm.keygen(key)
    pk = sm.public_key(sm.load_seed(key)).hex()
    out = tmp_path / "out"
    _run_env(_client_trusting(tmp_path, pk),
             "--skip-gate", "--no-python", "--modules", "base", "--out", str(out),
             "--sign-key", str(key))
    m = out / ver.VERSION / "manifest.json"
    assert (out / ver.VERSION / "manifest.json.sig").is_file()
    doc = mfmod.load_verified_files(m, public_key_hex=pk)
    assert doc["latest"] == ver.VERSION


def test_signing_with_the_wrong_key_is_refused(tmp_path, monkeypatch):
    """A manifest signed by a key shipped clients do not trust is rejected by EVERY install --
    a release that looks fine on the build machine and is inert in the field."""
    key = tmp_path / "k.key"
    sm.keygen(key)
    wrong = sm.public_key(bytes(range(1, 33))).hex()

    # Simulate a shipped client that trusts a different key.
    p = _run_env(_client_trusting(tmp_path, wrong), "--skip-gate", "--no-python",
                 "--modules", "base", "--out", str(tmp_path / "o"), "--sign-key", str(key),
                 expect_fail=True)
    out = p.stdout + p.stderr
    assert "REFUSING to sign" in out
    assert "Nothing would accept this manifest" in out


def test_an_empty_public_key_warns_loudly(tmp_path):
    """A build with no anchor compiled in trusts NOTHING; signing against it must say so.

    That was the shipped default until the release key was generated. The branch is still live
    for anyone building from a fresh checkout, so it is exercised through a client shim rather
    than deleted.
    """
    key = tmp_path / "k.key"
    sm.keygen(key)
    p = _run_env(_client_trusting(tmp_path, ""),
                 "--skip-gate", "--no-python", "--modules", "base",
                 "--out", str(tmp_path / "o"), "--sign-key", str(key))
    assert "PUBLIC_KEY_HEX is empty" in p.stdout


# ---------------------------------------------------------------- HD pack sidecar

import hash_hd_pack as hhp  # noqa: E402


def test_pipeline_ini_overrides_are_resolved_per_clip():
    """build_hd_pack.py resolves [clip:<name>] then [pipeline]; the sidecar must agree, or the
    recorded provenance is wrong for exactly the clips that were treated specially."""
    defaults, overrides = hhp.read_pipeline()
    assert defaults.get("model") == "realesrgan-x4plus"
    assert len(overrides) == 11, "pipeline.ini documents 11 per-clip model overrides"
    for stem in overrides:
        assert hhp.settings_for(stem, defaults, overrides)["model"] == "realesr-animevideov3"
    assert hhp.settings_for("not-overridden", defaults, overrides)["model"] == defaults["model"]


def test_sidecar_round_trips_and_is_incremental(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.mp4").write_bytes(b"aaa")
    (pack / "b.mp4").write_bytes(b"bbbb")

    assert hhp.build(pack) == 0
    doc = json.loads((pack / hhp.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert doc["clip_count"] == 2
    assert doc["total_size"] == 7
    assert {c["name"] for c in doc["clips"]} == {"a.mp4", "b.mp4"}

    assert hhp.build(pack, verify=True) == 0          # unchanged pack verifies

    (pack / "a.mp4").write_bytes(b"CHANGED")           # tamper
    assert hhp.build(pack, verify=True) == 1, "a changed clip must fail verification"


def test_sidecar_verify_reports_a_missing_clip(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.mp4").write_bytes(b"aaa")
    (pack / "b.mp4").write_bytes(b"bbb")
    hhp.build(pack)
    (pack / "b.mp4").unlink()
    assert hhp.build(pack, verify=True) == 1, "a clip that vanished must fail verification"


def test_sidecar_refuses_an_empty_or_missing_pack(tmp_path):
    assert hhp.build(tmp_path / "nope") == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert hhp.build(empty) == 2


def test_the_real_pack_sidecar_matches_pipeline_ini():
    """Cross-check against the built pack: 61 clips, 11 of them on the video-tuned model."""
    sidecar = _REPO / "dist" / "movies-hd" / hhp.SIDECAR_NAME
    if not sidecar.is_file():
        pytest.skip("HD pack sidecar not generated on this machine")
    doc = json.loads(sidecar.read_text(encoding="utf-8"))
    assert doc["clip_count"] == 61
    anime = [c for c in doc["clips"] if c["model"] == "realesr-animevideov3"]
    assert len(anime) == 11
    assert all(len(c["sha256"]) == 64 for c in doc["clips"])
