"""Build the MicMic disk image with dmgbuild, mounted somewhere private while it is filled.

    uvx --from dmgbuild==1.6.7 python native/dmg/make_dmg.py <MicMic.app> <out.dmg>

dmgbuild attaches its scratch image at /Volumes/<volume name>. On a Mac that has run
MicMic from a downloaded disk image, macOS refuses writes to /Volumes/MicMic/MicMic.app
("Operation not permitted", 2026-09-25, even after a restart), while the same copy to
any other mount point works. So the one attach dmgbuild makes gets -mountrandom, which
puts the volume under a fresh private folder. Nothing else about dmgbuild changes, and
the volume is still named MicMic when a user opens the download.
"""
import os
import sys
import tempfile

import dmgbuild
from dmgbuild import core

HERE = os.path.dirname(os.path.abspath(__file__))
_hdiutil = core.hdiutil


def _private_attach(cmd, *args, **kwargs):
    if cmd == "attach":
        args = ("-mountrandom", tempfile.mkdtemp(prefix="micmic-dmg-")) + args
    return _hdiutil(cmd, *args, **kwargs)


class _Alias(core.Alias):
    """The window background is saved as an alias, and an alias remembers where its
    volume was mounted. Record where a user's Mac mounts it, not the private folder."""

    @classmethod
    def for_file(cls, path):
        alias = super().for_file(path)
        alias.volume.posix_path = "/Volumes/MicMic"
        return alias


core.hdiutil = _private_attach
core.Alias = _Alias

if __name__ == "__main__":
    app, out = sys.argv[1], sys.argv[2]
    dmgbuild.build_dmg(out, "MicMic", settings_file=os.path.join(HERE, "dmgbuild-settings.py"),
                       defines={"app": app})
