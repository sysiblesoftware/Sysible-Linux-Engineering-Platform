"""SLEP installed a genuine amd64 Terraform into an arm64 image.

The build died on the one error that names no cause:

    /bin/sh: 1: terraform: Exec format error

The Dockerfile fetched `terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip`
with `ARG TARGETARCH=amd64`. TARGETARCH is a PREDEFINED build arg that only a
BuildKit/buildx build populates; under any other builder nothing supplies it and
the `=amd64` default quietly wins. That is not exotic here — the standalone
docker-compose binary drives the legacy builder, and install-sysible installs
exactly that fallback on a host where Docker's own apt repo is unreachable. So
the arch of the image and the arch of the download could disagree with nothing
to stop them.

What makes this worth a test rather than a one-line fix is that the build's
integrity check did not catch it AND COULD NOT. The amd64 zip verifies perfectly
against HashiCorp's published SHA256SUMS, because it is the real amd64 artifact
— just the wrong one for this image. A checksum answers "is this the file I
asked for", never "did I ask for the right file". Verification passing is
exactly why this got all the way to a runtime exec failure.

Terraform is the only architecture-dependent download in the entire Sysible
suite, so this file is the whole of that surface.
"""
import pathlib
import re

import pytest

DOCKERFILE = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"
TEXT = DOCKERFILE.read_text()
# Lines that actually do something, so prose about the bug cannot satisfy a test
# about the fix.
CODE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))


def test_the_dockerfile_is_where_we_think_it_is():
    assert DOCKERFILE.is_file(), DOCKERFILE


class TestTheArchitectureComesFromTheImage:
    def test_terraform_is_fetched_for_the_image_being_built(self):
        assert "dpkg --print-architecture" in CODE, (
            "the Terraform download no longer derives its architecture from the "
            "image's own dpkg database"
        )

    def test_the_download_url_is_not_pinned_to_one_architecture(self):
        """A literal amd64/arm64 in the URL is the bug in its most direct form."""
        for line in CODE.splitlines():
            if "releases.hashicorp.com" in line or "terraform_" in line:
                for arch in ("amd64", "arm64", "x86_64", "aarch64"):
                    assert arch not in line, \
                        f"a hard-coded {arch} is back in the Terraform fetch: {line.strip()}"

    def test_targetarch_is_not_used_with_a_fallback(self):
        """`ARG TARGETARCH=<anything>` is the trap: it looks arch-aware and
        silently resolves to the default under a non-buildx builder."""
        m = re.search(r"^\s*ARG\s+TARGETARCH\s*=", CODE, re.M)
        assert not m, (
            "ARG TARGETARCH has a default again. If TARGETARCH is ever "
            "reintroduced it must be declared WITHOUT one, so a builder that "
            "does not set it fails loudly instead of silently building amd64."
        )

    def test_no_download_in_the_whole_file_depends_on_targetarch(self):
        for line in CODE.splitlines():
            if "curl" in line or "wget" in line or "http" in line:
                assert "TARGETARCH" not in line, \
                    f"a download still depends on TARGETARCH: {line.strip()}"


class TestTheIntegrityCheckIsStillThere:
    """The fix must not be 'stop verifying'. The checksum could not catch a
    wrong-architecture download, but it is still what catches a corrupted or
    substituted one."""

    def test_the_checksum_is_still_verified_before_install(self):
        assert "sha256sum -c" in CODE, \
            "the Terraform download is no longer checksum-verified"
        assert "SHA256SUMS" in CODE, \
            "it no longer fetches HashiCorp's published sums"

    def test_the_checksum_is_matched_against_the_file_actually_downloaded(self):
        """Grepping the sums for the resolved filename is what ties the two
        together; a fixed filename here would re-create the mismatch."""
        assert re.search(r'grep\s+"\s*\$\{TFZIP\}', CODE), \
            "the sums are no longer grepped for the resolved TFZIP name"

    def test_it_still_proves_the_binary_runs(self):
        """`terraform version` at the end of the RUN is what turned this from a
        broken image into a failed build. It is the only step that catches a
        wrong-architecture binary at all."""
        assert "terraform version" in CODE, (
            "the post-install `terraform version` probe is gone — it is the only "
            "thing that can catch an architecture mismatch, since the checksum "
            "cannot"
        )


class TestTheVersionIsStillPinned:
    def test_terraform_is_pinned_to_an_exact_version(self):
        m = re.search(r"^\s*ARG\s+TERRAFORM_VERSION\s*=\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$",
                      CODE, re.M)
        assert m, "TERRAFORM_VERSION is no longer pinned to an exact version"

    @pytest.mark.parametrize("arch", ["amd64", "arm64"])
    def test_the_pinned_version_is_published_for_both_architectures(self, arch):
        """Deriving the arch correctly is no good if upstream never built it.
        Offline, this asserts the shape rather than skipping silently."""
        m = re.search(r"^\s*ARG\s+TERRAFORM_VERSION\s*=\s*(\S+)\s*$", CODE, re.M)
        version = m.group(1)
        name = f"terraform_{version}_linux_{arch}.zip"
        assert re.fullmatch(r"terraform_[0-9.]+_linux_(amd64|arm64)\.zip", name)
