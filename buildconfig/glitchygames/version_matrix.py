"""GlitchyGames version matrix: our WebAssembly patches on newer pygame-ce and Pyodide releases.

GlitchyGames' fork only (branch glitchygames-wasm). Pyodide repackages pygame-ce on its own
schedule and PyScript pins one Pyodide, so this tries every pygame-ce release newer than the one
the branch is based on (and upstream main) against the Pyodide cross-build environments that
matter, to learn early which combinations build and pass with our patches, and when upstream
makes a patch unnecessary.

For each (pygame-ce ref, Pyodide version) pair:
  1. a fresh temporary git worktree at the upstream ref;
  2. our patches (the glitchygames-wasm commits on top of the base release, minus the
     "GlitchyGames:" tooling commits) applied one by one with `git am`, each reported as
     applied-cleanly, applied-3way (needed a 3-way merge), already-upstream (no longer needed)
     or conflicted;
  3. the version marked X.Y.Z.post1, as the branch does, so the wheel is told apart from upstream;
  4. build_pyodide_wheel.sh with PYODIDE_VERSION set (skipped if a patch conflicted: a wheel
     missing one of our fixes is not one we would ship);
  5. optionally the browser checks (browser_checks/run_browser_checks.py, Playwright).
Results: one pair-result.json per pair, then `report` merges them (and the browser results) into
version-matrix.json and version-matrix.md.

Usage (python3 3.11 or newer, standard library only; git and uv on PATH):
  python3 buildconfig/glitchygames/version_matrix.py discover [--all-pyodide-versions]
  python3 buildconfig/glitchygames/version_matrix.py run --pygame-ref 2.5.8 --pyodide 314.0.3 \\
      --output-directory dist-version-matrix [--browsers chromium,webkit]
  python3 buildconfig/glitchygames/version_matrix.py report --results-directory dist-version-matrix
  python3 buildconfig/glitchygames/version_matrix.py all --output-directory dist-version-matrix
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - runs git, the build script and the browser checks
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_DIRECTORY = SCRIPT_DIRECTORY.parent.parent
BUILD_SCRIPT = SCRIPT_DIRECTORY / 'build_pyodide_wheel.sh'
BROWSER_CHECKS_SCRIPT = SCRIPT_DIRECTORY / 'browser_checks' / 'run_browser_checks.py'

UPSTREAM_REPOSITORY = 'https://github.com/pygame-community/pygame-ce.git'
PYPI_METADATA_URL = 'https://pypi.org/pypi/pygame-ce/json'
# The list `pyodide xbuildenv search` reads (pyodide_build/xbuildenv_releases.py, 0.39.2)
XBUILDENV_METADATA_URL = 'https://pyodide.github.io/pyodide/api/v2/pyodide-cross-build-environments.json'

# The release the glitchygames-wasm branch is based on: our patches are the commits after it
CURRENT_PYGAME_VERSION = '2.5.8'
# The Pyodide PyScript 2026.7.3 pins (the runtime GlitchyGames' browser builds use)
PYSCRIPT_PYODIDE_VERSION = '314.0.3'
# build_pyodide_wheel.sh's toolchain venv is Python 3.14.2; pyodide-build refuses a cross-build
# environment for another Python, so only these are buildable without a second venv
BUILD_PYTHON_MAJOR_MINOR = '3.14'
# Commits on the branch that are GlitchyGames tooling (build script, version), not fixes
TOOLING_SUBJECT_PREFIX = 'GlitchyGames:'
PLAYWRIGHT_VERSION = '1.56.0'
# Commits the driver makes in its temporary worktree (git am, the version marker) need an
# identity; these are never pushed anywhere
MATRIX_GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'GlitchyGames version matrix',
    'GIT_AUTHOR_EMAIL': 'version-matrix@glitchygames.invalid',
    'GIT_COMMITTER_NAME': 'GlitchyGames version matrix',
    'GIT_COMMITTER_EMAIL': 'version-matrix@glitchygames.invalid',
}
RELEASE_VERSION_PATTERN = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
PYODIDE_VERSION_PATTERN = re.compile(r'^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$')


# ---------------------------------------------------------------------------------------------
# Helpers


def run_git(*arguments: str, cwd: Path = REPOSITORY_DIRECTORY, check: bool = True,
            environment: dict | None = None) -> subprocess.CompletedProcess:
    """Run git and capture its output as text.

    Returns:
        The completed process.
    """
    return subprocess.run(  # noqa: S603 - fixed program, arguments built here
        ['git', *arguments], cwd=cwd, capture_output=True, text=True, check=check,
        env={**os.environ, **(environment or {})})


def fetch_json(url: str) -> dict:
    """Download and parse a JSON document.

    Returns:
        The parsed document.
    """
    request = urllib.request.Request(url, headers={'User-Agent': 'glitchygames-version-matrix'})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs
        return json.load(response)


def release_version_key(version: str) -> tuple[int, ...]:
    """Sort key for a plain X.Y.Z release.

    Returns:
        The integer parts.
    """
    return tuple(int(part) for part in RELEASE_VERSION_PATTERN.match(version).groups())


def pyodide_version_key(version: str) -> tuple:
    """Sort key for a Pyodide version (pre-releases sort before their final release).

    Returns:
        (major, minor, patch, pre-release rank, pre-release number).
    """
    match = PYODIDE_VERSION_PATTERN.match(version)
    major, minor, patch, prerelease_kind, prerelease_number = match.groups()
    prerelease_rank = {'a': 0, 'b': 1, 'rc': 2, None: 3}[prerelease_kind]
    return (int(major), int(minor), int(patch), prerelease_rank, int(prerelease_number or 0))


def pair_slug(pygame_ref: str, pyodide_version: str) -> str:
    """A file and artifact name for one pair.

    Returns:
        For example pygame-ce-2.5.8-pyodide-314.0.3.
    """
    safe_ref = re.sub(r'[^A-Za-z0-9._-]', '-', pygame_ref)
    return f'pygame-ce-{safe_ref}-pyodide-{pyodide_version}'


def upstream_git_ref(pygame_ref: str) -> str:
    """The upstream ref to fetch: a release is a tag, anything else a branch.

    Returns:
        A full ref name.
    """
    if RELEASE_VERSION_PATTERN.match(pygame_ref):
        return f'refs/tags/{pygame_ref}'
    return f'refs/heads/{pygame_ref}'


def fetch_upstream_commit(git_ref: str, expected_commit: str = '') -> str:
    """Fetch an upstream ref (into FETCH_HEAD only: no local refs are created).

    If discovery saw a particular commit (main moves), that commit is fetched, so every job of
    one CI run builds the same tree.

    Returns:
        The commit the ref points at (a tag is peeled).
    """
    if expected_commit:
        run_git('fetch', '--quiet', '--no-tags', UPSTREAM_REPOSITORY, expected_commit)
    else:
        run_git('fetch', '--quiet', '--no-tags', UPSTREAM_REPOSITORY, git_ref)
    return run_git('rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()


# ---------------------------------------------------------------------------------------------
# Discovery


def discover_pygame_versions(current_version: str) -> tuple[list[dict], list[dict]]:
    """pygame-ce releases newer than the current one (upstream tags and PyPI), plus main.

    A release must have an upstream tag to be built (we build from git); one only on PyPI is
    reported as excluded.

    Returns:
        (refs to build, excluded versions with reasons)
    """
    listing = run_git('ls-remote', '--tags', '--heads', UPSTREAM_REPOSITORY).stdout
    tag_commits: dict[str, str] = {}
    peeled_commits: dict[str, str] = {}
    main_commit = ''
    for line in listing.splitlines():
        commit, reference = line.split('\t')
        if reference == 'refs/heads/main':
            main_commit = commit
        elif reference.startswith('refs/tags/'):
            name = reference.removeprefix('refs/tags/')
            # Annotated tags list the tag object, then "<name>^{}" with the commit it points at
            if name.endswith('^{}'):
                peeled_commits[name.removesuffix('^{}')] = commit
            else:
                tag_commits[name] = commit
    tag_commits.update(peeled_commits)
    tagged_releases = {name for name in tag_commits if RELEASE_VERSION_PATTERN.match(name)}

    pypi_releases = set()
    try:
        pypi_metadata = fetch_json(PYPI_METADATA_URL)
        for version, files in pypi_metadata['releases'].items():
            # A release whose every file is yanked is not a release anyone should build
            if RELEASE_VERSION_PATTERN.match(version) and files and not all(
                    file.get('yanked') for file in files):
                pypi_releases.add(version)
    except OSError as error:
        print(f'warning: PyPI lookup failed ({error}); using upstream tags only', file=sys.stderr)

    current_key = release_version_key(current_version)
    newer = sorted((version for version in tagged_releases | pypi_releases
                    if release_version_key(version) > current_key), key=release_version_key)
    refs = [{'ref': current_version, 'commit': tag_commits.get(current_version, ''),
             'sources': ['current']}]
    excluded = []
    for version in newer:
        sources = [source for source, versions in (('git-tag', tagged_releases), ('pypi', pypi_releases))
                   if version in versions]
        if version not in tagged_releases:
            excluded.append({'pygame_ce': version, 'reason': 'on PyPI but no upstream git tag'})
            continue
        refs.append({'ref': version, 'commit': tag_commits[version], 'sources': sources})
    refs.append({'ref': 'main', 'commit': main_commit, 'sources': ['git-branch']})
    return refs, excluded


def discover_pyodide_versions(pinned_version: str, all_versions: bool) -> tuple[list[dict], list[dict]]:
    """Pyodide cross-build environments: the one PyScript pins, plus newer usable ones.

    By default the pinned version and the newest newer release of each major line; with
    all_versions, every newer release. Patch releases of one Pyodide line share Python,
    Emscripten and the wheel ABI, and each cross-build environment is about 2.4 GB in the
    CI cache (10 GB per repository), so trying every one is opt-in.

    Returns:
        (versions to build against, excluded versions with reasons)
    """
    releases = fetch_json(XBUILDENV_METADATA_URL)['releases']
    pinned_key = pyodide_version_key(pinned_version)
    candidates = []
    excluded = []
    for version, release in releases.items():
        if not PYODIDE_VERSION_PATTERN.match(version) or pyodide_version_key(version) <= pinned_key:
            continue
        python_major_minor = '.'.join(release['python_version'].split('.')[:2])
        if pyodide_version_key(version)[3] != 3:
            excluded.append({'pyodide': version, 'reason': 'pre-release'})
        elif python_major_minor != BUILD_PYTHON_MAJOR_MINOR:
            excluded.append({'pyodide': version, 'reason':
                             f'Python {release["python_version"]}; the build venv is {BUILD_PYTHON_MAJOR_MINOR}'})
        else:
            candidates.append(version)
    candidates.sort(key=pyodide_version_key)
    if not all_versions:
        newest_per_major = {}
        for version in candidates:
            newest_per_major[pyodide_version_key(version)[0]] = version
        kept = set(newest_per_major.values())
        excluded.extend({'pyodide': version, 'reason': 'superseded patch release (--all-pyodide-versions)'}
                        for version in candidates if version not in kept)
        candidates = [version for version in candidates if version in kept]

    def describe(version: str) -> dict:
        release = releases[version]
        return {'version': version, 'python': release['python_version'],
                'emscripten': release['emscripten_version'], 'published_at': release.get('published_at')}

    excluded.sort(key=lambda entry: pyodide_version_key(entry['pyodide']))
    return [describe(pinned_version)] + [describe(version) for version in candidates], excluded


def discover(arguments: argparse.Namespace) -> dict:
    """Find the pairs to try and print (and optionally emit for GitHub Actions) the matrix.

    Returns:
        The discovery record.
    """
    pygame_refs, pygame_excluded = discover_pygame_versions(arguments.current_pygame_version)
    pyodide_versions, pyodide_excluded = discover_pyodide_versions(
        arguments.pinned_pyodide_version, arguments.all_pyodide_versions)
    pairs = [{'pygame_ref': pygame['ref'], 'pygame_commit': pygame['commit'], 'pyodide': pyodide['version'],
              'slug': pair_slug(pygame['ref'], pyodide['version'])}
             for pygame in pygame_refs for pyodide in pyodide_versions]
    record = {
        'generated_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        'pygame_ce': pygame_refs, 'pyodide': pyodide_versions,
        'excluded': pygame_excluded + pyodide_excluded, 'pairs': pairs,
    }
    print(json.dumps(record, indent=2))
    if arguments.github_output:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as github_output:
            # matrix: one build job per pair; pairs: crossed with the browsers for the checks
            github_output.write(f'matrix={json.dumps({"include": pairs})}\n')
            github_output.write(f'pairs={json.dumps(pairs)}\n')
            github_output.write(f'discovery={json.dumps(record)}\n')
    return record


# ---------------------------------------------------------------------------------------------
# One pair: tree, patches, build, browser checks


def resolve_patch_base(patch_base: str) -> str:
    """The commit our patches sit on (fetched from upstream if this clone lacks the tag).

    Returns:
        A commit id.
    """
    local = run_git('rev-parse', '--verify', '--quiet', f'{patch_base}^{{commit}}', check=False)
    if local.returncode == 0:
        return local.stdout.strip()
    return fetch_upstream_commit(upstream_git_ref(patch_base))


def collect_patches(patch_base: str, patch_branch: str, patch_directory: Path) -> list[dict]:
    """Write our fixes as mbox patches, oldest first, skipping GlitchyGames tooling commits.

    Returns:
        One record per patch: commit, subject, file.
    """
    base_commit = resolve_patch_base(patch_base)
    commits = run_git('rev-list', '--reverse', '--no-merges', f'{base_commit}..{patch_branch}').stdout.split()
    patches = []
    for commit in commits:
        subject = run_git('log', '-1', '--format=%s', commit).stdout.strip()
        if subject.startswith(TOOLING_SUBJECT_PREFIX):
            continue
        patch_file = patch_directory / f'{len(patches) + 1:04d}-{commit[:12]}.patch'
        patch_file.write_text(run_git('format-patch', '-1', '--stdout', commit).stdout)
        patches.append({'commit': commit, 'subject': subject, 'file': str(patch_file)})
    return patches


def apply_patch(tree: Path, patch: dict, upstream_commit: str, environment: dict) -> dict:
    """Apply one patch and say how it went.

    already-upstream: upstream has a commit with the same patch id (git cherry), or the 3-way
        merge produced no change (the fix is already there in the same form).
    applied-cleanly: `git am` applied it as is.
    applied-3way: it needed `git am -3` (context moved; git merged it without help).
    conflicted: even the 3-way merge conflicted; the files are listed and the patch is left out.

    Returns:
        The patch record with status and detail.
    """
    result = {'commit': patch['commit'], 'subject': patch['subject']}
    # "-" means upstream has an equivalent change (same patch id)
    cherry = run_git('cherry', upstream_commit, patch['commit'], f'{patch["commit"]}^', cwd=tree, check=False)
    if cherry.returncode == 0 and cherry.stdout.strip().startswith('-'):
        return {**result, 'status': 'already-upstream', 'detail': 'upstream has a commit with the same patch id'}

    head_before = run_git('rev-parse', 'HEAD', cwd=tree).stdout.strip()
    plain = run_git('am', '--quiet', patch['file'], cwd=tree, check=False, environment=environment)
    if plain.returncode == 0:
        return {**result, 'status': 'applied-cleanly', 'detail': ''}
    run_git('am', '--abort', cwd=tree, check=False)

    three_way = run_git('am', '-3', patch['file'], cwd=tree, check=False, environment=environment)
    output = (three_way.stdout + three_way.stderr).strip()
    if three_way.returncode == 0:
        head_after = run_git('rev-parse', 'HEAD', cwd=tree).stdout.strip()
        if head_after == head_before:
            return {**result, 'status': 'already-upstream', 'detail': 'the 3-way merge made no change'}
        return {**result, 'status': 'applied-3way', 'detail': output[-1500:]}
    conflicted_files = run_git('diff', '--name-only', '--diff-filter=U', cwd=tree, check=False).stdout.split()
    run_git('am', '--abort', cwd=tree, check=False)
    return {**result, 'status': 'conflicted', 'conflicted_files': conflicted_files, 'detail': output[-1500:]}


def mark_version(tree: Path, environment: dict) -> str:
    """Mark a release X.Y.Z as X.Y.Z.post1, as the glitchygames-wasm branch does.

    A local version (X.Y.Z+label) would not work: buildconfig/get_version.py needs integer
    major, minor and patch parts and takes a fourth dot-separated part as the version tag.
    A pre-release (main's X.Y.Z.devN) already has a fourth part, so it is left alone.

    Returns:
        The version the wheel will carry.
    """
    pyproject = tree / 'pyproject.toml'
    text = pyproject.read_text()
    match = re.search(r'^version = "([^"]+)"$', text, flags=re.MULTILINE)
    version = match.group(1)
    if not RELEASE_VERSION_PATTERN.match(version):
        return version
    marked = f'{version}.post1'
    pyproject.write_text(text.replace(match.group(0), f'version = "{marked}"', 1))
    run_git('commit', '--quiet', '-am', f'GlitchyGames: version {marked} (version matrix build)',
            cwd=tree, environment=environment)
    return marked


def zip_tests(tree: Path, output_file: Path) -> None:
    """Zip the tree's test/ directory (as patched) for the browser checks, deterministically."""
    test_directory = tree / 'test'
    with zipfile.ZipFile(output_file, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(test_directory.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                information = zipfile.ZipInfo(str(path.relative_to(test_directory)), date_time=(1980, 1, 1, 0, 0, 0))
                information.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(information, path.read_bytes())


def run_browser_checks(wheel: Path, tests_zip: Path, pyodide_version: str, browser: str,
                       output_file: Path, slug: str) -> dict:
    """Run browser_checks/run_browser_checks.py for one browser (Playwright via uv).

    Returns:
        Its result record.
    """
    command = ['uv', 'run', '-q', '--no-project', '--python', BUILD_PYTHON_MAJOR_MINOR,
               '--with', f'playwright=={PLAYWRIGHT_VERSION}', 'python', str(BROWSER_CHECKS_SCRIPT),
               '--wheel', str(wheel), '--tests-zip', str(tests_zip), '--pyodide', pyodide_version,
               '--browser', browser, '--slug', slug, '--output', str(output_file)]
    completed = subprocess.run(command, check=False)  # noqa: S603 - fixed program
    if output_file.exists():
        return json.loads(output_file.read_text())
    return {'browser': browser, 'passed': False, 'error': f'runner exited {completed.returncode} without a result'}


def run_pair(arguments: argparse.Namespace) -> dict:
    """Prepare, patch, build and (optionally) browser-check one pair.

    Writes <output>/<slug>/pair-result.json (also when a step fails) and returns it.

    Returns:
        The pair result.
    """
    slug = pair_slug(arguments.pygame_ref, arguments.pyodide)
    pair_directory = Path(arguments.output_directory).resolve() / slug
    if pair_directory.exists():
        shutil.rmtree(pair_directory)
    pair_directory.mkdir(parents=True)
    patch_source_commit = run_git('rev-parse', arguments.patch_branch).stdout.strip()
    # Record the branch name, not "HEAD" (a detached CI checkout keeps "HEAD")
    patch_source_name = run_git('rev-parse', '--abbrev-ref', arguments.patch_branch).stdout.strip()
    result: dict = {
        'slug': slug, 'pygame_ref': arguments.pygame_ref, 'pyodide': arguments.pyodide,
        'patch_source': {'branch': patch_source_name, 'commit': patch_source_commit,
                         'base': arguments.patch_base},
        'started_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        'patches': [], 'build': {'status': 'not-run'}, 'browsers': {},
    }
    result_file = pair_directory / 'pair-result.json'
    work_directory = Path(tempfile.mkdtemp(prefix='glitchygames-version-matrix-'))
    tree = work_directory / 'pygame-ce'
    try:
        upstream_commit = fetch_upstream_commit(upstream_git_ref(arguments.pygame_ref), arguments.pygame_commit)
        result['upstream_commit'] = upstream_commit
        # Commits made here take the upstream commit's date, so SOURCE_DATE_EPOCH (the build
        # script reads HEAD's commit time) and hence the wheel are the same on every run
        committer_date = run_git('log', '-1', '--format=%cI', upstream_commit).stdout.strip()
        environment = {**MATRIX_GIT_IDENTITY, 'GIT_COMMITTER_DATE': committer_date, 'GIT_AUTHOR_DATE': committer_date}

        patch_directory = work_directory / 'patches'
        patch_directory.mkdir()
        patches = collect_patches(arguments.patch_base, arguments.patch_branch, patch_directory)
        run_git('worktree', 'add', '--quiet', '--detach', str(tree), upstream_commit)
        for patch in patches:
            outcome = apply_patch(tree, patch, upstream_commit, environment)
            result['patches'].append(outcome)
            print(f'{slug}: {outcome["status"]:17} {patch["subject"]}', flush=True)
        result['version'] = mark_version(tree, environment)
        tests_zip = pair_directory / 'pygame-ce-tests.zip'
        zip_tests(tree, tests_zip)

        if any(patch['status'] == 'conflicted' for patch in result['patches']):
            result['build'] = {'status': 'skipped', 'reason': 'a patch conflicted'}
        else:
            wheel_directory = pair_directory / 'wheel'
            build_log = pair_directory / 'build.log'
            print(f'{slug}: building (log: {build_log})', flush=True)
            with build_log.open('w') as log:
                completed = subprocess.run(  # noqa: S603 - this repository's build script
                    ['bash', str(BUILD_SCRIPT), str(tree), str(wheel_directory)],
                    stdout=log, stderr=subprocess.STDOUT, check=False,
                    env={**os.environ, 'PYODIDE_VERSION': arguments.pyodide})
            build_info_file = wheel_directory / 'build-info.json'
            if completed.returncode == 0 and build_info_file.exists():
                build_info = json.loads(build_info_file.read_text())
                # What the matrix knows and the build script does not: where the tree came from
                build_info.update({
                    'upstream_repository': UPSTREAM_REPOSITORY, 'upstream_ref': arguments.pygame_ref,
                    'upstream_commit': upstream_commit, 'glitchygames_patch_source': result['patch_source'],
                    'patches': [{key: patch[key] for key in ('commit', 'subject', 'status')}
                                for patch in result['patches']],
                })
                build_info_file.write_text(json.dumps(build_info, indent=2) + '\n')
                result['build'] = {'status': 'ok', 'wheel': build_info['wheel'], 'sha256': build_info['sha256'],
                                   'bytes': build_info['bytes'], 'emscripten': build_info['emscripten'],
                                   'pyodide_build': build_info['pyodide_build']}
            else:
                log_tail = build_log.read_text(errors='replace').splitlines()[-40:]
                result['build'] = {'status': 'failed', 'exit_code': completed.returncode, 'log_tail': log_tail}
        print(f'{slug}: build {result["build"]["status"]}', flush=True)

        if result['build']['status'] == 'ok':
            wheel = pair_directory / 'wheel' / result['build']['wheel']
            for browser in [name for name in arguments.browsers.split(',') if name]:
                browser_result = run_browser_checks(
                    wheel, tests_zip, arguments.pyodide, browser,
                    pair_directory / f'browser-result-{browser}.json', slug)
                result['browsers'][browser] = browser_result
                print(f'{slug}: {browser} {"passed" if browser_result.get("passed") else "FAILED"}', flush=True)
    except subprocess.CalledProcessError as error:
        result['error'] = f'{" ".join(error.cmd)}: exit {error.returncode}: {(error.stderr or "").strip()[-1500:]}'
        print(f'{slug}: {result["error"]}', file=sys.stderr)
    finally:
        if tree.exists() and not arguments.keep_tree:
            run_git('worktree', 'remove', '--force', str(tree), check=False)
        if not arguments.keep_tree:
            shutil.rmtree(work_directory, ignore_errors=True)
            run_git('worktree', 'prune', check=False)
        else:
            result['kept_tree'] = str(tree)
        result['finished_at'] = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
        result_file.write_text(json.dumps(result, indent=2) + '\n')
    return result


# ---------------------------------------------------------------------------------------------
# Report


def browser_passed(browser_result: dict) -> bool:
    """Whether a browser run passed (see run_browser_checks.py for the criteria).

    Returns:
        True when it ran and passed.
    """
    return bool(browser_result.get('passed'))


def pair_verdict(pair: dict) -> dict:
    """Summarise one pair: publishable only if every patch is in (or upstream), it built, and
    every browser that ran passed (and at least one ran).

    Returns:
        patches_ok, build_ok, browsers_ok, publishable.
    """
    patches_ok = bool(pair['patches']) and all(patch['status'] != 'conflicted' for patch in pair['patches'])
    build_ok = pair['build'].get('status') == 'ok'
    browsers_ok = bool(pair['browsers']) and all(browser_passed(result) for result in pair['browsers'].values())
    return {'patches_ok': patches_ok, 'build_ok': build_ok, 'browsers_ok': browsers_ok,
            'publishable': patches_ok and build_ok and browsers_ok and 'error' not in pair}


def release_tag(pair: dict) -> str:
    """The experimental pre-release tag for a pair (the upstream commit in it, so main's builds
    get one each and a rerun of the same inputs updates the same release).

    Returns:
        A tag name (not glitchygames-wasm-*, which the single-version workflow releases).
    """
    return f'experimental-{pair_slug(pair["pygame_ref"], pair["pyodide"])}-{pair.get("upstream_commit", "")[:10]}'


PATCH_STATUS_LABELS = {
    'applied-cleanly': 'clean', 'applied-3way': '3-way', 'already-upstream': 'upstream',
    'conflicted': '**CONFLICT**',
}


def browser_cell(pair: dict, browser: str) -> str:
    """One browser's table cell.

    Returns:
        pass, FAIL (with the failing checks) or a dash when it did not run.
    """
    result = pair['browsers'].get(browser)
    if result is None:
        return '-'
    if result.get('passed'):
        return 'pass'
    failing = [name for name, check in (result.get('checks') or {}).items() if not check.get('passed')]
    return f'**FAIL** ({", ".join(failing) or result.get("error", "?")})'


def render_markdown(report: dict) -> str:
    """The readable table (the CI job summary).

    Returns:
        Markdown text.
    """
    pairs = report['pairs']
    browsers = sorted({browser for pair in pairs for browser in pair['browsers']}) or ['chromium', 'webkit']
    patch_subjects: list[str] = []
    for pair in pairs:
        for patch in pair['patches']:
            if patch['subject'] not in patch_subjects:
                patch_subjects.append(patch['subject'])
    lines = ['# GlitchyGames pygame-ce / Pyodide version matrix', '',
             f'Generated {report["generated_at"]}. Patch columns: clean = applied as is, 3-way = needed a '
             '3-way merge, upstream = no longer needed (already upstream), CONFLICT = needs rework.', '']
    header = ['pygame-ce', 'upstream commit', 'Pyodide']
    header += [f'P{number}' for number in range(1, len(patch_subjects) + 1)]
    header += ['build', 'wheel sha256', *browsers, 'publish']
    lines.append('| ' + ' | '.join(header) + ' |')
    lines.append('|' + '---|' * len(header))
    for pair in pairs:
        statuses = {patch['subject']: patch['status'] for patch in pair['patches']}
        build = pair['build']
        build_cell = build.get('status', '?')
        if build_cell != 'ok' and build.get('reason'):
            build_cell += f' ({build["reason"]})'
        if 'error' in pair:
            build_cell = f'error: {pair["error"][:120]}'
        row = [pair.get('version', pair['pygame_ref']) + (' (main)' if pair['pygame_ref'] == 'main' else ''),
               f'`{pair.get("upstream_commit", "")[:10]}`', pair['pyodide']]
        row += [PATCH_STATUS_LABELS.get(statuses.get(subject, ''), statuses.get(subject, '-'))
                for subject in patch_subjects]
        row += [build_cell, f'`{build["sha256"][:16]}...`' if build.get('sha256') else '-']
        row += [browser_cell(pair, browser) for browser in browsers]
        row.append('yes' if pair['verdict']['publishable'] else 'no')
        lines.append('| ' + ' | '.join(row) + ' |')
    lines += ['', 'Patches:']
    lines += [f'- P{number}: {subject}' for number, subject in enumerate(patch_subjects, start=1)]
    if report.get('excluded'):
        lines += ['', 'Not tried:']
        lines += [f'- {entry.get("pygame_ce") or "Pyodide " + entry.get("pyodide", "?")}: {entry["reason"]}'
                  for entry in report['excluded']]
    problems = []
    for pair in pairs:
        for browser, result in pair['browsers'].items():
            for name, check in (result.get('checks') or {}).items():
                for problem in check.get('unexpected_problems', [])[:10]:
                    problems.append(f'- {pair["slug"]} / {browser} / {name}: {problem}')
                if check.get('error'):
                    problems.append(f'- {pair["slug"]} / {browser} / {name}: {check["error"]}')
    if problems:
        lines += ['', 'Unexpected browser test problems:', *problems]
    return '\n'.join(lines) + '\n'


def render_release_notes(pair: dict) -> str:
    """Body of a pair's experimental pre-release.

    Returns:
        Markdown text.
    """
    lines = [
        f'Experimental GlitchyGames Pyodide wheel: pygame-ce {pair["pygame_ref"]} '
        f'(upstream {pair.get("upstream_commit", "")}) with our WebAssembly patches, built against '
        f'Pyodide {pair["pyodide"]} by the version-matrix workflow. Not a supported release.', '',
        f'Patches from {pair["patch_source"]["branch"]} at {pair["patch_source"]["commit"]}:']
    lines += [f'- {patch["subject"]}: {patch["status"]}' for patch in pair['patches']]
    lines += ['', f'Wheel sha256: `{pair["build"].get("sha256")}`', '', 'Browser checks: ' + ', '.join(
        f'{browser} {"passed" if result.get("passed") else "failed"}' for browser, result in pair['browsers'].items())]
    return '\n'.join(lines) + '\n'


def build_report(arguments: argparse.Namespace) -> dict:
    """Merge every pair-result.json (and browser-result-*.json beside or below it) into
    version-matrix.json and version-matrix.md.

    Browser results are matched to pairs by their 'slug' field, so the CI artifacts can be
    downloaded into any layout.

    Returns:
        The report.
    """
    results_directory = Path(arguments.results_directory).resolve()
    pairs = {}
    for result_file in sorted(results_directory.rglob('pair-result.json')):
        pair = json.loads(result_file.read_text())
        pairs[pair['slug']] = pair
    for browser_file in sorted(results_directory.rglob('browser-result-*.json')):
        browser_result = json.loads(browser_file.read_text())
        slug = browser_result.get('slug') or browser_file.parent.name
        if slug in pairs:
            pairs[slug]['browsers'][browser_result['browser']] = browser_result
    discovery_file = results_directory / 'discovery.json'
    excluded = json.loads(discovery_file.read_text()).get('excluded', []) if discovery_file.exists() else []
    ordered = sorted(pairs.values(), key=lambda pair: (
        (1, ()) if pair['pygame_ref'] == 'main' else (0, release_version_key(pair['pygame_ref'])),
        pyodide_version_key(pair['pyodide'])))
    for pair in ordered:
        pair['verdict'] = pair_verdict(pair)
        pair['release_tag'] = release_tag(pair)
    report = {'generated_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
              'pairs': ordered, 'excluded': excluded}
    markdown = render_markdown(report)
    output_directory = Path(arguments.report_directory or results_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / 'version-matrix.json').write_text(json.dumps(report, indent=2) + '\n')
    (output_directory / 'version-matrix.md').write_text(markdown)
    notes_directory = output_directory / 'release-notes'
    publishable = []
    for pair in ordered:
        if pair['verdict']['publishable']:
            notes_directory.mkdir(exist_ok=True)
            (notes_directory / f'{pair["slug"]}.md').write_text(render_release_notes(pair))
            publishable.append({'slug': pair['slug'], 'tag': pair['release_tag'],
                                'target_commit': pair['patch_source']['commit']})
    print(markdown)
    if arguments.github_output:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as github_output:
            github_output.write(f'publishable={json.dumps({"include": publishable})}\n')
            github_output.write(f'publishable_count={len(publishable)}\n')
    if arguments.github_step_summary and os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write(markdown)
    return report


# ---------------------------------------------------------------------------------------------
# Command line


def run_all(arguments: argparse.Namespace) -> None:
    """Discover, run every pair in turn (a local, sequential version of the workflow), report."""
    output_directory = Path(arguments.output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    arguments.github_output = False
    record = discover(arguments)
    (output_directory / 'discovery.json').write_text(json.dumps(record, indent=2) + '\n')
    for pair in record['pairs']:
        arguments.pygame_ref = pair['pygame_ref']
        arguments.pygame_commit = pair['pygame_commit']
        arguments.pyodide = pair['pyodide']
        run_pair(arguments)
    arguments.results_directory = str(output_directory)
    arguments.report_directory = ''
    arguments.github_step_summary = False
    build_report(arguments)


def add_discovery_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by discover and all."""
    parser.add_argument('--current-pygame-version', default=CURRENT_PYGAME_VERSION,
                        help='the release our patches are based on (always tried first)')
    parser.add_argument('--pinned-pyodide-version', default=PYSCRIPT_PYODIDE_VERSION,
                        help='the Pyodide PyScript pins (always tried)')
    parser.add_argument('--all-pyodide-versions', action='store_true',
                        help='every newer Pyodide release, not just the newest of each line')


def add_pair_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by run and all."""
    parser.add_argument('--output-directory', default='dist-version-matrix')
    parser.add_argument('--patch-base', default=CURRENT_PYGAME_VERSION,
                        help='our patches are the commits after this (default: %(default)s)')
    parser.add_argument('--patch-branch', default='HEAD',
                        help='the branch holding our patches (default: this checkout)')
    parser.add_argument('--browsers', default='',
                        help='comma-separated Playwright browsers to check the wheel in (default: none)')
    parser.add_argument('--keep-tree', action='store_true',
                        help='keep the temporary worktree (remove later: git worktree remove)')


def main() -> None:
    """Parse the command line and run the subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest='command', required=True)

    discover_parser = subcommands.add_parser('discover', help='list the pairs to try')
    add_discovery_options(discover_parser)
    discover_parser.add_argument('--github-output', action='store_true',
                                 help='also write matrix= and discovery= to $GITHUB_OUTPUT')
    discover_parser.set_defaults(handler=discover)

    run_parser = subcommands.add_parser('run', help='prepare, patch, build and check one pair')
    run_parser.add_argument('--pygame-ref', required=True, help='a release tag (2.5.8) or a branch (main)')
    run_parser.add_argument('--pygame-commit', default='',
                            help='the commit discovery saw for the ref (main moves)')
    run_parser.add_argument('--pyodide', default=PYSCRIPT_PYODIDE_VERSION, help='cross-build environment version')
    add_pair_options(run_parser)
    run_parser.set_defaults(handler=run_pair)

    report_parser = subcommands.add_parser('report', help='merge results into JSON and markdown')
    report_parser.add_argument('--results-directory', default='dist-version-matrix')
    report_parser.add_argument('--report-directory', default='',
                               help='where to write the report (default: the results directory)')
    report_parser.add_argument('--github-output', action='store_true',
                               help='also write publishable= to $GITHUB_OUTPUT')
    report_parser.add_argument('--github-step-summary', action='store_true',
                               help='also append the table to $GITHUB_STEP_SUMMARY')
    report_parser.set_defaults(handler=build_report)

    all_parser = subcommands.add_parser('all', help='discover, run every pair here, report')
    add_discovery_options(all_parser)
    add_pair_options(all_parser)
    all_parser.set_defaults(handler=run_all)

    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == '__main__':
    main()
