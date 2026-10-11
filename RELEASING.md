# Releasing

1. Add a `debian/changelog` entry (`dch -v X.Y.Z -D unstable`). It is the only
   place the version lives; `pyproject.toml` reads it from there.
2. Commit as `Release X.Y.Z`, then
   `git tag -a vX.Y.Z -m vX.Y.Z && git push origin main vX.Y.Z`.

The tag makes CI build the `.deb`, publish a GitHub Release and update the
apt repo.
