"""Run the GlitchyGames browser checks on a pygame-ce Pyodide wheel (Playwright, headless).

Adapted from the pygame-ce-wasm feasibility study's tools/run_pygame_tests_in_browser.py,
tools/inspect_page.py and tools/probe_server.py (../pygame-ce-wasm/tools in the GlitchyGames
workspace): the same pygame-ce test modules (freetype, ftfont, controller, touch, display; the
ones pyodide-recipes leaves out or that our patches touch) and the static-module probe, one page
load per check with a hard timeout, but waiting for the page's result instead of a fixed time
and judging it against expected_failures.json.

A check passes when its page answered, the module loaded, and every failing test is listed in
expected_failures.json for this browser (things a browser cannot do, such as iconify or gamma).
The run passes when every check passes.

Usage (Playwright 1.56.0 with its browsers installed):
  uv run --no-project --python 3.14 --with playwright==1.56.0 python \\
      buildconfig/glitchygames/browser_checks/run_browser_checks.py \\
      --wheel dist/pygame_ce-...whl --tests-zip dist/pygame-ce-tests.zip --pyodide 314.0.3 \\
      --browser chromium --output browser-result-chromium.json
"""

import argparse
import functools
import json
import re
import shutil
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

CHECKS_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_CHECKS = ['static_modules', 'freetype_test', 'ftfont_test', 'controller_test', 'touch_test', 'display_test']
# Parameterised test ids end in " (gamma=0.25)" and the like; expectations name the test only
PARAMETER_SUFFIX_PATTERN = re.compile(r' \(.*\)$')


class QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler with explicit MIME types and no request logging."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        '.wasm': 'application/wasm',
        '.js': 'text/javascript',
        '.whl': 'application/zip',
        '.zip': 'application/zip',
    }

    def log_message(self, format: str, *args) -> None:  # noqa: A002, ANN002 - stdlib signature
        pass


def start_server(directory: Path) -> ThreadingHTTPServer:
    """Serve a directory on an ephemeral localhost port in a daemon thread.

    Returns:
        The running server.
    """
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def expected_problems(expectations: dict, check: str, browser: str) -> dict[str, str]:
    """The tests allowed to fail for one check in one browser.

    Returns:
        Test id -> reason.
    """
    allowed = {}
    for entry in expectations.get(check, []):
        if entry['browsers'] == 'all' or browser in entry['browsers']:
            allowed[entry['test']] = entry['reason']
    return allowed


def judge(outcome: dict, allowed: dict[str, str]) -> dict:
    """Decide whether one check passed.

    Returns:
        The outcome plus passed, unexpected_problems and expected_problems.
    """
    if outcome.get('error') or outcome.get('load_error'):
        return {**outcome, 'passed': False, 'unexpected_problems': [], 'expected_problems': []}
    unexpected, expected = [], []
    for problem in outcome.get('problems', []):
        test_id = PARAMETER_SUFFIX_PATTERN.sub('', problem.split(': ', 1)[0])
        (expected if test_id in allowed else unexpected).append(problem)
    # Every failure must be accounted for in the problem list (it is not truncated)
    accounted = outcome.get('failures', 0) + outcome.get('errors', 0) <= len(outcome.get('problems', []))
    return {**outcome, 'passed': not unexpected and accounted,
            'unexpected_problems': unexpected, 'expected_problems': expected}


def run_check(playwright: object, browser_name: str, url: str, timeout_seconds: float) -> dict:
    """Load the page once (a fresh browser, so a frozen page cannot affect the next check).

    Returns:
        The page's result, or an error.
    """
    browser = getattr(playwright, browser_name).launch()
    console: list[str] = []
    started = time.perf_counter()
    try:
        page = browser.new_page()
        page.on('console', lambda message: console.append(f'{message.type}: {message.text}'[:500]))
        page.goto(url)
        page.wait_for_function('window.__checks_result !== undefined || window.__checks_error !== undefined',
                               timeout=timeout_seconds * 1000, polling=500)
        error = page.evaluate('window.__checks_error')
        if error:
            outcome = {'error': f'page error: {error}'}
        else:
            outcome = json.loads(page.evaluate('window.__checks_result()'))
        outcome['browser_version'] = browser.version
    except PlaywrightError as error:
        outcome = {'error': f'{type(error).__name__}: {str(error).splitlines()[0][:500]}',
                   'console_tail': console[-20:]}
    finally:
        try:
            browser.close()
        except PlaywrightError:
            pass
    outcome['seconds'] = round(time.perf_counter() - started, 1)
    return outcome


def main() -> None:
    """Serve the wheel and tests, run every check in one browser, write the result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--tests-zip', required=True, help="zip of the tree's test/ directory")
    parser.add_argument('--pyodide', default='314.0.3')
    parser.add_argument('--browser', default='chromium', choices=['chromium', 'webkit', 'firefox'])
    parser.add_argument('--checks', default=','.join(DEFAULT_CHECKS))
    parser.add_argument('--timeout', type=float, default=240, help='seconds per check')
    parser.add_argument('--slug', default='', help='the version-matrix pair this result belongs to')
    parser.add_argument('--output', required=True)
    arguments = parser.parse_args()

    expectations = json.loads((CHECKS_DIRECTORY / 'expected_failures.json').read_text())
    expectations.pop('_comment', None)
    wheel = Path(arguments.wheel).resolve()
    # Serve copies from a private directory: only these files are reachable, and the wheel
    # keeps its file name (Pyodide reads the platform tag from it)
    serve_directory = Path(tempfile.mkdtemp(prefix='glitchygames-browser-checks-'))
    shutil.copy(CHECKS_DIRECTORY / 'checks.html', serve_directory / 'checks.html')
    shutil.copy(wheel, serve_directory / wheel.name)
    shutil.copy(arguments.tests_zip, serve_directory / 'pygame-ce-tests.zip')
    server = start_server(serve_directory)
    port = server.server_address[1]
    result = {'slug': arguments.slug, 'browser': arguments.browser, 'wheel': wheel.name,
              'pyodide': arguments.pyodide, 'checks': {}}
    try:
        with sync_playwright() as playwright:
            for check in [name for name in arguments.checks.split(',') if name]:
                url = (f'http://127.0.0.1:{port}/checks.html?pyodide={arguments.pyodide}'
                       f'&wheel={wheel.name}&tests=pygame-ce-tests.zip&check={check}')
                outcome = judge(run_check(playwright, arguments.browser, url, arguments.timeout),
                                expected_problems(expectations, check, arguments.browser))
                result['checks'][check] = outcome
                brief = {key: outcome.get(key) for key in ('run', 'failures', 'errors', 'skipped', 'error',
                                                           'load_error', 'seconds') if outcome.get(key) is not None}
                print(f'{arguments.browser:8} {check:16} {"pass" if outcome["passed"] else "FAIL"} {brief}',
                      flush=True)
                for problem in outcome['unexpected_problems']:
                    print(f'    unexpected: {problem}', flush=True)
    finally:
        server.shutdown()
        shutil.rmtree(serve_directory, ignore_errors=True)
    result['passed'] = bool(result['checks']) and all(check['passed'] for check in result['checks'].values())
    Path(arguments.output).write_text(json.dumps(result, indent=2) + '\n')
    print(f'{arguments.browser}: {"passed" if result["passed"] else "FAILED"}')


if __name__ == '__main__':
    main()
