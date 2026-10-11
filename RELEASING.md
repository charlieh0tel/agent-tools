# Releasing

CI does the building and publishing; a release is a version bump, a tag and a
push.

1. Check that `main` is clean and up to date, and that the checks pass:

   ```
   git fetch && git status
   uv run ruff format --check && uv run ruff check && uv run pytest
   ```

2. Pick the version: patch for fixes, minor for new features or tools.

3. Bump it everywhere it appears:
   - `debian/changelog`: add an entry at the top with distribution `unstable`
     and one bullet per user-visible change, prefixed with the tool's name
     (`dch -v X.Y.Z -D unstable`, or by hand with `date -R` for the trailer).
   - `pyproject.toml`: `version`, then `uv lock` to update `uv.lock`.
   - The `.TH` line of each man page (`*.1`): date and `agent-tools X.Y.Z`.

4. Optionally build the package the way CI does, in `debian:stable-slim`
   (`dpkg-buildpackage -us -uc -b` after installing the build dependencies).

5. Commit as `Release X.Y.Z`, tag, and push both:

   ```
   git commit -m "Release X.Y.Z" debian/changelog pyproject.toml uv.lock *.1
   git tag -a vX.Y.Z -m vX.Y.Z
   git push origin main vX.Y.Z
   ```

6. The `v*` tag makes CI build the `.deb`, attach it to a GitHub Release, and
   trigger a rebuild of [charlieh0tel/apt-repo](https://github.com/charlieh0tel/apt-repo).
   If the `APT_REPO_TOKEN` secret is missing, the job warns and the daily
   apt-repo rebuild picks the release up instead. Check with:

   ```
   gh run list -L 3
   gh release view vX.Y.Z
   ```
