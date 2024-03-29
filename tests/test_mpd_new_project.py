# Copyright 2013-2023 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import pytest
import subprocess

from spack.main import SpackCommand, SpackCommandError

mpd = SpackCommand("mpd")


def test_new_project_paths(tmpdir):
    # Specify neither -T or -S
    top_level_a = tmpdir / "a"
    top_level_a.mkdir()
    result = subprocess.run(
        ["spack", "mpd", "new-project", "--name", "a"],
        capture_output=True,
        text=True,
        cwd=top_level_a,
    )
    assert f"build area: {top_level_a}/build" in result.stdout
    assert f"local area: {top_level_a}/local" in result.stdout
    assert f"sources area: {top_level_a}/srcs" in result.stdout
    mpd("rm", "a")

    # Specify only -T
    top_level_b = tmpdir / "b"
    top_level_b.mkdir()
    out = mpd("new-project", "--name", "b", "-T", str(top_level_b))
    assert f"build area: {top_level_b}/build" in out
    assert f"local area: {top_level_b}/local" in out
    assert f"sources area: {top_level_b}/srcs" in out
    mpd("rm", "b")

    # Specify only -S
    top_level_c = tmpdir / "c"
    srcs_c = tmpdir / "c_srcs"
    top_level_c.mkdir()
    srcs_c.mkdir()
    result = subprocess.run(
        ["spack", "mpd", "new-project", "--name", "c", "-S", str(srcs_c)],
        capture_output=True,
        text=True,
        cwd=top_level_c,
    )
    assert f"build area: {top_level_c}/build" in result.stdout
    assert f"local area: {top_level_c}/local" in result.stdout
    assert f"sources area: {srcs_c}" in result.stdout
    mpd("rm", "c")

    # Specify both -T and -S
    top_level_d = tmpdir / "d"
    srcs_d = tmpdir / "d_srcs"
    top_level_d.mkdir()
    srcs_d.mkdir()
    out = mpd("new-project", "--name", "d", "-T", str(top_level_d), "-S", str(srcs_d))
    assert f"build area: {top_level_d}/build" in out
    assert f"local area: {top_level_d}/local" in out
    assert f"sources area: {srcs_d}" in out
    mpd("rm", "d")
