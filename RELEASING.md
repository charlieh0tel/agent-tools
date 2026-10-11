# Releasing

1. Bump the version in `debian/changelog` (`dch -v X.Y.Z -D unstable`),
   `pyproject.toml` (then `uv lock`) and the `.TH` line of each `*.1`.
2. Commit as `Release X.Y.Z`, then
   `git tag -a vX.Y.Z -m vX.Y.Z && git push origin main vX.Y.Z`.

The tag makes CI build the `.deb`, publish a GitHub Release and update the
apt repo.
